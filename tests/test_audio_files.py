"""用合成音频、HTTP 替身和真实 Store/Worker 验证音频文件原生交付。

不访问模型、用户文件或声音设备。核验实际容器而非扩展名和响应 MIME；
取消测试保留读取与关闭所有权，跨层测试确认 Worker 不承载音频 base64。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import httpx
import pytest
from purrcept_core.models import AudioBlock, AudioBytes, Message, ModelResponse, ToolResultBlock
from test_host_subject import Backend, proposal, wait_processed
from test_images import Inputs, close, context, offline_transport, ports

from purrcept_engine.app.assembly import prepare_plugins
from purrcept_engine.app.config import EngineConfig, ExecutionSettings, PluginSettings
from purrcept_engine.app.evidence import EvidenceAccess
from purrcept_engine.app.host import EngineHost
from purrcept_engine.domain.context.materials import media_blocks
from purrcept_engine.domain.context.token_estimation import estimate_tokens
from purrcept_engine.domain.evidence.models import EvidenceSnapshot
from purrcept_engine.domain.execution.catalog import Catalog, ObjectDeclaration
from purrcept_engine.domain.execution.providers import CapabilityInputError
from purrcept_engine.domain.inputs.models import InputSource, InputSubmission, MediaAsset
from purrcept_engine.errors import ConfigError, StorageError
from purrcept_engine.infrastructure.audio.wav import encode_wav
from purrcept_engine.infrastructure.execution.service import ExecutionService
from purrcept_engine.infrastructure.models.compiler import compile_materials
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system.contracts import PluginPorts
from purrcept_engine.plugins import audio_files
from purrcept_engine.values import JsonObject, decode_object

if TYPE_CHECKING:
    from av.audio.stream import AudioStream


class _AudioOutput(Protocol):
    """本测试只选择音频编码器，收窄 PyAV 同时包含字幕和视频的重载返回值。"""

    def add_stream(self, codec_name: str, rate: int) -> AudioStream:
        """为显式音频编码取得对应音轨，不将未知流强行作为音频解码。"""
        ...


def audio(format: str = "wav") -> bytes:
    """生成短单声道音频，编码过程只读取内存，不需要外部 FFmpeg 程序。"""
    data = encode_wav(bytes(8000), 16000)
    if format == "wav":
        return data
    import av

    output = io.BytesIO()
    with (
        av.open(io.BytesIO(data), mode="r") as source,
        av.open(output, mode="w", format=format) as target,
    ):
        stream = cast(_AudioOutput, target).add_stream(
            "libmp3lame" if format == "mp3" else "flac", 16000
        )
        stream.layout = "mono"
        for frame in source.decode(audio=0):
            for packet in stream.encode(frame):
                target.mux(packet)
        for packet in stream.encode(None):
            target.mux(packet)
    return output.getvalue()


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    """每个场景独占临时库，插件实际收尾后才归还存储线程。"""
    value = await Store.open(tmp_path / "engine.sqlite3")
    try:
        yield value
    finally:
        await value.close()


@pytest.mark.parametrize("format,media_type", [("wav", "audio/wav"), ("mp3", "audio/mpeg")])
async def test_local_audio_keeps_actual_bytes_and_can_expand_native_blocks(
    store: Store, tmp_path: Path, format: str, media_type: str
) -> None:
    """不相信扩展名；解码、证据、BLOB 和模型原生块对应同一份原字节。"""
    data = audio(format)
    (tmp_path / "recording.not-an-extension").write_bytes(data)
    runtime = await audio_files.audio_files_blueprint(tmp_path).build(ports(store))
    try:
        result = await runtime.capabilities[0].invoke(
            {"source": "recording.not-an-extension", "media_id": "origin"}, context()
        )
        assert result.state == "confirmed" and isinstance(result.value, dict)
        assert result.value["media_type"] == media_type
        assert result.value["duration_seconds"] == pytest.approx(0.25, abs=0.05)
        assert result.value["sample_rate"] == 16000 and result.value["channels"] == 1
        assert await store.get_media(hashlib.sha256(data).hexdigest()) == data
        assert await media_blocks(store, result.media) == (
            AudioBlock(AudioBytes(data, media_type)),
        )
        identity = result.value["evidence_ref"]
        assert isinstance(identity, str)
        saved = await store.evidence.get_evidence(identity)
        assert saved is not None and '"origin_media_id":"origin"' in saved.source_json
        assert await store.pending() == ()
    finally:
        await close(runtime)


@pytest.mark.parametrize(
    "scenario", ["empty", "invalid", "unsupported", "large", "duration", "missing"]
)
async def test_audio_rejections_do_not_publish_a_successful_media_reference(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    data = (
        b""
        if scenario == "empty"
        else b"not audio"
        if scenario == "invalid"
        else audio("flac")
        if scenario == "unsupported"
        else audio()
    )
    if scenario == "large":
        monkeypatch.setattr(audio_files, "MAX_AUDIO_BYTES", len(data) - 1)
    if scenario == "duration":
        monkeypatch.setattr(audio_files, "MAX_AUDIO_SECONDS", 0.1)
    if scenario != "missing":
        (tmp_path / "recording.wav").write_bytes(data)
    runtime = await audio_files.audio_files_blueprint(tmp_path).build(ports(store))
    try:
        with pytest.raises(CapabilityInputError):
            await runtime.capabilities[0].invoke({"source": "recording.wav"}, context())
        assert await store.get_media(hashlib.sha256(data).hexdigest()) is None
    finally:
        await close(runtime)


@pytest.mark.parametrize("bad_redirect", [False, True])
async def test_remote_audio_uses_bounded_http_and_checks_actual_format(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_redirect: bool
) -> None:
    data = audio()
    paths: list[str] = []

    def response(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/start":
            return httpx.Response(
                302, headers={"location": "file:///private.wav" if bad_redirect else "/audio"}
            )
        return httpx.Response(200, content=data, headers={"content-type": "text/plain"})

    offline_transport(monkeypatch, httpx.MockTransport(response))
    runtime = await audio_files.audio_files_blueprint(tmp_path).build(ports(store))
    try:
        if bad_redirect:
            with pytest.raises(CapabilityInputError):
                await runtime.capabilities[0].invoke(
                    {"source": "https://audio.test/start"}, context()
                )
            assert paths == ["/start"]
        else:
            result = await runtime.capabilities[0].invoke(
                {"source": "https://audio.test/start"}, context()
            )
            assert result.media[0].media_type == "audio/wav" and paths == ["/start", "/audio"]
    finally:
        await close(runtime)


async def test_cancellation_and_duplicate_close_keep_actual_read_and_capture_owned(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()
    data = audio()

    def delayed(source: str, base_dir: Path) -> bytes:
        entered.set()
        assert release.wait(5)
        return data

    monkeypatch.setattr(audio_files, "_local_bytes", delayed)
    client = httpx.AsyncClient(trust_env=False)
    runtime = await audio_files.audio_files_blueprint(
        tmp_path, client_factory=lambda: client
    ).build(ports(store))
    reading = asyncio.create_task(runtime.capabilities[0].invoke({"source": "slow.wav"}, context()))
    closing: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
        closing = asyncio.create_task(close(runtime))
        await asyncio.sleep(0)
        assert not closing.done() and not client.is_closed
        release.set()
        await closing
        await close(runtime)
        assert client.is_closed and await store.get_media(hashlib.sha256(data).hexdigest()) == data
    finally:
        release.set()
        if closing is not None:
            await closing
        else:
            await close(runtime)


async def test_storage_failure_is_not_translated_to_a_file_input_error(
    store: Store, tmp_path: Path
) -> None:
    class BrokenEvidence(EvidenceAccess):
        async def capture(
            self, snapshot: EvidenceSnapshot, assets: tuple[MediaAsset, ...]
        ) -> EvidenceSnapshot:
            """模拟正式接管失败，确保插件不将权威存储故障误报为输入问题。"""
            raise StorageError("synthetic storage failure", code="invalid_record")

    (tmp_path / "recording.wav").write_bytes(audio())
    runtime = await audio_files.audio_files_blueprint(tmp_path).build(
        PluginPorts(Inputs(), lambda: {}, BrokenEvidence(store.evidence, 8000))
    )
    try:
        with pytest.raises(StorageError, match="synthetic storage"):
            await runtime.capabilities[0].invoke({"source": "recording.wav"}, context())
    finally:
        await close(runtime)


async def test_real_worker_returns_metadata_and_native_audio_comes_from_same_store(
    store: Store, tmp_path: Path
) -> None:
    data = audio()
    (tmp_path / "recording.wav").write_bytes(data)
    blueprint = audio_files.audio_files_blueprint(tmp_path)
    runtime = await blueprint.build(ports(store))
    catalog = Catalog((ObjectDeclaration("subject", "me", "主体"),), blueprint.capabilities)
    execution = ExecutionService(
        catalog, runtime.capabilities, store, ExecutionSettings(), scratch_root=tmp_path / "scratch"
    )
    try:
        result = await execution.execute(
            'from world import me\nreturn await me.listen_audio(source="recording.wav")'
        )
        assert result.state == "completed" and result.calls[0].state == "confirmed"
        assert result.result_json is not None and "base64" not in result.result_json
        assert await media_blocks(store, result.calls[0].media) == (
            AudioBlock(AudioBytes(data, "audio/wav")),
        )
    finally:
        await execution.close()
        await close(runtime)


def test_audio_declaration_is_pure_and_reserved_instance_is_checked(tmp_path: Path) -> None:
    def forbidden() -> httpx.AsyncClient:
        raise AssertionError("declaration must not create a client")

    blueprint = audio_files.audio_files_blueprint(tmp_path, client_factory=forbidden)
    assert blueprint.capabilities[0].timeout_seconds == 30
    prepared = prepare_plugins((), base_dir=tmp_path, factories={})
    assert "me.listen_audio" in prepared.catalog.bindings
    with pytest.raises(ConfigError, match="audio_file"):
        prepare_plugins(
            (PluginSettings(instance_id="audio_file", entry="unavailable"),),
            base_dir=tmp_path,
            factories={},
        )
    with pytest.raises(ConfigError, match="绝对"):
        audio_files.audio_files_blueprint(Path("relative"))


@pytest.mark.parametrize("seconds", [0.25, 217])
async def test_foreground_receives_native_audio_after_explicit_file_read(
    tmp_path: Path, seconds: float
) -> None:
    """实际 Host/Core 工具循环把已接管音频交给后续模型请求，不以路径代替。"""
    data = encode_wav(bytes(int(16000 * 2 * seconds)), 16000)
    (tmp_path / "recording.wav").write_bytes(data)
    backend = Backend(
        (
            proposal(
                "listen",
                "play_action",
                {
                    "action_code": (
                        'from world import me\nreturn await me.listen_audio(source="recording.wav")'
                    ),
                },
            ),
            ModelResponse(Message.assistant("本批内部处理完成。")),
        )
    )
    config = EngineConfig.validate(
        {
            "host": {"data_dir": tmp_path / "state"},
            "console": False,
            "models": {
                "main": {
                    "kind": "generation",
                    "model": "openai/offline",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "context_window": 100_000,
                    "max_output_tokens": 1000,
                    "supports_contents": ["text", "audio"],
                }
            },
            "subject": {
                "foreground": "main",
                "reflection": "main",
                "behavior_review_enabled": False,
            },
        },
        base_dir=tmp_path,
    )
    host = EngineHost(config, generation_backends={"main": backend})
    await host.start()
    try:
        now = datetime.now(UTC)
        admitted = await host.submit(
            InputSubmission(
                InputSource("test", "a", "c", "text", "one"), "读取本地音频文件。", now, now
            )
        )
        await wait_processed(host, admitted.input_id)
        assert len(backend.requests) == 2
        blocks = [
            item
            for message in backend.requests[1].messages
            for block in message.content
            if isinstance(block, ToolResultBlock)
            for item in block.content
            if isinstance(item, AudioBlock)
        ]
        assert len(blocks) == 1 and blocks[0].source == AudioBytes(data, "audio/wav")
        # Core 收到原生块仍不足以证明协议正确。实际 SDK 编译必须把音频放在
        # user 内容中；tool 只放文字，容量估算才不会与网关的材料解释相冲突。
        compiled, _ = compile_materials(backend.requests[1], model="openai/offline")
        payload = decode_object(compiled.encode())["payload"]
        assert isinstance(payload, dict)
        messages = payload["messages"]
        assert isinstance(messages, list)
        audio_messages: list[JsonObject] = []
        for message in messages:
            assert isinstance(message, dict)
            if message["role"] == "tool":
                assert isinstance(message["content"], str)
            content = message.get("content")
            if not isinstance(content, list):
                continue
            if any(
                isinstance(part, dict) and part.get("type") == "input_audio" for part in content
            ):
                assert message["role"] == "user"
                audio_messages.append(message)
        assert len(audio_messages) == 1
        assert estimate_tokens({"messages": audio_messages}) < seconds * 32 + 1000
    finally:
        await host.close()
