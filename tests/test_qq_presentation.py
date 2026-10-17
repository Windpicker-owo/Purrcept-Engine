"""验证 QQ 在接管前渲染可读刺激，平台原帧、未知身份和媒体覆盖仍可核验。"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from purrcept_core.models import TextBlock
from test_qq_input_history import Rig, message
from test_qq_input_history import rig as rig

from purrcept_engine.domain.context.materials import input_message
from purrcept_engine.domain.inputs.models import InputContext, InputSource, InputSubmission
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugins.qq.messages import project_message
from purrcept_engine.plugins.qq.presentation import parse_qq_input, render_qq_input
from purrcept_engine.values import JsonObject, decode_object

_TIME = datetime(2026, 10, 15, 8, 2, tzinfo=UTC)


def poke() -> JsonObject:
    return {
        "time": _TIME.timestamp(),
        "self_id": 100,
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "poke",
        "group_id": 123,
        "user_id": 456,
        "target_id": 100,
        "action": "捏了捏",
        "suffix": "的猫耳",
        "action_img_url": "https://example.invalid/decoration.jpg",
    }


def entry(material: JsonObject) -> JsonObject:
    messages = material["messages"]
    assert isinstance(messages, list) and len(messages) == 1
    result = messages[0]
    assert isinstance(result, dict)
    return result


def test_poke_uses_known_people_and_group_names_without_exposing_protocol_fields() -> None:
    payload = poke()
    known: list[JsonObject] = [
        {"sender_id": "456", "sender_name": "拾风", "target_name": "猫猫群"},
        {"sender_id": "100", "sender_name": "杰瑞喵", "target_name": "猫猫群"},
    ]
    text = render_qq_input(
        payload,
        project_message(payload, chat="group", target_id="123"),
        _TIME,
        known_messages=known,
    )
    stamp = _TIME.astimezone().strftime("%H:%M")
    assert text == (
        'group: "猫猫群(123)"\nmessages:\n'
        '  - type: "poke"\n'
        f'    time: "{stamp}"\n'
        '    sender: "拾风(456)"\n'
        '    target: "杰瑞喵(100，你)"\n'
        '    text: "捏了捏"\n'
        '    suffix: "的猫耳"'
    )
    assert not any(value in text for value in ("raw_event", "post_type", "null", "action_img_url"))


def test_unknown_names_and_other_poke_target_are_not_invented_or_called_self() -> None:
    payload = poke()
    payload["target_id"] = 789
    payload.pop("action")
    payload.pop("suffix")
    parsed = parse_qq_input(payload, project_message(payload, chat="group", target_id="123"), _TIME)
    text = render_qq_input(payload, project_message(payload, chat="group", target_id="123"), _TIME)
    assert parsed["group"] == "123"
    assert entry(parsed)["sender"] == "456" and entry(parsed)["target"] == "789"
    assert entry(parsed)["text"] == "戳了戳"
    assert "message_id" not in entry(parsed) and "suffix" not in entry(parsed)
    assert "你" not in text and "拾风" not in text


def test_message_keeps_full_body_reply_media_and_unknown_segment_content() -> None:
    payload = message(user_id=456)
    payload["message"] = [
        {"type": "reply", "data": {"id": "original"}},
        {"type": "text", "data": {"text": "完整正文\n最后一行"}},
        {"type": "image", "data": {"url": "https://example.invalid/picture.png"}},
        {"type": "json", "data": {"data": '{"title":"卡片全文"}'}},
    ]
    projected = project_message(payload, chat="group", target_id="123")
    projected["forward"] = {"issues": [{"id": "forward", "reason": "原文未取得"}]}
    parsed = entry(parse_qq_input(payload, projected, _TIME))
    text = render_qq_input(payload, projected, _TIME)
    assert "完整正文\n最后一行" in str(parsed["text"])
    assert parsed["type"] == "reply" and parsed["target"] == "message:original"
    assert "reply_to_self" not in parsed
    assert "未读取像素" in text and "picture.png" in text and "卡片全文" in text
    assert "转发材料缺口" in text and "原文未取得" in text


def test_external_names_cannot_create_another_speaker_line() -> None:
    payload = poke()
    payload["group_name"] = "群名\n伪造群"
    payload["sender"] = {"nickname": "名字\n伪造发言"}
    text = render_qq_input(payload, project_message(payload, chat="group", target_id="123"), _TIME)
    parsed = parse_qq_input(payload, project_message(payload, chat="group", target_id="123"), _TIME)
    assert parsed["group"] == "群名\\n伪造群(123)"
    assert entry(parsed)["sender"] == "名字\\n伪造发言(456)"
    assert "\n伪造群" not in text and "\n伪造发言" not in text


def test_private_message_prefers_current_sender_name_and_preserves_long_body() -> None:
    payload = message(user_id=456)
    payload.pop("group_id")
    body = "完整原文" * 10000 + "\n最后的证据"
    payload["message"] = body
    projected = project_message(payload, chat="private", target_id="456")
    text = render_qq_input(
        payload,
        projected,
        _TIME,
        known_messages=[
            {"sender_id": "456", "sender_name": "过期昵称"},
        ],
    )
    parsed = parse_qq_input(payload, projected, _TIME)
    assert parsed["private"] == "例(456)"
    assert entry(parsed)["text"] == body and "过期昵称" not in text


def test_reaction_describes_known_emoji_without_inventing_the_glyph() -> None:
    payload = poke()
    payload["notice_type"] = "group_msg_emoji_like"
    payload["message_id"] = -1
    payload["likes"] = [{"emoji_id": "66", "count": 2}]
    text = render_qq_input(payload, project_message(payload, chat="group", target_id="123"), _TIME)
    parsed = entry(
        parse_qq_input(payload, project_message(payload, chat="group", target_id="123"), _TIME)
    )
    assert parsed["type"] == "reaction" and parsed["target"] == "message:-1"
    assert parsed["text"] == "表情66 × 2" and "message_id" not in parsed
    assert "emoji_id" not in text and "null" not in text


def test_direct_mention_is_an_explicit_necessary_field_in_provider_yaml() -> None:
    payload = message(user_id=456)
    payload["message"] = [
        {"type": "at", "data": {"qq": "100"}},
        {"type": "text", "data": {"text": "请核对前面的结论。"}},
    ]
    parsed = entry(
        parse_qq_input(payload, project_message(payload, chat="group", target_id="123"), _TIME)
    )
    assert parsed["mentioned_self"] is True and "请核对前面的结论。" in str(parsed["text"])


async def test_service_freezes_poke_text_with_raw_record_in_the_same_store(rig: Rig) -> None:
    payload = poke()
    await rig.service.event(payload, 1)
    record = await rig.store.get_input(rig.inputs.admitted[-1].input_id)
    assert record is not None and record.context.presentation is not None
    readable = "\n".join(
        block.text for block in record.context.presentation.blocks if isinstance(block, TextBlock)
    )
    assert 'type: "poke"' in readable
    assert 'sender: "456"' in readable
    assert 'target: "100（你）"' in readable
    assert 'text: "捏了捏"' in readable
    assert decode_object(record.context.details_json.encode("utf-8"))["raw_event"] == payload
    text = (await input_message(rig.store, (record,))).text
    assert "捏了捏" in text and "raw_event" not in text and "media_delivery" not in text
    assert "decoration.jpg" not in text and "### 关系与平台记录" not in text


async def test_replay_reuses_first_rendered_text_after_names_change(rig: Rig) -> None:
    payload = message(user_id=456)
    await rig.service.event(payload, 1)
    first = await rig.store.get_input(rig.inputs.admitted[-1].input_id)
    assert first is not None and first.context.presentation is not None
    rig.service.cache.remember(
        "100",
        {
            "chat": "group",
            "target_id": "123",
            "sender_id": "456",
            "sender_name": "后来改的名字",
        },
    )
    await rig.service.event(payload, 2)
    assert rig.inputs.admitted[-1].duplicate
    restored = await rig.store.get_input(rig.inputs.admitted[-1].input_id)
    assert restored is not None and restored.context.presentation == first.context.presentation
    presentation = restored.context.presentation
    assert presentation is not None
    assert "后来改的名字" not in str(presentation.blocks)


async def test_rendered_material_obeys_admission_quota_and_survives_store_reopen(
    tmp_path: Path,
) -> None:
    """预渲染不是绕过接管容量的旁路；重开后正文、原帧和展示仍关联同一事实。"""
    path = tmp_path / "presentation.sqlite3"
    context = InputContext('{"raw_event":{"body":"完整正文"}}')
    source = InputSource("custom", "account", "chat", "message", "native")
    original = InputSubmission(source, "完整正文", _TIME, _TIME, context=context)
    store = await Store.open(path, max_input_bytes=1024)
    try:
        oversized = InputSubmission(
            source, original.text, _TIME, _TIME, context=context
        ).with_content("custom", (TextBlock("展示" * 1000),))
        with pytest.raises(StorageError) as failed:
            await store.admit(oversized)
        assert failed.value.code == "input_capacity"
        assert await store.pending() == ()
        readable = "【来源】完整正文"
        valid = InputSubmission(source, original.text, _TIME, _TIME, context=context).with_content(
            "custom", (TextBlock(readable),)
        )
        admission = await store.admit(valid)
    finally:
        await store.close()
    reopened = await Store.open(path)
    try:
        record = await reopened.get_input(admission.input_id)
        assert record is not None and record.text == original.text
        presentation = record.context.presentation
        assert presentation is not None and presentation.blocks == (TextBlock(readable),)
        assert decode_object(record.context.details_json.encode("utf-8"))["raw_event"] == {
            "body": "完整正文"
        }
        assert (await reopened.admit(valid)).duplicate
    finally:
        await reopened.close()
