"""外部插件、真实loopback传输和临时库音频验收；不连B站、模型或设备。"""

from __future__ import annotations

import asyncio
import json
import time
import zlib
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest
from test_audio import Player, Source, context, runtime
from websockets.asyncio.server import ServerConnection, serve

from plugins.live_stage.bilibili import (
    HEADER,
    USER_AGENT,
    BilibiliSource,
    Danmaku,
    PlatformDenied,
    ProtocolError,
    frames,
    packet,
    parse_message,
)
from plugins.live_stage.runtime import LiveStage, Settings, display_caption
from purrcept_engine import EngineConfig, EngineHost
from purrcept_engine.app.assembly import prepare_plugins
from purrcept_engine.app.config import PluginSettings
from purrcept_engine.errors import ConfigError
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system import (
    Admission,
    InputProcessing,
    InputRecord,
    InputSubmission,
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
)
from purrcept_engine.plugin_system.captions import SpeechCaption
from purrcept_engine.plugins.audio.models import AudioSettings, SpeechParameters
from purrcept_engine.plugins.audio.plugin import make_factory
from purrcept_engine.values import JsonObject, decode_object


class Inputs:
    def __init__(self) -> None:
        self.submissions: list[InputSubmission] = []

    async def submit(self, submission: InputSubmission) -> Admission:
        self.submissions.append(submission)
        return Admission(str(len(self.submissions)), False)

    async def query_input(self, input_id: str) -> InputRecord | None:
        del input_id
        return None

    async def query_processing(self, input_id: str) -> InputProcessing | None:
        del input_id
        return None


def native(text: str = "真实传输替身") -> bytes:
    return json.dumps(
        {
            "cmd": "DANMU_MSG",
            "msg_id": "native-1",
            "info": [[0, 1, 25, 0, 1700000000], text, [12, "测试观众"]],
        }
    ).encode()


async def http_snapshot(
    port: int, method: str = "GET", origin: str | None = None
) -> tuple[int, JsonObject]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = f"{method} /snapshot HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
    if origin is not None:
        request += f"Origin: {origin}\r\n"
    writer.write((request + "\r\n").encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    header, body = raw.split(b"\r\n\r\n", 1)
    return int(header.split(b" ")[1]), decode_object(body)


def test_packet_and_compressed_batch() -> None:
    plain = packet(5, native()) + packet(3, b"\0\0\0\1")
    compressed = zlib.compress(plain)
    value = HEADER.pack(16 + len(compressed), 16, 2, 5, 1) + compressed
    decoded = frames(value)
    assert [op for op, _ in decoded] == [5, 3]
    message = parse_message(decoded[0][1])
    assert message is not None and message.text == "真实传输替身"
    assert message.native_id == "native-1"
    assert parse_message(b'{"cmd":"SUPER_CHAT_MESSAGE","data":{}}') is None


@pytest.mark.parametrize("invalid", [b"x", packet(5, b"x")[:-1], HEADER.pack(16, 8, 1, 5, 1)])
def test_invalid_binary_frames(invalid: bytes) -> None:
    with pytest.raises(ProtocolError):
        frames(invalid)


def test_decompression_bomb_rejected() -> None:
    compressed = zlib.compress(b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(ProtocolError):
        frames(HEADER.pack(16 + len(compressed), 16, 2, 5, 1) + compressed)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("[happy]你好。", "你好。"),
        ("<think>内部内容</think>你好。", "你好。"),
        ("<analysis>隐私</analysis><tool_call>指令</tool_call>晚安", "晚安"),
        ("你好<think>未闭合思考", "你好"),
    ],
)
def test_caption_filters_internal_markup(text: str, expected: str) -> None:
    assert display_caption(text) == expected


def test_explicit_local_plugin_declaration_does_not_listen() -> None:
    root = Path(__file__).resolve().parents[1]
    setting = PluginSettings.model_validate(
        {"instance_id": "stage", "entry": "local/live_stage:factory", "config": {"port": 0}}
    )
    prepared = prepare_plugins((setting,), base_dir=root, factories={})
    assert prepared.blueprints[-1][0] == "stage"
    assert prepared.blueprints[-1][1].capabilities == ()


