"""时钟定义、可靠闹铃交接与唯一主体休息状态的短事务。

本模块在Store专属连接执行SQL。闹铃的持久occurrence_id不是文本时间散列；
fire_alarm借用宿主原文接管函数，同事务消耗/推进闹铃并保存输入身份。单次
错过后补投一次，每日重启只推进下一次。休息到期没有自动状态转移或输出。
调度等待属于plugins/time，SQLite连接和全局接管额度始终属于Store。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta, tzinfo
from functools import cache
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ValidationError

from purrcept_engine.domain.inputs.models import (
    Admission,
    InputContext,
    InputSource,
    InputSubmission,
)
from purrcept_engine.domain.time.models import (
    Alarm,
    AlarmFiring,
    RestChange,
    RestState,
    TimeError,
    validate_aware_datetime,
)
from purrcept_engine.errors import StorageError
from purrcept_engine.values import JsonObject, encode_json

MAX_ALARMS = 32
_TIME_OF_DAY = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$")


class AdmitInput(Protocol):
    """Store注入的同连接接管函数，调用方拥有事务，不自行BEGIN或commit。

    实现须遵守同一输入去重、BLOB/pending/原文及全局积压额度；函数不发送
    模型或异步通知，返回意味着本事务已生成正式接管记录而非已经提交。
    """

    def __call__(self, connection: sqlite3.Connection, submission: InputSubmission) -> Admission:
        """把不变发生信封加入当前事务；任何拒收使外层闹铃事务全部回滚。"""
        ...


SCHEMA = (
    "CREATE TABLE clock_alarms(id TEXT PRIMARY KEY,clock_id TEXT NOT NULL,at_utc REAL NOT NULL, "
    "state TEXT NOT NULL CHECK(state IN('pending','consumed','cancelled')),"
    "payload_json TEXT NOT NULL)",
    "CREATE INDEX clock_alarms_due ON clock_alarms(clock_id,state,at_utc,id)",
    "CREATE TABLE alarm_firings(occurrence_id TEXT PRIMARY KEY,clock_id TEXT NOT NULL, "
    "alarm_id TEXT NOT NULL REFERENCES clock_alarms(id),input_id TEXT NOT NULL UNIQUE "
    "REFERENCES inputs(input_id),payload_json TEXT NOT NULL)",
    "CREATE TABLE subject_rest(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,"
    "payload_json TEXT)",
    "CREATE TABLE rest_changes(call_id TEXT PRIMARY KEY,operation TEXT NOT NULL,"
    "payload_json TEXT NOT NULL)",
)


@cache
def resolve_timezone(name: str) -> tzinfo:
    """解析明确IANA时区，UTC/GMT无需时区数据；缺数据不改用机器时区。"""
    if name in ("UTC", "GMT"):
        return UTC
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise TimeError("IANA时区不存在或缺少tzdata。", code="timezone") from error


def parse_alarm_at(value: str, *, timezone: str, now: datetime) -> datetime:
    """HH:MM(:SS)取下一次墙钟时刻，ISO无偏移按配置时区，过去ISO明确拒绝。"""
    validate_aware_datetime(now)
    if not isinstance(value, str) or not value.strip():
        raise TimeError("闹铃时间不能为空。", code="invalid_time")
    zone = resolve_timezone(timezone)
    current = now.astimezone(zone)
    text = value.strip()
    match = _TIME_OF_DAY.fullmatch(text)
    if match is not None:
        candidate = current.replace(
            hour=int(match[1]), minute=int(match[2]), second=int(match[3] or "0"), microsecond=0
        )
        return candidate + timedelta(days=1) if candidate <= current else candidate
    try:
        candidate = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        )
    except ValueError as error:
        raise TimeError("时间必须为HH:MM、HH:MM:SS或ISO日期时间。", code="invalid_time") from error
    candidate = (
        candidate.replace(tzinfo=zone)
        if candidate.utcoffset() is None
        else candidate.astimezone(zone)
    )
    if candidate <= current:
        raise TimeError("完整日期闹铃必须在未来。", code="invalid_time")
    return candidate


def next_daily_occurrence(at: datetime, *, timezone: str, now: datetime) -> datetime:
    """每日仅取严格晚于now的下次同一墙钟时刻，跨多日不逐条追补。"""
    validate_aware_datetime(at)
    validate_aware_datetime(now)
    zone = resolve_timezone(timezone)
    candidate, current = at.astimezone(zone), now.astimezone(zone)
    if candidate <= current:
        days = max(1, (current.date() - candidate.date()).days)
        candidate += timedelta(days=days)
        if candidate <= current:
            candidate += timedelta(days=1)
    return candidate


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise StorageError("时间账本文本字段损坏。", code="invalid_record")
    return value


def _decode[Record: BaseModel](value: object, model: type[Record]) -> Record:
    try:
        return model.model_validate_json(_text(value))
    except ValidationError as error:
        raise StorageError("时间账本记录形状损坏。", code="invalid_record") from error


def _alarm_record(row: tuple[object, ...]) -> Alarm:
    """读取边界同时核对查询索引和正文，坏索引不能给timer恢复旧发生资格。"""
    alarm = _decode(row[0], Alarm)
    if (
        row[1] != alarm.state
        or row[2] != alarm.at.timestamp()
        or row[3] != alarm.clock_id
        or row[4] != alarm.id
    ):
        raise StorageError("时间账本索引与定义不一致。", code="invalid_record")
    return alarm


def _get_alarm(connection: sqlite3.Connection, clock_id: str, alarm_id: str) -> Alarm | None:
    row: tuple[object, ...] | None = connection.execute(
        "SELECT payload_json,state,at_utc,clock_id,id FROM clock_alarms WHERE clock_id=? AND id=?",
        (clock_id, alarm_id),
    ).fetchone()
    return None if row is None else _alarm_record(row)


def alarms(connection: sqlite3.Connection, clock_id: str) -> tuple[Alarm, ...]:
    """读取最多32条当前待响定义，历史消耗记录不冒充待响。"""
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT payload_json,state,at_utc,clock_id,id FROM clock_alarms "
        "WHERE clock_id=? AND state='pending' ORDER BY at_utc,id LIMIT 33",
        (clock_id,),
    ).fetchall()
    if len(rows) > MAX_ALARMS:
        raise StorageError("持久化待闹铃数量超出既定额度。", code="invalid_record")
    return tuple(_alarm_record(row) for row in rows)


def _save_alarm(connection: sqlite3.Connection, alarm: Alarm) -> None:
    """状态与查询索引同一事务更新，timer不另维护一份可写闹铃表。"""
    connection.execute(
        "INSERT INTO clock_alarms VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "at_utc=excluded.at_utc,state=excluded.state,payload_json=excluded.payload_json",
        (alarm.id, alarm.clock_id, alarm.at.timestamp(), alarm.state, alarm.model_dump_json()),
    )


def set_alarm(connection: sqlite3.Connection, alarm: Alarm, now: datetime) -> Alarm:
    """同事务检查32条额度并保存新发生身份，重复相同定义不新增闹铃。"""
    validate_aware_datetime(now)
    if alarm.state != "pending" or alarm.version != 1 or alarm.at <= now:
        raise TimeError("新闹铃须是未来的初始待响定义。", code="invalid_time")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        owner: tuple[object, ...] | None = connection.execute(
            "SELECT clock_id FROM clock_alarms WHERE id=?", (alarm.id,)
        ).fetchone()
        if owner is not None and owner != (alarm.clock_id,):
            raise TimeError("闹铃身份已属于另一个时钟。", code="alarm_conflict")
        existing = _get_alarm(connection, alarm.clock_id, alarm.id)
        if existing is not None:
            if existing != alarm:
                raise TimeError("闹铃身份已有不同定义。", code="alarm_conflict")
            return existing
        if len(alarms(connection, alarm.clock_id)) >= MAX_ALARMS:
            raise TimeError("最多32条闹铃待响。", code="alarm_capacity")
        _save_alarm(connection, alarm)
    return alarm


def cancel_alarm(connection: sqlite3.Connection, clock_id: str, alarm_id: str) -> Alarm:
    """仅取消当前待响定义，未知或已消耗身份明确拒绝。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        current = _get_alarm(connection, clock_id, alarm_id)
        if current is None or current.state != "pending":
            raise TimeError("闹铃不存在或已经响完。", code="unknown_alarm")
        cancelled = current.model_copy(
            update={"state": "cancelled", "version": current.version + 1}
        )
        _save_alarm(connection, cancelled)
    return cancelled


