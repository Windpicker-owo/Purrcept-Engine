"""验证 QQ 重放跨缓存和重启复用权威事实，来源冲突只拒绝当前推送。

使用真实临时 SQLite 和内存传输；补取转发原文只接受显式样例应答。测试从
接收消费者进入正式接管，不依赖直接调用缓存或伪造已经成功的接管结果。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from purrcept_core.models import Message, ModelEventSink, ModelRequest, ModelResponse
from test_qq_input_history import Inputs, Rig, message
from test_qq_input_history import rig as rig
from test_qq_plugin import config
from test_qq_transport import Connector, Socket

from purrcept_engine import EngineHost
from purrcept_engine.app.evidence import EvidenceAccess
from purrcept_engine.app.receipts import LateCallResults
from purrcept_engine.errors import StorageError
from purrcept_engine.execution.protocol import JsonObject, decode_object, encode_json
from purrcept_engine.plugins.base import PluginPorts
from purrcept_engine.plugins.qq.messages import HistoryCache, project_message
from purrcept_engine.plugins.qq.plugin import factory_with_connector
from purrcept_engine.plugins.qq.service import QQService
from purrcept_engine.plugins.qq.settings import QQSettings
from purrcept_engine.plugins.qq.stickers import StickerRecord
from purrcept_engine.storage.models import Admission, InputRecord, InputSource, InputSubmission
from purrcept_engine.storage.store import Store


class Backend:
    """只结束内部模型轮次；返回文字不产生 QQ 回复或真实能力调用。"""

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        """接受完整 Core 请求，不替代宿主接管或消费者。"""
        del request, model, emit
        return ModelResponse(Message.assistant("内部结束"))


async def test_replay_after_reply_cache_eviction_keeps_first_relationship(rig: Rig) -> None:
    """首次核验的回复关系不随之后的历史缓存淘汰重新解释。"""
    rig.service.cache = HistoryCache(QQSettings(cache_conversations=1))
    quoted = message(message_id=-10, user_id=100)
    quoted["post_type"] = "message_sent"
    await rig.service.event(quoted, 1)
    payload = message(message_id=-11)
    payload["message"] = [
        {"type": "reply", "data": {"id": "-10"}},
        {"type": "text", "data": {"text": "回复正文"}},
    ]
    await rig.service.event(payload, 1)
    first = await rig.store.get_input(rig.inputs.admitted[0].input_id)
    assert first is not None
    details = decode_object(first.context.details_json.encode())
    parsed = details["message"]
    assert isinstance(parsed, dict) and parsed["reply_to_self"] is True

    other = project_message(message(group_id=456), chat="group", target_id="456")
    rig.service.cache.remember("100", other)
    assert rig.service.cache.read("100", "group", "123", 20) == []
    await rig.service.event(payload, 2)
    assert rig.inputs.admitted[-1].duplicate
    assert rig.inputs.admitted[-1].input_id == first.input_id
    assert await rig.store.get_input(first.input_id) == first


async def test_native_member_order_does_not_change_replay_identity(rig: Rig) -> None:
    """JSON 对象成员重排不改变实际原生事实或第一次接管关系。"""
    payload = message(message_id=-12)
    await rig.service.event(payload, 1)
    rig.service.cache = HistoryCache(rig.service.settings)
    reordered = dict(reversed(tuple(payload.items())))
    await rig.service.event(reordered, 2)
    assert rig.inputs.admitted[-1].duplicate
    assert rig.inputs.admitted[-1].input_id == rig.inputs.admitted[0].input_id


async def test_consumer_replay_after_cache_eviction_keeps_following_message(rig: Rig) -> None:
    """通过实际收包队列重放，无发生时间的输入也沿用第一次补定时间。"""
    payload = message(message_id=-20)
    payload.pop("time")
    rig.socket.incoming.put_nowait(encode_json(payload).decode())
    await rig.inputs.wait_admissions(1)
    first = await rig.store.get_input(rig.inputs.admitted[0].input_id)
    assert first is not None
    rig.service.cache = HistoryCache(rig.service.settings)
    rig.socket.incoming.put_nowait(encode_json(payload).decode())
    rig.socket.incoming.put_nowait(encode_json(message(message_id=-21)).decode())
    await rig.inputs.wait_admissions(3)
    assert rig.inputs.admitted[1].duplicate
    assert rig.inputs.admitted[1].input_id == first.input_id
    assert rig.inputs.admitted[2].input_id != first.input_id
    assert rig.service.snapshot()["state"] == "online"
    assert await rig.store.get_input(first.input_id) == first


async def test_two_real_hosts_replay_first_native_record_without_reply_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两次实际装配核对持久查询入口；第二次缓存中没有旧引用消息。"""
    admissions: asyncio.Queue[Admission] = asyncio.Queue()
    original_submit = EngineHost.submit

    async def observed_submit(host: EngineHost, submission: InputSubmission) -> Admission:
        admitted = await original_submit(host, submission)
        admissions.put_nowait(admitted)
        return admitted

    monkeypatch.setattr(EngineHost, "submit", observed_submit)
    native = InputSource("qq", "100", "group:123", "qq.message.received", "-31")
    quoted = message(message_id=-30, user_id=100)
    quoted["post_type"] = "message_sent"
    payload = message(message_id=-31)
    payload.pop("time")
    payload["message"] = [
        {"type": "reply", "data": {"id": "-30"}},
        {"type": "text", "data": {"text": "跨重启的相同原文"}},
    ]
    socket = Socket()
    connector = Connector((socket,))
    first_host = EngineHost(
        config(tmp_path),
        generation_backends={"local": Backend()},
        plugin_factories={"qq-fixture": factory_with_connector(connector)},
    )
    await first_host.start()
    try:
        await asyncio.wait_for(connector.connected.wait(), 3)
        socket.incoming.put_nowait(encode_json(quoted).decode())
        socket.incoming.put_nowait(encode_json(payload).decode())
        admitted = await asyncio.wait_for(admissions.get(), 3)
        first = await first_host.query_native_input(native)
        assert first is not None and first.input_id == admitted.input_id
        details = decode_object(first.context.details_json.encode())
        original_message = details["message"]
        assert isinstance(original_message, dict) and original_message["reply_to_self"] is True
    finally:
        await first_host.close()

    socket = Socket()
    connector = Connector((socket,))
    second_host = EngineHost(
        config(tmp_path),
        generation_backends={"local": Backend()},
        plugin_factories={"qq-fixture": factory_with_connector(connector)},
    )
    await second_host.start()
    try:
        await asyncio.wait_for(connector.connected.wait(), 3)
        socket.incoming.put_nowait(encode_json(payload).decode())
        next_source = InputSource("qq", "100", "group:123", "qq.message.received", "-32")
        socket.incoming.put_nowait(encode_json(message(message_id=-32)).decode())
        replayed = await asyncio.wait_for(admissions.get(), 3)
        followed = await asyncio.wait_for(admissions.get(), 3)
        assert replayed.duplicate and replayed.input_id == first.input_id
        next_record = await second_host.query_native_input(next_source)
        assert next_record is not None and next_record.input_id == followed.input_id
        restored = await second_host.query_native_input(native)
        assert restored is not None and restored.input_id == first.input_id
        assert restored.context == first.context and restored.occurred_at == first.occurred_at
        assert restored.text == first.text and restored.source == first.source
        assert socket.sent.empty()
    finally:
        await second_host.close()


