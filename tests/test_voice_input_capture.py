"""有限原生库替身验证生产 SoundDeviceCapture 的采样与资源所有权。

替身执行真实驱动的回调、线程队列、重采样交接和 finally，不打开本机
设备。取消设备创建与原生回调溢出均须关闭原取得的同一输入流。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from types import ModuleType

import pytest

from purrcept_engine.plugins.voice_input import capture
from purrcept_engine.plugins.voice_input.capture import SoundDeviceCapture
from purrcept_engine.plugins.voice_input.models import VoiceInputError


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
