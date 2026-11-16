"""以真实 SQLite 和进程内 ASGI 验证配对、刺激去重、补送及命令未知语义。"""

import asyncio
import io
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from PIL import Image
from purrcept_core.models import TextBlock
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from purrcept_engine.app.assembly import prepare_plugins
from purrcept_engine.app.config import PluginSettings
from purrcept_engine.app.evidence import EvidenceAccess
from purrcept_engine.domain.inputs.models import Admission, InputRecord, InputSubmission
from purrcept_engine.domain.subject.models import InputProcessing
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system import (
    CallContext,
    CallOutcome,
    CallRecord,
    InputContext,
    InputSource,
)
from purrcept_engine.plugins.phone.plugin import PhoneSettings
from purrcept_engine.plugins.phone.protocol import ChatSubmission
from purrcept_engine.plugins.phone.runtime import PhoneRuntime
from purrcept_engine.plugins.phone.transport import PhoneServer
from purrcept_engine.values import JsonObject


@pytest.mark.parametrize("waveform", [[True], ["20"], [-1], [101], [20] * 65])
def test_json_waveform_keeps_strict_sample_and_size_limits(waveform: list[object]) -> None:
    """接受 JSON 容器不等于放宽波形元素类型、幅度或长度。"""
    with pytest.raises(ValidationError):
        ChatSubmission.model_validate(
            {
                "message_id": "invalid-waveform",
                "text": "[语音消息]",
                "occurred_at": "2026-11-16T00:00:00Z",
                "kind": "voice",
                "waveform": waveform,
            }
        )


class DevicePeer:
    """记录真实传输边界的派发，测试只显式提供设备回执。"""

    device_id = "device"
    connection_id = "connection"

    def __init__(self) -> None:
        self.sent: list[JsonObject] = []
        self.dispatched = asyncio.Event()

    async def send(self, kind: str, payload: JsonObject, reply_to: str | None = None) -> None:
        self.sent.append(payload)
        self.dispatched.set()