async def test_replay_without_cache_does_not_refetch_frozen_forward(rig: Rig) -> None:
    """转发补取只在首次接管发生；以后原生重放借用完整已存原文。"""
    payload = message(message_id=-40)
    payload["message"] = [{"type": "forward", "data": {"id": "forward-id"}}]
    owner = asyncio.create_task(rig.service.event(payload, 1))
    request = await asyncio.wait_for(rig.socket.sent.get(), 3)
    rig.socket.reply(
        request,
        {
            "messages": [
                {
                    "sender": {"user_id": 77, "nickname": "引用者"},
                    "content": [{"type": "text", "data": {"text": "首次完整补取原文"}}],
                }
            ]
        },
    )
    await owner
    first = await rig.store.get_input(rig.inputs.admitted[0].input_id)
    assert first is not None and "首次完整补取原文" in first.text
    rig.service.cache = HistoryCache(rig.service.settings)
    await rig.service.event(payload, 2)
    assert rig.inputs.admitted[-1].duplicate
    assert rig.inputs.admitted[-1].input_id == first.input_id
    assert rig.socket.sent.empty()
    assert await rig.store.get_input(first.input_id) == first


async def test_replay_after_local_sticker_change_keeps_first_projection(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """后来新增本地贴纸标签不改变已经接管的图片消息事实。"""
    payload = message(message_id=-41)
    payload["message"] = [{"type": "image", "data": {"file": "native-image"}}]
    await rig.service.event(payload, 1)
    first = await rig.store.get_input(rig.inputs.admitted[0].input_id)
    assert first is not None
    matched = StickerRecord(
        sticker_id="new-sticker",
        tag="new-tag",
        digest="0" * 32,
        filename="example.png",
        media_type="image/png",
        path=str(rig.directory / "example.png"),
        created_at="2026-10-13",
    )

    def changed(data: JsonObject) -> StickerRecord:
        del data
        return matched

    monkeypatch.setattr(rig.service.stickers, "match", changed)
    rig.service.cache = HistoryCache(rig.service.settings)
    await rig.service.event(payload, 2)
    assert rig.inputs.admitted[-1].duplicate
    assert await rig.store.get_input(first.input_id) == first
    restored = rig.service.cache.read("100", "group", "123", 1)
    assert len(restored) == 1
    assert "new-tag" not in encode_json(restored).decode()


@pytest.mark.parametrize("changed", ("text", "attachment", "sender", "occurred_at", "boolean"))
async def test_real_native_conflict_rejects_only_current_frame(rig: Rig, changed: str) -> None:
    """正文、附件、发送者或平台发生时间改变仍是真冲突，不覆盖首次事实。"""
    payload = message(message_id=-50)
    payload["native_flag"] = 1
    await rig.service.event(payload, 1)
    first = await rig.store.get_input(rig.inputs.admitted[0].input_id)
    assert first is not None
    replay = decode_object(encode_json(payload))
    if changed == "text":
        replay["message"] = [{"type": "text", "data": {"text": "不同正文"}}]
    elif changed == "attachment":
        parts = replay["message"]
        assert isinstance(parts, list)
        parts.append({"type": "image", "data": {"file": "different-reference"}})
    elif changed == "sender":
        replay["user_id"] = 777
    elif changed == "occurred_at":
        replay["time"] = 1700000010
    else:
        replay["native_flag"] = True
    rig.socket.incoming.put_nowait(encode_json(replay).decode())
    rig.socket.incoming.put_nowait(encode_json(message(message_id=-51)).decode())
    await rig.inputs.wait_admissions(2)
    assert rig.service.snapshot()["state"] == "online"
    assert rig.service.snapshot()["gap_counts"] == {"source_conflict": 1}
    assert rig.inputs.admitted[-1].input_id != first.input_id
    assert await rig.store.get_input(first.input_id) == first


async def test_native_query_authority_failure_remains_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原生查询失败不是内容冲突，消费者和关闭观察均保留同一权威异常。"""
    failure = StorageError("权威记录损坏", code="invalid_record")

    async def failed(source: InputSource) -> InputRecord | None:
        del source
        raise failure

    store = await Store.open(tmp_path / "facts.sqlite3")
    inputs, socket = Inputs(store), Socket()
    service = QQService(
        "qq",
        QQSettings(mode="direct"),
        tmp_path,
        PluginPorts(
            inputs,
            lambda: {},
            EvidenceAccess(store.knowledge, 700),
            late_results=LateCallResults(store, frozenset()),
            native_inputs=inputs,
        ),
        Connector((socket,)),
    )
    monkeypatch.setattr(inputs, "query_native_input", failed)
    await service.client.start()
    await service.client.wait_state("online")
    closing_observed = False
    try:
        socket.incoming.put_nowait(encode_json(message(message_id=-60)).decode())
        await service.client.wait_state("failed", timeout_seconds=3)
        assert inputs.admitted == [] and service.snapshot()["gap_counts"] == {}
        with pytest.raises(StorageError) as observed:
            await service.close()
        closing_observed = True
        assert observed.value is failure
    finally:
        # 同一关闭任务永久保留原故障；异常观察完成后仍真正归还 SQLite 线程。
        try:
            if not closing_observed:
                await service.close()
        finally:
            await store.close()
