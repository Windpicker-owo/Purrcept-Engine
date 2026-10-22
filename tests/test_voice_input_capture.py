"""有限原生库替身验证生产 SoundDeviceCapture 的采样与资源所有权。

替身执行真实驱动的回调、线程队列、重采样交接和 finally，不打开本机
设备。取消设备创建与原生回调溢出均须关闭原取得的同一输入流。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from purrcept_core.models import ConversationCheckpoint, Message, ModelResponse, ToolSet
from test_subject_session import ScriptedBackend, admitted, capacity, refresh
from test_voice_input import Inputs, Transcriber, settings

from purrcept_engine.app.config import ContextSettings, RetrySettings
from purrcept_engine.domain.context import history_preparation as history_preparation_module
from purrcept_engine.domain.context.checkpoints import decode_checkpoint, encode_checkpoint
from purrcept_engine.domain.context.history import HistoryMaintenance
from purrcept_engine.domain.context.history_preparation import HistoryPreparation
from purrcept_engine.domain.model_requests.service import prepare_off_loop
from purrcept_engine.domain.subject import session as session_module
from purrcept_engine.domain.subject.session import ContinuousSession
from purrcept_engine.infrastructure.models.compiler import compile_materials
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugins.voice_input import capture
from purrcept_engine.plugins.voice_input.capture import SoundDeviceCapture
from purrcept_engine.plugins.voice_input.models import CaptureGap, VoiceInputError
from purrcept_engine.plugins.voice_input.runtime import VoiceInputRuntime
from purrcept_engine.values import JsonObject


class Status:
    """原生输入回调状态替身。"""

    def __init__(self, overflow: bool = False) -> None:
        self.input_overflow = overflow


class Plane:
    """不可变字节与帧平面的有限适配。"""

    def __init__(self, pcm: bytes = b"\0" * 1024) -> None:
        self.pcm = pcm

    def update(self, data: bytes) -> None:
        """复制进入本帧。"""
        self.pcm = data

    def __bytes__(self) -> bytes:
        """只交付冻结字节。"""
        return self.pcm


class Frame:
    """PyAV 帧替身，样本和采样率仍由生产驱动写入。"""

    def __init__(self, *, format: str, layout: str, samples: int) -> None:
        assert format == "s16" and layout == "mono"
        self.samples, self.sample_rate = samples, 0
        self.planes = (Plane(),)


class Resampler:
    """记录连续输入帧，不验证 PyAV 自身的重采样算法。"""

    def resample(self, frame: Frame) -> list[Frame]:
        """输出固定 VAD 帧，证明生产适配器采用正确设备采样率。"""
        assert frame.sample_rate == 48000 and frame.samples == 480
        output = Frame(format="s16", layout="mono", samples=512)
        output.sample_rate = 16000
        return [output]


class Native:
    """一次被真实驱动创建的输入流。"""

    def __init__(
        self, callback: Callable[[object, int, object, Status], None], *, overflow: bool
    ) -> None:
        self.callback, self.overflow = callback, overflow
        self.active = False
        self.closed = False
        self.aborted = False

    def start(self) -> None:
        """调用生产回调，溢出时仍保留原资源供驱动收回。"""
        self.active = True
        try:
            self.callback(Plane(b"\0" * 960), 480, object(), Status(self.overflow))
        except CallbackAbort:
            self.active = False

    def abort(self) -> None:
        """原流确认停止。"""
        self.active, self.aborted = False, True

    def close(self) -> None:
        """原流确认释放。"""
        self.closed = True


class CallbackAbort(Exception):
    """PortAudio 的明确回调终止替身。"""


class PortAudioError(Exception):
    """PortAudio 外部故障替身。"""


def modules(
    monkeypatch: pytest.MonkeyPatch,
    *,
    overflow: bool = False,
    opening: threading.Event | None = None,
    release: threading.Event | None = None,
) -> list[Native]:
    """仅替换动态原生库入口，其余生产采集流程保持真实。"""
    sounddevice, av = ModuleType("sounddevice"), ModuleType("av")
    streams: list[Native] = []

    def query_devices(device: int | None = None, kind: str | None = None) -> object:
        del device
        return (
            [{"max_input_channels": 1, "name": "替身声卡"}]
            if kind is None
            else {"default_samplerate": 48000.0}
        )

    def stream(
        *,
        samplerate: int,
        channels: int,
        dtype: str,
        blocksize: int,
        device: int | None,
        callback: Callable[[object, int, object, Status], None],
    ) -> Native:
        assert samplerate == 48000 and channels == 1 and dtype == "int16" and blocksize == 0
        del device
        value = Native(callback, overflow=overflow)
        streams.append(value)
        if opening:
            opening.set()
        if release:
            assert release.wait(3)
        return value

    def resampler(*, format: str, layout: str, rate: int) -> Resampler:
        assert format == "s16" and layout == "mono" and rate == 16000
        return Resampler()

    monkeypatch.setattr(sounddevice, "query_devices", query_devices, raising=False)
    monkeypatch.setattr(sounddevice, "RawInputStream", stream, raising=False)
    monkeypatch.setattr(sounddevice, "CallbackAbort", CallbackAbort, raising=False)
    monkeypatch.setattr(sounddevice, "PortAudioError", PortAudioError, raising=False)
    monkeypatch.setattr(av, "AudioFrame", Frame, raising=False)
    monkeypatch.setattr(av, "AudioResampler", resampler, raising=False)

    def load(name: str) -> ModuleType:
        assert name in {"sounddevice", "av"}
        return sounddevice if name == "sounddevice" else av

    monkeypatch.setattr(capture, "import_module", load)
    return streams


async def test_production_native_rate_and_generator_close(monkeypatch: pytest.MonkeyPatch) -> None:
    streams = modules(monkeypatch)
    driver = SoundDeviceCapture()
    assert driver.resources_released()
    devices = await driver.devices()
    assert devices[0].name == "替身声卡" and streams == []
    frames = driver.frames(None)
    assert await anext(frames) == b"\0" * 1024
    assert not driver.resources_released()
    await frames.aclose()
    assert driver.resources_released()
    assert len(streams) == 1 and streams[0].closed and streams[0].aborted


async def test_native_close_failure_keeps_release_proof_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """原生 close 抛错后生成器虽已结束，归还明证仍保持否定。"""
    streams = modules(monkeypatch)
    failure = OSError("替身设备关闭失败")

    def broken_close(stream: Native) -> None:
        del stream
        raise failure

    monkeypatch.setattr(Native, "close", broken_close)
    driver = SoundDeviceCapture()
    frames = driver.frames(None)
    await anext(frames)
    with pytest.raises(OSError) as raised:
        await frames.aclose()
    assert raised.value is failure
    assert streams[0].aborted and not streams[0].closed
    assert not driver.resources_released()


async def test_native_overflow_marks_gap_and_keeps_same_input_stream(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    streams = modules(monkeypatch, overflow=True)
    frames = SoundDeviceCapture().frames(None)
    try:
        gap = await anext(frames)
        assert isinstance(gap, CaptureGap) and "溢出" in gap.reason
        assert await anext(frames) == bytes(1024)
        assert not streams[0].closed and streams[0].active
    finally:
        await frames.aclose()
    assert streams[0].closed
    assert "sample_rate=48000 queued_blocks=0 consumer_delay_ms=" in caplog.text


async def test_repeated_native_overflow_still_pauses_and_releases_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有限容错只跨越孤立缺口，连续故障不能伪装成仍在正常收音。"""
    streams = modules(monkeypatch)

    def start(native: Native) -> None:
        native.active = True
        for _ in range(4):
            native.callback(Plane(bytes(960)), 480, object(), Status(True))

    monkeypatch.setattr(Native, "start", start)
    frames = SoundDeviceCapture().frames(None)
    try:
        for _ in range(3):
            assert isinstance(await anext(frames), CaptureGap)
            assert await anext(frames) == bytes(1024)
        with pytest.raises(VoiceInputError, match="连续输入溢出"):
            await anext(frames)
    finally:
        await frames.aclose()
    assert streams[0].closed and streams[0].aborted


