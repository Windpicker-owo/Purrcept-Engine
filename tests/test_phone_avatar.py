"""验证 Bot 自主头像的实际能力入口、持久版本与认证媒体边界。"""

import asyncio
from pathlib import Path

import httpx
import pytest
from test_phone import Inputs
from test_stickers import context, image

from purrcept_engine.app.assembly import prepare_plugins
from purrcept_engine.app.config import PluginSettings
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system import PluginPorts
from purrcept_engine.plugins.phone.plugin import PhoneSettings
from purrcept_engine.plugins.phone.runtime import PhoneRuntime
from purrcept_engine.plugins.phone.transport import PhoneServer, token_hash
from purrcept_engine.values import JsonObject


async def test_avatar_capability_persists_and_rejects_invalid_media(tmp_path: Path) -> None:
    """模型可在离线时设置或重置头像；拒绝坏图片不改原头像，也不锁死数据库。"""
    path = tmp_path / "engine.sqlite3"
    store = await Store.open(path)
    try:
        prepared = prepare_plugins(
            (
                PluginSettings.model_validate(
                    {
                        "instance_id": "phone",
                        "entry": "phone",
                        "config": {
                            "public_url": "https://phone.test",
                            "certificate": "cert.pem",
                            "private_key": "key.pem",
                        },
                    }
                ),
            ),
            base_dir=tmp_path,
            factories={},
        )
        assert "me.phone.chat.set_avatar" in prepared.catalog.bindings
        resource = await dict(prepared.blueprints)["phone"].build(
            PluginPorts(
                Inputs(store),
                lambda: {},
                phone=store.phone("phone"),
                stickers=store.stickers,
            )
        )
        capability = next(
            item for item in resource.capabilities if item.declaration.name == "set_avatar"
        )
        # 媒体来自另一个宿主通道；头像不是往 Bot 贴纸库加一条收藏。
        identity = await store.phone("source").put_media("other", image().content, "image/png")
        saved = await capability.invoke({"media_id": identity}, context())
        assert saved.state == "confirmed"
        assert isinstance(saved.value, dict) and saved.value["delivery_state"] == "queued"
        assert await store.stickers.list() == ()
        avatar = await store.phone("phone").avatar()
        assert avatar is not None and avatar["media_id"] == identity
        assert await store.phone("another").avatar() is None
        bad = await store.phone("source").put_media("other", b"not-an-image", "image/png")
        for invalid in (bad, "f" * 64):
            result = await capability.invoke({"media_id": invalid}, context())
            assert result.state == "failed"
            assert await store.phone("phone").avatar() == avatar
        assert await store.collect_unused_media() == 0
    finally:
        await store.close()

    store = await Store.open(path)
    try:
        ledger = store.phone("phone")
        assert await ledger.avatar() == avatar
        changed = await ledger.set_avatar(identity)
        assert changed["revision"] == 2
        reset = await ledger.set_avatar(None)
        assert reset == {"media_id": None, "media_type": None, "revision": 3}
    finally:
        await store.close()


async def test_avatar_state_sync_and_authenticated_download(tmp_path: Path) -> None:
    """活动连接得到最新头像版本，媒体接口只公开当前头像且必须认证。"""
    store = await Store.open(tmp_path / "engine.sqlite3")
    updates: asyncio.Queue[tuple[str, JsonObject]] = asyncio.Queue()

    class Peer:
        device_id, connection_id = "device", "connection"

        async def send(self, kind: str, payload: JsonObject, reply_to: str | None = None) -> None:
            """保留实际发送循环的结果，不代替运行时生成事件。"""
            await updates.put((kind, payload))

    ledger = store.phone("phone")
    runtime = PhoneRuntime("phone", Inputs(store), ledger)
    delivery = asyncio.create_task(runtime.deliver(Peer(), 0))
    try:
        kind, initial = await asyncio.wait_for(updates.get(), 2)
        assert kind == "engine.state" and initial["avatar"] is None
        identity = await store.phone("source").put_media("other", image().content, "image/png")
        await runtime.set_avatar(identity)
        kind, updated = await asyncio.wait_for(updates.get(), 2)
        assert kind == "engine.state" and updated["avatar"] == await ledger.avatar()
        await ledger.pair("device", "Phone", token_hash("synthetic"))
        server = PhoneServer(
            runtime,
            PhoneSettings(
                public_url="https://phone.test",
                certificate=tmp_path / "unused.pem",
                private_key=tmp_path / "unused.key",
            ),
        )
        async with httpx.ASGITransport(app=server.app) as transport:

            async def get(auth: bool = True) -> httpx.Response:
                """直接调用本地 ASGI 入口，不经过全局禁网客户端。"""
                response = await transport.handle_async_request(
                    httpx.Request(
                        "GET",
                        "https://phone.test/phone/v1/media/" + identity,
                        headers={"Authorization": "Bearer synthetic"} if auth else {},
                    )
                )
                await response.aread()
                return response

            assert (await get(False)).status_code == 401
            response = await get()
            assert response.status_code == 200 and response.content == image().content
            await runtime.set_avatar(None)
            assert (await get()).status_code == 404
    finally:
        delivery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await delivery
        await store.close()
