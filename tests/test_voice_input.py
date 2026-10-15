"""生成 PCM、替身设备和真实临时 Store 验证发言接管及输入资源边界。

不打开本机声卡，不请求 ASR/生成模型；替身只替换设备和网络，不替换
配置、VAD 分段、插件能力绑定、原文事务和处理状态查询。
"""

from __future__ import annotations

import asyncio
import io
import struct
import time
import wave
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from pathlib import Path

import pytest
from pydantic import SecretStr

from purrcept_engine.execution.protocol import decode_object
from purrcept_engine.execution.providers import CallContext
from purrcept_engine.plugins.base import PluginPorts
from purrcept_engine.plugins.voice_input.models import (
    InputDevice,
    TranscriptUpdate,
    VoiceInputError,
    VoiceInputSettings,
)
from purrcept_engine.plugins.voice_input.plugin import factory, make_factory
from purrcept_engine.plugins.voice_input.runtime import VoiceInputRuntime
from purrcept_engine.plugins.voice_input.vad import Segmenter
from purrcept_engine.storage.models import Admission, InputRecord, InputSubmission
from purrcept_engine.storage.store import Store
from purrcept_engine.storage.subject import InputProcessing

SILENCE = b"\0" * 1024
SPEECH = struct.pack("<512h", *([1000] * 512))


class Inputs:
    """真实入账后保存接管事实，阻塞门用于核对取消不撤销已经开始的事务。"""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.submissions: list[InputSubmission] = []
        self.admissions: list[Admission] = []
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def submit(self, submission: InputSubmission) -> Admission:
        """门控只延迟确认，实际接管仍由同一 Store 原子完成。"""
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        result = await self.store.admit(submission)
        self.submissions.append(submission)
        self.admissions.append(result)
        return result

    async def query_input(self, input_id: str) -> InputRecord | None:
        """读取真实原文和来源。"""
        return await self.store.get_input(input_id)

    async def query_processing(self, input_id: str) -> InputProcessing | None:
        """读取真实处理状态，没有假认知确认。"""
        return await self.store.input_processing(input_id)


class Capture:
    """可控制帧序列的替身声卡，队列有界，关闭标记来自迭代器 finally。"""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=128)
        self.queue.put_nowait(SILENCE)
        self.closed = asyncio.Event()
        self.opened = 0

    async def devices(self) -> tuple[InputDevice, ...]:
        """只列出替身设备，不开启采集。"""
        return (InputDevice(7, "生成 PCM 设备"),)

    async def frames(self, device: int | None) -> AsyncGenerator[bytes]:
        """交付脚本帧后等待，不用真实时间伪造设备状态。"""
        del device
        self.opened += 1
        try:
            while True:
                yield await self.queue.get()
        finally:
            self.closed.set()

    async def feed(self, pcm: bytes, count: int) -> None:
        """有界生产帧并让采集/识别推进，不创建逐帧后台任务。"""
        async with asyncio.timeout(3):
            for _ in range(count):
                await self.queue.put(pcm)
                await asyncio.sleep(0.001)


class Detector:
    """生成帧明确标注人声，不调用真实 ONNX。"""

    def probability(self, pcm: bytes) -> float:
        """仅供测试把非零样本看作人声。"""
        return 1.0 if pcm != SILENCE else 0.0


class Transcriber:
    """消耗完整有序帧后才交付最终文字，可故意保留临时结果。"""

    def __init__(self, *, final: bool = True, block: bool = False) -> None:
        self.final, self.block = final, block
        self.release = asyncio.Event()
        self.calls = 0
        self.byte_count = 0

    async def transcribe(self, frames: AsyncIterator[bytes]) -> AsyncIterator[TranscriptUpdate]:
        """同一调用内不重试，EOF 才表示发言已结束。"""
        self.calls += 1
        if self.block:
            await self.release.wait()
        async for pcm in frames:
            self.byte_count += len(pcm)
        yield TranscriptUpdate("0", "完整识别文字", self.final)


def settings(**values: object) -> VoiceInputSettings:
    """使用短门槛完成本地时序场景，其他字段仍经过真实配置校验。"""
    return VoiceInputSettings.model_validate(
        {
            "start_ms": 32,
            "pre_roll_ms": 128,
            "min_speech_ms": 32,
            "end_ms": 224,
            "interrupt_ms": 128,
            "interrupt_gap_ms": 64,
            "asr_api_key": SecretStr("test"),
            **values,
        }
    )