class Inputs:
    """替代 app 唤醒交接，保留真实输入接管事务。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def submit(self, submission: InputSubmission) -> Admission:
        return await self.store.admit(submission)

    async def query_input(self, input_id: str) -> InputRecord | None:
        return await self.store.get_input(input_id)

    async def query_processing(self, input_id: str) -> InputProcessing | None:
        return await self.store.input_processing(input_id)


@pytest.mark.asyncio
async def test_phone_journal_survives_reopen_and_rejects_conflicting_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine.sqlite3"
    store = await Store.open(path)
    try:
        ledger = store.phone("phone")
        await ledger.pair("device", "Android", "hash")
        first = await ledger.append("device", "one", {"text": "hello"})
        sequence = first["seq"]
        assert isinstance(sequence, int)
        assert await ledger.append("device", "one", {"text": "hello"}) == first
        with pytest.raises(StorageError, match="冲突"):
            await ledger.append("device", "one", {"text": "changed"})
        assert await ledger.events("other", 0) == []
        with pytest.raises(StorageError, match="游标"):
            await ledger.acknowledge("device", sequence + 1)
        await ledger.acknowledge("device", sequence)
    finally:
        await store.close()
    reopened = await Store.open(path)
    try:
        assert await reopened.phone("phone").events("device", 0) == [first]
        assert await reopened.phone("phone").authenticate("hash") == "device"
        assert await reopened.phone("other").authenticate("hash") is None
        await reopened.phone("phone").revoke()
        assert await reopened.phone("phone").authenticate("hash") is None
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_chat_replay_admits_one_input_and_outgoing_is_explicitly_queued(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "engine.sqlite3")
    try:
        ledger = store.phone("phone")
        await ledger.pair("device", "Android", "hash")
        runtime = PhoneRuntime("phone", Inputs(store), ledger)
        message = ChatSubmission(
            message_id="m1", text="你好", occurred_at=datetime.now(UTC).isoformat()
        )
        first = await runtime.submit_chat("device", message)
        second = await runtime.submit_chat("device", message)
        assert second["duplicate"] is True
        assert first["input_id"] == second["input_id"]
        outcome = await runtime.send_chat(
            "你好呀", CallContext("call", "action", asyncio.get_running_loop().time() + 5)
        )
        assert outcome.state == "confirmed"
        assert isinstance(outcome.value, dict)
        assert outcome.value["delivery_state"] == "queued"
        events = await ledger.events("device", 0)
        assert len(events) == 2
        assert events[0]["direction"] == "incoming"
        assert events[1]["direction"] == "outgoing"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_command_disconnect_is_unknown_and_reconnect_does_not_restore_control(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "engine.sqlite3")

    class Peer:
        device_id = "device"
        connection_id = "connection"

        def __init__(self) -> None:
            self.sent: list[JsonObject] = []
            self.dispatched = asyncio.Event()

        async def send(self, kind: str, payload: JsonObject, reply_to: str | None = None) -> None:
            self.sent.append(payload)
            self.dispatched.set()

    try:
        runtime = PhoneRuntime("phone", Inputs(store), store.phone("phone"))
        peer = Peer()
        runtime.attach(peer)
        context = CallContext("call", "action", asyncio.get_running_loop().time() + 5)
        pending = asyncio.create_task(runtime.command("connect", {}, context))
        await asyncio.wait_for(peer.dispatched.wait(), 2)
        assert len(peer.sent) == 1
        runtime.detach(peer)
        assert (await pending).state == "unknown"
        runtime.attach(Peer())
        assert runtime.control_session is None
        assert (await runtime.command("screen", {}, context)).state == "failed"
    finally:
        await store.close()


def test_pairing_and_authenticated_websocket_restore_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the explicitly supplied TestClient transport bypasses the external-network guard.
    transports: dict[httpx.Client, httpx.BaseTransport] = {}

    def local_transport(self: httpx.Client, **options: object) -> httpx.BaseTransport:
        transport = options["transport"]
        assert isinstance(transport, httpx.BaseTransport)
        assert transport.__class__.__name__ == "_TestClientTransport"
        transports[self] = transport
        return transport

    def selected(self: httpx.Client, url: httpx.URL) -> httpx.BaseTransport:
        return transports[self]

    monkeypatch.setattr(httpx.Client, "_init_transport", local_transport)
    monkeypatch.setattr(httpx.Client, "_transport_for_url", selected)
    settings = PhoneSettings(
        public_url="https://localhost:8766",
        certificate=tmp_path / "cert.pem",
        private_key=tmp_path / "key.pem",
        pairing_enabled=True,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        store = await Store.open(tmp_path / "engine.sqlite3")
        runtime = PhoneRuntime("phone", Inputs(store), store.phone("phone"))
        server = PhoneServer(
            runtime, settings, invitation="test-invitation-without-real-credentials"
        )
        app.router.routes = server.app.router.routes
        try:
            yield
        finally:
            await store.close()

    with TestClient(Starlette(lifespan=lifespan)) as client:
        assert client.get("/phone/v1/state").status_code == 401
        reply = client.post(
            "/phone/v1/pair",
            json={"invitation": "test-invitation-without-real-credentials", "name": "Android"},
        )
        assert reply.status_code == 200
        token = reply.json()["token"]
        assert (
            client.post(
                "/phone/v1/pair",
                json={"invitation": "test-invitation-without-real-credentials", "name": "Android"},
            ).status_code
            == 403
        )
        with client.websocket_connect(
            "/phone/v1/channel", headers={"Authorization": "Bearer " + token}
        ) as socket:
            socket.send_json({"v": 1, "type": "hello", "id": "hello", "payload": {"through": 0}})
            welcome = socket.receive_json()
            connection = welcome["connection_id"]
            payload = {
                "message_id": "m1",
                "text": "hello",
                "occurred_at": "2026-11-15T12:00:00+00:00",
            }
            socket.send_json(
                {
                    "v": 1,
                    "type": "chat.submit",
                    "id": "submit",
                    "connection_id": connection,
                    "payload": payload,
                }
            )
            received: list[dict[str, object]] = []
            # 实时状态与可靠聊天共用连接；不能把前两帧假定为固定聊天应答。
            for _ in range(6):
                frame = socket.receive_json()
                if frame["type"] == "engine.state":
                    continue
                received.append(frame)
                if len(received) == 2:
                    break
            assert {item["type"] for item in received} == {"chat.accepted", "sync.event"}
        with client.websocket_connect(
            "/phone/v1/channel", headers={"Authorization": "Bearer " + token}
        ) as socket:
            socket.send_json({"v": 1, "type": "hello", "id": "hello2", "payload": {"through": 0}})
            assert socket.receive_json()["type"] == "welcome"
            assert socket.receive_json()["payload"]["message_id"] == "m1"


def test_phone_and_qq_share_one_parent(tmp_path: Path) -> None:
    settings = PluginSettings.model_validate(
        {
            "instance_id": "phone",
            "entry": "phone",
            "config": {
                "public_url": "https://localhost:8766",
                "certificate": "cert.pem",
                "private_key": "key.pem",
            },
        }
    )
    qq = PluginSettings(instance_id="qq", entry="qq")
    catalog = prepare_plugins((settings, qq), base_dir=tmp_path, factories={}).catalog
    assert "me.phone.chat.send" in catalog.bindings
    assert catalog.object_paths["phone"] == "me.phone"
    assert "me.phone.qq.send" in catalog.bindings


async def test_recovery_repairs_input_projection_without_readmitting(tmp_path: Path) -> None:
    """接管后索引前中断，重建与原始时区消息重试保持同一事实。"""
    store = await Store.open(tmp_path / "engine.sqlite3")
    try:
        message = ChatSubmission(
            message_id="repair", text="原文", occurred_at="2026-11-15T20:00:00+08:00"
        )
        submission = InputSubmission(
            InputSource("phone", "device", "chat", "phone.chat.message", "repair", "device"),
            message.text,
            datetime.fromisoformat(message.occurred_at),
            datetime.now(UTC),
            (),
            InputContext(minimum_wake=True),
        ).with_content("me.phone.chat", (TextBlock(message.text),))
        admitted = await store.admit(submission)
        ledger = store.phone("phone")
        assert await ledger.recover_inputs("other") == 0
        assert await ledger.recover_inputs("device") == 1
        assert await ledger.recover_inputs("device") == 0
        replay = await PhoneRuntime("phone", Inputs(store), ledger).submit_chat("device", message)
        assert replay["input_id"] == admitted.input_id
        assert replay["duplicate"] is True
        assert len(await ledger.events("device", 0)) == 1
    finally:
        await store.close()


async def test_late_result_is_recorded_under_original_identity_without_redispatch(
    tmp_path: Path,
) -> None:
    """断线后新连接只采用原命令回执，不恢复操控或再次派发。"""
    store = await Store.open(tmp_path / "engine.sqlite3")

    class Late:
        def __init__(self) -> None:
            self.contexts: list[CallContext] = []

        async def record(self, context: CallContext, outcome: CallOutcome) -> CallRecord:
            self.contexts.append(context)
            raise AssertionError("late-result hook reached")

    late = Late()
    try:
        runtime = PhoneRuntime("phone", Inputs(store), store.phone("phone"), late)
        peer = DevicePeer()
        runtime.attach(peer)
        original = CallContext("old-call", "old-action", asyncio.get_running_loop().time() + 5)
        pending = asyncio.create_task(runtime.command("connect", {}, original))
        await asyncio.wait_for(peer.dispatched.wait(), 2)
        runtime.detach(peer)
        assert (await pending).state == "unknown"
        next_peer = DevicePeer()
        runtime.attach(next_peer)
        with pytest.raises(AssertionError, match="late-result hook"):
            await runtime.receive_result(
                next_peer,
                {"command_id": "old-call", "state": "confirmed", "data": {"connected": True}},
            )
        assert late.contexts == [original]
        assert next_peer.sent == []
        assert runtime.control_session is None
    finally:
        await store.close()


async def test_screenshot_is_native_evidence_and_not_a_second_stimulus(tmp_path: Path) -> None:
    """截图来源必须属于当前手机，结果携带原生媒体而不伪造聊天输入。"""
    store = await Store.open(tmp_path / "engine.sqlite3")
    try:
        ledger = store.phone("phone")
        data = io.BytesIO()
        Image.new("RGB", (2, 2)).save(data, format="JPEG")
        media_id = await ledger.put_media("device", data.getvalue(), "image/jpeg")
        assert await ledger.media("other", media_id) is None
        runtime = PhoneRuntime(
            "phone", Inputs(store), ledger, evidence=EvidenceAccess(store.evidence, 8000)
        )
        peer = DevicePeer()
        runtime.attach(peer)
        runtime.control_session = "session"
        context = CallContext("screenshot", "action", asyncio.get_running_loop().time() + 5)
        pending = asyncio.create_task(runtime.command("screenshot", {}, context))
        await asyncio.wait_for(peer.dispatched.wait(), 2)
        await runtime.receive_result(
            peer,
            {
                "command_id": "screenshot",
                "state": "confirmed",
                "data": {"media_id": media_id, "captured_at": 1000.0},
            },
        )
        outcome = await pending
        assert outcome.state == "confirmed"
        assert outcome.media[0].media_id == media_id
        assert await ledger.events("device", 0) == []
    finally:
        await store.close()


async def test_rich_chat_voice_sticker_and_reply_roundtrip(tmp_path: Path) -> None:
    """手机端富媒体（16kHz WAV 语音、贴纸、引用回复）进入真实多模态输入，主脑亦可回复贴纸与语音。"""
    import base64
    import struct
    import wave

    from purrcept_engine.plugins.phone.transport import normalize_phone_voice

    wav_buf = io.BytesIO()
    with wave.open(wav_buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(struct.pack("<8000h", *([4200, -4200] * 4000)))
    raw_wav = wav_buf.getvalue()
    normalized, duration_ms = normalize_phone_voice(raw_wav)
    assert normalized[:4] == b"RIFF" and 480 <= duration_ms <= 520

    store = await Store.open(tmp_path / "rich_phone.sqlite3")
    try:
        ledger = store.phone("phone")
        await ledger.pair("device", "Purrcept Go", "token_hash_rich")
        voice_media_id = await ledger.put_media("device", normalized, "audio/wav")

        png_buf = io.BytesIO()
        Image.new("RGBA", (32, 32), (200, 230, 255, 255)).save(png_buf, format="PNG")
        img_media_id = await ledger.put_media("device", png_buf.getvalue(), "image/png")

        runtime = PhoneRuntime("phone", Inputs(store), ledger, subject_name="小白")

        res_voice = await runtime.submit_chat(
            "device",
            ChatSubmission(
                message_id="m_voice_1",
                text="[语音消息 1″]",
                occurred_at="2026-11-16T12:00:00+08:00",
                kind="voice",
                media_id=voice_media_id,
                media_type="audio/wav",
                duration_ms=duration_ms,
                waveform=(20, 55, 80, 40),
            ),
        )
        assert res_voice["duplicate"] is False

        res_sticker = await runtime.submit_chat(
            "device",
            ChatSubmission(
                message_id="m_stk_1",
                text="[贴纸：摸摸头]",
                occurred_at="2026-11-16T12:00:02+08:00",
                kind="sticker",
                sticker_id="pat_head",
                media_id=img_media_id,
                media_type="image/png",
                reply_to_id="m_voice_1",
            ),
        )
        assert res_sticker["duplicate"] is False

        out_stk = await runtime.send_chat(
            "",
            CallContext("out_stk_1", "action_1", 999999999.0),
            sticker="peek_curious",
            reply_to="m_stk_1",
        )
        assert out_stk.state == "confirmed"

        out_voice = await runtime.send_chat(
            "听到了哟～",
            CallContext("out_voice_1", "action_1", 999999999.0),
            voice_base64=base64.b64encode(raw_wav).decode("ascii"),
            reply_to="m_voice_1",
        )
        assert out_voice.state == "confirmed"

        events = await ledger.events("device", 0)
        assert len(events) == 4
        assert events[0]["kind"] == "voice" and events[0]["waveform"] == [20, 55, 80, 40]
        assert events[1]["sticker_id"] == "pat_head" and events[1]["reply_to_id"] == "m_voice_1"
        assert events[2]["kind"] == "sticker" and events[2]["sticker_id"] == "peek_curious"
        waveform = events[3]["waveform"]
        assert events[3]["kind"] == "voice"
        assert isinstance(waveform, list) and len(waveform) == 24
    finally:
        await store.close()
