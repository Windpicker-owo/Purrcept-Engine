"""在公开 Core 值与权威检查点之间进行封闭、无损的类型转换。

编码沿用模型证据的类型标签；解码只恢复 Core 0.6 的明确内容块和检查点，
不根据名字导入类、不恢复客户端、工具实现或任意 Python 实例。SQL 媒体
交接复用 Store 同一连接：调用方开启事务，保存检查点和媒体关系，再提交。
本模块不打开连接，不拥有会话，也不将未提交候选装入 Conversation。
"""

from __future__ import annotations

import base64
import json
import math
import sqlite3
from collections.abc import Mapping
from typing import cast

from purrcept_core.models import (
    AudioBlock,
    AudioBytes,
    ContentBlock,
    ConversationCheckpoint,
    ConversationState,
    ImageBlock,
    ImageBytes,
    ImageUrl,
    JsonValue,
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

from purrcept_engine.errors import StorageError
from purrcept_engine.storage.model_requests import encode, intern_core_media, restore_core_media


class CheckpointError(StorageError):
    """检查点材料缺失、不支持或损坏，不能替换为一个空会话。"""

    def __init__(self, message: str) -> None:
        """统一暴露已知检查点损坏原因，不回显完整正文或隐藏材料。"""
        super().__init__(message, code="invalid_checkpoint")


def encode_checkpoint(checkpoint: ConversationCheckpoint) -> str:
    """编码完整公开状态，未知扩展内容块明确失败，不静默忽略。

    history 保留完整消息、工具关联、隐藏 reasoning、continuation 及真实轮次
    边界；reminders 保留顺序、作用域、位置和优先级。配置和资源不在状态内。
    """
    encoded = encode(checkpoint)
    decode_checkpoint(encoded)
    return encoded


def decode_checkpoint(encoded: str) -> ConversationCheckpoint:
    """严格恢复 Core 值；字段和标签封闭，Core 构造器再次核验跨字段约束。"""
    try:
        value: object = json.loads(
            encoded, parse_constant=_reject_constant, object_pairs_hook=_unique_fields
        )
        root = _record(value, "ConversationCheckpoint", "history", "reminders")
        history = _record(
            root["history"],
            "ConversationState",
            "messages",
            "continuation",
            "turn_index",
            "turn_boundaries",
        )
        reminders = _record(root["reminders"], "ReminderState", "reminders")
        state = ConversationState(
            tuple(_message(item) for item in _list(history["messages"])),
            continuation=_continuation(history["continuation"]),
            turn_index=_integer(history["turn_index"]),
            turn_boundaries=tuple(_integer(item) for item in _list(history["turn_boundaries"])),
        )
        original_reminders = tuple(_reminder(item) for item in _list(reminders["reminders"]))
        reminder_state = ReminderState(original_reminders)
        if reminder_state.reminders != original_reminders:
            raise CheckpointError("检查点提醒含重复身份，恢复不能改变既有顺序或内容。")
        return ConversationCheckpoint(state, reminder_state)
    except CheckpointError:
        raise
    except (ValueError, TypeError, RecursionError) as error:
        # 此处只翻译真实 JSON/公开 Core 值边界，不捕获内部程序异常。
        raise CheckpointError("检查点不满足公开 Core 值约束。") from error


def decode_message(encoded: str) -> Message:
    """封闭恢复真实交付的完整 Core Message，不恢复任意类或资源实例。"""
    try:
        value: object = json.loads(
            encoded, parse_constant=_reject_constant, object_pairs_hook=_unique_fields
        )
        return _message(value)
    except CheckpointError:
        raise
    except (ValueError, TypeError, RecursionError) as error:
        raise CheckpointError("交付 Message 不满足公开 Core 值约束。") from error


def decode_content(encoded: str) -> tuple[ContentBlock, ...]:
    """封闭恢复真实工具交付的全部内容块，保持原顺序及原生媒体。"""
    try:
        value: object = json.loads(
            encoded, parse_constant=_reject_constant, object_pairs_hook=_unique_fields
        )
        return tuple(_block(item) for item in _list(value))
    except CheckpointError:
        raise
    except (ValueError, TypeError, RecursionError) as error:
        raise CheckpointError("交付内容不满足公开 Core 值约束。") from error


def intern_checkpoint(
    connection: sqlite3.Connection,
    encoded: str,
) -> tuple[str, tuple[str, ...]]:
    """在调用方事务内保存媒体并返回缩为引用的状态及 media_ids。

    输入须为 encode_checkpoint 的原材料。调用方同时保存返回状态和引用关系；
    任一步失败由调用方回滚整个事务。本函数不 BEGIN、commit 或创建连接。
    """
    decode_checkpoint(encoded)
    return intern_core_media(connection, encoded)


def restore_checkpoint(connection: sqlite3.Connection, encoded: str) -> ConversationCheckpoint:
    """借用同一读取事务还原媒体及完整状态，缺失 BLOB 不假造空字节。"""
    # 媒体恢复会重新编码 JSON；先拒绝原始歧义键，不能让重新编码抹去损坏。
    try:
        json.loads(encoded, parse_constant=_reject_constant, object_pairs_hook=_unique_fields)
    except (ValueError, TypeError, RecursionError) as error:
        raise CheckpointError("已保存检查点 JSON 无效。") from error
    return decode_checkpoint(restore_core_media(connection, encoded))


def _record(value: object, tag: str, *fields: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CheckpointError("检查点对象结构无效。")
    item = cast(Mapping[str, object], value)
    if item.get("type") != tag or set(item) != {"type", *fields}:
        raise CheckpointError(f"检查点 {tag} 标签或字段无效。")
    return item


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise CheckpointError("检查点列表字段无效。")
    return cast(list[object], value)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise CheckpointError("检查点字符串字段无效。")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CheckpointError("检查点整数不能被隐式转换。")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise CheckpointError("检查点布尔字段无效。")
    return value


def _reject_constant(value: str) -> object:
    raise CheckpointError("检查点 JSON 不能含非有限数字。")


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """拒绝歧义 JSON 键，不能依赖解析器静默选择最后一个身份字段。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CheckpointError("检查点 JSON 包含重复字段。")
        result[key] = value
    return result


def _json(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_json(item) for item in cast(list[object], value)]
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in cast(Mapping[object, object], value).items():
            if not isinstance(key, str):
                raise CheckpointError("检查点 JSON 对象键必须是字符串。")
            result[key] = _json(item)
        return result
    raise CheckpointError("检查点元数据不是有限 JSON。")


def _json_object(value: object) -> Mapping[str, JsonValue]:
    result = _json(value)
    if not isinstance(result, Mapping):
        raise CheckpointError("检查点元数据必须是 JSON 对象。")
    return result


def _bytes(value: object) -> bytes:
    item = _record(value, "bytes", "base64")
    text = _text(item["base64"])
    content = base64.b64decode(text, validate=True)
    if base64.b64encode(content).decode("ascii") != text:
        raise CheckpointError("检查点字节编码不是规范 base64。")
    return content


def _message(value: object) -> Message:
    item = _record(value, "Message", "role", "content", "name", "metadata")
    return Message(
        MessageRole(_text(item["role"])),
        tuple(_block(block) for block in _list(item["content"])),
        name=_optional_text(item["name"]),
        metadata=_json_object(item["metadata"]),
    )


def _block(value: object) -> ContentBlock:
    """仅逐个构造公开的内容类型，工具参数始终作为普通 JSON 保留。"""
    if not isinstance(value, Mapping):
        raise CheckpointError("检查点内容块结构无效。")
    raw = cast(Mapping[str, object], value)
    tag = raw.get("type")
    if tag in ("TextBlock", "ReasoningBlock"):
        item = _record(raw, cast(str, tag), "text")
        return (
            TextBlock(_text(item["text"]))
            if tag == "TextBlock"
            else ReasoningBlock(_text(item["text"]))
        )
    if tag == "ToolCallBlock":
        item = _record(raw, "ToolCallBlock", "id", "name", "arguments")
        return ToolCallBlock(
            _text(item["id"]), _text(item["name"]), arguments=_json_object(item["arguments"])
        )
    if tag == "ToolResultBlock":
        item = _record(raw, "ToolResultBlock", "tool_call_id", "content", "is_error")
        return ToolResultBlock(
            _text(item["tool_call_id"]),
            content=tuple(_block(block) for block in _list(item["content"])),
            is_error=_boolean(item["is_error"]),
        )
    if tag == "ImageBlock":
        item = _record(raw, "ImageBlock", "source", "alt_text")
        raw_source = item["source"]
        if (
            isinstance(raw_source, Mapping)
            and cast(Mapping[str, object], raw_source).get("type") == "ImageUrl"
        ):
            source_item = _record(cast(object, raw_source), "ImageUrl", "url")
            return ImageBlock(
                ImageUrl(_text(source_item["url"])), alt_text=_optional_text(item["alt_text"])
            )
        source_item = _record(cast(object, raw_source), "ImageBytes", "data", "media_type")
        return ImageBlock(
            ImageBytes(_bytes(source_item["data"]), _text(source_item["media_type"])),
            alt_text=_optional_text(item["alt_text"]),
        )
    if tag == "AudioBlock":
        item = _record(raw, "AudioBlock", "source", "alt_text")
        source_item = _record(item["source"], "AudioBytes", "data", "media_type")
        return AudioBlock(
            AudioBytes(_bytes(source_item["data"]), _text(source_item["media_type"])),
            alt_text=_optional_text(item["alt_text"]),
        )
    raise CheckpointError("检查点包含未支持的内容块。")


def _continuation(value: object) -> ModelContinuation | None:
    if value is None:
        return None
    item = _record(value, "ModelContinuation", "provider", "data")
    return ModelContinuation(_text(item["provider"]), _json_object(item["data"]))


def _reminder(value: object) -> SystemReminder:
    item = _record(value, "SystemReminder", "content", "key", "scope", "placement", "priority")
    return SystemReminder(
        tuple(_block(block) for block in _list(item["content"])),
        key=_optional_text(item["key"]),
        scope=ReminderScope(_text(item["scope"])),
        placement=ReminderPlacement(_text(item["placement"])),
        priority=_integer(item["priority"]),
    )
