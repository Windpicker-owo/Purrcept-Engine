"""真实回环 TLS/WSS 验证，仅使用临时证书和测试输入，不连接模型或设备。"""

import asyncio
import json
import socket
import ssl
import wave
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from test_phone import Inputs
from websockets.asyncio.client import connect

from purrcept_engine.app.phone_cli import initialize
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system import CallContext
from purrcept_engine.plugins.phone.plugin import PhoneSettings
from purrcept_engine.plugins.phone.runtime import PhoneRuntime
from purrcept_engine.plugins.phone.transport import PhoneServer, token_hash
from purrcept_engine.values import decode_object


async def test_voice_json_waveform_survives_real_upload_and_wss_submission(tmp_path: Path) -> None:
    """手机的 JSON 数组波形须通过实际网络入口，不能仅用 Python 元组构造消息自证。"""
    initialize(tmp_path, "127.0.0.1")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    store = await Store.open(tmp_path / "engine.sqlite3")
    ledger = store.phone("phone")
    await ledger.pair("device", "Android", token_hash("synthetic-voice-token"))
    runtime = PhoneRuntime("phone", Inputs(store), ledger)
    settings = PhoneSettings(
        public_url=f"https://127.0.0.1:{port}",
        port=port,
        certificate=tmp_path / "phone-cert.pem",
        private_key=tmp_path / "phone-key.pem",
        invitation_file=tmp_path / "invitation.json",
    )
    server = PhoneServer(runtime, settings)
    audio = BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x10" * 8000)
    try:
        await server.start()
        tls = ssl.create_default_context(cafile=str(settings.certificate))
        headers = {"Authorization": "Bearer synthetic-voice-token"}
        # 显式借用真实回环传输；全局离线夹具仍拒绝其他隐式 HTTP 请求。
        async with httpx.AsyncHTTPTransport(verify=tls) as transport:
            uploaded = await transport.handle_async_request(
                httpx.Request(
                    "POST",
                    f"https://127.0.0.1:{port}/phone/v1/media",
                    headers={**headers, "Content-Type": "audio/wav"},
                    content=audio.getvalue(),
                )
            )
            try:
                await uploaded.aread()
                assert uploaded.status_code == 200
                media = uploaded.json()
            finally:
                await uploaded.aclose()
        async with connect(
            f"wss://127.0.0.1:{port}/phone/v1/channel",
            ssl=tls,
            additional_headers=headers,
        ) as channel:
            await channel.send(
                json.dumps({"v": 1, "type": "hello", "id": "hello", "payload": {"through": 0}})
            )
            welcome = json.loads(await channel.recv())
            submission = {
                "message_id": "voice-json",
                "text": "[语音消息]",
                "occurred_at": "2026-11-16T12:00:00+08:00",
                "kind": "voice",
                "media_id": media["media_id"],
                "media_type": media["media_type"],
                "duration_ms": media["duration_ms"],
                "waveform": [20, 55, 80, 40],
            }
            await channel.send(
                json.dumps(
                    {
                        "v": 1,
                        "type": "chat.submit",
                        "id": "voice-frame",
                        "connection_id": welcome["connection_id"],
                        "payload": submission,
                    }
                )
            )
            seen = set[str]()
            async with asyncio.timeout(5):
                while seen != {"chat.accepted", "sync.event"}:
                    frame = json.loads(await channel.recv())
                    if frame["type"] in ("chat.accepted", "sync.event"):
                        seen.add(frame["type"])
                        assert frame["payload"]["message_id"] == "voice-json"
            events = await ledger.events("device", 0)
            assert len(events) == 1
            assert events[0]["waveform"] == [20, 55, 80, 40]
            assert events[0]["media_id"] == media["media_id"]
            assert events[0]["duration_ms"] == 500
    finally:
        await runtime.stop_inputs()
        await server.close()
        await store.close()


async def test_real_tls_listener_carries_only_explicit_outgoing_messages(tmp_path: Path) -> None:
    """证书身份、真实监听、消息落盘和关闭须沿同一循环完成。"""
    initialize(tmp_path, "127.0.0.1")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    store = await Store.open(tmp_path / "engine.sqlite3")
    ledger = store.phone("phone")
    await ledger.pair("device", "Android", token_hash("synthetic-test-token"))
    runtime = PhoneRuntime("phone", Inputs(store), ledger)
    settings = PhoneSettings(
        public_url=f"https://127.0.0.1:{port}",
        port=port,
        certificate=tmp_path / "phone-cert.pem",
        private_key=tmp_path / "phone-key.pem",
        invitation_file=tmp_path / "invitation.json",
    )
    server = PhoneServer(runtime, settings)
    try:
        await server.start()
        tls = ssl.create_default_context(cafile=str(settings.certificate))
        async with connect(
            f"wss://127.0.0.1:{port}/phone/v1/channel",
            ssl=tls,
            additional_headers={"Authorization": "Bearer synthetic-test-token"},
        ) as channel:
            await channel.send(
                json.dumps({"v": 1, "type": "hello", "id": "h", "payload": {"through": 0}})
            )
            welcome = decode_object(str(await channel.recv()).encode())
            assert welcome["type"] == "welcome"
            outcome = await runtime.send_chat(
                "test-only message",
                CallContext("call", "action", asyncio.get_running_loop().time() + 5),
            )
            assert outcome.state == "confirmed"
            event = decode_object(str(await asyncio.wait_for(channel.recv(), 5)).encode())
            if event["type"] == "engine.state":
                event = decode_object(str(await asyncio.wait_for(channel.recv(), 5)).encode())
            payload = event["payload"]
            assert isinstance(payload, dict)
            assert payload["text"] == "test-only message"
            assert payload["direction"] == "outgoing"
    finally:
        await runtime.stop_inputs()
        await server.close()
        await store.close()


def test_certificate_generation_never_overwrites_existing_identity(tmp_path: Path) -> None:
    """重复初始化必须保留手机已经固定的证书。"""
    initialize(tmp_path, "127.0.0.1")
    before = (tmp_path / "phone-cert.pem").read_bytes()
    with pytest.raises(FileExistsError):
        initialize(tmp_path, "127.0.0.1")
    assert (tmp_path / "phone-cert.pem").read_bytes() == before


async def test_port_conflict_does_not_exit_process_or_delete_unowned_invitation(
    tmp_path: Path,
) -> None:
    """端口失败保留既有本地文件，关闭不清理本实例尚未取得的资源。"""
    store = await Store.open(tmp_path / "engine.sqlite3")
    invitation = tmp_path / "invitation.json"
    invitation.write_text("existing-file", encoding="utf-8")
    try:
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            port = occupied.getsockname()[1]
            server = PhoneServer(
                PhoneRuntime("phone", Inputs(store), store.phone("phone")),
                PhoneSettings(
                    public_url=f"https://127.0.0.1:{port}",
                    port=port,
                    certificate=tmp_path / "unused.pem",
                    private_key=tmp_path / "unused-key.pem",
                    invitation_file=invitation,
                ),
            )
            with pytest.raises(OSError):
                await server.start()
            await server.close()
            assert invitation.read_text(encoding="utf-8") == "existing-file"
    finally:
        await store.close()