async def until(predicate: Callable[[], bool]) -> None:
    """等待有界本地状态推进，超时保留失败而非继续测试。"""
    for _ in range(600):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise TimeoutError("本地状态未在三秒内完成。")


async def test_slow_start_admission_does_not_stop_capture(tmp_path: Path) -> None:
    """持续人声入库等待期间仍读完音频，最终材料等待原起音身份后有序入账。"""
    store = await Store.open(tmp_path / "slow-admission.sqlite3")
    inputs, capture = Inputs(store), Capture()
    inputs.gate = asyncio.Event()
    runtime = VoiceInputRuntime(
        "voice",
        settings(min_speech_ms=96),
        inputs,
        lambda: capture,
        Detector,
        Transcriber(),
        native_audio_enabled=True,
    )
    try:
        await runtime.start("audio", 7)
        await capture.feed(SPEECH, 5)
        await asyncio.wait_for(inputs.entered.wait(), 2)
        await capture.feed(SPEECH, 70)
        await capture.feed(SILENCE, 7)
        await until(lambda: capture.queue.empty())
        assert runtime.state == "listening" and runtime.current is None
        assert inputs.admissions == []
        # 前一句仍在等入库时，短噪声的丢弃也不能卡住后续静音读取。
        await capture.feed(SPEECH, 1)
        await capture.feed(SILENCE, 20)
        await until(lambda: capture.queue.empty())
        assert runtime.state == "listening"
        inputs.gate.set()
        await until(lambda: len(inputs.admissions) == 2)
        started, final = inputs.submissions
        assert started.source.event_kind == "speech_started"
        details = decode_object(final.context.details_json.encode())["voice"]
        assert isinstance(details, dict)
        assert details["speech_started_input_id"] == inputs.admissions[0].input_id
        assert len(final.media) == 1
    finally:
        inputs.gate.set()
        await runtime.close()
        await store.close()


async def test_buffered_frames_allow_recognition_to_drain(tmp_path: Path) -> None:
    """一次取得多帧时也给识别消费者运行机会，不将调度饥饿误报为网络积压。"""
    store = await Store.open(tmp_path / "burst.sqlite3")
    inputs, capture, transcriber = Inputs(store), Capture(), Transcriber()
    runtime = VoiceInputRuntime(
        "voice",
        settings(interrupt_ms=2000, frame_queue_size=8),
        inputs,
        lambda: capture,
        Detector,
        transcriber,
        native_audio_enabled=False,
    )
    try:
        await runtime.start("asr", 7)
        for _ in range(50):
            capture.queue.put_nowait(SPEECH)
        for _ in range(7):
            capture.queue.put_nowait(SILENCE)
        await until(lambda: len(inputs.admissions) == 1 or runtime.state != "listening")
        assert runtime.state == "listening"
        assert inputs.submissions[0].text == "完整识别文字"
        assert transcriber.byte_count >= 50 * 1024
    finally:
        await runtime.close()
        await store.close()


@pytest.mark.asyncio
async def test_real_store_speech_started_then_final_does_not_interrupt_twice(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "facts.sqlite3")
    inputs, capture, transcriber = Inputs(store), Capture(), Transcriber()
    runtime = VoiceInputRuntime(
        "voice",
        settings(),
        inputs,
        lambda: capture,
        Detector,
        transcriber,
        native_audio_enabled=False,
    )
    try:
        assert runtime.snapshot()["devices_loaded"] is False
        await runtime.devices()
        assert runtime.snapshot()["devices"] == [{"index": 7, "name": "生成 PCM 设备"}]
        assert capture.opened == 0
        assert (await runtime.start("asr", 7))["state"] == "listening"
        await capture.feed(SPEECH, 5)
        await until(lambda: len(inputs.admissions) == 1)
        started = inputs.submissions[0]
        assert started.context.speech_started and started.context.interrupt_generation
        await capture.feed(SILENCE, 7)
        await until(lambda: len(inputs.admissions) == 2)
        final = inputs.submissions[1]
        assert final.text == "完整识别文字" and not final.context.interrupt_generation
        assert not final.context.speech_started and final.context.minimum_wake
        assert final.source.native_event_id == started.source.native_event_id
        assert final.source.conversation_id == started.source.conversation_id
        details = decode_object(final.context.details_json.encode())["voice"]
        assert isinstance(details, dict)
        assert details["speech_started_input_id"] == inputs.admissions[0].input_id
        record = await store.get_input(inputs.admissions[1].input_id)
        assert record is not None and record.source.display_name == "本机麦克风"
        status = await runtime.status()
        history = status["history"]
        assert isinstance(history, list) and isinstance(history[0], dict)
        assert history[0]["processing_stage"] == "foreground"
        await runtime.stop()
        assert capture.closed.is_set() and runtime.snapshot()["session_id"] == ""
        assert await store.get_input(inputs.admissions[1].input_id) == record
    finally:
        await runtime.close()
        await store.close()


