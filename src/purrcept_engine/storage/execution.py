"""保存行动意图、逐次派发门槛和独立于会话提交的真实回执。

Store 的唯一数据库线程调用这里的短事务函数；模块不打开连接、不派发能力，
也不回放未完成调用。准备、启动门槛和效果确认分别入账。未知结果可以由迟到
真实回执澄清，旧版本与可靠待交接身份同事务保留，不由 Worker 自报覆盖。
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal, cast

from purrcept_engine.errors import StorageError

from .models import MediaReference

CallOutcome = Literal["confirmed", "failed", "unknown", "not_started"]
CallState = Literal["prepared", "started", "confirmed", "failed", "unknown", "not_started"]
ActionOutcome = Literal["completed", "failed", "timed_out", "cancelled"]
ActionState = Literal["running", "completed", "failed", "timed_out", "cancelled"]

SCHEMA = (
    "CREATE TABLE actions (action_id TEXT PRIMARY KEY, catalog_version TEXT NOT NULL, "
    "source TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN "
    "('running','completed','failed','timed_out','cancelled')), created_at REAL NOT NULL, "
    "finished_at REAL, output TEXT NOT NULL DEFAULT '', result_json TEXT, error TEXT, "
    "input_id TEXT)",
    "CREATE INDEX actions_input ON actions(input_id,created_at)",
    "CREATE TABLE calls (call_id TEXT PRIMARY KEY, action_id TEXT NOT NULL REFERENCES "
    "actions(action_id), sequence INTEGER NOT NULL CHECK(sequence >= 0), path TEXT NOT NULL, "
    "target_id TEXT NOT NULL, arguments_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN "
    "('prepared','started','confirmed','failed','unknown','not_started')), "
    "prepared_at REAL NOT NULL, "
    "started_at REAL, receipt_revision INTEGER NOT NULL DEFAULT 0 CHECK(receipt_revision >= 0), "
    "UNIQUE(action_id, sequence))",
    "CREATE TABLE call_results (call_id TEXT NOT NULL REFERENCES calls(call_id), "
    "revision INTEGER NOT NULL CHECK(revision > 0), state TEXT NOT NULL CHECK(state IN "
    "('confirmed','failed','unknown','not_started')), finished_at REAL NOT NULL, result_json TEXT, "
    "error_type TEXT, error TEXT, PRIMARY KEY(call_id, revision))",
    "CREATE TABLE pending_call_receipts (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
    "call_id TEXT NOT NULL, revision INTEGER NOT NULL, UNIQUE(call_id, revision), "
    "FOREIGN KEY(call_id, revision) REFERENCES call_results(call_id, revision))",
    "CREATE TABLE call_result_media (call_id TEXT NOT NULL, revision INTEGER NOT NULL, "
    "ordinal INTEGER NOT NULL, media_id TEXT NOT NULL REFERENCES media(media_id), "
    "media_type TEXT NOT NULL, byte_length INTEGER NOT NULL, name TEXT, "
    "PRIMARY KEY(call_id,revision,ordinal), "
    "FOREIGN KEY(call_id,revision) REFERENCES call_results(call_id,revision))",
)

_CALL_COLUMNS = (
    "call_id, action_id, sequence, path, target_id, arguments_json, state, prepared_at, "
    "started_at, receipt_revision"
)
_RESULT_COLUMNS = "call_id, revision, state, finished_at, result_json, error_type, error"
_ACTION_COLUMNS = (
    "action_id, catalog_version, source, state, created_at, finished_at, output, "
    "result_json, error, input_id"
)


@dataclass(frozen=True, slots=True)
class CallResultRecord:
    """一次不可变效果结论；版本身份也是可靠交接的确认范围。

    首次结论为版本 1。未知升级为确认或失败生成新版本，原未知事实不删除。
    finished_at 为存储提交时的 UTC Unix 时间，不替代平台提供的实际发生时间。
    """

    call_id: str
    revision: int
    state: CallOutcome
    finished_at: float
    result_json: str | None
    error_type: str | None
    error: str | None
    media: tuple[MediaReference, ...] = ()


@dataclass(frozen=True, slots=True)
class CallRecord:
    """派发意图与某个回执版本的快照，历史包含到该版本的全部结论。

    started_at 记录进入 Provider 前的持久门槛，并非外界操作已发生的证据。
    实时查询使用最新版本；待交接查询返回对应历史版本，不让新回执覆盖未交接
    的旧未知结论。参数 JSON 保留调用方原表示，查询不执行或重新投递调用。
    """

    call_id: str
    action_id: str
    sequence: int
    path: str
    target_id: str
    arguments_json: str
    state: CallState
    prepared_at: float
    started_at: float | None
    finished_at: float | None
    receipt_revision: int
    result_json: str | None
    error_type: str | None
    error: str | None
    history: tuple[CallResultRecord, ...]
    media: tuple[MediaReference, ...] = ()


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """行动代码、目录版本和整体结果，逐次能力回执拥有独立提交寿命。

    running 只表示行动意图已接管，不证明 Worker 已运行。行动失败、超时或取消
    不撤销调用回执；迟到真实结果仍可澄清原调用，而不会重写行动的原结束结果。
    """

    action_id: str
    catalog_version: str
    source: str
    state: ActionState
    created_at: float
    finished_at: float | None
    output: str
    result_json: str | None
    error: str | None
    calls: tuple[CallRecord, ...]
    input_id: str | None = None


def payload_bytes(*values: str | None) -> int:
    """在异步提交边界核验 UTF-8 文本，并计入控制队列实际载荷。"""
    total = 0
    for value in values:
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError("执行记录字段必须是字符串或 None。")
        try:
            total += len(value.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("执行记录不能表示为 UTF-8。") from error
    return total


def validate_json(value: str | None, *, arguments: bool = False) -> None:
    """只校验真实 JSON 边界；参数要求对象，结果允许任意标准 JSON 值。"""
    if value is None:
        if arguments:
            raise ValueError("调用参数 JSON 不能缺失。")
        return
    if not isinstance(value, str):
        raise ValueError("执行记录 JSON 必须是字符串。")
    try:
        parsed: object = json.loads(value, parse_constant=_reject_constant)
        _finite_json(parsed)
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError("执行记录不是标准有限 JSON。") from error
    if arguments and not isinstance(parsed, Mapping):
        raise ValueError("调用参数 JSON 必须表示对象。")


def _reject_constant(value: str) -> object:
    raise ValueError(f"非标准 JSON 常量：{value}")


def _finite_json(value: object) -> None:
    """拒绝解析为无穷大的合法数字文本，不只拦截 NaN/Infinity 扩展词。"""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON 数字必须有限。")
    if isinstance(value, Mapping):
        for item in cast(Mapping[str, object], value).values():
            _finite_json(item)
    elif isinstance(value, list):
        for item in cast(list[object], value):
            _finite_json(item)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise StorageError("执行记录字符串字段无效。", code="invalid_record")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise StorageError("执行记录时间字段无效。", code="invalid_record")
    return float(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StorageError("执行记录计数字段无效。", code="invalid_record")
    return value


def _json(value: object, *, arguments: bool = False) -> str | None:
    """持久化读取是独立信任边界，损坏 JSON 属于权威故障。"""
    text = _optional_text(value)
    try:
        validate_json(text, arguments=arguments)
    except ValueError as error:
        raise StorageError("执行记录 JSON 无效。", code="invalid_record") from error
    return text


def _result(connection: sqlite3.Connection, row: Sequence[object]) -> CallResultRecord:
    state = row[2]
    if state not in ("confirmed", "failed", "unknown", "not_started"):
        raise StorageError("调用回执状态无效。", code="invalid_record")
    revision = _integer(row[1])
    if revision == 0:
        raise StorageError("调用回执版本无效。", code="invalid_record")
    media_rows: list[tuple[object, ...]] = connection.execute(
        "SELECT media_id,media_type,byte_length,name FROM call_result_media "
        "WHERE call_id=? AND revision=? ORDER BY ordinal",
        (_text(row[0]), revision),
    ).fetchall()
    media = tuple(
        MediaReference(_text(item[0]), _text(item[1]), _integer(item[2]), _optional_text(item[3]))
        for item in media_rows
    )
    return CallResultRecord(
        _text(row[0]),
        revision,
        state,
        _number(row[3]),
        _json(row[4]),
        _optional_text(row[5]),
        _optional_text(row[6]),
        media,
    )


def _call(connection: sqlite3.Connection, row: Sequence[object]) -> CallRecord:
    """核对当前版本指针，并完整重建调用和回执历史。"""
    call_id = _text(row[0])
    state = row[6]
    if state not in ("prepared", "started", "confirmed", "failed", "unknown", "not_started"):
        raise StorageError("调用派发状态无效。", code="invalid_record")
    current_revision = _integer(row[9])
    result_rows: list[tuple[object, ...]] = connection.execute(
        f"SELECT {_RESULT_COLUMNS} FROM call_results WHERE call_id = ? ORDER BY revision",
        (call_id,),
    ).fetchall()
    history = tuple(_result(connection, item) for item in result_rows)
    if len(history) != current_revision or any(
        item.revision != index for index, item in enumerate(history, 1)
    ):
        raise StorageError("调用回执历史与版本指针不一致。", code="invalid_record")
    if history and history[-1].state != state:
        raise StorageError("调用状态与最新回执不一致。", code="invalid_record")
    if not history and state not in ("prepared", "started"):
        raise StorageError("终态调用缺少真实回执。", code="invalid_record")
    latest = history[-1] if history else None
    started_at = None if row[8] is None else _number(row[8])
    if (state == "prepared" and started_at is not None) or (
        state in ("started", "confirmed", "unknown") and started_at is None
    ):
        raise StorageError("调用状态与持久启动门槛不一致。", code="invalid_record")
    arguments = _json(row[5], arguments=True)
    assert arguments is not None
    return CallRecord(
        call_id,
        _text(row[1]),
        _integer(row[2]),
        _text(row[3]),
        _text(row[4]),
        arguments,
        latest.state if latest else state,
        _number(row[7]),
        started_at,
        latest.finished_at if latest else None,
        current_revision,
        latest.result_json if latest else None,
        latest.error_type if latest else None,
        latest.error if latest else None,
        history,
        latest.media if latest else (),
    )


def _get_call(connection: sqlite3.Connection, call_id: str) -> CallRecord:
    row: tuple[object, ...] | None = connection.execute(
        f"SELECT {_CALL_COLUMNS} FROM calls WHERE call_id = ?", (call_id,)
    ).fetchone()
    if row is None:
        raise StorageError("调用身份不存在。", code="call_missing")
    return _call(connection, row)


def begin_action(
    connection: sqlite3.Connection,
    action_id: str,
    catalog_version: str,
    source: str,
    input_id: str | None = None,
) -> None:
    """保存不可变行动意图；同身份同材料幂等，不同材料拒绝覆盖。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        row: tuple[object, ...] | None = connection.execute(
            "SELECT catalog_version, source, input_id FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is not None:
            if row != (catalog_version, source, input_id):
                raise StorageError("行动身份与既有意图冲突。", code="action_conflict")
            return
        connection.execute(
            "INSERT INTO actions(action_id, catalog_version, source, state, created_at, input_id) "
            "VALUES (?, ?, ?, 'running', ?, ?)",
            (action_id, catalog_version, source, time.time(), input_id),
        )


def begin_call(
    connection: sqlite3.Connection,
    call_id: str,
    action_id: str,
    sequence: int,
    path: str,
    target_id: str,
    arguments_json: str,
) -> None:
    """派发前提交准备事实，未提交成功不得进入能力实现。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        existing: tuple[object, ...] | None = connection.execute(
            "SELECT action_id, sequence, path, target_id, arguments_json "
            "FROM calls WHERE call_id = ?",
            (call_id,),
        ).fetchone()
        facts = (action_id, sequence, path, target_id, arguments_json)
        if existing is not None:
            if existing != facts:
                raise StorageError("调用身份与既有派发意图冲突。", code="call_conflict")
            return
        action: tuple[object, ...] | None = connection.execute(
            "SELECT state FROM actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        if action != ("running",):
            raise StorageError("行动不接受新的调用。", code="action_inactive")
        occupied: tuple[object, ...] | None = connection.execute(
            "SELECT 1 FROM calls WHERE action_id = ? AND sequence = ?", (action_id, sequence)
        ).fetchone()
        if occupied is not None:
            raise StorageError("行动调用顺序身份已存在。", code="call_conflict")
        connection.execute(
            "INSERT INTO calls(call_id, action_id, sequence, path, target_id, arguments_json, "
            "state, prepared_at) VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?)",
            (call_id, *facts, time.time()),
        )


def mark_call_started(connection: sqlite3.Connection, call_id: str) -> None:
    """只允许准备态通过一次门槛，重复门槛不能成为重复执行许可。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        call = _get_call(connection, call_id)
        action: tuple[object, ...] | None = connection.execute(
            "SELECT state FROM actions WHERE action_id = ?", (call.action_id,)
        ).fetchone()
        if call.state != "prepared" or action != ("running",):
            raise StorageError("调用已失去启动资格。", code="call_inactive")
        connection.execute(
            "UPDATE calls SET state='started', started_at=? WHERE call_id=?", (time.time(), call_id)
        )


def record_call_result(
    connection: sqlite3.Connection,
    call_id: str,
    state: CallOutcome,
    result_json: str | None,
    error_type: str | None,
    error: str | None,
    media: tuple[MediaReference, ...] = (),
) -> CallRecord:
    """结果、不可变历史与可靠交接同事务保存，不借行动结束回滚效果。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        call = _get_call(connection, call_id)
        facts = (state, result_json, error_type, error, media)
        if call.receipt_revision:
            if (call.state, call.result_json, call.error_type, call.error, call.media) == facts:
                return call
            # 超时未知之后，迟到的协议材料可能仍无法确认效果。新材料同样
            # 追加不可变版本，不能因结论仍为 unknown 丢掉实际观察。
            if call.state != "unknown" or state not in ("confirmed", "failed", "unknown"):
                raise StorageError("已保存的真实回执不能被覆盖。", code="receipt_conflict")
        elif state in ("confirmed", "unknown") and call.state != "started":
            raise StorageError("未通过启动门槛的调用没有外界效果。", code="call_inactive")
        revision = call.receipt_revision + 1
        connection.execute(
            "INSERT INTO call_results(call_id, revision, state, finished_at, result_json, "
            "error_type, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (call_id, revision, state, time.time(), result_json, error_type, error),
        )
        connection.execute(
            "UPDATE calls SET state=?, receipt_revision=? WHERE call_id=?",
            (state, revision, call_id),
        )
        for ordinal, reference in enumerate(media):
            connection.execute(
                "INSERT INTO call_result_media VALUES(?,?,?,?,?,?,?)",
                (
                    call_id,
                    revision,
                    ordinal,
                    reference.media_id,
                    reference.media_type,
                    reference.byte_length,
                    reference.name,
                ),
            )
        connection.execute(
            "INSERT INTO pending_call_receipts(call_id, revision) VALUES (?, ?)",
            (call_id, revision),
        )
        return _get_call(connection, call_id)


def finish_action(
    connection: sqlite3.Connection,
    action_id: str,
    state: ActionOutcome,
    result_json: str | None,
    output: str,
    error: str | None,
) -> None:
    """结束行动整体状态，不修改任何已保存或仍在途的调用记录。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        row: tuple[object, ...] | None = connection.execute(
            "SELECT state, result_json, output, error FROM actions WHERE action_id=?", (action_id,)
        ).fetchone()
        if row is None:
            raise StorageError("行动身份不存在。", code="action_missing")
        if row[0] != "running":
            if row != (state, result_json, output, error):
                raise StorageError("行动结束结果不能被覆盖。", code="action_conflict")
            return
        connection.execute(
            "UPDATE actions SET state=?, finished_at=?, result_json=?, output=?, error=? "
            "WHERE action_id=?",
            (state, time.time(), result_json, output, error, action_id),
        )


def get_action(connection: sqlite3.Connection, action_id: str) -> ActionRecord | None:
    """在同一读取快照中取得行动和当前调用，旧回执仍在每项 history 中。"""
    with connection:
        connection.execute("BEGIN")
        row: tuple[object, ...] | None = connection.execute(
            f"SELECT {_ACTION_COLUMNS} FROM actions WHERE action_id=?", (action_id,)
        ).fetchone()
        if row is None:
            return None
        state = row[3]
        if state not in ("running", "completed", "failed", "timed_out", "cancelled"):
            raise StorageError("行动状态无效。", code="invalid_record")
        call_rows: list[tuple[object, ...]] = connection.execute(
            f"SELECT {_CALL_COLUMNS} FROM calls WHERE action_id=? ORDER BY sequence", (action_id,)
        ).fetchall()
        return ActionRecord(
            _text(row[0]),
            _text(row[1]),
            _text(row[2]),
            state,
            _number(row[4]),
            None if row[5] is None else _number(row[5]),
            _text(row[6]),
            _json(row[7]),
            _optional_text(row[8]),
            tuple(_call(connection, item) for item in call_rows),
            _optional_text(row[9]),
        )


def pending_call_receipts(connection: sqlite3.Connection, limit: int) -> tuple[CallRecord, ...]:
    """读取有限历史版本但不确认交接，重复查询不会删除可靠待办。"""
    with connection:
        connection.execute("BEGIN")
        identities: list[tuple[object, ...]] = connection.execute(
            "SELECT call_id, revision FROM pending_call_receipts ORDER BY sequence LIMIT ?",
            (limit,),
        ).fetchall()
        records: list[CallRecord] = []
        for identity in identities:
            call = _get_call(connection, _text(identity[0]))
            revision = _integer(identity[1])
            if revision == 0 or revision > call.receipt_revision:
                raise StorageError("待办引用无效回执版本。", code="invalid_record")
            result = call.history[revision - 1]
            records.append(
                replace(
                    call,
                    state=result.state,
                    finished_at=result.finished_at,
                    receipt_revision=revision,
                    result_json=result.result_json,
                    error_type=result.error_type,
                    error=result.error,
                    history=call.history[:revision],
                )
            )
        return tuple(records)


def initialize(connection: sqlite3.Connection) -> None:
    """核对新增必需列，并保守结束上个宿主留下的未完成派发。"""
    connection.execute(f"SELECT {_ACTION_COLUMNS} FROM actions LIMIT 0")
    connection.execute(f"SELECT {_CALL_COLUMNS} FROM calls LIMIT 0")
    connection.execute(f"SELECT {_RESULT_COLUMNS} FROM call_results LIMIT 0")
    connection.execute("SELECT sequence, call_id, revision FROM pending_call_receipts LIMIT 0")
    # 每项回执事务本身含其可靠待办；整个恢复再用外层事务确保启动失败不留下
    # 部分恢复。这里直接写入，不调用会开启独立 BEGIN 的公开事务入口。
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        interrupted: list[tuple[object, ...]] = connection.execute(
            "SELECT call_id, state FROM calls WHERE state IN ('prepared','started') "
            "ORDER BY prepared_at"
        ).fetchall()
        for row in interrupted:
            call = _get_call(connection, _text(row[0]))
            state: CallOutcome = "not_started" if call.state == "prepared" else "unknown"
            connection.execute(
                "INSERT INTO call_results(call_id, revision, state, finished_at, "
                "error_type, error) "
                "VALUES (?, 1, ?, ?, 'host_interrupted', '宿主结束前未取得回执')",
                (call.call_id, state, time.time()),
            )
            connection.execute(
                "UPDATE calls SET state=?, receipt_revision=1 WHERE call_id=?",
                (state, call.call_id),
            )
            connection.execute(
                "INSERT INTO pending_call_receipts(call_id, revision) VALUES (?, 1)",
                (call.call_id,),
            )
        connection.execute(
            "UPDATE actions SET state='failed', finished_at=?, error='host_interrupted' "
            "WHERE state='running'",
            (time.time(),),
        )
