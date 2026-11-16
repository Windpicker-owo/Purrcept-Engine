"""验证 QQ 大历史的有界返回、完整原文引用和不可变续读。

复用真实临时 Store 与 OneBot 样例连接，不连接用户账号或修改实际贴纸库。
"""

import asyncio

from test_qq_input_history import Rig
from test_qq_input_history import rig as rig

from purrcept_engine.app.evidence import EvidenceAccess
from purrcept_engine.domain.execution.providers import CallContext
from purrcept_engine.plugins.qq.parameters import Read
from purrcept_engine.values import decode_object, encode_json


async def test_large_history_pages_bound_metadata_and_preserve_full_bodies(rig: Rig) -> None:
    """全局页长再大也不扩大 QQ 页；引用仅覆盖当前页，长正文仍可独立读取。"""
    rig.service.evidence = EvidenceAccess(rig.store.evidence, 20000)
    context = CallContext("read", "action", asyncio.get_running_loop().time() + 30)
    task = asyncio.create_task(
        rig.service.read(Read(chat="private", target_id="999", count=100), context)
    )
    request = await rig.socket.sent.get()
    body = "长正文" * 1800
    rig.socket.reply(
        request,
        {
            "messages": [
                {"message_id": index, "user_id": 999, "message": body, "time": 1700000000 + index}
                for index in range(1, 41)
            ]
        },
    )
    outcome = await task
    first = outcome.value
    assert isinstance(first, dict) and isinstance(first["next_cursor"], str)
    messages = first["messages"]
    assert isinstance(messages, list) and isinstance(messages[0], dict)
    reference = messages[0]["body_ref"]
    assert isinstance(reference, dict)
    body_id = reference["evidence_ref"]
    assert isinstance(body_id, str)
    page = await rig.service.evidence.read_evidence(body_id, max_chars=100)
    assert page.text == body[:100] and page.next_cursor is not None

    seen: list[str] = []
    while True:
        value = outcome.value
        assert isinstance(value, dict)
        messages = value["messages"]
        assert isinstance(messages, list)
        assert len(encode_json(messages).decode()) <= 4000
        assert len(encode_json(value).decode()) < 5000
        assert "large_message_body_refs" not in value
        for item in messages:
            assert isinstance(item, dict) and isinstance(item["message_id"], str)
            seen.append(item["message_id"])
        assert value["evidence_ref"] == first["evidence_ref"]
        cursor = value["next_cursor"]
        if cursor is None:
            break
        assert isinstance(cursor, str)
        outcome = await rig.service.read(Read(cursor=cursor), context)
    assert seen == [str(index) for index in range(1, 41)]
    assert rig.socket.sent.empty()


async def test_history_does_not_repeat_onebot_text_but_keeps_original_reference(rig: Rig) -> None:
    """相同文字只随页面出现一次，原始消息段可通过独立快照核对。"""
    context = CallContext("read", "action", asyncio.get_running_loop().time() + 5)
    task = asyncio.create_task(rig.service.read(Read(chat="private", target_id="999"), context))
    request = await rig.socket.sent.get()
    rig.socket.reply(
        request,
        {
            "messages": [
                {
                    "message_id": 1,
                    "user_id": 999,
                    "message": [{"type": "text", "data": {"text": "唯一正文"}}],
                    "time": 1700000000,
                }
            ]
        },
    )
    outcome = await task
    assert isinstance(outcome.value, dict)
    values = outcome.value["messages"]
    assert isinstance(values, list) and isinstance(values[0], dict)
    item = values[0]
    assert item["text"] == "唯一正文"
    assert "raw_message" not in item and "authored_text" not in item
    assert encode_json(outcome.value).decode().count("唯一正文") == 1
    reference = item["original_ref"]
    assert isinstance(reference, dict) and isinstance(reference["evidence_ref"], str)
    page = await rig.service.evidence.read_evidence(reference["evidence_ref"], max_chars=4000)
    original = decode_object(page.text.encode())
    assert original["text"] == "唯一正文" and isinstance(original["raw_message"], list)