@pytest.mark.asyncio
async def test_native_media_split_and_source_identity_through_factory(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "native.sqlite3")
    inputs, capture = Inputs(store), Capture()
    plugin = make_factory(
        lambda config: capture, lambda config: Detector(), lambda config: Transcriber()
    )
    blueprint = plugin.prepare("voice", settings().model_dump(), tmp_path)
    resource = await blueprint.build(PluginPorts(inputs, lambda: {}, native_audio_enabled=True))
    implementations = {item.declaration.name: item for item in resource.capabilities}
    try:
        assert resource.panel is not None and resource.panel.kind == "voice_input"
        assert "interrupt" not in implementations and resource.start is None
        context = CallContext("start", "action", time.monotonic() + 30)
        await implementations["start"].invoke({"mode": "audio", "device": 7}, context)
        await capture.feed(SPEECH, 660)
        await capture.feed(SILENCE, 7)
        await until(lambda: len(inputs.admissions) == 2)
        final = inputs.submissions[-1]
        assert len(final.media) == 2 and final.source.event_kind == "utterance"
        lengths: list[int] = []
        for asset in final.media:
            with wave.open(io.BytesIO(asset.content), "rb") as audio:
                assert audio.getframerate() == 16000 and audio.getnchannels() == 1
                lengths.append(audio.getnframes())
        assert lengths[0] == 16000 * 20 and lengths[1] > 0
        assert not final.context.interrupt_generation
    finally:
        if resource.close:
            await resource.close()
        await store.close()


@pytest.mark.asyncio
async def test_mode_rejection_precedes_device_open(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "reject.sqlite3")
    capture = Capture()
    runtime = VoiceInputRuntime(
        "voice",
        settings(asr_api_key=SecretStr("")),
        Inputs(store),
        lambda: capture,
        Detector,
        Transcriber(),
        native_audio_enabled=False,
    )
    try:
        for mode in ("audio", "asr"):
            with pytest.raises(VoiceInputError):
                await runtime.start(mode, None)
        assert capture.opened == 0 and runtime.snapshot()["state"] == "stopped"
        assert factory.prepare("voice", {}, tmp_path).required_modules == (
            "sounddevice",
            "av",
            "numpy",
            "onnxruntime",
            "websockets",
        )
    finally:
        await runtime.close()
        await store.close()


@pytest.mark.asyncio
async def test_overload_pauses_without_unbounded_tasks_or_partial_final(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "overload.sqlite3")
    capture, inputs = Capture(), Inputs(store)
    runtime = VoiceInputRuntime(
        "voice",
        settings(frame_queue_size=8),
        inputs,
        lambda: capture,
        Detector,
        Transcriber(block=True),
        native_audio_enabled=False,
    )
    try:
        await runtime.start("asr", None)
        await capture.feed(SPEECH, 16)
        await until(lambda: capture.closed.is_set())
        await until(lambda: runtime.snapshot()["pending_utterances"] == 0)
        assert runtime.snapshot()["state"] == "paused"
        assert runtime.snapshot()["pending_utterances"] == 0
        assert runtime.snapshot()["gaps"] and len(inputs.admissions) == 1
        material = await runtime.prepare_request()
        assert material["state"] == "paused" and material["gaps"] == runtime.snapshot()["gaps"]
        assert material["error"] == runtime.snapshot()["error"]
        assert "history" not in material and "pending_utterances" not in material
        assert "partial" not in material and "revision" not in material
        assert inputs.submissions[0].source.event_kind == "speech_started"
    finally:
        await runtime.close()
        await store.close()


