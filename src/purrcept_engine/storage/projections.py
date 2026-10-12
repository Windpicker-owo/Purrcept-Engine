"""从权威记录读取面板概况，不暴露完整提示、行动代码或媒体正文。

查询只由 Store 的数据库线程执行，固定条数与明确预览覆盖保证快照有限。
原始事实仍长期保留在所属记录中，页面查询既不确认可靠待办，也不更新设备。
实际业务状态来自同一连接；实时运行预算由拥有单调时钟的会话/活动服务补充。
"""

from __future__ import annotations

import sqlite3

from purrcept_engine.errors import StorageError
from purrcept_engine.execution.protocol import JsonObject, JsonValue

from . import activities, execution, subject


def runtime(connection: sqlite3.Connection) -> JsonObject:
    """读取积压、当前会话、有限最近行动和活动，不运行模型或回放工作。"""
    counts: list[tuple[object, ...]] = connection.execute(
        "SELECT s.stage,count(*),sum(i.logical_bytes) FROM pending_inputs p "
        "JOIN inputs i USING(input_id) JOIN input_processing s USING(input_id) GROUP BY s.stage"
    ).fetchall()
    pending: list[JsonValue] = [
        {"stage": _text(row[0]), "count": _integer(row[1]), "logical_bytes": _integer(row[2])}
        for row in counts
    ]
    sessions: list[JsonValue] = []
    for scope in ("foreground", "reflection"):
        record = subject.session(connection, scope)
        sessions.append(
            {"scope": scope, "version": record.version, "active_turn": record.active_turn}
        )
    action_rows: list[tuple[object, ...]] = connection.execute(
        "SELECT action_id FROM actions ORDER BY created_at DESC LIMIT 12"
    ).fetchall()
    recent: list[JsonValue] = []
    for row in action_rows:
        action = execution.get_action(connection, _text(row[0]))
        if action is None:
            raise StorageError("行动概况关联损坏。", code="invalid_record")
        recent.append(action_projection(action))
    active: list[JsonValue] = []
    for activity in activities.activity_records(connection):
        active.append(
            {
                "activity_id": activity.activity_id,
                "definition": activity.definition,
                "goal": activity.goal,
                "state": activity.state,
                "version": activity.version,
                "run_generation": activity.run_generation,
                "remaining_seconds": activity.remaining_seconds,
                "budget_basis": "committed_checkpoint",
                "needs_reconciliation": activity.needs_reconciliation,
                "timing_incomplete": activity.timing_incomplete,
                "last_command_id": activity.last_command_id,
            }
        )
    return {
        "pending_inputs": pending,
        "sessions": sessions,
        "recent_actions": recent,
        "activities": active,
    }


def action_projection(action: execution.ActionRecord) -> JsonObject:
    """用逐次真实状态表达部分结果，长正文只给明确截断的有限预览。"""
    return {
        "action_id": action.action_id,
        "state": action.state,
        "created_at": action.created_at,
        "finished_at": action.finished_at,
        "error": _preview(action.error),
        "calls": [
            {
                "call_id": call.call_id,
                "path": call.path,
                "state": call.state,
                "receipt_revision": call.receipt_revision,
                "started_at": call.started_at,
                "finished_at": call.finished_at,
                "error_type": call.error_type,
                "error": _preview(call.error),
                "result": _preview(call.result_json),
                "media_count": len(call.media),
            }
            for call in action.calls
        ],
    }


def _preview(value: str | None) -> JsonObject | None:
    if value is None:
        return None
    return {"text": value[:512], "truncated": len(value) > 512, "original_chars": len(value)}


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise StorageError("面板权威字段损坏。", code="invalid_record")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StorageError("面板权威计数字段损坏。", code="invalid_record")
    return value
