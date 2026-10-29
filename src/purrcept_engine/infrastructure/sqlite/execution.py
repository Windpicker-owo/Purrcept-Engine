"""保存行动意图、逐次派发门槛和独立于会话提交的真实回执。

Store 的唯一数据库线程调用这里的短事务函数；模块不打开连接、不派发能力，
也不回放未完成调用。准备、启动门槛和效果确认分别入账。未知结果可以由迟到
真实回执澄清，旧版本与可靠待交接身份同事务保留，不由 Worker 自报覆盖。
"""

from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import replace

from purrcept_engine.domain.execution.models import (
    ActionOutcome,
    ActionRecord,
    CallOutcome,
    CallRecord,
    CallResultRecord,
)
from purrcept_engine.domain.execution.record_validation import (
    validate_json,
)
from purrcept_engine.domain.inputs.models import MediaReference
from purrcept_engine.errors import StorageError

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
    """从已保存的确切结果版本恢复回执，不用缺省值伪造成功。"""
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
    """保存不可变行动意图。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
        catalog_version: 本次行动冻结的目录内容摘要，执行期间保持不变。
        source: 行动原始材料，保存受限源码或冻结的 structured_call JSON。
        input_id: 正式接管返回的输入身份，用于关联原文与后续处理。 None 表示无单一输入因果关联。

    Raises:
        StorageError: 行动身份与既有意图冲突。

    Note:
        同身份同材料幂等，不同材料拒绝覆盖。
    """
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
    """派发前提交准备事实，未提交成功不得进入能力实现。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        call_id: 实际能力调用的唯一身份，用于保存启动门槛及结果版本。
        action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
        sequence: 行动内从 1 开始的调用顺序号。
        path: 冻结目录中的完整能力调用路径，例如 me.console.output。
        target_id: 能力绑定的目标对象身份。
        arguments_json: 已编码为严格 JSON 对象的原始能力参数。

    Raises:
        StorageError: 权威记录缺失、版本不一致、载荷无效或提交条件不成立。
    """
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
    """只允许准备态通过一次门槛，重复门槛不能成为重复执行许可。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        call_id: 实际能力调用的唯一身份，用于保存启动门槛及结果版本。

    Raises:
        StorageError: 调用已失去启动资格。
    """
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
    """结果、不可变历史与可靠交接同事务保存，不借行动结束回滚效果。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        call_id: 实际能力调用的唯一身份，用于保存启动门槛及结果版本。
        state: 本次实际调用的确认、失败或未知结论。
        result_json: 已编码的实际结果；无可用结果时为 None。
        error_type: 回执中稳定的错误类别，无错误时为 None。
        error: 当前流程已经取得的真实故障或失败说明。
        media: 已接管媒体的引用集合，展开时必须取得原始 BLOB。 默认不附加此类条目。

    Returns:
        原调用当前的权威结果及不可变版本历史。

    Raises:
        StorageError: 已保存的真实回执不能被覆盖；未通过启动门槛的调用没有外界效果。
    """
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
    """结束行动整体状态，不修改任何已保存或仍在途的调用记录。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
        state: 行动结束状态，不能抹除已经发生的逐次效果。
        result_json: 已编码的实际结果；无可用结果时为 None。
        output: 此次 Worker 实际产生的有界标准输出。
        error: 当前流程已经取得的真实故障或失败说明。

    Raises:
        StorageError: 行动身份不存在；行动结束结果不能被覆盖。
    """
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
    """在同一读取快照中取得行动和当前调用，旧回执仍在每项 history 中。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。

    Returns:
        已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。
        未取得对应记录或素材时返回 None；读取故障仍抛出。

    Raises:
        StorageError: 行动状态无效。
    """
    with connection:
        connection.execute("BEGIN")
        return get_action_in_transaction(connection, action_id)


def get_action_in_transaction(
    connection: sqlite3.Connection, action_id: str
) -> ActionRecord | None:
    """在调用者事务中读取行动与全部回执，不提交或结束外层原子操作。

    Args:
        connection: Store 线程中已开始事务的唯一连接。
        action_id: 要查询的真实行动身份。

    Returns:
        与 get_action 相同的完整记录；行动不存在时返回 None。

    Raises:
        RuntimeError: 调用者未持有事务。
        StorageError: 行动或逐次回执损坏。

    Note:
        人工评价必须在保存前复核当前回执，不能调用会自行结束事务的读取入口。
    """
    if not connection.in_transaction:
        raise RuntimeError("行动快照读取需要调用者持有事务。")
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
    """读取有限历史版本但不确认交接，重复查询不会删除可靠待办。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        limit: 本次读取或更新的最大条目数。

    Returns:
        条目集合：原调用当前的权威结果及不可变版本历史。

    Raises:
        StorageError: 待办引用无效回执版本。
    """
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
    """借用调用方启动事务核对必需列并结束旧派发，不独立提交恢复。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
    """
    connection.execute(f"SELECT {_ACTION_COLUMNS} FROM actions LIMIT 0")
    connection.execute(f"SELECT {_CALL_COLUMNS} FROM calls LIMIT 0")
    connection.execute(f"SELECT {_RESULT_COLUMNS} FROM call_results LIMIT 0")
    connection.execute("SELECT sequence, call_id, revision FROM pending_call_receipts LIMIT 0")
    # 每项回执事务本身含其可靠待办；整个恢复再用外层事务确保启动失败不留下
    # 部分恢复。这里直接写入，不调用会开启独立 BEGIN 的公开事务入口。
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
