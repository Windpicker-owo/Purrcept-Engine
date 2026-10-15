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
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    input_overflow: bool


class _Stream(Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    active: bool

    def start(self) -> None:
        """开始当前已经创建的原生捕获流。"""
        ...

    def abort(self) -> None:
        """立即停止原生捕获，不等待缓冲区继续产出。"""
        ...

    def close(self) -> None:
        """释放当前所有者实际取得的原生资源。"""
        ...


class _SoundDevice(Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    CallbackAbort: type[Exception]
    PortAudioError: type[Exception]

    @overload
    def query_devices(self) -> Iterable[Mapping[str, object]]:
        """查询捕获库报告的设备信息，未指定设备时返回完整枚举。

        Returns:
            捕获库实际枚举的原生设备字段序列。
        """
        ...

    @overload
    def query_devices(self, device: int | None, kind: str) -> Mapping[str, object]:
        """查询捕获库报告的设备信息，未指定设备时返回完整枚举。

        Args:
            device: 显式选定的输入设备序号；None 交由设备适配器解析。
            kind: 此业务值所属的稳定种类。

        Returns:
            指定设备的原生字段，采用前仍需校验采样率与输入通道。
        """
        ...

    def RawInputStream(
        self,
        *,
        samplerate: int,
        channels: int,
        dtype: str,
        blocksize: int,
        device: int | None,
        callback: Callable[[object, int, object, _Status], None],
    ) -> _Stream:
        """按明确格式构造捕获流，取得后由捕获所有者关闭。

        Args:
            samplerate: 原生捕获流的采样率（Hz）。
            channels: 原生音频的通道数或显式身体参数通道映射。
            dtype: 原生采样数组的元素格式。
            blocksize: 捕获回调每块请求的音频帧数。
            device: 显式选定的输入设备序号；None 交由设备适配器解析。
            callback: 底层库调用的同步回调，跨线程状态交回所属事件循环。

        Returns:
            尚需显式启动并由捕获所有者关闭的原生输入流。
        """
        ...


class _Plane(Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def update(self, data: bytes) -> None:
        """将完整 PCM 字节写入当前原生音频平面。

        Args:
            data: 当前边界接收的原始协议或媒体内容。
        """
        ...

    def __bytes__(self) -> bytes: ...


class _Frame(Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    sample_rate: int
    samples: int
    planes: tuple[_Plane, ...]


class _Resampler(Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def resample(self, frame: _Frame) -> list[_Frame]:
        """将当前原生音频帧转换为已配置目标格式的顺序帧。

        Args:
            frame: PyAV 交付的原生音频帧。

        Returns:
            按输入顺序转换为目标采样格式的原生音频帧。
        """
        ...


class _Av(Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def AudioFrame(self, *, format: str, layout: str, samples: int) -> _Frame:
        """按明确采样格式与声道布局分配原生音频帧。

        Args:
            format: FFmpeg/PyAV 使用的显式采样格式。
            layout: 原生音频声道布局。
            samples: 本帧包含的采样数量。

        Returns:
            按指定格式分配的原生音频帧。
        """
        ...

    def AudioResampler(self, *, format: str, layout: str, rate: int) -> _Resampler:
        """为当前捕获实例创建独立的目标格式重采样器。

        Args:
            format: FFmpeg/PyAV 使用的显式采样格式。
            layout: 原生音频声道布局。
            rate: 重采样器的目标采样率（Hz）。

        Returns:
            归当前捕获实例所有的重采样器。
        """
        ...


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
        """保存有限回调队列额度，不在构造时加载或打开设备。

        Args:
            queue_size: 有界帧队列的最大块数，过载不得无限缓存。 默认 64。
        """
        self.queue_size = queue_size

    async def devices(self) -> tuple[InputDevice, ...]:
        """枚举 PortAudio 当前输入列表，不启动输入流。

        Returns:
            捕获库实际枚举的输入设备及采样信息。

        Raises:
            VoiceInputError: 输入设备枚举失败，请检查本机音频设备。
        """
        sd = cast(_SoundDevice, import_module("sounddevice"))

        def query() -> tuple[InputDevice, ...]:
            """枚举原生捕获库报告的输入设备及默认采样率。"""
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
        """按设备原生采样率打开输入流，连续转换到 16 kHz，不刷新取消尾帧。

        Args:
            device: 显式选定的输入设备序号；None 交由设备适配器解析。

        Yields:
            按原始采集、解码或播放顺序逐块交付的完整字节流。

        Raises:
            VoiceInputError: 设备、捕获格式、VAD/ASR 协议或有界帧队列不满足收音要求。
        """
        sd = cast(_SoundDevice, import_module("sounddevice"))
        av = cast(_Av, import_module("av"))
        chunks: queue.Queue[tuple[bytes, int]] = queue.Queue(maxsize=self.queue_size)
        failure: queue.Queue[str] = queue.Queue(maxsize=1)
        acquired: list[_Stream] = []

        def callback(raw: object, frames: int, timing: object, status: _Status) -> None:
            """将捕获线程交付的帧和溢出状态送回所属事件循环。"""
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
            """核验输入设备原生格式，再取得对应捕获流。"""
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
