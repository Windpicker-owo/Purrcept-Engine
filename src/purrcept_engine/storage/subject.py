"""在同一权威连接保存注意处置、前台批次和连续会话版本。

接管原文与处理状态分别记账。普通输入只筛选一次，defer 到期直接交前台；
成功会话、输入处理结束和检查点同事务提交。失败批次保留错误与来源，不能
以 Core 丢弃临时历史为由抹掉已经发生的行动。这里不推进模型或等待设备。
所有 SQL 函数由 Store 专属线程调用，事务内仅访问本连接。
最低唤起的紧急升级单独保存宿主、旧代次和输入版本；它只结案一次判断，
不另建待办。重启撤销旧观察，保留原前台交接资格。
"""

from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass
from typing import Literal

from purrcept_engine.context.checkpoints import decode_checkpoint
from purrcept_engine.errors import StorageError
from purrcept_engine.execution.protocol import decode_object, encode_json

from .model_requests import intern_core_media, restore_core_media

type SessionScope = Literal["foreground", "reflection"]
type InputStage = Literal[
    "attention", "deferred", "foreground", "processing", "processed", "failed"
]
type AttentionLevel = Literal["observe", "defer", "wake", "urgent"]
type TurnOutcome = Literal["running", "succeeded", "failed", "cancelled", "interrupted"]

MINIMUM_WAKE_SCHEMA = (
    "CREATE TABLE minimum_wake_assessments (input_id TEXT PRIMARY KEY REFERENCES inputs(input_id), "
    "owner_id TEXT NOT NULL, observed_generation INTEGER NOT NULL, input_version INTEGER NOT NULL, "
    "state TEXT NOT NULL, level TEXT, reason TEXT, request_id TEXT)",
    "CREATE INDEX minimum_wake_pending ON minimum_wake_assessments(owner_id,state)",
)

SCHEMA = (
    "CREATE TABLE input_processing (input_id TEXT PRIMARY KEY REFERENCES inputs(input_id), "
    "version INTEGER NOT NULL, stage TEXT NOT NULL, reason TEXT NOT NULL, "
    "attention_request_id TEXT, due_at REAL, handed_off INTEGER NOT NULL DEFAULT 0, "
    "admitted_at REAL NOT NULL)",
    *MINIMUM_WAKE_SCHEMA,
    "CREATE TABLE subject_sessions (scope TEXT PRIMARY KEY, version INTEGER NOT NULL, "
    "checkpoint_json TEXT, active_turn TEXT)",
    "CREATE TABLE session_media (scope TEXT NOT NULL REFERENCES subject_sessions(scope), "
    "media_id TEXT NOT NULL REFERENCES media(media_id), PRIMARY KEY(scope,media_id))",
    "CREATE TABLE subject_turns (turn_id TEXT PRIMARY KEY, scope TEXT NOT NULL, "
    "session_version INTEGER NOT NULL, generation INTEGER NOT NULL, state TEXT NOT NULL, "
    "started_at REAL NOT NULL, finished_at REAL, error_json TEXT)",
    "CREATE TABLE turn_inputs (turn_id TEXT NOT NULL REFERENCES subject_turns(turn_id), "
    "ordinal INTEGER NOT NULL, input_id TEXT NOT NULL REFERENCES inputs(input_id), "
    "PRIMARY KEY(turn_id,ordinal))",
    "CREATE TABLE failed_turns (turn_id TEXT PRIMARY KEY REFERENCES subject_turns(turn_id), "
    "presented_in TEXT REFERENCES subject_turns(turn_id))",
)


@dataclass(frozen=True, slots=True)
class InputProcessing:
    """输入处置的版本事实；筛选与处理结束不等同于模型理解或外界输出。"""

    input_id: str
    version: int
    stage: InputStage
    reason: str
    attention_request_id: str | None
    due_at: float | None
    handed_off: bool
    admitted_at: float


@dataclass(frozen=True, slots=True)
class MinimumWakeAssessment:
    """直接唤起后的一次紧急升级资格，绑定创建它的宿主与旧生成代次。

    记录不另建前台待办。input_version 冻结仍在 foreground 的处置身份；
    一旦被前台接管或宿主重启，旧结果只能留下判断事实，不能抢占新轮次。
    """

    input_id: str
    owner_id: str
    observed_generation: int
    input_version: int


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """同一会话的已提交版本及完整 Core 检查点编码，不包含临时轮次。"""

    scope: SessionScope
    version: int
    checkpoint_json: str | None
    active_turn: str | None


