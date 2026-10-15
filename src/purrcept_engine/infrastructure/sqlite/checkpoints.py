"""在调用方同一事务保存、恢复检查点媒体；不推进会话或提交事务。"""

from __future__ import annotations

import sqlite3

from purrcept_core.models import (
    ConversationCheckpoint,
)

from purrcept_engine.domain.context.checkpoints import decode_checkpoint, validate_checkpoint_json
from purrcept_engine.infrastructure.sqlite.model_requests import (
    intern_core_media,
    restore_core_media,
)


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
    validate_checkpoint_json(encoded)
    return decode_checkpoint(restore_core_media(connection, encoded))