def recover_daily(connection: sqlite3.Connection, clock_id: str, now: datetime) -> None:
    """启动时每日错过项只推进未来，单次错过项保持待响供可靠补投。"""
    validate_aware_datetime(now)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        for alarm in alarms(connection, clock_id):
            if alarm.repeat == "daily" and alarm.at <= now:
                next_at = next_daily_occurrence(alarm.at, timezone=alarm.timezone, now=now)
                _save_alarm(
                    connection,
                    alarm.model_copy(
                        update={
                            "at": next_at,
                            "occurrence_id": str(uuid4()),
                            "version": alarm.version + 1,
                        }
                    ),
                )


def fire_alarm(
    connection: sqlite3.Connection,
    clock_id: str,
    alarm_id: str,
    occurrence_id: str,
    now: datetime,
    admit: AdmitInput,
) -> AlarmFiring | None:
    """原生发生身份、输入/pending、消耗或每日推进同事务，取消等待不能回滚它。"""
    validate_aware_datetime(now)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        row: tuple[object, ...] | None = connection.execute(
            "SELECT payload_json FROM alarm_firings WHERE occurrence_id=?", (occurrence_id,)
        ).fetchone()
        if row is not None:
            existing = _decode(row[0], AlarmFiring)
            if (existing.clock_id, existing.alarm_id) != (clock_id, alarm_id):
                raise TimeError("闹铃发生身份冲突。", code="alarm_conflict")
            return existing
        current = _get_alarm(connection, clock_id, alarm_id)
        if (
            current is None
            or current.state != "pending"
            or current.occurrence_id != occurrence_id
            or current.at > now
        ):
            return None
        next_at = (
            next_daily_occurrence(current.at, timezone=current.timezone, now=now)
            if current.repeat == "daily"
            else None
        )
        payload: JsonObject = {
            "alarm_id": current.id,
            "at": current.at.isoformat(),
            "timezone": current.timezone,
            "message": current.message,
            "repeat": current.repeat,
            "fired_at": now.isoformat(),
            "late_seconds": max(0.0, (now - current.at).total_seconds()),
        }
        if next_at is not None:
            payload["next_at"] = next_at.isoformat()
        submission = InputSubmission(
            InputSource(clock_id, "clock", clock_id, "clock.alarm.fired", occurrence_id),
            encode_json(payload).decode(),
            now,
            now,
            context=InputContext(minimum_wake=True),
        )
        admitted = admit(connection, submission)
        changed = current.model_copy(
            update={
                "version": current.version + 1,
                "state": "consumed" if next_at is None else "pending",
                **({} if next_at is None else {"at": next_at, "occurrence_id": str(uuid4())}),
            }
        )
        _save_alarm(connection, changed)
        firing = AlarmFiring(
            clock_id=clock_id,
            alarm_id=alarm_id,
            occurrence_id=occurrence_id,
            input_id=admitted.input_id,
            scheduled_at=current.at,
            fired_at=now,
            next_at=next_at,
        )
        connection.execute(
            "INSERT INTO alarm_firings VALUES(?,?,?,?,?)",
            (occurrence_id, clock_id, alarm_id, admitted.input_id, firing.model_dump_json()),
        )
    return firing