@dataclass(frozen=True, slots=True)
class TurnRecord:
    """一次主动处理的独立身份；失败材料与真实回执通过来源身份关联。"""

    turn_id: str
    scope: SessionScope
    session_version: int
    generation: int
    state: TurnOutcome
    input_ids: tuple[str, ...]
    started_at: float
    finished_at: float | None
    error_json: str | None


def register_input(connection: sqlite3.Connection, input_id: str, *, minimum_wake: bool) -> None:
    """由输入接管事务登记唯一待处理身份，不另开或提交事务。"""
    connection.execute(
        "INSERT INTO input_processing(input_id,version,stage,reason,admitted_at) VALUES(?,1,?,?,?)",
        (
            input_id,
            "foreground" if minimum_wake else "attention",
            "trusted.minimum_wake" if minimum_wake else "admitted",
            time.time(),
        ),
    )


def processing(connection: sqlite3.Connection, input_id: str) -> InputProcessing | None:
    """读取实际处置状态，查询不会交接延后输入。"""
    row: tuple[object, ...] | None = connection.execute(
        "SELECT version,stage,reason,attention_request_id,due_at,handed_off,admitted_at "
        "FROM input_processing WHERE input_id=?",
        (input_id,),
    ).fetchone()
    if row is None:
        return None
    stage = _string(row[1])
    if stage not in ("attention", "deferred", "foreground", "processing", "processed", "failed"):
        raise StorageError("输入处置状态无效。", code="invalid_record")
    return InputProcessing(
        input_id,
        _integer(row[0]),
        stage,
        _string(row[2]),
        _optional_text(row[3]),
        _optional_number(row[4]),
        bool(_integer(row[5])),
        _number(row[6]),
    )


def pending_ids(connection: sqlite3.Connection, stage: InputStage, limit: int) -> tuple[str, ...]:
    """按正式接管顺序返回有限待办，不能凭正文相似合并来源。"""
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT p.input_id FROM pending_inputs p JOIN input_processing s USING(input_id) "
        "WHERE s.stage=? ORDER BY p.sequence LIMIT ?",
        (stage, limit),
    ).fetchall()
    return tuple(_string(row[0]) for row in rows)


