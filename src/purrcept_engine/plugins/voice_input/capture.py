"""PortAudio 输入所有者，将本机原生采样率转换为有序 VAD 帧。

回调只复制到有限线程队列，重采样及 VAD 均在回调之外。队列满、原生
溢出或输入流失活明确终止，不能把缺样后的录音冒充连续发言。插件停止
时关闭迭代器并回收声卡，浏览器寿命不影响设备。
"""

from __future__ import annotations

import asyncio
import queue
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping
from importlib import import_module
from typing import Protocol, cast, overload

from purrcept_engine.plugins.voice_input.models import InputDevice, VoiceInputError


class _Status(Protocol):
    input_overflow: bool


class _Stream(Protocol):
    active: bool

    def start(self) -> None: ...

    def abort(self) -> None: ...

    def close(self) -> None: ...


class _SoundDevice(Protocol):
    CallbackAbort: type[Exception]
    PortAudioError: type[Exception]

    @overload
    def query_devices(self) -> Iterable[Mapping[str, object]]: ...

    @overload
    def query_devices(self, device: int | None, kind: str) -> Mapping[str, object]: ...

    def RawInputStream(
        self,
        *,
        samplerate: int,
        channels: int,
        dtype: str,
        blocksize: int,
        device: int | None,
        callback: Callable[[object, int, object, _Status], None],
    ) -> _Stream: ...


class _Plane(Protocol):
    def update(self, data: bytes) -> None: ...

    def __bytes__(self) -> bytes: ...


class _Frame(Protocol):
    sample_rate: int
    samples: int
    planes: tuple[_Plane, ...]


class _Resampler(Protocol):
    def resample(self, frame: _Frame) -> list[_Frame]: ...


class _Av(Protocol):
    def AudioFrame(self, *, format: str, layout: str, samples: int) -> _Frame: ...

    def AudioResampler(self, *, format: str, layout: str, rate: int) -> _Resampler: ...


async def _work[T](call: Callable[[], T]) -> T:
    """取消也等线程归还原生资源，避免下一会话与旧设备启动/关闭竞争。"""
    task = asyncio.create_task(asyncio.to_thread(call), name="voice-device-operation")
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class SoundDeviceCapture:
    """每次开始由 runtime 创建的本机输入驱动，不共享回调缓冲与重采样状态。"""

    def __init__(self, queue_size: int = 64) -> None:
        """保存有限回调队列额度，不在构造时加载或打开设备。"""
        self.queue_size = queue_size

    async def devices(self) -> tuple[InputDevice, ...]:
        """枚举 PortAudio 当前输入列表，不启动输入流。"""
        sd = cast(_SoundDevice, import_module("sounddevice"))

        def query() -> tuple[InputDevice, ...]:
            result: list[InputDevice] = []
            for index, item in enumerate(sd.query_devices()):
                channels, name = item.get("max_input_channels"), item.get("name")
                if not isinstance(channels, int) or not isinstance(name, str):
                    raise VoiceInputError("PortAudio 返回了无效设备信息。")
                if channels > 0:
                    result.append(InputDevice(index, name))
            return tuple(result)

        try:
            return await _work(query)
        except sd.PortAudioError as error:
            raise VoiceInputError("输入设备枚举失败，请检查本机音频设备。") from error

    async def frames(self, device: int | None) -> AsyncGenerator[bytes]:
        """按设备原生采样率打开输入流，连续转换到 16 kHz，不刷新取消尾帧。"""
        sd = cast(_SoundDevice, import_module("sounddevice"))
        av = cast(_Av, import_module("av"))
        chunks: queue.Queue[tuple[bytes, int]] = queue.Queue(maxsize=self.queue_size)
        failure: queue.Queue[str] = queue.Queue(maxsize=1)
        acquired: list[_Stream] = []

        def callback(raw: object, frames: int, timing: object, status: _Status) -> None:
            del timing
            # 缓冲接口来自 PortAudio；复制后原生回调可立即归还输入内存。
            if status.input_overflow:
                if failure.empty():
                    failure.put_nowait("麦克风输入溢出，当前发言存在缺样。")
                raise sd.CallbackAbort
            try:
                chunks.put_nowait((bytes(cast(_Plane, raw)), frames))
            except queue.Full:
                if failure.empty():
                    failure.put_nowait("麦克风缓存已满，收音已暂停，当前发言存在缺样。")
                raise sd.CallbackAbort from None

        def open_stream() -> tuple[_Stream, int]:
            # 使用选定设备的原生采样率；不因设备不支持 16k 偷换成另一设备。
            info = sd.query_devices(device, "input")
            raw_rate = info.get("default_samplerate")
            if not isinstance(raw_rate, (float, int)) or not 8000 <= raw_rate <= 192000:
                raise VoiceInputError("输入设备的原生采样率无效。")
            rate = round(raw_rate)
            stream = sd.RawInputStream(
                samplerate=rate,
                channels=1,
                dtype="int16",
                blocksize=max(1, rate // 100),
                device=device,
                callback=callback,
            )
            acquired.append(stream)
            return stream, rate

        try:
            try:
                stream, rate = await _work(open_stream)
            except sd.PortAudioError as error:
                raise VoiceInputError(
                    "麦克风无法打开，收音未开始，请检查设备及系统权限。"
                ) from error
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            pending = bytearray()
            try:
                await _work(stream.start)
            except sd.PortAudioError as error:
                raise VoiceInputError("麦克风无法开始采集，请检查设备。") from error
            while True:
                if not failure.empty():
                    raise VoiceInputError(failure.get_nowait())
                if not stream.active:
                    raise VoiceInputError("麦克风已断开，当前未完成发言已释放。")
                try:
                    raw, count = chunks.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.005)
                    continue
                if count < 1 or len(raw) != count * 2:
                    raise VoiceInputError("设备采样帧不连续，当前发言已释放。")
                frame = av.AudioFrame(format="s16", layout="mono", samples=count)
                frame.sample_rate = rate
                frame.planes[0].update(raw)
                for output in resampler.resample(frame):
                    pending.extend(bytes(output.planes[0])[: output.samples * 2])
                while len(pending) >= 1024:
                    pcm = bytes(pending[:1024])
                    del pending[:1024]
                    yield pcm
        finally:

            def release() -> None:
                # 创建在线程中完成也立即登记。取消启动或转换器构造失败
                # 仍能取得同一输入流并归还，不能丢弃已打开设备的返回值。
                for stream in acquired:
                    try:
                        stream.abort()
                    finally:
                        stream.close()

            await _work(release)
