"""真实临时Store、有限异步替身与MockTransport验证现场声音事实。

测试不合成付费声音、不播放设备、不访问模型或真实消息；阻塞合成
替身用于重放无法物理取消的工作。排队、期限、明确停音和关闭必须从
同库阶段事实证明，不能只断言本地 accepted 或播放器被调用。
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr

from purrcept_engine.errors import StorageError
from purrcept_engine.execution.protocol import JsonObject
from purrcept_engine.execution.providers import CallContext, CallOutcome
from purrcept_engine.plugins.audio import player as player_module
from purrcept_engine.plugins.audio.dsp import encode_wav
from purrcept_engine.plugins.audio.models import (
    AudioError,
    AudioSettings,
    PlaybackResult,
    RecordParameters,
    SpeechAudio,
    SpeechParameters,
)
from purrcept_engine.plugins.audio.player import FFplayPlayer
from purrcept_engine.plugins.audio.runtime import AudioRuntime
from purrcept_engine.plugins.audio.tts import FishSource
from purrcept_engine.plugins.live2d.models import BodyIntent
from purrcept_engine.storage.audio import make_fact
from purrcept_engine.storage.store import Store


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    """只打开本次测试生成的数据库，连接仍归Store单线程。"""
    value = await Store.open(tmp_path / "audio.sqlite3")
    try:
        yield value
    finally:
        await value.close()


def context(identity: str, seconds: float = 10) -> CallContext:
    """宿主身份替身使用事件循环绝对期限，不从文本前缀猜来源。"""
    return CallContext(
        identity, "action:" + identity, asyncio.get_running_loop().time() + seconds, "input:现场"
    )


class Source:
    """可冻结首项合成的外部服务替身，停止请求不能替它完成原工作。"""

    def __init__(self, *, block: bool = False) -> None:
        self.block = block
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.calls: list[str] = []
        self.closed = 0

    async def synthesize(
        self, parameters: SpeechParameters | RecordParameters, identity: str
    ) -> SpeechAudio:
        del identity
        self.calls.append(parameters.text)
        self.entered.set()
        if self.block and len(self.calls) == 1:
            await self.release.wait()
        return SpeechAudio(parameters.text.encode(), "mp3")

    async def stream(
        self, parameters: SpeechParameters, identity: str
    ) -> AsyncGenerator[bytes, None]:
        result = await self.synthesize(parameters, identity)
        yield result.data

    async def close(self) -> None:
        self.closed += 1


class Player:
    """无声设备替身，只有显式started回调才产生实际播放事实。"""

    def __init__(self, *, block: bool = False) -> None:
        self.block = block
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.played: list[bytes] = []
        self.interrupted = False
        self.closed = 0

    def check_available(self) -> None:
        return

    async def play(
        self,
        pcm: AsyncIterator[bytes],
        *,
        playback_id: str,
        deadline: float,
        started: Callable[[], Awaitable[None]],
        progress: Callable[[float, float], None],
    ) -> PlaybackResult:
        del deadline, playback_id
        self.interrupted = False
        self.played.append(b"".join([chunk async for chunk in pcm]))
        await started()
        progress(0.1, 0.4)
        self.entered.set()
        if self.block and len(self.played) == 1:
            await self.release.wait()
        return PlaybackResult("interrupted" if self.interrupted else "completed", True, 0.1)

    async def interrupt(self, playback_id: str | None = None) -> None:
        del playback_id
        self.interrupted = True
        self.release.set()

    async def close(self) -> None:
        self.closed += 1
        await self.interrupt()


class Body:
    """记录轻量视觉交接，不打开VTS或拥有声音。"""

    def __init__(self) -> None:
        self.events: list[str] = []

    def playback_started(
        self, playback_id: str, intent: BodyIntent | None, deadline: float
    ) -> JsonObject:
        del intent, deadline
        self.events.append("started:" + playback_id)
        return {"accepted": True, "adopted": False}

    def playback_progress(self, playback_id: str, elapsed_seconds: float, energy: float) -> None:
        del elapsed_seconds, energy
        self.events.append("progress:" + playback_id)

    def playback_pcm(
        self, playback_id: str, sample_rate: int, sample_offset: int, pcm: bytes
    ) -> None:
        del playback_id, sample_rate, sample_offset, pcm

    def playback_finished(self, playback_id: str) -> JsonObject:
        self.events.append("finished:" + playback_id)
        return {"accepted": True, "adopted": True}


async def runtime(
    store: Store, tmp_path: Path, source: Source, player: Player, body: Body | None = None
) -> AudioRuntime:
    """所有测试沿同一个同库Port接线，只有解码/设备使用可观察替身。"""
    value = AudioRuntime(
        "audio",
        AudioSettings(playback_backend="ffplay"),
        source,
        player,
        store.audio,
        tmp_path,
        None,
        decode=False,
    )
    value.bind_body(body)
    await value.start()
    return value


def payload(outcome: CallOutcome) -> JsonObject:
    assert isinstance(outcome.value, dict)
    return outcome.value


async def test_one_active_four_pending_rejects_without_synthesizing(
    store: Store, tmp_path: Path
) -> None:
    source, player = Source(block=True), Player()
    owner = await runtime(store, tmp_path, source, player)
    tasks = [asyncio.create_task(owner.speak(SpeechParameters(text="首项"), context("first")))]
    await source.entered.wait()
    tasks.extend(
        asyncio.create_task(owner.speak(SpeechParameters(text=str(index)), context(str(index))))
        for index in range(4)
    )
    await asyncio.sleep(0)
    rejected = await owner.speak(SpeechParameters(text="超额"), context("overflow"))
    assert rejected.error_type == "AudioQueueFull"
    assert payload(rejected)["accepted"] is False
    assert source.calls == ["首项"]
    queued = owner.snapshot()["queued"]
    assert isinstance(queued, list)
    assert len(queued) == 4
    source.release.set()
    results = await asyncio.gather(*tasks)
    assert all(payload(result)["completed"] is True for result in results)
    assert source.calls == ["首项", "0", "1", "2", "3"]
    await owner.close()


async def test_interrupt_revokes_synthesis_and_all_existing_queue_then_new_speech_works(
    store: Store, tmp_path: Path
) -> None:
    source, player, body = Source(block=True), Player(), Body()
    owner = await runtime(store, tmp_path, source, player, body)
    first = asyncio.create_task(
        owner.speak(
            SpeechParameters(text="迟到合成", body=BodyIntent(gesture="nod")), context("first")
        )
    )
    await source.entered.wait()
    queued = asyncio.create_task(owner.speak(SpeechParameters(text="旧候播"), context("queued")))
    await asyncio.sleep(0)
    control = await owner.interrupt(context("stop", 0.01))
    assert control.state == "unknown"
    assert payload(control)["accepted"] is True
    assert payload(control)["confirmed"] is False
    queued_result = payload(await queued)
    assert queued_result["synthesis_started"] is False
    assert queued_result["started"] is False
    newer = asyncio.create_task(owner.speak(SpeechParameters(text="新声音"), context("new")))
    source.release.set()
    first_result, new_result = await asyncio.gather(first, newer)
    assert payload(first_result)["synthesized"] is True
    assert payload(first_result)["started"] is False
    assert payload(new_result)["completed"] is True
    assert player.played == ["新声音".encode()]
    new_id = payload(new_result)["playback_id"]
    assert isinstance(new_id, str)
    assert new_id != "new"
    assert body.events == ["started:" + new_id, "progress:" + new_id, "finished:" + new_id]
    queued_id = queued_result["playback_id"]
    first_id = payload(first_result)["playback_id"]
    assert isinstance(queued_id, str) and isinstance(first_id, str)
    assert [fact.fact for fact in await store.audio.facts("audio", queued_id)] == [
        "accepted",
        "finished",
    ]
    assert [fact.fact for fact in await store.audio.facts("audio", first_id)] == [
        "accepted",
        "synthesis_started",
        "synthesized",
        "finished",
    ]
    await owner.close()


async def test_actual_partial_playback_is_saved_and_stop_confirmed(
    store: Store, tmp_path: Path
) -> None:
    source, player = Source(), Player(block=True)
    owner = await runtime(store, tmp_path, source, player)
    speech = asyncio.create_task(owner.speak(SpeechParameters(text="现场"), context("voice")))
    await player.entered.wait()
    control = await owner.interrupt(context("stop"))
    assert payload(control)["confirmed"] is True
    value = payload(await speech)
    assert value["status"] == "interrupted"
    assert value["partial_playback_possible"] is True
    assert value["synthesized"] is True
    identity = value["playback_id"]
    assert isinstance(identity, str)
    facts = await store.audio.facts("audio", identity)
    assert facts[-2].fact == "playback_started"
    assert facts[-1].payload()["status"] == "interrupted"
    await owner.close()


async def test_queued_deadline_finishes_without_waiting_for_active_synthesis(
    store: Store, tmp_path: Path
) -> None:
    source, player = Source(block=True), Player()
    owner = await runtime(store, tmp_path, source, player)
    first = asyncio.create_task(owner.speak(SpeechParameters(text="阻塞"), context("first")))
    await source.entered.wait()
    value = await asyncio.wait_for(
        owner.speak(SpeechParameters(text="过期"), context("expired", 0.01)), 1
    )
    assert payload(value)["status"] == "not_started"
    assert payload(value)["synthesis_started"] is False
    assert owner.snapshot()["queued"] == []
    assert source.calls == ["阻塞"]
    source.release.set()
    await first
    await owner.close()


async def test_cancelled_waiter_does_not_revoke_taken_over_sound(
    store: Store, tmp_path: Path
) -> None:
    source, player = Source(block=True), Player()
    owner = await runtime(store, tmp_path, source, player)
    speech = asyncio.create_task(owner.speak(SpeechParameters(text="已经接管"), context("voice")))
    await source.entered.wait()
    speech.cancel()
    with pytest.raises(asyncio.CancelledError):
        await speech
    source.release.set()
    await owner.speak(SpeechParameters(text="下一次显式说话"), context("next"))
    assert player.played[0] == "已经接管".encode()
    await owner.close()


async def test_close_waits_unrevokable_synthesis_and_never_plays_late_audio(
    store: Store, tmp_path: Path
) -> None:
    source, player = Source(block=True), Player()
    owner = await runtime(store, tmp_path, source, player)
    speech = asyncio.create_task(owner.speak(SpeechParameters(text="关闭时合成"), context("voice")))
    await source.entered.wait()
    close = asyncio.create_task(owner.close())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not close.done()
    source.release.set()
    await close
    assert payload(await speech)["started"] is False
    assert player.played == []
    await owner.close()
    assert source.closed == player.closed == 1


async def test_record_exports_file_outside_live_queue_and_stop_does_not_remove_it(
    store: Store, tmp_path: Path
) -> None:
    source, player = Source(block=True), Player()
    owner = await runtime(store, tmp_path, source, player)
    speech = asyncio.create_task(owner.speak(SpeechParameters(text="现场阻塞"), context("voice")))
    await source.entered.wait()
    recorded = await owner.record(RecordParameters(text="文件", save_as="clip"), context("record"))
    assert recorded.state == "confirmed"
    assert recorded.value == str(tmp_path / "clip.mp3")
    assert (tmp_path / "clip.mp3").read_bytes() == "文件".encode()
    assert owner.snapshot()["queued"] == []
    await owner.interrupt(context("stop", 0.01))
    source.release.set()
    assert payload(await speech)["started"] is False
    rejected = await owner.record(
        RecordParameters(text="逃逸", save_as="../outside.mp3"), context("escape")
    )
    assert rejected.state == "failed"
    assert source.calls == ["现场阻塞", "文件"]
    await owner.close()
    assert (tmp_path / "clip.mp3").is_file()


async def test_audio_facts_use_real_store_and_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "reopen.sqlite3"
    database = await Store.open(path)
    await database.audio.append(
        make_fact("audio", "voice", "finished", 1.0, {"started": True, "completed": False})
    )
    with pytest.raises(StorageError, match="冲突"):
        await database.audio.append(
            make_fact("audio", "voice", "finished", 2.0, {"started": False})
        )
    await database.close()
    reopened = await Store.open(path)
    try:
        facts = await reopened.audio.facts("audio", "voice")
        assert facts[0].payload() == {"started": True, "completed": False}
    finally:
        await reopened.close()


@pytest.mark.parametrize(
    "stage,occurred",
    [
        ("arbitrary", 1.0),
        ("finished", float("nan")),
        ("finished", float("inf")),
        ("finished", -1.0),
    ],
)
def test_audio_fact_admission_rejects_unbounded_or_unknown_stages(
    stage: str, occurred: float
) -> None:
    with pytest.raises(ValueError):
        make_fact("audio", "voice", stage, occurred, {})


async def test_audio_reader_rejects_corrupt_saved_time_in_actual_temporary_store(
    tmp_path: Path,
) -> None:
    path = tmp_path / "corrupt-audio.sqlite3"
    database = await Store.open(path)
    await database.audio.append(make_fact("audio", "voice", "finished", 1.0, {"completed": False}))
    await database.close()
    with sqlite3.connect(path) as corruption:
        corruption.execute("UPDATE audio_facts SET occurred_at=?", (float("inf"),))
    reopened = await Store.open(path)
    try:
        with pytest.raises(StorageError, match="时间无效"):
            await reopened.audio.facts("audio", "voice")
    finally:
        await reopened.close()


async def test_stop_deadline_covers_delayed_player_cleanup_and_late_confirmation(
    store: Store, tmp_path: Path
) -> None:
    class SlowStop(Player):
        def __init__(self) -> None:
            super().__init__(block=True)
            self.stop_entered, self.stop_release = asyncio.Event(), asyncio.Event()

        async def interrupt(self, playback_id: str | None = None) -> None:
            self.stop_entered.set()
            await self.stop_release.wait()
            await super().interrupt(playback_id)

    source, player = Source(), SlowStop()
    owner = await runtime(store, tmp_path, source, player)
    speech = asyncio.create_task(owner.speak(SpeechParameters(text="现场"), context("voice")))
    await player.entered.wait()
    async with asyncio.timeout(0.5):
        control = await owner.interrupt(context("vad-without-action-row", 0.01))
    assert control.state == "unknown"
    assert player.stop_entered.is_set()
    assert not speech.done()
    player.stop_release.set()
    await speech
    await owner.close()
    facts = await store.audio.facts("audio", "vad-without-action-row")
    assert [fact.fact for fact in facts] == [
        "control_accepted",
        "control_result",
        "control_confirmed",
    ]
    assert facts[-2].payload()["confirmed"] is False
    assert facts[-1].payload()["confirmed"] is True


def metadata(request: httpx.Request) -> httpx.Response:
    """Fish v3原生目录替身，文件/流式操作声明都进入生产校验边界。"""
    if request.url.path.endswith("capabilities"):
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "id": "model",
                        "available": True,
                        "supportedOperations": {"synchronous": True, "alignedStreaming": True},
                        "outputFormats": ["mp3", "wav"],
                        "maxTextLength": 10000,
                        "controls": {
                            "speed": {"min": 0.5, "max": 2},
                            "volume": {"min": -20, "max": 20},
                        },
                    }
                ]
            },
        )
    return httpx.Response(200, json={"voice": {"modelIds": ["model"]}})


def install_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    """显式替换全局拒绝网络接缝，仅本场景使用已登记MockTransport。"""

    def route(client: httpx.AsyncClient, url: httpx.URL) -> httpx.MockTransport:
        del client, url
        return transport

    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", route)


async def test_fish_file_contract_uses_explicit_credentials_and_one_paid_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return (
            httpx.Response(200, content=b"audio", headers={"Content-Type": "audio/mpeg"})
            if request.method == "POST"
            else metadata(request)
        )

    transport = httpx.MockTransport(respond)
    install_transport(monkeypatch, transport)
    source = FishSource(
        AudioSettings(api_key=SecretStr("explicit"), model="model", voice_id="voice"),
        transport=transport,
    )
    try:
        result = await source.synthesize(SpeechParameters(text="完整内容"), "call")
        assert result.data == b"audio"
        assert len(calls) == 3
        assert calls[-1].headers["Authorization"] == "Bearer explicit"
        assert calls[-1].headers["Idempotency-Key"] == "call"
        assert result.duration_seconds is None
    finally:
        await source.close()


@pytest.mark.parametrize("complete", [True, False])
async def test_aligned_sse_requires_real_completed_event(
    complete: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = 'event: audio\ndata: {"audioBase64": "' + base64.b64encode(b"part").decode() + '"}\n\n'
    if complete:
        raw += "event: completed\ndata: {}\n\n"

    def respond(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(200, content=raw.encode(), headers={"Content-Type": "text/event-stream"})
            if request.method == "POST"
            else metadata(request)
        )

    transport = httpx.MockTransport(respond)
    install_transport(monkeypatch, transport)
    source = FishSource(
        AudioSettings(api_key=SecretStr("explicit"), model="model", voice_id="voice"),
        transport=transport,
    )
    try:
        if complete:
            assert [
                part async for part in source.stream(SpeechParameters(text="语音"), "call")
            ] == [b"part"]
        else:
            with pytest.raises(AudioError, match="completed"):
                _ = [part async for part in source.stream(SpeechParameters(text="语音"), "call")]
    finally:
        await source.close()


async def test_production_aligned_stream_decoder_and_player_chain_uses_actual_clock(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("本地FFmpeg未安装；不下载设备依赖。")
    encoder = await asyncio.create_subprocess_exec(
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "wav",
        "-i",
        "pipe:0",
        "-f",
        "mp3",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    encoded, error = await encoder.communicate(encode_wav(bytes(12800), 32000))
    assert encoder.returncode == 0 and not error
    event = (
        'event: audio\ndata: {"audioBase64":"'
        + base64.b64encode(encoded).decode()
        + '"}\n\nevent: completed\ndata: {}\n\n'
    )

    def respond(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(
                200, content=event.encode(), headers={"Content-Type": "text/event-stream"}
            )
            if request.method == "POST"
            else metadata(request)
        )

    transport = httpx.MockTransport(respond)
    install_transport(monkeypatch, transport)
    settings = AudioSettings(
        api_key=SecretStr("explicit"),
        model="model",
        voice_id="voice",
        processing_executable=executable,
        playback_executable=sys.executable,
    )
    source = FishSource(settings, transport=transport)
    script = (
        "import sys\nraw=sys.stdin.buffer.read()\nassert len(raw)>0 and len(raw)%2==0\n"
        "sys.stderr.write('0.20 M-A: 0.000 fd=0\\r')\nsys.stderr.flush()\n"
    )
    processes: list[asyncio.subprocess.Process] = []

    async def launch(*arguments: str) -> asyncio.subprocess.Process:
        del arguments
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(player_module, "launch", launch)
    owner = AudioRuntime(
        "audio", settings, source, FFplayPlayer(sys.executable), store.audio, tmp_path, None
    )
    owner.bind_body(None)
    await owner.start()
    try:
        outcome = await owner.speak(SpeechParameters(text="完整SSE到PCM"), context("stream"))
        value = payload(outcome)
        assert value["synthesized"] is True and value["started"] is True
        assert value["completed"] is True and value["elapsed_seconds"] == 0.2
        identity = value["playback_id"]
        assert isinstance(identity, str)
        assert (await store.audio.facts("audio", identity))[-1].fact == "finished"
        assert processes[0].returncode == 0
    finally:
        await owner.close()
