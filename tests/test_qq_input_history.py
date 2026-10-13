"""唯一真实临时 Store 的 QQ 原文接管、来源过滤、媒体和不可变历史分页。

模型和网络未启动；InputPort 仅把插件的真实提交转交 Store.admit，不替代其
事务、去重或容量规则。所有文件和帧为样例生成，原生通知无 ID 时不伪去重。
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_qq_transport import Connector, Socket

from purrcept_engine.app.evidence import EvidenceAccess
from purrcept_engine.app.receipts import LateCallResults
from purrcept_engine.execution.protocol import JsonObject, decode_object, encode_json
from purrcept_engine.execution.providers import CallContext
from purrcept_engine.plugins.base import PluginPorts
from purrcept_engine.plugins.qq.parameters import Read
from purrcept_engine.plugins.qq.ports import ConnectionRejected
from purrcept_engine.plugins.qq.service import QQService
from purrcept_engine.plugins.qq.settings import QQSettings
from purrcept_engine.storage.knowledge import EvidenceSnapshot, KnowledgeInputError
from purrcept_engine.storage.models import Admission, InputRecord, InputSource, InputSubmission
from purrcept_engine.storage.store import Store
from purrcept_engine.storage.subject import InputProcessing

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


class Inputs:
    """调用实际 Store 接管并保留确认身份，没有先行成功或隐藏重投队列。"""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.admitted: list[Admission] = []
        self.changed = asyncio.Event()

    async def submit(self, submission: InputSubmission) -> Admission:
        """先取得真实事务确认，再向样例公布 input_id。"""
        result = await self.store.admit(submission)
        self.admitted.append(result)
        self.changed.set()
        return result

    async def wait_admissions(self, count: int) -> None:
        """等待实际事务确认数量，避免用循环休眠推断消费者进度。"""
        async with asyncio.timeout(3):
            while len(self.admitted) < count:
                self.changed.clear()
                await self.changed.wait()

    async def query_input(self, input_id: str) -> InputRecord | None:
        """从同一个 Store 读取权威原文。"""
        return await self.store.get_input(input_id)

    async def query_native_input(self, source: InputSource) -> InputRecord | None:
        """按原生范围查询第一次权威接管，不借用测试本地列表。"""
        return await self.store.get_native_input(source)

    async def query_processing(self, input_id: str) -> InputProcessing | None:
        """查询真实处理状态，不启动模型。"""
        return await self.store.input_processing(input_id)


@dataclass(frozen=True)
class Rig:
    store: Store
    inputs: Inputs
    service: QQService
    socket: Socket
    directory: Path


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    store = await Store.open(tmp_path / "facts.sqlite3")
    inputs = Inputs(store)
    socket = Socket()
    settings = QQSettings(
        mode="direct", group_list_type="whitelist", group_list=("123",), ban_user_id=("999",)
    )
    evidence = EvidenceAccess(store.knowledge, 700)
    ports = PluginPorts(
        inputs,
        lambda: {},
        evidence,
        late_results=LateCallResults(store, frozenset({"me.phone.qq.read"})),
        native_inputs=inputs,
    )
    service = QQService("qq", settings, tmp_path, ports, Connector((socket,)))
    await service.client.start()
    await service.client.wait_state("online")
    try:
        yield Rig(store, inputs, service, socket, tmp_path)
    finally:
        try:
            await service.close()
        finally:
            await store.close()


def message(*, message_id: int = -1, group_id: int = 123, user_id: int = 456) -> JsonObject:
    """生成完整原生来源，引用中的忽略账号仍是原文的一部分。"""
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": 100,
        "group_id": group_id,
        "user_id": user_id,
        "message_id": message_id,
        "time": 1700000000,
        "sender": {"nickname": "例"},
        "message": [
            {"type": "at", "data": {"qq": "100"}},
            {"type": "text", "data": {"text": "正文中提到999"}},
        ],
    }


async def test_filter_precedes_media_cache_and_atomic_admission(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden(raw: str) -> object:
        raise AssertionError("过滤推送不能取得媒体：" + raw)

    monkeypatch.setattr(rig.service.media, "image", forbidden)
    excluded = message(group_id=987)
    excluded["message"] = [{"type": "image", "data": {"url": "https://example.invalid/secret"}}]
    await rig.service.event(excluded, 1)
    await rig.service.event(message(user_id=999), 1)
    assert rig.inputs.admitted == []
    assert rig.service.cache.read("100", "group", "987", 20) == []
    diagnostics = encode_json(rig.service.snapshot()).decode()
    assert "secret" not in diagnostics and "正文" not in diagnostics
    assert rig.service.snapshot()["gap_counts"] == {"group_out_of_scope": 1, "ignored_sender": 1}


async def test_real_raw_input_keeps_image_reference_without_claiming_pixels_and_native_dedup(
    rig: Rig,
) -> None:
    media = rig.directory / ".purrcept" / "image.png"
    media.parent.mkdir()
    media.write_bytes(PNG)
    payload = message()
    parts = payload["message"]
    assert isinstance(parts, list)
    parts.append({"type": "image", "data": {"file": str(media)}})
    await rig.service.event(payload, 1)
    await rig.service.event(payload, 1)
    assert rig.inputs.admitted[1].duplicate
    assert rig.inputs.admitted[0].input_id == rig.inputs.admitted[1].input_id
    record = await rig.store.get_input(rig.inputs.admitted[0].input_id)
    assert record is not None
    assert record.context.minimum_wake and "999" in record.text
    assert record.media == ()
    assert decode_object(record.context.details_json.encode())["pixels_acquired"] is False
    assert decode_object(record.context.details_json.encode())["raw_event"] == payload
    assert record.source.account_id == "100" and record.source.conversation_id == "group:123"
    assert record.source.native_event_id == "-1"
    assert rig.service.cache.read("100", "group", "123", 20)[0] is not None


async def test_sent_echo_is_cache_only_and_notices_have_no_fake_event_id(rig: Rig) -> None:
    sent = message()
    sent["post_type"] = "message_sent"
    await rig.service.event(sent, 1)
    assert rig.inputs.admitted == []
    notice: JsonObject = {
        "post_type": "notice",
        "notice_type": "group_msg_emoji_like",
        "self_id": 100,
        "group_id": 123,
        "user_id": 456,
        "message_id": -1,
        "time": 1700000000,
        "likes": [{"emoji_id": "66", "count": 1}],
    }
    await rig.service.event(notice, 1)
    await rig.service.event(notice, 1)
    assert len(rig.inputs.admitted) == 2
    assert rig.inputs.admitted[0].input_id != rig.inputs.admitted[1].input_id
    first = await rig.store.get_input(rig.inputs.admitted[0].input_id)
    assert first is not None and first.source.native_event_id is None


async def test_history_cursor_keeps_same_snapshot_and_does_not_refetch(rig: Rig) -> None:
    context = CallContext("call", "action", asyncio.get_running_loop().time() + 5)
    task = asyncio.create_task(rig.service.read(Read(chat="private", target_id="999"), context))
    request = await rig.socket.sent.get()
    assert request["action"] == "get_friend_msg_history" and request["params"] == {
        "user_id": 999,
        "count": 20,
    }
    rig.socket.reply(
        request,
        {
            "messages": [
                {
                    "message_id": index,
                    "self_id": 100,
                    "user_id": 999,
                    "message": "原文" + str(index),
                    "time": 1700000000 + index,
                }
                for index in range(1, 5)
            ]
        },
    )
    first = await task
    assert isinstance(first.value, dict)
    cursor = first.value["next_cursor"]
    assert isinstance(cursor, str)
    old_id = first.value["evidence_ref"]
    seen = list(first.value["messages"]) if isinstance(first.value["messages"], list) else []
    while cursor:
        outcome = await rig.service.read(Read(cursor=cursor), context)
        assert isinstance(outcome.value, dict)
        assert outcome.value["evidence_ref"] == old_id
        values = outcome.value["messages"]
        assert isinstance(values, list)
        seen.extend(values)
        next_cursor = outcome.value["next_cursor"]
        assert next_cursor is None or isinstance(next_cursor, str)
        cursor = next_cursor or ""
    assert len(seen) == 4 and rig.socket.sent.empty()
    other = EvidenceSnapshot(
        evidence_id="other",
        version=1,
        kind="qq.history",
        text="0123456789",
        source_json='{"plugin":"other","kind":"qq.history"}',
        occurred_at=1.0,
    )
    await rig.service.evidence.put_evidence(other)
    page = await rig.service.evidence.read_evidence("other", max_chars=2)
    assert page.next_cursor is not None
    with pytest.raises(KnowledgeInputError, match="当前插件"):
        await rig.service.read(Read(cursor=page.next_cursor), context)


async def test_expected_history_failure_marks_cache_and_empty_cache_is_unavailable(
    rig: Rig,
) -> None:
    await rig.service.event(message(), 1)
    context = CallContext("call", "action", asyncio.get_running_loop().time() + 5)
    task = asyncio.create_task(rig.service.read(Read(chat="group", target_id="123"), context))
    request = await rig.socket.sent.get()
    rig.socket.incoming.put_nowait(
        encode_json(
            {
                "status": "failed",
                "retcode": 403,
                "data": None,
                "message": "权限不足",
                "echo": request["echo"],
            }
        ).decode()
    )
    result = await task
    assert result.state == "confirmed" and isinstance(result.value, dict)
    assert result.value["source"] == "cache"
    failure = result.value["platform_failure"]
    assert isinstance(failure, dict) and failure["retcode"] == 403
    coverage = result.value["coverage"]
    assert isinstance(coverage, dict) and coverage["complete"] is None
    missing = asyncio.create_task(rig.service.read(Read(chat="private", target_id="987"), context))
    request = await rig.socket.sent.get()
    rig.socket.incoming.put_nowait(
        encode_json(
            {"status": "failed", "retcode": 403, "data": None, "echo": request["echo"]}
        ).decode()
    )
    result = await missing
    assert result.state == "failed" and isinstance(result.value, dict)
    assert result.value["source"] == "unavailable" and result.value["messages"] is None


async def test_malformed_success_is_not_silently_replaced_by_cache(rig: Rig) -> None:
    await rig.service.event(message(), 1)
    context = CallContext("call", "action", asyncio.get_running_loop().time() + 5)
    task = asyncio.create_task(rig.service.read(Read(chat="group", target_id="123"), context))
    request = await rig.socket.sent.get()
    rig.socket.reply(request, {"unexpected": []})
    with pytest.raises(ConnectionRejected, match="messages"):
        await task


async def test_forward_source_is_complete_and_replay_reuses_actual_frozen_submission(
    rig: Rig,
) -> None:
    payload = message()
    payload["message"] = [{"type": "forward", "data": {"id": "native-forward"}}]
    task = asyncio.create_task(rig.service.event(payload, 1))
    request = await rig.socket.sent.get()
    assert request["action"] == "get_forward_msg"
    rig.socket.reply(
        request,
        {
            "messages": [
                {
                    "sender": {"user_id": 999, "nickname": "被引用者"},
                    "content": [{"type": "text", "data": {"text": "引用保留完整正文"}}],
                }
            ]
        },
    )
    await task
    first = await rig.store.get_input(rig.inputs.admitted[0].input_id)
    assert first is not None and "引用保留完整正文" in first.text
    assert first.context.minimum_wake is False
    await rig.service.event(payload, 2)
    assert rig.inputs.admitted[1].duplicate and rig.socket.sent.empty()
    assert rig.inputs.admitted[1].input_id == first.input_id


async def test_large_history_body_has_independent_smaller_readable_source(rig: Rig) -> None:
    context = CallContext("call", "action", asyncio.get_running_loop().time() + 5)
    task = asyncio.create_task(rig.service.read(Read(chat="private", target_id="999"), context))
    request = await rig.socket.sent.get()
    body = "完整长正文" * 600
    rig.socket.reply(
        request,
        {"messages": [{"message_id": -9, "user_id": 999, "message": body, "time": 1700000000}]},
    )
    outcome = await task
    assert isinstance(outcome.value, dict)
    refs = outcome.value["large_message_body_refs"]
    assert isinstance(refs, list) and isinstance(refs[0], dict)
    identity = refs[0]["evidence_ref"]
    assert isinstance(identity, str)
    page = await rig.service.evidence.read_evidence(identity, max_chars=13)
    assert page.text == body[:13] and page.next_cursor is not None
    assert page.slice is not None and page.slice.unit == "characters"
