"""在宿主唯一 SQLite 连接中保存活动控制、运行累计和可靠反馈。

ActivityService 是状态提交者；Store 的数据库线程调用本模块的短事务函数。
这里不创建连接、不等待驱动、不启动活动。控制接管、驱动受理和真实确认分别
保存，迟到确认可以补齐控制事实，但不能自行改写当前生命周期。累计值通过
版本比较整值提交，避免迟到检查点重复加时。业务反馈另存原文与双消费者待办。
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from typing import Literal

from purrcept_engine.domain.activities.models import (
    ActivityCommand,
    ActivityFeedback,
    ActivityRecord,
    ActivityState,
    CommandCause,
    CommandKind,
    CommandStatus,
    FeedbackKind,
)
from purrcept_engine.domain.execution.record_validation import validate_json
from purrcept_engine.errors import StorageError

_STATES: tuple[ActivityState, ...] = (
    "starting",
    "running",
    "pause_requested",
    "paused",
    "resume_requested",
    "end_requested",
    "ended",
    "completed",
    "adjust_requested",
    "budget_exhausted",
    "interrupted",
    "awaiting_decision",
)
_COMMANDS: tuple[CommandKind, ...] = ("start", "pause", "resume", "end", "adjust", "extend")
_STATUSES: tuple[CommandStatus, ...] = ("admitted", "accepted", "confirmed", "rejected", "unknown")
_CAUSES: tuple[CommandCause, ...] = ("explicit", "budget", "shutdown")
_FEEDBACKS: tuple[FeedbackKind, ...] = (
    "progress",
    "completed",
    "failed",
    "needs_decision",
    "reconciled",
)

SCHEMA = (
    "CREATE TABLE activities (activity_id TEXT PRIMARY KEY, definition TEXT NOT NULL, "
    "goal TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL CHECK(version > 0), "
    "run_generation INTEGER NOT NULL CHECK(run_generation >= 0), "
    "granted_seconds REAL NOT NULL CHECK(granted_seconds > 0), "
    "used_seconds REAL NOT NULL CHECK(used_seconds >= 0), checkpoint_at REAL, "
    "clock_running INTEGER NOT NULL CHECK(clock_running IN (0,1)), "
    "timing_incomplete INTEGER NOT NULL CHECK(timing_incomplete IN (0,1)), "
    "needs_reconciliation INTEGER NOT NULL CHECK(needs_reconciliation IN (0,1)), "
    "last_command_id TEXT NOT NULL, work_json TEXT NOT NULL, progress_json TEXT, "
    "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
    "control_version INTEGER NOT NULL CHECK(control_version > 0))",
    "CREATE TABLE activity_commands (command_id TEXT PRIMARY KEY, "
    "activity_id TEXT NOT NULL REFERENCES activities(activity_id), command TEXT NOT NULL, "
    "definition TEXT, goal TEXT, additional_seconds REAL, reason TEXT, cause TEXT NOT NULL, "
    "prior_state TEXT NOT NULL, run_generation INTEGER NOT NULL, version INTEGER NOT NULL, "
    "status TEXT NOT NULL, admitted_at REAL NOT NULL, accepted_at REAL, confirmed_at REAL, "
    "actual_state TEXT, applied INTEGER CHECK(applied IN (0,1)), error_type TEXT, error TEXT)",
    "CREATE TABLE activity_feedback (feedback_id TEXT PRIMARY KEY, "
    "activity_id TEXT NOT NULL REFERENCES activities(activity_id), command_id TEXT, "
    "run_generation INTEGER NOT NULL, version INTEGER NOT NULL, kind TEXT NOT NULL, "
    "payload_json TEXT NOT NULL, occurred_at REAL NOT NULL, applied INTEGER NOT NULL "
    "CHECK(applied IN (0,1)))",
    "CREATE TABLE pending_activity_feedback (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
    "feedback_id TEXT UNIQUE NOT NULL REFERENCES activity_feedback(feedback_id), "
    "foreground INTEGER NOT NULL CHECK(foreground IN (0,1)), "
    "reflection INTEGER NOT NULL CHECK(reflection IN (0,1)))",
)

_ACTIVITY_COLUMNS = (
    "activity_id, definition, goal, state, version, run_generation, granted_seconds, "
    "used_seconds, checkpoint_at, clock_running, timing_incomplete, needs_reconciliation, "
    "last_command_id, work_json, progress_json, created_at, updated_at, control_version"
)
_COMMAND_COLUMNS = (
    "command_id, activity_id, command, definition, goal, additional_seconds, reason, cause, "
    "prior_state, run_generation, version, status, admitted_at, accepted_at, confirmed_at, "
    "actual_state, applied, error_type, error"
)
_FEEDBACK_COLUMNS = (
    "feedback_id, activity_id, command_id, run_generation, version, kind, payload_json, "
    "occurred_at, applied"
)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise StorageError("活动记录文本字段无效。", code="invalid_record")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise StorageError("活动记录数值字段无效。", code="invalid_record")
    return float(value)


def _optional_number(value: object) -> float | None:
    return None if value is None else _number(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StorageError("活动记录计数字段无效。", code="invalid_record")
    return value


def _boolean(value: object) -> bool:
    if value not in (0, 1) or isinstance(value, float):
        raise StorageError("活动记录布尔字段无效。", code="invalid_record")
    return bool(value)


def _state(value: object) -> ActivityState:
    for state in _STATES:
        if value == state:
            return state
    raise StorageError("活动记录生命周期无效。", code="invalid_record")


def _json(value: object) -> str:
    text = _text(value)
    try:
        validate_json(text)
    except ValueError as error:
        raise StorageError("活动记录 JSON 无效。", code="invalid_record") from error
    return text


def _activity(row: Sequence[object]) -> ActivityRecord:
    """读取是权威边界；拒绝坏累计，不用零或当前时间补齐。"""
    record = ActivityRecord(
        _text(row[0]),
        _text(row[1]),
        _text(row[2]),
        _state(row[3]),
        _integer(row[4]),
        _integer(row[5]),
        _number(row[6]),
        _number(row[7]),
        _optional_number(row[8]),
        _boolean(row[9]),
        _boolean(row[10]),
        _boolean(row[11]),
        _text(row[12]),
        _json(row[13]),
        None if row[14] is None else _json(row[14]),
        _number(row[15]),
        _number(row[16]),
        _integer(row[17]),
    )
    if (
        record.version == 0
        or record.granted_seconds <= 0
        or record.used_seconds < 0
        or not 0 < record.control_version <= record.version
    ):
        raise StorageError("活动版本或运行累计无效。", code="invalid_record")
    return record


def _command(row: Sequence[object]) -> ActivityCommand:
    """从同一权威行恢复命令字段，损坏记录必须明确失败。"""
    kind: CommandKind | None = next((kind for kind in _COMMANDS if row[2] == kind), None)
    cause: CommandCause | None = next((cause for cause in _CAUSES if row[7] == cause), None)
    status: CommandStatus | None = next((status for status in _STATUSES if row[11] == status), None)
    if kind is None or cause is None or status is None:
        raise StorageError("活动命令类别或状态无效。", code="invalid_record")
    return ActivityCommand(
        _text(row[0]),
        _text(row[1]),
        kind,
        _optional_text(row[3]),
        _optional_text(row[4]),
        _optional_number(row[5]),
        _optional_text(row[6]),
        cause,
        _state(row[8]),
        _integer(row[9]),
        _integer(row[10]),
        status,
        _number(row[12]),
        _optional_number(row[13]),
        _optional_number(row[14]),
        None if row[15] is None else _state(row[15]),
        None if row[16] is None else _boolean(row[16]),
        _optional_text(row[17]),
        _optional_text(row[18]),
    )


def _feedback(row: Sequence[object]) -> ActivityFeedback:
    kind: FeedbackKind | None = next((kind for kind in _FEEDBACKS if row[5] == kind), None)
    if kind is None:
        raise StorageError("活动反馈类别无效。", code="invalid_record")
    return ActivityFeedback(
        _text(row[0]),
        _text(row[1]),
        _optional_text(row[2]),
        _integer(row[3]),
        _integer(row[4]),
        kind,
        _json(row[6]),
        _number(row[7]),
        _boolean(row[8]),
    )


def get_activity(connection: sqlite3.Connection, activity_id: str) -> ActivityRecord | None:
    """查询当前生命周期与累计快照，不创建或恢复驱动。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        activity_id: 已创建活动的稳定身份。

    Returns:
        活动的控制、预算、运行代次与工作快照。 未取得对应记录或素材时返回 None；读取故障仍抛出。
    """
    row: tuple[object, ...] | None = connection.execute(
        f"SELECT {_ACTIVITY_COLUMNS} FROM activities WHERE activity_id = ?",
        (activity_id,),
    ).fetchone()
    return None if row is None else _activity(row)


def activity_records(connection: sqlite3.Connection) -> tuple[ActivityRecord, ...]:
    """只读取尚未结束的委托。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。

    Returns:
        条目集合：活动的控制、预算、运行代次与工作快照。

    Note:
        活跃上限同时约束服务启动时的内存集合。
    """
    rows: list[tuple[object, ...]] = connection.execute(
        f"SELECT {_ACTIVITY_COLUMNS} FROM activities WHERE state NOT IN ('ended','completed') "
        "ORDER BY created_at, activity_id",
    ).fetchall()
    return tuple(_activity(row) for row in rows)


def get_activity_command(
    connection: sqlite3.Connection,
    command_id: str,
) -> ActivityCommand | None:
    """读取同一命令的接管、受理与真实确认，不把查询转换为重投。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        command_id: 控制命令的稳定身份；重复提交须沿用原身份和内容。

    Returns:
        命令接管与实际确认记录；受理成功不等于驱动已完成。 未取得对应记录或素材时返回 None；
        读取故障仍抛出。
    """
    row: tuple[object, ...] | None = connection.execute(
        f"SELECT {_COMMAND_COLUMNS} FROM activity_commands WHERE command_id = ?",
        (command_id,),
    ).fetchone()
    return None if row is None else _command(row)


def get_activity_feedback(
    connection: sqlite3.Connection, feedback_id: str
) -> ActivityFeedback | None:
    """查询已保存反馈的原身份，重投不会生成新时间或新应用结论。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        feedback_id: 驱动反馈的稳定身份，用于去重和查询实际确认。

    Returns:
        已经按身份去重并保存的真实驱动反馈。 未取得对应记录或素材时返回 None；读取故障仍抛出。
    """
    row: tuple[object, ...] | None = connection.execute(
        f"SELECT {_FEEDBACK_COLUMNS} FROM activity_feedback WHERE feedback_id = ?",
        (feedback_id,),
    ).fetchone()
    return None if row is None else _feedback(row)


def _command_identity(command: ActivityCommand) -> tuple[object, ...]:
    return (
        command.command_id,
        command.activity_id,
        command.command,
        command.definition,
        command.goal,
        command.additional_seconds,
        command.reason,
        command.cause,
        command.prior_state,
        command.run_generation,
        command.version,
        command.admitted_at,
    )


def commit_activity(
    connection: sqlite3.Connection,
    record: ActivityRecord,
    *,
    expected_version: int | None,
    command: ActivityCommand | None = None,
    feedback: ActivityFeedback | None = None,
) -> None:
    """原子比较并提交活动、控制结论与原始反馈，不在事务中等待驱动。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        record: 待查询、核对或提交的当前活动快照。
        expected_version: 提交前观察到的版本，用于拒绝并发覆盖。
        command: 与状态变化同事务保存的控制命令；None 表示本次没有命令。 None
            表示本次没有伴随命令。
        feedback: 与活动状态同事务保存的驱动反馈；None 表示没有新反馈。

    Raises:
        StorageError: 权威记录缺失、版本不一致、载荷无效或提交条件不成立。
        ValueError: 活动命令与提交活动身份不符；活动反馈与提交活动身份不符。

    Note:
        首次创建 expected_version 为 None，后续提交必须精确递增一个版本。累计
        只增不减；命令的接管身份不可改写。关键反馈的前台/反思待办与原文同事务。
        Store 应将此函数放入控制提交队列，使暂停不受普通 Provider 等待者阻塞。
    """
    validate_json(record.work_json)
    validate_json(record.progress_json)
    if record.version != (1 if expected_version is None else expected_version + 1):
        raise StorageError("活动提交版本不连续。", code="activity_conflict")
    if command is not None and command.activity_id != record.activity_id:
        raise ValueError("活动命令与提交活动身份不符。")
    if feedback is not None:
        if feedback.activity_id != record.activity_id:
            raise ValueError("活动反馈与提交活动身份不符。")
        validate_json(feedback.payload_json)

    with connection:
        current = get_activity(connection, record.activity_id)
        if (current is None) != (expected_version is None) or (
            current is not None and current.version != expected_version
        ):
            raise StorageError("活动当前版本已改变。", code="activity_conflict")
        if current is not None and (
            record.used_seconds < current.used_seconds
            or record.granted_seconds < current.granted_seconds
            or record.run_generation < current.run_generation
            or (current.state in ("ended", "completed") and record.state != current.state)
        ):
            raise StorageError("活动累计、代次或终态不能回退。", code="activity_conflict")
        # Upsert 保持父行身份，不能用会先删除父行的 REPLACE 影响反馈和命令外键。
        assignments = ", ".join(
            name.strip() + "=excluded." + name.strip() for name in _ACTIVITY_COLUMNS.split(",")[1:]
        )
        connection.execute(
            f"INSERT INTO activities ({_ACTIVITY_COLUMNS}) "
            f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            f"ON CONFLICT(activity_id) DO UPDATE SET {assignments}",
            (
                record.activity_id,
                record.definition,
                record.goal,
                record.state,
                record.version,
                record.run_generation,
                record.granted_seconds,
                record.used_seconds,
                record.checkpoint_at,
                int(record.clock_running),
                int(record.timing_incomplete),
                int(record.needs_reconciliation),
                record.last_command_id,
                record.work_json,
                record.progress_json,
                record.created_at,
                record.updated_at,
                record.control_version,
            ),
        )
        if command is not None:
            previous = get_activity_command(connection, command.command_id)
            if previous is not None and _command_identity(previous) != _command_identity(command):
                raise StorageError("活动命令身份被重复用于不同接管。", code="activity_conflict")
            if previous is not None and previous.status in ("confirmed", "rejected"):
                if previous != command:
                    raise StorageError("活动已确认命令不能改写。", code="activity_conflict")
            connection.execute(
                "INSERT INTO activity_commands VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(command_id) DO UPDATE SET status=excluded.status, "
                "accepted_at=excluded.accepted_at, confirmed_at=excluded.confirmed_at, "
                "actual_state=excluded.actual_state, applied=excluded.applied, "
                "error_type=excluded.error_type, error=excluded.error",
                (
                    command.command_id,
                    command.activity_id,
                    command.command,
                    command.definition,
                    command.goal,
                    command.additional_seconds,
                    command.reason,
                    command.cause,
                    command.prior_state,
                    command.run_generation,
                    command.version,
                    command.status,
                    command.admitted_at,
                    command.accepted_at,
                    command.confirmed_at,
                    command.actual_state,
                    None if command.applied is None else int(command.applied),
                    command.error_type,
                    command.error,
                ),
            )
        if feedback is not None:
            row: tuple[object, ...] | None = connection.execute(
                f"SELECT {_FEEDBACK_COLUMNS} FROM activity_feedback WHERE feedback_id = ?",
                (feedback.feedback_id,),
            ).fetchone()
            if row is not None and _feedback(row) != feedback:
                raise StorageError("活动反馈身份冲突。", code="activity_conflict")
            if row is None:
                connection.execute(
                    "INSERT INTO activity_feedback VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        feedback.feedback_id,
                        feedback.activity_id,
                        feedback.command_id,
                        feedback.run_generation,
                        feedback.version,
                        feedback.kind,
                        feedback.payload_json,
                        feedback.occurred_at,
                        int(feedback.applied),
                    ),
                )
                if feedback.kind in ("completed", "failed", "needs_decision"):
                    connection.execute(
                        "INSERT INTO pending_activity_feedback "
                        "(feedback_id, foreground, reflection) VALUES (?,1,1)",
                        (feedback.feedback_id,),
                    )


def recover_activities(connection: sqlite3.Connection, *, now: float) -> tuple[ActivityRecord, ...]:
    """保留最后累计与未确认命令，将离线活动标为待核对，不扣离线时长。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        now: 本次规则计算采用的时刻，便于同一操作保持一致。

    Returns:
        条目集合：活动的控制、预算、运行代次与工作快照。

    Note:
        运行或控制中断变为 interrupted；已暂停/耗尽状态保留但同样要求核对。
        未落盘末段仅标缺口，不按离线时间或检查点间隔推算。这里从不重发命令。
    """
    with connection:
        for current in activity_records(connection):
            interrupted = current.clock_running or current.state in (
                "starting",
                "resume_requested",
                "pause_requested",
                "end_requested",
                "adjust_requested",
            )
            record = replace(
                current,
                state="interrupted" if interrupted else current.state,
                version=current.version + 1,
                clock_running=False,
                timing_incomplete=current.timing_incomplete or current.clock_running,
                needs_reconciliation=True,
                control_version=current.version + 1,
                updated_at=now,
            )
            connection.execute(
                "UPDATE activities SET state=?, version=?, clock_running=0, "
                "timing_incomplete=?, needs_reconciliation=1, updated_at=?, control_version=? "
                "WHERE activity_id=?",
                (
                    record.state,
                    record.version,
                    int(record.timing_incomplete),
                    now,
                    record.control_version,
                    record.activity_id,
                ),
            )
    return activity_records(connection)


def pending_activity_feedback(
    connection: sqlite3.Connection,
    consumer: Literal["foreground", "reflection"],
    *,
    limit: int = 8,
) -> tuple[ActivityFeedback, ...]:
    """分别读取消费者的可靠待办，查询不确认已处理。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        consumer: 本次可靠反馈交接的消费者，前台与反思分别维护游标。
        limit: 本次读取或更新的最大条目数。 默认 8。

    Returns:
        条目集合：已经按身份去重并保存的真实驱动反馈。

    Raises:
        ValueError: 活动反馈消费者或读取额度无效。
    """
    if (
        consumer not in ("foreground", "reflection")
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit <= 0
    ):
        raise ValueError("活动反馈消费者或读取额度无效。")
    columns = ", ".join("f." + column.strip() for column in _FEEDBACK_COLUMNS.split(","))
    rows: list[tuple[object, ...]] = connection.execute(
        f"SELECT {columns} FROM pending_activity_feedback p JOIN activity_feedback f "
        f"ON f.feedback_id = p.feedback_id WHERE p.{consumer}=1 ORDER BY p.sequence LIMIT ?",
        (limit,),
    ).fetchall()
    return tuple(_feedback(row) for row in rows)


def acknowledge_activity_feedback(
    connection: sqlite3.Connection,
    feedback_id: str,
    consumer: Literal["foreground", "reflection"],
) -> None:
    """仅确认指定消费者交接。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        feedback_id: 驱动反馈的稳定身份，用于去重和查询实际确认。
        consumer: 本次可靠反馈交接的消费者，前台与反思分别维护游标。

    Raises:
        ValueError: 活动反馈消费者无效。

    Note:
        另一消费者和原始反馈保留。
    """
    if consumer not in ("foreground", "reflection"):
        raise ValueError("活动反馈消费者无效。")
    with connection:
        connection.execute(
            f"UPDATE pending_activity_feedback SET {consumer}=0 WHERE feedback_id=?",
            (feedback_id,),
        )
        connection.execute(
            "DELETE FROM pending_activity_feedback WHERE feedback_id=? "
            "AND foreground=0 AND reflection=0",
            (feedback_id,),
        )