async def test_isolated_overflows_do_not_exhaust_a_lifetime_recovery_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只统计最近五秒的故障；稳定收音后的缺口不继承先前故障次数。"""
    clock = [0.0]
    native_time = ModuleType("capture_time")
    monkeypatch.setattr(native_time, "monotonic", lambda: clock[0], raising=False)
    monkeypatch.setattr(capture, "time", native_time)
    streams = modules(monkeypatch)
    frames = SoundDeviceCapture().frames(None)
    try:
        assert await anext(frames) == bytes(1024)
        for observed in (0.0, 1.0, 2.0, 10.0):
            clock[0] = observed
            streams[0].callback(Plane(bytes(960)), 480, object(), Status(True))
            assert isinstance(await anext(frames), CaptureGap)
            assert await anext(frames) == bytes(1024)
        assert streams[0].active and not streams[0].closed
    finally:
        await frames.aclose()


async def test_gap_discards_partial_pcm_and_replaces_resampler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺口前不足一帧的旧采样和滤波状态不得进入恢复后的声音。"""
    modules(monkeypatch)
    converters: list[Resampler] = []

    def start(native: Native) -> None:
        native.active = True
        for value, overflow in ((1, False), (2, True), (3, False)):
            native.callback(Plane(bytes((value, 0)) * 768), 768, object(), Status(overflow))

    def resample(resampler: Resampler, frame: Frame) -> list[Frame]:
        converters.append(resampler)
        result = Frame(format="s16", layout="mono", samples=256)
        result.planes[0].update(bytes(frame.planes[0])[:2] * 256)
        return [result]

    monkeypatch.setattr(Native, "start", start)
    monkeypatch.setattr(Resampler, "resample", resample)
    frames = SoundDeviceCapture().frames(None)
    try:
        assert isinstance(await anext(frames), CaptureGap)
        assert await anext(frames) == b"\x02\0" * 256 + b"\x03\0" * 256
        assert converters[0] is not converters[1]
        assert converters[1] is converters[2]
    finally:
        await frames.aclose()


