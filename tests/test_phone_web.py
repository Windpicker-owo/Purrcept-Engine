"""临时库、真实回环监听与 ASGI 验证 WebUI 配对，不连接真实手机或模型。"""

import asyncio
import json
import socket
import ssl
import time
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from websockets.asyncio.client import connect

from purrcept_engine.app.config import load_config
from purrcept_engine.app.phone_cli import initialize
from purrcept_engine.app.web import create_app
from purrcept_engine.errors import HostStateError, StorageError
from purrcept_engine.infrastructure.locking import InstanceLock
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system import CallContext
from purrcept_engine.plugin_system.panels import PairingOperation
from purrcept_engine.plugins.phone.plugin import PhoneSettings
from purrcept_engine.plugins.phone.runtime import PhoneRuntime
from purrcept_engine.plugins.phone.transport import PhoneServer, token_hash
from purrcept_engine.values import JsonObject
from tests.test_phone import Inputs
from tests.test_web import Host, local_asgi_only

# 复用只允许显式 ASGI HTTP 的夹具；实际 Phone TLS 监听不会连接外部设备。
pytestmark = pytest.mark.usefixtures("local_asgi_only")
__all__ = ["local_asgi_only"]


class PhoneHost(Host):
    """借出当前 Phone 所有者，模拟应用接缝，不在 WebUI 创建第二个运行时。"""

    server: PhoneServer | None = None

    def query_health(self) -> JsonObject:
        return {"instance_id": "current", "phase": "running"}

    async def phone_pairing(
        self,
        target: str,
        *,
        operation: PairingOperation = "read",
        expected_device_id: str | None = None,
    ) -> JsonObject:
        if target != "phone" or self.server is None:
            raise HostStateError("Phone 未启用。")
        if operation == "replace":
            assert expected_device_id is not None
            return await self.server.replace_pairing(expected_device_id)
        return await (self.server.open_pairing() if operation == "open" else self.server.pairing())


