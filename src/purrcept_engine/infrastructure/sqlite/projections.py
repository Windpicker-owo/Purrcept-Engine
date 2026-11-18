"""从权威记录读取面板概况与本机对话流水，不暴露模型原始上下文或未授权媒体。

查询只由 Store 的数据库线程执行，固定条数与明确预览覆盖保证快照有限。
原始事实仍长期保留在所属记录中，页面查询既不确认可靠待办，也不更新设备。
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import cast

import purrcept_engine.domain.execution.models as execution_models
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite import activities, execution
from purrcept_engine.infrastructure.sqlite.rows import as_int, as_text
from purrcept_engine.values import JsonObject, JsonValue, decode_object


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
    for scope in ("foreground", "behavior_review"):
        row = connection.execute(
            "SELECT version,active_turn FROM subject_sessions WHERE scope=?", (scope,)
        ).fetchone()
        sessions.append(
            {
                "scope": scope,
                "version": 0 if row is None else _integer(row[0]),
                "active_turn": None if row is None else _optional_str(row[1]),
            }
        )
    action_rows: list[tuple[object, ...]] = connection.execute(
        "SELECT action_id FROM actions ORDER BY created_at DESC,action_id DESC LIMIT 12"
    ).fetchall()
    recent: list[JsonValue] = []
    for row in action_rows:
        action = execution.get_action(connection, _text(row[0]))
        if action is None:
            raise StorageError("行动概况关联损坏。", code="invalid_record")
        recent.append(action_projection(action))
    active: list[JsonValue] = []
    for activity in activities.activity_records(connection):
        command = activities.get_activity_command(connection, activity.last_command_id)
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
                "progress_preview": _preview(activity.progress_json),
                "last_command": None
                if command is None
                else {
                    "command_id": command.command_id,
                    "command": command.command,
                    "status": command.status,
                    "actual_state": command.actual_state,
                    "applied": command.applied,
                    "error": _preview(command.error),
                },
            }
        )
    statistics = connection.execute(
        "SELECT payload_json FROM storage_statistics WHERE id=1"
    ).fetchone()
    observations = connection.execute(
        "SELECT n.id,n.state,o.opportunities,o.state,"
        "json_extract(n.payload_json,'$.expires_at') FROM supervision_outcomes o "
        "JOIN knowledge_supervision n ON n.id=o.notice_id ORDER BY "
        "n.created_at DESC,n.id DESC LIMIT 32"
    ).fetchall()
    return {
        "supervision_observations": [
            {
                "notice_id": identity,
                "adoption": adoption,
                "opportunities": opportunities,
                "outcome": outcome,
                "observation_state": "opportunities_exhausted"
                if opportunities >= 20
                else "expired"
                if expires is not None and expires <= time.time()
                else "observing",
                "expires_at": expires,
            }
            for identity, adoption, opportunities, outcome, expires in observations
        ],
        "storage_statistics": None
        if statistics is None
        else decode_object(_text(statistics[0]).encode()),
        "pending_inputs": pending,
        "sessions": sessions,
        "recent_actions": recent,
        "activities": active,
    }


def chat_feed(connection: sqlite3.Connection, *, limit: int = 40) -> JsonObject:
    """读取最近的用户刺激与主体回复/能力行动流水，供 Web 对话调试台展示。"""
    bounded = max(1, min(limit, 80))
    input_rows: list[tuple[object, ...]] = connection.execute(
        "SELECT i.input_id,i.adapter_id,i.conversation_id,i.display_name,i.text,"
        "i.occurred_at,i.received_at,p.stage,p.reason "
        "FROM inputs i LEFT JOIN input_processing p USING(input_id) "
        "WHERE i.adapter_id != 'subject.idle' "
        "ORDER BY i.rowid DESC LIMIT ?",
        (bounded,),
    ).fetchall()
    input_rows.reverse()

    turn_by_input: dict[str, str] = {}
    for row in input_rows:
        iid = _text(row[0])
        turn_row = connection.execute(
            "SELECT t.turn_id FROM turn_inputs ti JOIN subject_turns t USING(turn_id) "
            "WHERE ti.input_id=? AND t.scope='foreground' "
            "ORDER BY t.started_at DESC LIMIT 1",
            (iid,),
        ).fetchone()
        if turn_row is not None and isinstance(turn_row[0], str):
            turn_by_input[iid] = turn_row[0]

    inputs_out: list[JsonValue] = [
        {
            "input_id": _text(row[0]),
            "adapter_id": _text(row[1]),
            "conversation_id": _text(row[2]),
            "display_name": _optional_str(row[3]),
            "text": _text(row[4]),
            "occurred_at": _text(row[5]),
            "received_at": _text(row[6]),
            "stage": _optional_str(row[7]) or "unknown",
            "reason": _optional_str(row[8]),
            "turn_id": turn_by_input.get(_text(row[0])),
        }
        for row in input_rows
    ]

    action_rows: list[tuple[object, ...]] = connection.execute(
        "SELECT action_id FROM actions ORDER BY created_at DESC,action_id DESC LIMIT ?",
        (bounded,),
    ).fetchall()
    action_rows.reverse()
    actions_out: list[JsonValue] = []
    for (raw_aid,) in action_rows:
        action = execution.get_action(connection, _text(raw_aid))
        if action is None:
            continue
        replies: list[JsonValue] = []
        calls_summary: list[JsonValue] = []
        for call in action.calls:
            reply_info = _extract_reply(call)
            if reply_info is not None:
                replies.append(reply_info)
            calls_summary.append(
                {
                    "call_id": call.call_id,
                    "path": call.path,
                    "state": call.state,
                    "error": _preview(call.error),
                }
            )
        actions_out.append(
            {
                "action_id": action.action_id,
                "input_id": action.input_id,
                "turn_id": None if action.input_id is None else turn_by_input.get(action.input_id),
                "state": action.state,
                "created_at": action.created_at,
                "finished_at": action.finished_at,
                "error": _preview(action.error),
                "replies": replies,
                "calls": calls_summary,
            }
        )
    return {"inputs": inputs_out, "actions": actions_out}


def _extract_reply(call: execution_models.CallRecord) -> JsonObject | None:
    """仅把已确认的表达列作发言；其他尝试仍在同一行动的逐次调用列表中。"""
    if call.state != "confirmed":
        return None
    # 路径段由用户配置。目标身份保留内置对象种类，末段是实际调用的能力。
    target = call.target_id.rsplit(":", 1)[-1]
    operation = call.path.rsplit(".", 1)[-1]
    if (target, operation) == ("console", "output"):
        channel = "console"
    elif (target, operation) == ("microphone", "speak"):
        # 接管或中断播放都不能证明完整的这段文字已播完。
        playback = cast(JsonValue, json.loads(call.result_json)) if call.result_json else None
        if not isinstance(playback, dict) or playback.get("completed") is not True:
            return None
        channel = "voice"
    elif (target, operation) == ("qq", "send"):
        channel = "qq"
    elif (target, operation) == ("chat", "send"):
        channel = "phone"
    else:
        return None
    text = decode_object(call.arguments_json.encode()).get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return {
        "call_id": call.call_id,
        "channel": channel,
        "path": call.path,
        "state": call.state,
        "text": text[:4000],
        "truncated": len(text) > 4000,
    }


def action_projection(action: execution_models.ActionRecord) -> JsonObject:
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


_text = as_text


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


_integer = as_int