@pytest.mark.asyncio
async def test_cancel_capture_wait_does_not_revoke_admission(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "cancel.sqlite3")
    inputs, capture = Inputs(store), Capture()
    inputs.gate = asyncio.Event()
    runtime = VoiceInputRuntime(
        "voice",
        settings(),
        inputs,
        lambda: capture,
        Detector,
        Transcriber(),
        native_audio_enabled=False,
    )
    try:
        await runtime.start("asr", None)
        await capture.feed(SPEECH, 4)
        await asyncio.wait_for(inputs.entered.wait(), 2)
        stop = asyncio.create_task(runtime.stop())
        await asyncio.sleep(0)
        assert not stop.done()
        inputs.gate.set()
        await asyncio.wait_for(stop, 2)
        assert len(inputs.admissions) == 1
        assert await store.get_input(inputs.admissions[0].input_id) is not None
        assert capture.closed.is_set()
    finally:
        await runtime.close()
        await store.close()


@pytest.mark.asyncio
async def test_partial_transcript_reports_gap_and_never_submits_final(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "partial.sqlite3")
    inputs, capture = Inputs(store), Capture()
    runtime = VoiceInputRuntime(
        "voice",
        settings(),
        inputs,
        lambda: capture,
        Detector,
        Transcriber(final=False),
        native_audio_enabled=False,
    )
    try:
        await runtime.start("asr", None)
        await capture.feed(SPEECH, 2)
        await capture.feed(SILENCE, 7)
        await until(lambda: bool(runtime.snapshot()["gaps"]))
        assert inputs.admissions == []
    finally:
        await runtime.close()
        await store.close()


@pytest.mark.asyncio
async def test_cancel_stop_observation_keeps_original_device_owner(tmp_path: Path) -> None:
    class SlowCapture(Capture):
        """模拟原生输入流的延迟释放，不允许同时启动新流。"""

        def __init__(self) -> None:
            super().__init__()
            self.closing, self.release = asyncio.Event(), asyncio.Event()

        async def frames(self, device: int | None) -> AsyncGenerator[bytes]:
            del device
            self.opened += 1
            try:
                while True:
                    yield await self.queue.get()
            finally:
                self.closing.set()
                await self.release.wait()
                self.closed.set()

    store = await Store.open(tmp_path / "closing.sqlite3")
    capture = SlowCapture()
    runtime = VoiceInputRuntime(
        "voice",
        settings(),
        Inputs(store),
        lambda: capture,
        Detector,
        Transcriber(),
        native_audio_enabled=False,
    )
    try:
        await runtime.start("asr", None)
        stopped = asyncio.create_task(runtime.stop())
        await asyncio.wait_for(capture.closing.wait(), 2)
        stopped.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopped
        assert runtime.snapshot()["state"] == "stopping" and capture.opened == 1
        with pytest.raises(VoiceInputError, match="已经开启"):
            await runtime.start("asr", None)
        capture.release.set()
        await asyncio.wait_for(runtime.stop(), 2)
        assert capture.closed.is_set() and runtime.snapshot()["state"] == "stopped"
    finally:
        capture.release.set()
        await runtime.close()
        await store.close()


def test_continuous_speech_gate_ignores_long_gaps_and_long_utterance_is_rejected() -> None:
    segmenter = Segmenter(settings(interrupt_ms=160, interrupt_gap_ms=32))
    kinds: list[str] = []
    for probability in [1.0, 1.0, 0.0, 0.0, 1.0, 1.0]:
        kinds.extend(kind for kind, _ in segmenter.feed(SPEECH, probability))
    assert "speech_started" not in kinds
    for _ in range(3):
        kinds.extend(kind for kind, _ in segmenter.feed(SPEECH, 1.0))
    assert kinds.count("speech_started") == 1
    with pytest.raises(VoiceInputError, match="60 秒"):
        for _ in range(2000):
            segmenter.feed(SPEECH, 1.0)
