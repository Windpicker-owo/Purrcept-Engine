"""在 Store 的现有连接上分批淘汰模型流诊断正文。

模型请求、完整响应和原始参数仍由 model_requests 拥有。本模块只定义可淘汰的
独立诊断表与一次有界清理事务，不打开连接、不调度维护任务，也不删除任何
权威记录。Store 把清理函数交给自己的普通提交队列；宿主拥有调用频率与关闭。

原覆盖计数和正文摘要长期保留，因此过期后仍可解释缺口、核对重复提交。
过期时间是独立原因标记，不能把原先完整的流伪称为采集时就不完整。
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
from dataclasses import dataclass

from purrcept_engine.errors import StorageError
from purrcept_engine.execution.protocol import ActionProtocolError, decode_object

SCHEMA = (
    "CREATE TABLE model_attempt_diagnostics ("
    "attempt_id TEXT PRIMARY KEY REFERENCES model_attempts(attempt_id), "
    "events_json TEXT NOT NULL, original_events_digest TEXT NOT NULL, "
    "diagnostics_complete INTEGER NOT NULL CHECK(diagnostics_complete IN (0,1)), "
    "omitted_events INTEGER NOT NULL CHECK(omitted_events >= 0), "
    "created_at REAL NOT NULL, expired_at REAL)",
    "CREATE INDEX model_attempt_diagnostics_retained ON model_attempt_diagnostics "
    "(created_at,attempt_id) WHERE expired_at IS NULL",
)


@dataclass(frozen=True, slots=True)
class DiagnosticCleanup:
    """一次成功清理的实际范围；has_more 表示仍有超过时间边界的诊断。

    expired_attempts 包含原本为空的诊断，removed_events/removed_bytes 只计被
    删除的正文。宿主可在下次维护继续；返回值不授予无限循环或额外连接。
    """

    expired_attempts: int
    removed_events: int
    removed_bytes: int
    has_more: bool


def diagnostic_events_digest(events_json: str) -> str:
    """核验诊断数组并摘要原 UTF-8 文本，供过期后的重复提交核对。

    不重新排列或重编码 JSON。摘要身份对应 finish_attempt 实际交接的文本，
    即使两份材料解析为相同值，不同原文本仍不能冒充原提交。
    """
    try:
        encoded = events_json.encode("utf-8")
        events = decode_object(b'{"events":' + encoded + b"}")["events"]
    except (UnicodeEncodeError, ActionProtocolError) as error:
        raise StorageError("模型流诊断 JSON 损坏。", code="invalid_diagnostics") from error
    if not isinstance(events, list):
        raise StorageError("模型流诊断必须是 JSON 数组。", code="invalid_diagnostics")
    return hashlib.sha256(encoded).hexdigest()


def expire_diagnostics(
    connection: sqlite3.Connection,
    *,
    before: float,
    expired_at: float,
    limit: int = 128,
) -> DiagnosticCleanup:
    """在同一事务清除至多 limit 份早于 before 的诊断，保留独立过期标记。

    调用方必须串行借用 Store 所有的空闲连接；本函数拥有本次事务，不可嵌入
    另一未结束事务。before/expired_at 是 Unix 时间，等于边界的诊断仍保留。
    原覆盖计数、摘要、尝试结果与媒体引用不修改。失败回滚并原样传播，不能
    将清理失败当作已经清理；宿主可记录维护故障而不改变业务结果。
    """
    if (
        isinstance(before, bool)
        or isinstance(expired_at, bool)
        or not math.isfinite(before)
        or not math.isfinite(expired_at)
        or expired_at < before
    ):
        raise ValueError("诊断清理需要有限且有序的时间边界。")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("诊断清理批量必须是正整数。")

    with connection:
        connection.execute("BEGIN IMMEDIATE")
        rows: list[tuple[object, ...]] = connection.execute(
            "SELECT attempt_id,events_json,json_array_length(events_json),json_type(events_json) "
            "FROM model_attempt_diagnostics WHERE created_at < ? AND expired_at IS NULL "
            "ORDER BY created_at,attempt_id LIMIT ?",
            (before, limit),
        ).fetchall()

        removed_events = 0
        removed_bytes = 0
        for row in rows:
            attempt_id, events_json, event_count, event_type = row
            if (
                not isinstance(attempt_id, str)
                or not isinstance(events_json, str)
                or isinstance(event_count, bool)
                or not isinstance(event_count, int)
                or event_count < 0
                or event_type != "array"
            ):
                raise StorageError("模型流诊断记录损坏。", code="invalid_diagnostics")

            # 保留原摘要和覆盖事实，使正文清理不破坏结果的幂等身份。
            connection.execute(
                "UPDATE model_attempt_diagnostics SET events_json='[]',expired_at=? "
                "WHERE attempt_id=?",
                (expired_at, attempt_id),
            )
            removed_events += event_count
            if event_count:
                removed_bytes += len(events_json.encode("utf-8"))

        remaining: tuple[object, ...] = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM model_attempt_diagnostics "
            "WHERE created_at < ? AND expired_at IS NULL)",
            (before,),
        ).fetchone()
        return DiagnosticCleanup(len(rows), removed_events, removed_bytes, remaining == (1,))