def get_rest(connection: sqlite3.Connection) -> RestState | None:
    """读取唯一主体明确状态；时间过去不自动清除休息。"""
    row: tuple[object, ...] | None = connection.execute(
        "SELECT payload_json,version FROM subject_rest WHERE id=1"
    ).fetchone()
    if row is None or row[0] is None:
        return None
    state = _decode(row[0], RestState)
    if row[1] != state.version:
        raise StorageError("休息版本索引与正文不一致。", code="invalid_record")
    return state


def _rest_version(connection: sqlite3.Connection) -> int:
    row: tuple[object, ...] | None = connection.execute(
        "SELECT version FROM subject_rest WHERE id=1"
    ).fetchone()
    if row is None:
        return 0
    if isinstance(row[0], bool) or not isinstance(row[0], int) or row[0] < 1:
        raise StorageError("休息版本损坏。", code="invalid_record")
    return row[0]


def rest(
    connection: sqlite3.Connection, bed_id: str, minutes: int, now: datetime, call_id: str
) -> RestChange:
    """首次记录开始时刻，已休息只更新打算时长；每次真实变化留下独立回执。"""
    validate_aware_datetime(now)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        row: tuple[object, ...] | None = connection.execute(
            "SELECT operation,payload_json FROM rest_changes WHERE call_id=?", (call_id,)
        ).fetchone()
        if row is not None:
            if row[0] != "rest":
                raise TimeError("休息调用身份冲突。", code="rest_conflict")
            existing = _decode(row[1], RestChange)
            if (
                existing.state is None
                or existing.state.bed_id != bed_id
                or existing.state.expected_duration_minutes != minutes
            ):
                raise TimeError("休息调用身份已有不同参数。", code="rest_conflict")
            return existing
        previous = get_rest(connection)
        state = RestState(
            bed_id=bed_id,
            expected_duration_minutes=minutes,
            started_at=previous.started_at if previous is not None else now.astimezone(UTC),
            version=_rest_version(connection) + 1,
        )
        change = RestChange(
            at=now, state=state, previous=previous, changed=True, updated=previous is not None
        )
        connection.execute(
            "INSERT INTO subject_rest VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET "
            "version=excluded.version,payload_json=excluded.payload_json",
            (state.version, state.model_dump_json()),
        )
        connection.execute(
            "INSERT INTO rest_changes VALUES(?,'rest',?)", (call_id, change.model_dump_json())
        )
    return change


def leave(connection: sqlite3.Connection, now: datetime, call_id: str) -> RestChange:
    """醒着仅查询不写入；真正离开时保存原休息事实并清除当前状态。"""
    validate_aware_datetime(now)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        row: tuple[object, ...] | None = connection.execute(
            "SELECT operation,payload_json FROM rest_changes WHERE call_id=?", (call_id,)
        ).fetchone()
        if row is not None:
            if row[0] != "leave":
                raise TimeError("休息调用身份冲突。", code="rest_conflict")
            return _decode(row[1], RestChange)
        previous = get_rest(connection)
        change = RestChange(at=now, state=None, previous=previous, changed=previous is not None)
        if previous is None:
            return change
        connection.execute("UPDATE subject_rest SET version=version+1,payload_json=NULL WHERE id=1")
        connection.execute(
            "INSERT INTO rest_changes VALUES(?,'leave',?)", (call_id, change.model_dump_json())
        )
    return change


def initialize(connection: sqlite3.Connection) -> None:
    """只核验当前格式表，不自动响铃、起身、迁移或修复坏状态。"""
    for table in ("clock_alarms", "alarm_firings", "subject_rest", "rest_changes"):
        connection.execute(f"SELECT * FROM {table} LIMIT 0")