async def test_variable_native_blocks_preserve_samples_and_fixed_vad_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """声卡自行选择块大小，只有 VAD 边界固定为 512 样本，不丢弃或重复跨块余量。"""
    streams = modules(monkeypatch)

    def start(native: Native) -> None:
        native.active = True
        for value, samples in ((1, 768), (2, 1536), (3, 768)):
            native.callback(Plane(bytes((value, 0)) * samples), samples, object(), Status())

    def resample(resampler: Resampler, frame: Frame) -> list[Frame]:
        del resampler
        assert frame.sample_rate == 48000
        converted = Frame(format="s16", layout="mono", samples=frame.samples // 3)
        converted.planes[0].update(bytes(frame.planes[0])[:2] * converted.samples)
        return [converted]

    monkeypatch.setattr(Native, "start", start)
    monkeypatch.setattr(Resampler, "resample", resample)
    frames = SoundDeviceCapture().frames(None)
    try:
        first, second = await anext(frames), await anext(frames)
        assert isinstance(first, bytes) and isinstance(second, bytes)
        assert len(first) == len(second) == 1024
        assert first + second == b"\x01\0" * 256 + b"\x02\0" * 512 + b"\x03\0" * 256
    finally:
        await frames.aclose()
    assert streams[0].closed and streams[0].aborted


async def test_cancel_native_creation_still_recovers_acquired_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opening, release = threading.Event(), threading.Event()
    streams = modules(monkeypatch, opening=opening, release=release)
    frames = SoundDeviceCapture().frames(None)
    task = asyncio.create_task(anext(frames))
    assert await asyncio.to_thread(opening.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not streams[0].closed
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert streams[0].closed and streams[0].aborted


@pytest.mark.parametrize("phase", ["capacity", "checkpoint_restore", "checkpoint_save"])
async def test_slow_model_preparation_keeps_native_capture_draining(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
) -> None:
    """真实采集队列在多轮慢历史/请求计量期间持续排空，不打开物理设备。"""
    streams = modules(monkeypatch)
    stop = threading.Event()
    producers: list[threading.Thread] = []

    def start(native: Native) -> None:
        """以独立回调线程模拟真实声卡，不随事件循环停顿而暂停生产。"""
        native.active = True

        def produce() -> None:
            while not stop.wait(0.01):
                try:
                    native.callback(Plane(bytes(960)), 480, object(), Status())
                except CallbackAbort:
                    native.active = False
                    break

        producer = threading.Thread(target=produce)
        producers.append(producer)
        producer.start()

    monkeypatch.setattr(Native, "start", start)
    frames_read = 0

    class Detector:
        """静音帧只用来观察采集持续推进，不发起识别。"""

        def probability(self, pcm: bytes) -> float:
            """记录已经从生产驱动取出的帧，静音不触发发言。"""
            nonlocal frames_read
            assert pcm == bytes(1024)
            frames_read += 1
            return 0.0

    measurements = 0
    loop_thread = threading.get_ident()

    def slow_capacity(payload: JsonObject):
        """保留完整计量结果，只增加本地计算等待。"""
        nonlocal measurements
        measurements += 1
        # 超过生产队列的 64 × 10 ms 容量，模拟完整媒体布局和长历史编码。
        time.sleep(0.85)
        return capacity(payload)

    def slow_decode(encoded: str) -> ConversationCheckpoint:
        """模拟连续原声历史展开耗时，仍执行完整检查点校验。"""
        assert threading.get_ident() != loop_thread
        time.sleep(1.2)
        return decode_checkpoint(encoded)

    def slow_encode(checkpoint: ConversationCheckpoint) -> str:
        """模拟完整历史编码耗时，不替换实际提交材料。"""
        assert threading.get_ident() != loop_thread
        time.sleep(1.2)
        return encode_checkpoint(checkpoint)

    if phase == "checkpoint_restore":
        monkeypatch.setattr(session_module, "decode_checkpoint", slow_decode)
        monkeypatch.setattr(history_preparation_module, "decode_checkpoint", slow_decode)
    elif phase == "checkpoint_save":
        monkeypatch.setattr(session_module, "encode_checkpoint", slow_encode)

    store = await Store.open(tmp_path / "responsive.sqlite3")
    session = ContinuousSession(
        store,
        "foreground",
        can_start=lambda: None,
        compiler=compile_materials,
    )
    backend = ScriptedBackend(tuple(ModelResponse(Message.assistant("内部完成")) for _ in range(2)))
    maintenance = HistoryMaintenance(
        store,
        store,
        backend,
        model="openai/test",
        summary_capacity=capacity,
        retry=RetrySettings(),
        output_tokens=1000,
        diagnostic_bytes=4096,
        compiler=compile_materials,
    )
    preparation = HistoryPreparation(
        store,
        maintenance,
        backend=backend,
        model="openai/test",
        assess=slow_capacity,
        context=ContextSettings(),
        compiler=compile_materials,
    )
    runtime = VoiceInputRuntime(
        "voice",
        settings(),
        Inputs(store),
        SoundDeviceCapture,
        Detector,
        Transcriber(),
        native_audio_enabled=False,
    )
    try:
        await runtime.start("asr", None)
        for index in range(2):
            before = frames_read
            identity = await admitted(store, str(index), "离线输入")
            await session.run(
                Message.user("离线输入"),
                turn_id=f"turn-{index}",
                input_ids=(identity,),
                backend=backend,
                evidence=store,
                model="openai/test",
                instructions=(),
                tools=ToolSet(),
                output_tokens=1000,
                max_requests=8,
                turn_seconds=30,
                retry=RetrySettings(),
                diagnostic_bytes=4096,
                refresh=refresh,
                capacity=slow_capacity,
                prepare_history=preparation,
            )
            # 给采集任务观察回调线程的错误；会话返回不代表采集仍健康。
            await asyncio.sleep(0.02)
            assert runtime.state == "listening", runtime.error
            assert frames_read - before > 64
        assert measurements == 4 and len(backend.requests) == 2
        assert not runtime.gaps
    finally:
        stop.set()
        for producer in producers:
            await asyncio.to_thread(producer.join, 2)
            assert not producer.is_alive()
        await runtime.close()
        await session.close()
        await preparation.close()
        await store.close()
    assert streams[0].closed and streams[0].aborted


async def test_cancel_preparation_waits_for_original_thread() -> None:
    """重复取消也不遗弃仍借用请求状态的线程，归还后才向调用方传播取消。"""
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()

    def prepare() -> None:
        entered.set()
        assert release.wait(3)
        returned.set()

    task = asyncio.create_task(prepare_off_loop(prepare))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert returned.is_set()