def decide_inputs(
    connection: sqlite3.Connection,
    expected: tuple[InputProcessing, ...],
    level: AttentionLevel,
    reason: str,
    *,
    request_id: str | None,
    defer_until: float | None,
) -> bool:
    """整批采用同一判断，版本过期不覆盖已处理身份；defer 仅登记一次到期。"""
    if not expected or any(item.stage != "attention" for item in expected):
        raise ValueError("注意提交需要尚未筛选的非空输入批次。")
    if level not in ("observe", "defer", "wake", "urgent"):
        raise ValueError("注意级别无效。")
    if (level == "defer") != (defer_until is not None):
        raise ValueError("仅 defer 可携带明确到期时刻。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if any(processing(connection, item.input_id) != item for item in expected):
            return False
        stage = (
            "processed" if level == "observe" else "deferred" if level == "defer" else "foreground"
        )
        for item in expected:
            connection.execute(
                "UPDATE input_processing SET version=version+1,stage=?,reason=?,"
                "attention_request_id=?,due_at=? WHERE input_id=?",
                (stage, reason, request_id, defer_until, item.input_id),
            )
            if level == "observe":
                connection.execute("DELETE FROM pending_inputs WHERE input_id=?", (item.input_id,))
    return True


def register_minimum_wake_assessment(
    connection: sqlite3.Connection,
    expected: InputProcessing,
    *,
    owner_id: str,
    observed_generation: int,
) -> bool:
    """同事务绑定首次升级观察；重放或已接管输入不获得新的判断资格。"""
    if expected.stage != "foreground" or not owner_id or observed_generation <= 0:
        raise ValueError("最低唤起升级必须绑定待处理输入和有效旧生成。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if processing(connection, expected.input_id) != expected:
            return False
        cursor = connection.execute(
            "INSERT OR IGNORE INTO minimum_wake_assessments"
            "(input_id,owner_id,observed_generation,input_version,state) VALUES(?,?,?,?,'pending')",
            (expected.input_id, owner_id, observed_generation, expected.version),
        )
        return cursor.rowcount == 1


def minimum_wake_assessments(
    connection: sqlite3.Connection, owner_id: str, *, limit: int
) -> tuple[MinimumWakeAssessment, ...]:
    """按接管顺序取得本宿主的有限升级身份，不建立全部积压的内存副本。"""
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT a.input_id,a.observed_generation,a.input_version "
        "FROM minimum_wake_assessments a JOIN input_processing p USING(input_id) "
        "WHERE a.owner_id=? AND a.state='pending' ORDER BY p.admitted_at,a.input_id LIMIT ?",
        (owner_id, limit),
    ).fetchall()
    return tuple(
        MinimumWakeAssessment(_string(row[0]), owner_id, _integer(row[1]), _integer(row[2]))
        for row in rows
    )


def finish_minimum_wake_assessment(
    connection: sqlite3.Connection,
    assessment: MinimumWakeAssessment,
    level: AttentionLevel,
    reason: str,
    *,
    request_id: str | None,
) -> bool:
    """永久结案一次判断，只有原 foreground 版本可采用；最低 WAKE 始终保留。

    observe/defer 是模型的实际回答，不能撤销已经接管的直接唤起或新设延期。
    迟到结果仍记录在升级身份，但不覆盖 processing/终态，也不重复投递输入。
    """
    if level not in ("observe", "defer", "wake", "urgent"):
        raise ValueError("注意级别无效。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE minimum_wake_assessments SET state='completed',level=?,reason=?,request_id=? "
            "WHERE input_id=? AND owner_id=? AND observed_generation=? AND input_version=? "
            "AND state='pending'",
            (
                level,
                reason,
                request_id,
                assessment.input_id,
                assessment.owner_id,
                assessment.observed_generation,
                assessment.input_version,
            ),
        )
        if cursor.rowcount != 1:
            return False
        cursor = connection.execute(
            "UPDATE input_processing SET version=version+1,reason=?,attention_request_id=? "
            "WHERE input_id=? AND version=? AND stage='foreground'",
            (reason, request_id, assessment.input_id, assessment.input_version),
        )
        return cursor.rowcount == 1


def deliver_due(connection: sqlite3.Connection, now: float) -> tuple[str, ...]:
    """延后到期与唯一前台交接同事务，恢复后不重新进入注意判断。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        rows: list[tuple[object, ...]] = connection.execute(
            "SELECT input_id FROM input_processing WHERE stage='deferred' AND due_at<=? "
            "AND handed_off=0 ORDER BY due_at,input_id",
            (now,),
        ).fetchall()
        ids = tuple(_string(row[0]) for row in rows)
        for input_id in ids:
            connection.execute(
                "UPDATE input_processing SET version=version+1,stage='foreground',"
                "reason='attention.defer_due',handed_off=1 WHERE input_id=?",
                (input_id,),
            )
        return ids


def next_deferred(connection: sqlite3.Connection) -> float | None:
    """返回最早未交接期限，宿主用单一定时等待而非每输入一个任务。"""
    row: tuple[object, ...] = connection.execute(
        "SELECT min(due_at) FROM input_processing WHERE stage='deferred' AND handed_off=0"
    ).fetchone()
    return _optional_number(row[0])


def session(connection: sqlite3.Connection, scope: SessionScope) -> SessionRecord:
    """返回尚未建立的空版本或完整已提交历史，不以空值覆盖损坏记录。"""
    row: tuple[object, ...] | None = connection.execute(
        "SELECT version,checkpoint_json,active_turn FROM subject_sessions WHERE scope=?",
        (scope,),
    ).fetchone()
    if row is None:
        return SessionRecord(scope, 0, None, None)
    encoded = _optional_text(row[1])
    return SessionRecord(
        scope,
        _integer(row[0]),
        None if encoded is None else restore_core_media(connection, encoded),
        _optional_text(row[2]),
    )


def begin_turn(
    connection: sqlite3.Connection,
    turn_id: str,
    scope: SessionScope,
    expected_version: int,
    generation: int,
    input_ids: tuple[str, ...],
) -> TurnRecord:
    """先接管处理身份与固定会话版本；不得与同一会话的在途轮次重叠。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        current = session(connection, scope)
        if current.version != expected_version or current.active_turn is not None:
            raise StorageError("会话版本变化或已有在途处理。", code="session_conflict")
        for input_id in input_ids:
            item = processing(connection, input_id)
            if item is None or item.stage != "foreground":
                raise StorageError("输入不再具有前台交接资格。", code="input_conflict")
        connection.execute(
            "INSERT INTO subject_sessions(scope,version,active_turn) VALUES(?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET active_turn=excluded.active_turn",
            (scope, expected_version, turn_id),
        )
        started = time.time()
        connection.execute(
            "INSERT INTO subject_turns(turn_id,scope,session_version,generation,state,started_at) "
            "VALUES(?,?,?,?,'running',?)",
            (turn_id, scope, expected_version, generation, started),
        )
        for ordinal, input_id in enumerate(input_ids):
            connection.execute(
                "INSERT INTO turn_inputs VALUES(?,?,?)", (turn_id, ordinal, input_id)
            )
            connection.execute(
                "UPDATE input_processing SET version=version+1,stage='processing' WHERE input_id=?",
                (input_id,),
            )
    return TurnRecord(
        turn_id, scope, expected_version, generation, "running", input_ids, started, None, None
    )


def finish_turn(
    connection: sqlite3.Connection,
    turn: TurnRecord,
    outcome: TurnOutcome,
    *,
    checkpoint_json: str | None,
    error_json: str | None,
    accepted_failures: tuple[str, ...] = (),
) -> SessionRecord:
    """成功检查点和输入结案原子安装；失败保留来源和旧历史，不重放行动。"""
    if outcome == "running" or (outcome == "succeeded") != (checkpoint_json is not None):
        raise ValueError("只有成功终态可安装候选检查点。")
    if error_json is not None:
        decode_object(error_json.encode("utf-8"))
    if accepted_failures:
        if outcome != "succeeded" or checkpoint_json is None:
            raise ValueError("只有成功轮次可以确认承接失败经历。")
        checkpoint = decode_checkpoint(checkpoint_json)
        boundaries = checkpoint.history.turn_boundaries
        start = boundaries[-2] if len(boundaries) > 1 else 0
        initial = checkpoint.history.messages[start]
        if initial.metadata.get("accepted_failures") != accepted_failures:
            raise ValueError("成功历史初始材料没有携带待承接的失败身份。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        current = session(connection, turn.scope)
        if current.version != turn.session_version or current.active_turn != turn.turn_id:
            raise StorageError("会话提交已失去原版本资格。", code="session_conflict")
        for failure_id in accepted_failures:
            row: tuple[object, ...] | None = connection.execute(
                "SELECT t.scope,f.presented_in FROM failed_turns f "
                "JOIN subject_turns t USING(turn_id) WHERE f.turn_id=?",
                (failure_id,),
            ).fetchone()
            if row != (turn.scope, None):
                raise StorageError("失败经历已经交接或不属于本会话。", code="session_conflict")
        version = current.version
        if checkpoint_json is not None:
            stored, media_ids = intern_core_media(connection, checkpoint_json)
            version += 1
            connection.execute(
                "UPDATE subject_sessions SET version=?,checkpoint_json=? WHERE scope=?",
                (version, stored, turn.scope),
            )
            connection.execute("DELETE FROM session_media WHERE scope=?", (turn.scope,))
            for media_id in media_ids:
                connection.execute("INSERT INTO session_media VALUES(?,?)", (turn.scope, media_id))
        connection.execute(
            "UPDATE subject_sessions SET active_turn=NULL WHERE scope=?", (turn.scope,)
        )
        connection.execute(
            "UPDATE subject_turns SET state=?,finished_at=?,error_json=? WHERE turn_id=?",
            (outcome, time.time(), error_json, turn.turn_id),
        )
        for input_id in turn.input_ids:
            connection.execute(
                "UPDATE input_processing SET version=version+1,stage=?,reason=? WHERE input_id=?",
                ("processed" if outcome == "succeeded" else "failed", outcome, input_id),
            )
            connection.execute("DELETE FROM pending_inputs WHERE input_id=?", (input_id,))
        if outcome != "succeeded":
            connection.execute("INSERT INTO failed_turns(turn_id) VALUES(?)", (turn.turn_id,))
        for failure_id in accepted_failures:
            connection.execute(
                "UPDATE failed_turns SET presented_in=? WHERE turn_id=?", (turn.turn_id, failure_id)
            )
    return session(connection, turn.scope)


def failures(connection: sqlite3.Connection, scope: SessionScope) -> tuple[TurnRecord, ...]:
    """读取尚未由后续成功交互承接的失败事实，事实永久留在原批次。"""
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT t.turn_id,t.session_version,t.generation,t.state,t.started_at,"
        "t.finished_at,t.error_json "
        "FROM subject_turns t JOIN failed_turns f USING(turn_id) "
        "WHERE t.scope=? AND f.presented_in IS NULL ORDER BY t.started_at,t.turn_id",
        (scope,),
    ).fetchall()
    result: list[TurnRecord] = []
    for row in rows:
        state = _string(row[3])
        if state not in ("failed", "cancelled", "interrupted"):
            raise StorageError("失败经历引用非失败轮次。", code="invalid_record")
        ids: list[tuple[object, ...]] = connection.execute(
            "SELECT input_id FROM turn_inputs WHERE turn_id=? ORDER BY ordinal", (row[0],)
        ).fetchall()
        result.append(
            TurnRecord(
                _string(row[0]),
                scope,
                _integer(row[1]),
                _integer(row[2]),
                state,
                tuple(_string(item[0]) for item in ids),
                _number(row[4]),
                _optional_number(row[5]),
                _optional_text(row[6]),
            )
        )
    return tuple(result)


def get_turn(connection: sqlite3.Connection, turn_id: str) -> TurnRecord | None:
    """查询已接管主动处理，取消 begin_turn 等待后仍可核对真正所有权。"""
    row: tuple[object, ...] | None = connection.execute(
        "SELECT scope,session_version,generation,state,started_at,finished_at,error_json "
        "FROM subject_turns WHERE turn_id=?",
        (turn_id,),
    ).fetchone()
    if row is None:
        return None
    scope = _string(row[0])
    state = _string(row[3])
    if scope not in ("foreground", "reflection") or state not in (
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "interrupted",
    ):
        raise StorageError("主体处理身份损坏。", code="invalid_record")
    inputs: list[tuple[object, ...]] = connection.execute(
        "SELECT input_id FROM turn_inputs WHERE turn_id=? ORDER BY ordinal",
        (turn_id,),
    ).fetchall()
    return TurnRecord(
        turn_id,
        scope,
        _integer(row[1]),
        _integer(row[2]),
        state,
        tuple(_string(item[0]) for item in inputs),
        _number(row[4]),
        _optional_number(row[5]),
        _optional_text(row[6]),
    )


def initialize(connection: sqlite3.Connection) -> None:
    """借用调用方启动事务将旧处理记为中断，不提交或自动重做输入。"""
    connection.execute("SELECT input_id,stage FROM input_processing LIMIT 0")
    connection.execute(
        "SELECT input_id,owner_id,observed_generation,input_version,state,level,reason,request_id "
        "FROM minimum_wake_assessments LIMIT 0"
    )
    # 启动事务由 Store 持有；恢复失败不能单独提交任何子系统。
    # 重启没有旧宿主正在运行的生成身份。只撤销升级资格，原最低唤起
    # 继续留在前台；不得根据新进程恰好相同的数字 generation 重新判断。
    connection.execute("UPDATE minimum_wake_assessments SET state='expired' WHERE state='pending'")
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT turn_id FROM subject_turns WHERE state='running'"
    ).fetchall()
    for row in rows:
        turn_id = _string(row[0])
        connection.execute(
            "UPDATE subject_turns SET state='interrupted',finished_at=?,error_json=? "
            "WHERE turn_id=?",
            (time.time(), encode_json({"error_type": "host_interrupted"}).decode(), turn_id),
        )
        connection.execute("INSERT OR IGNORE INTO failed_turns(turn_id) VALUES(?)", (turn_id,))
        connection.execute(
            "UPDATE input_processing SET version=version+1,stage='failed',"
            "reason='host_interrupted' "
            "WHERE input_id IN (SELECT input_id FROM turn_inputs WHERE turn_id=?)",
            (turn_id,),
        )
        connection.execute(
            "DELETE FROM pending_inputs WHERE input_id IN "
            "(SELECT input_id FROM turn_inputs WHERE turn_id=?)",
            (turn_id,),
        )
    connection.execute("UPDATE subject_sessions SET active_turn=NULL")


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise StorageError("主体记录的文字字段损坏。", code="invalid_record")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _string(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StorageError("主体记录的版本字段损坏。", code="invalid_record")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise StorageError("主体记录的时间字段损坏。", code="invalid_record")
    return float(value)


def _optional_number(value: object) -> float | None:
    return None if value is None else _number(value)
