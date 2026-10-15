"""用完整公开 Core 值及调用方 SQLite 事务核验封闭检查点往返。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from purrcept_core.models import (
    AudioBlock,
    AudioBytes,
    ConversationCheckpoint,
    ConversationState,
    ImageBlock,
    ImageBytes,
    ImageUrl,
    Message,
    MessageRole,
    ModelContinuation,
    ReasoningBlock,
    ReminderPlacement,
    ReminderScope,
    ReminderState,
    SystemReminder,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)

from purrcept_engine.domain.context.checkpoints import (
    CheckpointError,
    decode_checkpoint,
    decode_content,
    decode_message,
    encode_checkpoint,
)
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite.checkpoints import intern_checkpoint, restore_checkpoint
from purrcept_engine.serialization import encode


def checkpoint() -> ConversationCheckpoint:
    """覆盖所有内置内容类型、过程关联、隐藏材料和提醒生命周期。"""
    messages = (
        Message(
            MessageRole.USER,
            content=(TextBlock("input"), ImageBlock(ImageUrl("https://local.invalid/image"))),
            name="source",
            metadata={"nested": {"original": [1, True, None]}},
        ),
        Message(
            MessageRole.ASSISTANT,
            content=(
                ReasoningBlock("retained reasoning"),
                ToolCallBlock("call", "play_action", arguments={"code": "pass"}),
            ),
        ),
        Message(
            MessageRole.TOOL,
            content=(
                ToolResultBlock(
                    "call",
                    is_error=True,
                    content=(
                        ImageBlock(ImageBytes(b"shared", "image/png"), alt_text="image"),
                        AudioBlock(AudioBytes(b"shared", "audio/mpeg"), alt_text="recording"),
                        TextBlock("result"),
                    ),
                ),
            ),
        ),
        Message.assistant("final"),
        Message.user("current uncommitted suffix"),
    )
    state = ConversationState(
        messages,
        continuation=ModelContinuation("provider", {"cursor": "opaque"}),
        turn_index=1,
        turn_boundaries=(4,),
    )
    reminders = ReminderState(
        (
            SystemReminder((TextBlock("next"),), key="next"),
            SystemReminder(
                (ImageBlock(ImageBytes(b"shared", "image/png")),),
                key="ongoing",
                scope=ReminderScope.CONVERSATION,
                placement=ReminderPlacement.TAIL,
                priority=7,
            ),
        )
    )
    return ConversationCheckpoint(state, reminders)


def test_all_public_core_checkpoint_fields_roundtrip_without_loss() -> None:
    """角色、隐藏推理、工具参数/结果、媒体、提醒及真实边界原样恢复。"""
    original = checkpoint()
    encoded = encode_checkpoint(original)
    restored = decode_checkpoint(encoded)
    assert restored == original
    assert encode_checkpoint(restored) == encoded
    assert restored.history.messages[-1].text == "current uncommitted suffix"
    assert restored.history.turn_index == 1 and restored.history.turn_boundaries == (4,)
    assert isinstance(restored.history.messages[1].content[0], ReasoningBlock)


def test_delivery_codecs_preserve_full_messages_and_content_without_guessing() -> None:
    """交付使用与检查点相同的封闭公开类型，媒体和工具错误标记保持完整。"""
    for message in checkpoint().history.messages:
        assert decode_message(encode(message)) == message
        assert decode_content(encode(message.content)) == message.content
    with pytest.raises(CheckpointError):
        decode_content('[{"type":"FutureBlock","text":"original"}]')
    with pytest.raises(CheckpointError, match="重复"):
        decode_message(
            encode(Message.user("original")).replace(
                '"role":"user"', '"role":"assistant","role":"user"'
            )
        )


def test_checkpoint_media_and_relationships_share_callers_transaction(tmp_path: Path) -> None:
    """同字节只存一份 BLOB，引用与状态同事务；helper 不自行提交。"""
    connection = sqlite3.connect(tmp_path / "state.sqlite")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("CREATE TABLE media (media_id TEXT PRIMARY KEY, content BLOB NOT NULL)")
    connection.execute(
        "CREATE TABLE checkpoints (version INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE refs (version INTEGER REFERENCES checkpoints(version), "
        "media_id TEXT REFERENCES media(media_id))"
    )
    connection.commit()
    original = checkpoint()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            stored, media_ids = intern_checkpoint(connection, encode_checkpoint(original))
            assert connection.in_transaction and len(media_ids) == 1
            connection.execute("INSERT INTO checkpoints VALUES (1,?)", (stored,))
            connection.executemany("INSERT INTO refs VALUES (1,?)", ((item,) for item in media_ids))
        assert restore_checkpoint(connection, stored) == original
        assert connection.execute("SELECT count(*) FROM media").fetchone() == (1,)
        connection.execute("BEGIN IMMEDIATE")
        changed = replace(
            original, reminders=ReminderState((SystemReminder((AudioBlock(AudioBytes(b"new")),)),))
        )
        second, _ = intern_checkpoint(connection, encode_checkpoint(changed))
        connection.execute("INSERT INTO checkpoints VALUES (2,?)", (second,))
        connection.rollback()
        assert connection.execute("SELECT count(*) FROM media").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM checkpoints").fetchone() == (1,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "change",
    [
        "unknown_block",
        "unknown_field",
        "wrong_role",
        "broken_tool_flag",
        "bad_boundary",
        "duplicate_reminder",
        "nonfinite",
    ],
)
def test_malformed_checkpoint_is_rejected_without_partial_recovery(change: str) -> None:
    """缺失或错误不能通过默认值、删块、提醒去重或隐式布尔转换恢复。"""
    value = cast(dict[str, object], json.loads(encode_checkpoint(checkpoint())))
    history = cast(dict[str, object], value["history"])
    messages = cast(list[dict[str, object]], history["messages"])
    if change == "unknown_block":
        messages[0]["content"] = [{"type": "FutureBlock", "value": "secret"}]
    elif change == "unknown_field":
        value["future"] = 1
    elif change == "wrong_role":
        messages[0]["role"] = "system"
    elif change == "broken_tool_flag":
        cast(list[dict[str, object]], messages[2]["content"])[0]["is_error"] = 1
    elif change == "bad_boundary":
        history["turn_boundaries"] = [6]
    elif change == "duplicate_reminder":
        reminders = cast(dict[str, object], value["reminders"])
        reminders["reminders"] = [cast(list[object], reminders["reminders"])[0]] * 2
    else:
        messages[0]["metadata"] = {"number": float("inf")}
    with pytest.raises(CheckpointError):
        decode_checkpoint(json.dumps(value))


def test_missing_media_fails_instead_of_replacing_original_bytes(tmp_path: Path) -> None:
    """同库媒体回滚后无法恢复检查点，远端 URL 也不在恢复期访问。"""
    connection = sqlite3.connect(tmp_path / "missing.sqlite")
    connection.execute("CREATE TABLE media (media_id TEXT PRIMARY KEY, content BLOB NOT NULL)")
    connection.commit()
    try:
        connection.execute("BEGIN IMMEDIATE")
        stored, _ = intern_checkpoint(connection, encode_checkpoint(checkpoint()))
        connection.rollback()
        with pytest.raises(StorageError, match="缺失"):
            restore_checkpoint(connection, stored)
    finally:
        connection.close()


def test_duplicate_json_identity_is_not_silently_chosen(tmp_path: Path) -> None:
    """重复标签有歧义，即使解析器默认最后值也能构造公开对象仍须拒绝。"""
    encoded = encode_checkpoint(checkpoint())
    encoded = encoded.replace(
        '"type":"ConversationCheckpoint"', '"type":"Other","type":"ConversationCheckpoint"', 1
    )
    with pytest.raises(CheckpointError, match="重复"):
        decode_checkpoint(encoded)
    connection = sqlite3.connect(tmp_path / "duplicate.sqlite")
    try:
        with pytest.raises(CheckpointError, match="重复"):
            restore_checkpoint(connection, encoded)
    finally:
        connection.close()
