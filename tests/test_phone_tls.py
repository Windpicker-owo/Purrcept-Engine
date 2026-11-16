"""真实回环 TLS/WSS 验证，仅使用临时证书和测试输入，不连接模型或设备。"""

import asyncio
import json
import socket
import ssl
from pathlib import Path

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
