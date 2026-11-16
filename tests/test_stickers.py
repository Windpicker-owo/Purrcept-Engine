"""用同一个真实 Store 验证通用贴纸库、QQ/手机交付与手机管理接口。

素材由测试创建，通道边界使用样例连接；不读取用户文件或连接真实账号。
"""

import asyncio
import base64
import io
import sqlite3
import threading
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError
from test_phone import Inputs
from test_qq_input_history import Rig
from test_qq_input_history import rig as rig

from purrcept_engine.app.assembly import prepare_plugins
from purrcept_engine.app.config import ExecutionSettings
from purrcept_engine.app.evidence import EvidenceAccess
from purrcept_engine.domain.execution.catalog import Catalog, ObjectDeclaration
from purrcept_engine.domain.inputs.models import MediaAsset
from purrcept_engine.domain.stickers import StickerError, StickerRecord
from purrcept_engine.infrastructure.execution.service import ExecutionService
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system import CallContext, PluginPorts
from purrcept_engine.plugins.images import images_blueprint
from purrcept_engine.plugins.phone.plugin import PhoneSettings
from purrcept_engine.plugins.phone.runtime import PhoneRuntime
from purrcept_engine.plugins.phone.transport import PhoneServer, token_hash
from purrcept_engine.plugins.qq.parameters import SendSticker
from purrcept_engine.plugins.qq.plugin import QQCapabilities
from purrcept_engine.plugins.stickers import (
    ListStickers,
    SaveSticker,
    StickerCapabilities,
    stickers_blueprint,
)
from purrcept_engine.values import encode_json