async def test_live_pairing_renews_and_consumes_the_exact_listener_invitation(
    tmp_path: Path,
) -> None:
    """普通查询不打开窗口；令牌写入生成可编码邀请，刷新和消费均撤销旧资格。"""
    initialize(tmp_path, "127.0.0.1")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    store = await Store.open(tmp_path / "engine.sqlite3")
    runtime = PhoneRuntime("phone", Inputs(store), store.phone("phone"))
    settings = PhoneSettings(
        public_url=f"https://127.0.0.1:{port}",
        port=port,
        certificate=tmp_path / "phone-cert.pem",
        private_key=tmp_path / "phone-key.pem",
        invitation_file=tmp_path / "invitation.json",
    )
    server = PhoneServer(runtime, settings)
    host = PhoneHost()
    host.server = server
    transport = httpx.ASGITransport(
        create_app(host, port=8765, token="token"), client=("127.0.0.1", 123)
    )
    endpoint = "/api/phone/phone/pairing"
    try:
        await server.start()
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8765"
        ) as client:
            assert (await client.get(endpoint)).json() == {"status": "closed"}
            assert not settings.invitation_file.exists()
            assert (await client.post(endpoint)).status_code == 403
            first = await client.post(endpoint, headers={"X-Purrcept-Token": "token"})
            public = first.json()
            assert first.headers["cache-control"] == "no-store"
            assert public["status"] == "ready" and public["qr_path"].startswith("M")
            assert public["qr_size"] > 21 and "invitation" not in public
            assert not settings.invitation_file.exists()
            old_invitation = await server.pairing()
            nonce = old_invitation["invitation"]
            assert isinstance(nonce, str)
            assert nonce not in first.text
            assert (await client.get(endpoint)).json() == public
            with patch(
                "purrcept_engine.plugins.phone.transport.time",
                SimpleNamespace(monotonic=lambda: time.monotonic() + 121),
            ):
                assert (await client.get(endpoint)).json() == {"status": "expired"}
            assert (await client.get(endpoint)).json() == public
            refreshed = (await client.post(endpoint, headers={"X-Purrcept-Token": "token"})).json()
            assert refreshed["qr_path"] != public["qr_path"]
            invitation = await server.pairing()
            phone_transport = httpx.ASGITransport(server.app)
            async with httpx.AsyncClient(
                transport=phone_transport, base_url="https://phone"
            ) as phone:
                rejected = await phone.post(
                    "/phone/v1/pair",
                    json={"invitation": old_invitation["invitation"], "name": "测试手机"},
                )
                assert rejected.status_code == 403
                paired = await phone.post(
                    "/phone/v1/pair",
                    json={"invitation": invitation["invitation"], "name": "测试手机"},
                )
                assert paired.status_code == 200
            assert not settings.invitation_file.exists()
            old_device = paired.json()["device_id"]
            old_token = paired.json()["token"]
            assert (await client.get(endpoint)).json() == {
                "status": "paired",
                "name": "测试手机",
                "device_id": old_device,
            }
            assert (await client.post(endpoint, headers={"X-Purrcept-Token": "token"})).json()[
                "status"
            ] == "paired"
            assert "invitation" not in json.dumps(runtime.snapshot())

            await runtime.store.append(
                old_device,
                "preserved-message",
                {"type": "chat.message", "text": "保留在 Engine 的历史"},
            )
            history = await runtime.store.events(old_device, 0)
            replacement = endpoint + "/replace"
            assert (
                await client.post(replacement, json={"device_id": old_device})
            ).status_code == 403
            assert (
                await client.post(replacement, headers={"X-Purrcept-Token": "token"}, json={})
            ).status_code == 400
            stale = await client.post(
                replacement,
                headers={"X-Purrcept-Token": "token"},
                json={"device_id": "another-device"},
            )
            assert stale.status_code == 503
            assert await runtime.store.authenticate(token_hash(old_token)) == old_device
            replaced = await client.post(
                replacement, headers={"X-Purrcept-Token": "token"}, json={"device_id": old_device}
            )
            assert replaced.json()["status"] == "ready"
            assert await runtime.store.authenticate(token_hash(old_token)) is None
            assert await runtime.store.events(old_device, 0) == history
            invitation = await server.pairing()
            async with httpx.AsyncClient(
                transport=phone_transport, base_url="https://phone"
            ) as phone:
                assert (
                    await phone.get(
                        "/phone/v1/state", headers={"Authorization": "Bearer " + old_token}
                    )
                ).status_code == 401
                fresh = await phone.post(
                    "/phone/v1/pair",
                    json={"invitation": invitation["invitation"], "name": "重装的 App"},
                )
                assert fresh.status_code == 200
                fresh_device = fresh.json()["device_id"]
                assert fresh_device != old_device
                assert (
                    await runtime.store.authenticate(token_hash(fresh.json()["token"]))
                    == fresh_device
                )
                assert await runtime.store.events(fresh_device, 0) == []
            stale_again = await client.post(
                replacement, headers={"X-Purrcept-Token": "token"}, json={"device_id": old_device}
            )
            assert stale_again.status_code == 503
            assert (await runtime.store.device())["device_id"] == fresh_device
    finally:
        await server.close()
        await store.close()


