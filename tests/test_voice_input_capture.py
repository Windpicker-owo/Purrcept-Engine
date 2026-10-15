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
from purrcept_core.models import Message, ModelResponse, ToolSet
from test_subject_session import ScriptedBackend, admitted, capacity, refresh
from test_voice_input import Inputs, Transcriber, settings

from purrcept_engine.app.config import ContextSettings, RetrySettings
from purrcept_engine.app.history import HistoryPreparation
from purrcept_engine.execution.protocol import JsonObject
from purrcept_engine.plugins.voice_input import capture
from purrcept_engine.plugins.voice_input.capture import SoundDeviceCapture
from purrcept_engine.plugins.voice_input.models import VoiceInputError
from purrcept_engine.plugins.voice_input.runtime import VoiceInputRuntime
from purrcept_engine.storage.store import Store
from purrcept_engine.subject.history import HistoryMaintenance
from purrcept_engine.subject.model_requests import prepare_off_loop
from purrcept_engine.subject.session import ContinuousSession


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
        assert samplerate == 48000 and channels == 1 and dtype == "int16" and blocksize == 480
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
    devices = await driver.devices()
    assert devices[0].name == "替身声卡" and streams == []
    frames = driver.frames(None)
    assert await anext(frames) == b"\0" * 1024
    await frames.aclose()
    assert len(streams) == 1 and streams[0].closed and streams[0].aborted


async def test_native_overflow_closes_same_input_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    streams = modules(monkeypatch, overflow=True)
    frames = SoundDeviceCapture().frames(None)
    with pytest.raises(VoiceInputError, match="溢出"):
        await anext(frames)
    assert streams[0].closed


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


async def test_slow_model_preparation_keeps_native_capture_draining(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    def slow_capacity(payload: JsonObject):
        """保留完整计量结果，只增加本地计算等待。"""
        nonlocal measurements
        measurements += 1
        # 超过生产队列的 64 × 10 ms 容量，模拟首次分词加载和长历史计算。
        time.sleep(0.85)
        return capacity(payload)

    store = await Store.open(tmp_path / "responsive.sqlite3")
    session = ContinuousSession(store, "foreground", can_start=lambda: None)
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
    )
    preparation = HistoryPreparation(
        store,
        maintenance,
        backend=backend,
        model="openai/test",
        assess=slow_capacity,
        context=ContextSettings(),
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