def image(number: int = 0) -> MediaAsset:
    """每个颜色产生不同完整 PNG，目录测试不以损坏字节绕过实际校验。"""
    output = io.BytesIO()
    Image.new("RGB", (2, 2), (number % 256, number // 256, 20)).save(output, format="PNG")
    return MediaAsset(output.getvalue(), "image/png")


def context() -> CallContext:
    """创建只用于本地调用的实际期限，不连接执行器或模型。"""
    return CallContext("fixture", "action", asyncio.get_running_loop().time() + 60)


async def test_worker_can_read_then_collect_in_one_action(tmp_path: Path) -> None:
    """真实 Worker 可直接把看图回执的实际媒体 ID 交给通用收藏能力。"""
    store = await Store.open(tmp_path / "engine.sqlite3")
    local = tmp_path / "cat.png"
    local.write_bytes(image().content)
    ports = PluginPorts(
        Inputs(store), lambda: {}, EvidenceAccess(store.evidence, 4000), stickers=store.stickers
    )
    picture_blueprint = images_blueprint(tmp_path)
    shared_blueprint = stickers_blueprint()
    picture = await picture_blueprint.build(ports)
    shared = await shared_blueprint.build(ports)
    catalog = Catalog(
        (ObjectDeclaration("subject", "me", "主体"), *shared_blueprint.objects),
        (*picture_blueprint.capabilities, *shared_blueprint.capabilities),
    )
    execution = ExecutionService(
        catalog,
        (*picture.capabilities, *shared.capabilities),
        store,
        ExecutionSettings(),
        scratch_root=tmp_path / "scratch",
    )
    try:
        result = await execution.execute(
            "from world import me\n"
            'picture = await me.view_image(source="cat.png")\n'
            'return await me.stickers.save(media_id=picture["media_id"], tag="Shared cat")'
        )
        assert result.state == "completed"
        records = await store.stickers.list()
        assert len(records) == 1 and records[0].tag == "Shared cat"
        assert records[0].media_id == result.calls[0].media[0].media_id
    finally:
        await execution.close()
        if picture.close is not None:
            await picture.close()
        await store.close()


async def test_shared_identity_conflicts_restart_and_media_retention(tmp_path: Path) -> None:
    """重存沿用身份，冲突回滚；收藏的外键保护图片，重启后仍可读。"""
    path = tmp_path / "engine.sqlite3"
    store = await Store.open(path)
    try:
        library = store.stickers
        first = await library.save_image(image(), tag="猫猫")
        renamed = await library.save_image(image(), tag="摸摸")
        assert first.sticker_id == renamed.sticker_id
        assert first.created_at == renamed.created_at
        with pytest.raises(StickerError, match="标签"):
            await library.save_image(image(1), tag="摸摸")
        with pytest.raises(StickerError):
            await library.save_image(MediaAsset(b"not-image", "image/png"), tag="损坏")
        assert await library.list() == (renamed,)
        # 媒体回收沿实际外键扫描，不能把仍被贴纸库引用的图片当作孤儿。
        assert await store.collect_unused_media() == 0
    finally:
        await store.close()
    store = await Store.open(path)
    try:
        record, asset = await store.stickers.read(sticker_id=first.sticker_id)
        assert record.tag == "摸摸" and asset.content == image().content
        await store.stickers.delete(record.sticker_id)
        with pytest.raises(StickerError):
            await store.stickers.read(sticker_id=record.sticker_id)
    finally:
        await store.close()


async def test_default_catalog_and_large_search_use_frozen_pages(tmp_path: Path) -> None:
    """无 QQ 也有通用入口；大目录与旧游标不因后续删除而截断、跳项或改查询。"""
    prepared = prepare_plugins((), base_dir=tmp_path, factories={})
    for name in ("save", "list", "view", "delete"):
        assert "me.stickers." + name in prepared.catalog.bindings
    store = await Store.open(tmp_path / "engine.sqlite3")
    try:
        library = store.stickers
        records = [
            await library.save_image(image(index), tag=("猫" if index % 2 else "狗") + str(index))
            for index in range(1200)
        ]
        tools = StickerCapabilities(library, EvidenceAccess(store.evidence, 20000))
        outcome = await tools.list(ListStickers(), context())
        assert isinstance(outcome.value, dict) and outcome.value["total"] == 1200
        identity = outcome.value["evidence_ref"]
        for record in records[3:]:
            await library.delete(record.sticker_id)
        seen: list[str] = []
        while True:
            value = outcome.value
            assert isinstance(value, dict) and value["evidence_ref"] == identity
            items = value["stickers"]
            assert isinstance(items, list) and len(encode_json(items).decode()) <= 4000
            assert len(encode_json(value).decode()) < 4500
            for item in items:
                assert isinstance(item, dict) and isinstance(item["sticker_id"], str)
                assert "path" not in item
                seen.append(item["sticker_id"])
            cursor = value["next_cursor"]
            if cursor is None:
                break
            assert isinstance(cursor, str)
            outcome = await tools.list(ListStickers(cursor=cursor), context())
        assert seen == [item.sticker_id for item in records]
        filtered = await tools.list(ListStickers(query="猫"), context())
        assert isinstance(filtered.value, dict) and filtered.value["total"] == 1
        empty = await tools.list(ListStickers(query="不存在"), context())
        assert isinstance(empty.value, dict) and empty.value["stickers"] == []
        with pytest.raises(ValidationError, match="query"):
            ListStickers(cursor="old-page", query="猫")
    finally:
        await store.close()


async def test_phone_sends_shared_sticker_and_delete_preserves_chat_media(tmp_path: Path) -> None:
    """同一贴纸作为真实附件发送到手机；撤销收藏不会损坏已经发送的气泡。"""
    store = await Store.open(tmp_path / "engine.sqlite3")
    try:
        ledger = store.phone("phone")
        await ledger.pair("device", "Phone", token_hash("fixture"))
        runtime = PhoneRuntime("phone", Inputs(store), ledger, stickers=store.stickers)
        record = await store.stickers.save_image(image(), tag="摸摸")
        result = await runtime.send_chat("", context(), sticker_id=record.sticker_id)
        assert result.state == "confirmed"
        event = (await ledger.events("device", 0))[0]
        assert event["sticker_id"] == record.sticker_id and event["media_id"] == record.media_id
        assert event["text"] == "[贴纸：摸摸]"
        assert await ledger.media("other", record.media_id) is None
        await store.stickers.delete(record.sticker_id)
        cached = await ledger.media("device", record.media_id)
        assert cached is not None and cached.content == image().content
        assert (
            await runtime.send_chat("", context(), sticker_id=record.sticker_id)
        ).state == "failed"
    finally:
        await store.close()


async def test_public_tools_bind_save_view_and_delete_without_qq(tmp_path: Path) -> None:
    """实际能力绑定完成保存、原生像素交付与删除，整个流程无需装配 QQ。"""
    store = await Store.open(tmp_path / "engine.sqlite3")
    try:
        media_id = await store.phone("phone").put_media("device", image().content, "image/png")
        resource = await stickers_blueprint().build(
            PluginPorts(
                Inputs(store),
                lambda: {},
                EvidenceAccess(store.evidence, 4000),
                stickers=store.stickers,
            )
        )
        tools = {item.declaration.name: item for item in resource.capabilities}
        saved = await tools["save"].invoke({"media_id": media_id, "tag": "A shared cat"}, context())
        assert isinstance(saved.value, dict)
        identity = saved.value["sticker_id"]
        shown = await tools["view"].invoke({"sticker_id": identity}, context())
        assert shown.state == "confirmed" and shown.media[0].media_id == media_id
        deleted = await tools["delete"].invoke({"sticker_id": identity}, context())
        assert deleted.state == "confirmed" and await store.stickers.list() == ()
    finally:
        await store.close()


async def test_common_save_collects_phone_upload_and_qq_uses_same_identity(rig: Rig) -> None:
    """手机上传的媒体通过通用工具收藏，QQ 适配器按同一身份发送原始字节。"""
    media_id = await rig.store.phone("phone").put_media("device", image().content, "image/png")
    tools = StickerCapabilities(rig.store.stickers, rig.service.evidence)
    saved = await tools.save(SaveSticker(media_id=media_id, tag="共享"), context())
    assert isinstance(saved.value, dict)
    sticker_id = str(saved.value["sticker_id"])
    task = asyncio.create_task(
        QQCapabilities(rig.service).invoke(
            "send_sticker",
            SendSticker(chat="private", target_id="123", sticker_id=sticker_id),
            context(),
        )
    )
    request = await rig.socket.sent.get()
    params = request["params"]
    assert isinstance(params, dict)
    parts = params["message"]
    assert isinstance(parts, list) and isinstance(parts[0], dict)
    assert parts[0]["data"] == {
        "file": "base64://" + base64.b64encode(image().content).decode(),
        "sub_type": 1,
        "summary": "[共享]",
    }
    rig.socket.reply(request, {"message_id": -100})
    outcome = await task
    assert outcome.state == "confirmed"


async def test_cancelled_save_keeps_transaction_owned_until_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取消通道的等待不能撤销已由唯一数据库队列接管的完整导入。"""
    from purrcept_engine.infrastructure.sqlite import stickers as sql

    entered, release = threading.Event(), threading.Event()
    # 故障注入刻意截停事务内部，验证取消等待不取消已接管的数据库工作。
    original = sql._save  # pyright: ignore[reportPrivateUsage]

    def delayed(connection: sqlite3.Connection, data: bytes, tag: str) -> StickerRecord:
        entered.set()
        assert release.wait(5)
        return original(connection, data, tag)

    monkeypatch.setattr(sql, "_save", delayed)
    store = await Store.open(tmp_path / "engine.sqlite3")
    try:
        pending = asyncio.create_task(store.stickers.save_image(image(), tag="已接管"))
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        release.set()
        records = await store.stickers.list()
        assert len(records) == 1 and records[0].tag == "已接管"
    finally:
        release.set()
        await store.close()


async def test_phone_cannot_browse_or_mutate_bot_library(tmp_path: Path) -> None:
    """APP 收藏属于用户；认证手机也没有 Bot 贴纸目录的读取、写入和删除接口。"""
    store = await Store.open(tmp_path / "engine.sqlite3")
    try:
        ledger = store.phone("phone")
        await ledger.pair("device", "Phone", token_hash("synthetic"))
        record = await store.stickers.save_image(image(), tag="Bot收藏")
        runtime = PhoneRuntime("phone", Inputs(store), ledger, stickers=store.stickers)
        server = PhoneServer(
            runtime,
            PhoneSettings(
                public_url="https://phone.test",
                certificate=tmp_path / "unused.pem",
                private_key=tmp_path / "unused.key",
            ),
        )
        async with httpx.ASGITransport(app=server.app) as transport:

            async def request(method: str, path: str) -> httpx.Response:
                """通过真实路由验证能力边界，不向外部网络发请求。"""
                response = await transport.handle_async_request(
                    httpx.Request(
                        method,
                        "https://phone.test" + path,
                        headers={"Authorization": "Bearer synthetic"},
                    )
                )
                await response.aread()
                return response

            assert (await request("GET", "/phone/v1/stickers")).status_code == 404
            assert (await request("POST", "/phone/v1/stickers")).status_code == 404
            assert (
                await request("DELETE", "/phone/v1/stickers/" + record.sticker_id)
            ).status_code == 404
            assert (await request("GET", "/phone/v1/media/" + record.media_id)).status_code == 404
            assert await store.stickers.list() == (record,)
            await runtime.send_chat("", context(), sticker_id=record.sticker_id)
            response = await request("GET", "/phone/v1/media/" + record.media_id)
            assert response.status_code == 200 and response.content == image().content
    finally:
        await store.close()