async def test_web_configuration_preview_and_save_borrow_current_host_lock(tmp_path: Path) -> None:
    """WebUI 只编辑 CLI 的固定配置，持有当前宿主锁时仍可保存下次启动设置。"""
    path = tmp_path / "purrcept.toml"
    path.write_text(
        files("purrcept_engine.app").joinpath("default.toml").read_text("utf-8"), encoding="utf-8"
    )
    import tomlkit

    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    document["web"] = {"enabled": True, "port": 8766}
    path.write_text(tomlkit.dumps(document), encoding="utf-8")
    before = path.read_bytes()
    data_dir = load_config(path).host.data_dir
    host = PhoneHost()
    lock = InstanceLock(data_dir)
    lock.acquire()
    try:
        transport = httpx.ASGITransport(
            create_app(host, port=8765, token="token", config_path=path, owner_data_dir=data_dir),
            client=("127.0.0.1", 123),
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8765",
            headers={"X-Purrcept-Token": "token"},
        ) as client:
            settings = await client.get("/api/phone/setup")
            assert settings.json()["available"] is True
            assert settings.json()["port"] == 8767
            assert "replace-with-explicit-local-key" not in settings.text
            preview = await client.post(
                "/api/phone/setup",
                json={"action": "preview", "address": "192.0.2.10", "port": 8767},
            )
            assert preview.status_code == 200
            assert path.read_bytes() == before
            assert "replace-with-explicit-local-key" not in preview.text
            assert (
                await client.post(
                    "/api/phone/setup", json={"action": "save", "preview_id": "wrong"}
                )
            ).status_code == 409
            saved = await client.post(
                "/api/phone/setup",
                json={"action": "save", "preview_id": preview.json()["preview_id"]},
            )
            assert saved.json()["restart_required"] is True
            assert (tmp_path / saved.json()["backup_name"]).read_bytes() == before
            assert load_config(path).plugins[0].entry == "phone"
            assert (
                await client.post(
                    "/api/phone/setup",
                    json={"action": "save", "preview_id": preview.json()["preview_id"]},
                )
            ).status_code == 409
    finally:
        lock.release()


@pytest.mark.parametrize("revoke_fails", [False, True])
async def test_repair_disconnects_live_old_phone_and_keeps_inflight_effect_unknown(
    tmp_path: Path,
    revoke_fails: bool,
) -> None:
    """真实 TLS/WSS 证明旧连接被撤销，未确认效果不会成为失败或重派发到新手机。"""
    initialize(tmp_path, "127.0.0.1")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    store = await Store.open(tmp_path / "live.sqlite3")
    ledger = store.phone("phone")
    await ledger.pair("old-device", "旧手机", token_hash("synthetic-old-token"))
    runtime = PhoneRuntime("phone", Inputs(store), ledger)
    server = PhoneServer(
        runtime,
        PhoneSettings(
            public_url=f"https://127.0.0.1:{port}",
            port=port,
            certificate=tmp_path / "phone-cert.pem",
            private_key=tmp_path / "phone-key.pem",
        ),
    )
    try:
        await server.start()
        tls = ssl.create_default_context(cafile=str(tmp_path / "phone-cert.pem"))
        async with connect(
            f"wss://127.0.0.1:{port}/phone/v1/channel",
            ssl=tls,
            additional_headers={"Authorization": "Bearer synthetic-old-token"},
        ) as channel:
            await channel.send(
                json.dumps({"v": 1, "id": "hello", "type": "hello", "payload": {"through": 0}})
            )
            assert json.loads(await channel.recv())["type"] == "welcome"
            runtime.control_session = "synthetic-session"
            pending = asyncio.create_task(
                runtime.command(
                    "screen",
                    {},
                    CallContext("inflight", "action", asyncio.get_running_loop().time() + 5),
                )
            )
            async with asyncio.timeout(2):
                while json.loads(await channel.recv())["type"] != "device.command":
                    pass
            if revoke_fails:
                with patch.object(
                    ledger,
                    "revoke",
                    side_effect=StorageError("synthetic failure", code="synthetic"),
                ):
                    with pytest.raises(StorageError):
                        await server.replace_pairing("old-device")
                assert (await server.pairing())["status"] == "paired"
            else:
                window = await server.replace_pairing("old-device")
                assert window["status"] == "ready"
            assert (await pending).state == "unknown"
            await asyncio.wait_for(channel.wait_closed(), 2)
            assert channel.close_code == 4401
            assert runtime.peer is None and runtime.control_session is None
            assert runtime.device_state == {}
            assert await ledger.authenticate(token_hash("synthetic-old-token")) == (
                "old-device" if revoke_fails else None
            )
            records = await ledger.commands(identity="inflight")
            assert len(records) == 1 and records[0]["result"] is None
    finally:
        await server.close()
        await store.close()