@pytest.mark.parametrize(
    "entry", ["local/../bad:factory", "local/_bad:factory", "local/missing:factory"]
)
def test_local_loader_rejects_escape_or_missing(entry: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        prepare_plugins(
            (PluginSettings(instance_id="test", entry=entry),), base_dir=tmp_path, factories={}
        )


async def test_live_http_whitelist_caption_dedup_and_close() -> None:
    inputs = Inputs()
    current = [SpeechCaption("play-1", "<think>秘密</think>[happy]你好", 0.1)]
    owner = LiveStage(
        "stage",
        Settings(port=0, max_messages=2),
        PluginPorts(inputs, lambda: {"phase": "running"}, captions=lambda: tuple(current)),
    )
    await owner.start()
    try:
        await owner.receive(Danmaku("A", "1", "<script>不会执行</script>", time.time(), "id1"))
        await owner.receive(Danmaku("A", "1", "重复原生ID", time.time(), "id1"))
        assert len(inputs.submissions) == 1
        assert not inputs.submissions[0].context.minimum_wake
        status, result = await http_snapshot(owner.bound_port)
        assert status == 200
        caption = result["caption"]
        assert isinstance(caption, dict) and caption["text"] == "你好"
        messages = result["messages"]
        assert isinstance(messages, list) and len(messages) == 1
        assert "秘密" not in json.dumps(result, ensure_ascii=False)
        assert (await http_snapshot(owner.bound_port, "POST"))[0] == 404
        assert (await http_snapshot(owner.bound_port, origin="http://example.invalid"))[0] == 403
        current.clear()
        assert (await http_snapshot(owner.bound_port))[1]["caption"] is None
        for _ in range(3):
            await owner.receive(Danmaku("A", "1", "无ID同文是不同发言", time.time()))
        assert len(inputs.submissions) == 4 and len(owner.messages) == 2
    finally:
        await owner.close()
    assert owner.released and owner.snapshot()["caption"] is None
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", owner.bound_port)


async def test_bilibili_reconnect_and_heartbeat_over_real_loopback() -> None:
    connections = 0
    heartbeats = 0
    statuses: list[str] = []

    async def handler(ws: ServerConnection) -> None:
        nonlocal connections, heartbeats
        assert ws.request is not None
        assert ws.request.headers["User-Agent"] == USER_AGENT
        connections += 1
        auth = await ws.recv()
        assert isinstance(auth, bytes) and frames(auth)[0][0] == 7
        await ws.send(packet(8, b'{"code":0}'))
        heartbeat = await ws.recv()
        assert isinstance(heartbeat, bytes) and frames(heartbeat)[0][0] == 2
        heartbeats += 1
        await ws.send(packet(5, native(str(connections))))
        await ws.close()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = int(next(iter(server.sockets)).getsockname()[1])

        class LocalSource(BilibiliSource):
            async def _connection(self, client: httpx.AsyncClient) -> tuple[str, JsonObject]:
                del client
                return f"ws://127.0.0.1:{port}", {"uid": 0, "roomid": 1, "protover": 2}

        source = LocalSource(1, statuses.append)
        stream = source.messages()
        try:
            async with asyncio.timeout(5):
                assert (await anext(stream)).text == "1"
                assert (await anext(stream)).text == "2"
        finally:
            await stream.aclose()
    assert connections == heartbeats == 2 and "reconnecting" in statuses


async def test_platform_denial_stops_without_retry() -> None:
    calls = 0

    class Denied:
        async def messages(self) -> AsyncGenerator[Danmaku, None]:
            nonlocal calls
            calls += 1
            raise PlatformDenied("不会记录该异常正文")
            yield Danmaku("", "", "", 0)

    owner = LiveStage(
        "stage",
        Settings(port=0, room_id=1),
        PluginPorts(Inputs(), lambda: {"phase": "running"}),
        source_factory=lambda room, status: Denied(),
    )
    await owner.start()
    try:
        assert owner.reader is not None
        await owner.reader
        assert owner.snapshot()["bilibili"] == "authorization_required"
        assert calls == 1
    finally:
        await owner.close()


async def test_audio_caption_only_after_player_clock_and_clears_on_interrupt(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "captions.sqlite3")
    source, player = Source(block=True), Player(block=True)
    owner = await runtime(store, tmp_path, source, player)
    try:
        task = asyncio.create_task(
            owner.speak(SpeechParameters(text="实际台词"), context("caption"))
        )
        await source.entered.wait()
        assert owner.caption() is None
        source.release.set()
        await player.entered.wait()
        caption = owner.caption()
        assert caption is not None and caption.text == "实际台词"
        await owner.interrupt(context("stop"))
        await task
        assert owner.caption() is None
    finally:
        await owner.close()
        await store.close()


@pytest.mark.parametrize("stage_first", [True, False])
async def test_real_host_worker_and_shared_caption_port(tmp_path: Path, stage_first: bool) -> None:
    source, player = Source(), Player(block=True)
    stages: list[LiveStage] = []

    def prepare(instance: str, raw: object, base: Path) -> PluginBlueprint:
        del base
        settings = Settings.model_validate(raw)

        async def build(ports: PluginPorts) -> PluginRuntime:
            stage = LiveStage(instance, settings, ports)
            stages.append(stage)
            return PluginRuntime(
                (),
                start=stage.start,
                stop_inputs=stage.stop_inputs,
                close=stage.close,
                resources_released=lambda: stage.released,
            )

        return PluginBlueprint((), (), build)

    plugins: list[dict[str, object]] = [
        {"instance_id": "stage", "entry": "stage-test", "config": {"port": 0}},
        {"instance_id": "audio", "entry": "audio-test", "config": {"playback_backend": "ffplay"}},
    ]
    if not stage_first:
        plugins.reverse()
    settings = EngineConfig.validate(
        {
            "host": {"data_dir": tmp_path / "data"},
            "console": False,
            "models": {
                "offline": {
                    "kind": "generation",
                    "model": "openai/offline",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "context_window": 100000,
                    "max_output_tokens": 1000,
                    "capacity_rule": "reference-text-only-v1",
                }
            },
            "subject": {"foreground": "offline", "reflection": "offline", "summary": "offline"},
            "plugins": plugins,
        },
        base_dir=tmp_path,
    )

    def synthesis(settings: AudioSettings) -> Source:
        del settings
        return source

    def playback(settings: AudioSettings) -> Player:
        del settings
        return player

    host = EngineHost(
        settings,
        plugin_factories={
            "stage-test": PluginFactory(prepare),
            "audio-test": make_factory(synthesis, playback, decode=False),
        },
    )
    await host.start()
    try:
        task = asyncio.create_task(
            host.execute(
                'from world import me\nreturn await me.microphone.speak(text="宿主播放台词")'
            )
        )
        await asyncio.wait_for(player.entered.wait(), 5)
        caption = stages[0].snapshot()["caption"]
        assert isinstance(caption, dict) and caption["text"] == "宿主播放台词"
        player.release.set()
        assert (await task).state == "completed"
        assert stages[0].snapshot()["caption"] is None
    finally:
        await host.close()
    assert stages[0].released
