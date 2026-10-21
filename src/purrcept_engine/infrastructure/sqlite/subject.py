"""在同一权威连接保存注意处置、前台批次和连续会话版本。

接管、提交与唤醒分别记账。observe 留在收件箱，后续唤醒原子取尽已提交批次；
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

from purrcept_engine.domain.context.checkpoints import decode_checkpoint
from purrcept_engine.domain.subject.models import (
    AttentionLevel,
    InputProcessing,
    InputStage,
    MinimumWakeAssessment,
    SessionRecord,
    SessionScope,
    TurnOutcome,
    TurnRecord,
)
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite.model_requests import (
    intern_core_media,
    restore_core_media,
)
from purrcept_engine.serialization import encode
from purrcept_engine.values import decode_object, encode_json

MINIMUM_WAKE_SCHEMA = (
    "CREATE TABLE minimum_wake_assessments (batch_id TEXT PRIMARY KEY, "
    "expected_json TEXT NOT NULL, owner_id TEXT NOT NULL, observed_generation INTEGER NOT NULL, "
    "state TEXT NOT NULL, level TEXT, reason TEXT, request_id TEXT)",
    "CREATE INDEX minimum_wake_pending ON minimum_wake_assessments(owner_id,state)",
)

SCHEMA = (
    "CREATE TABLE input_processing (input_id TEXT PRIMARY KEY REFERENCES inputs(input_id), "
    "version INTEGER NOT NULL, stage TEXT NOT NULL, reason TEXT NOT NULL, "
    "attention_request_id TEXT, "
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


def register_input(connection: sqlite3.Connection, input_id: str, *, minimum_wake: bool) -> None:
    """由输入接管事务登记唯一待处理身份，不另开或提交事务。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        input_id: 正式接管返回的输入身份，用于关联原文与后续处理。
        minimum_wake: 直接唤起所建立的最低 WAKE 事实或对应升级判断资格。
    """
    connection.execute(
        "INSERT INTO input_processing(input_id,version,stage,reason,admitted_at) VALUES(?,1,?,?,?)",
        (
            input_id,
            "priority" if minimum_wake else "attention",
            "trusted.minimum_wake" if minimum_wake else "admitted",
            time.time(),
        ),
    )


def processing(connection: sqlite3.Connection, input_id: str) -> InputProcessing | None:
    """读取实际处置状态，查询不提交输入或请求唤醒。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        input_id: 正式接管返回的输入身份，用于关联原文与后续处理。

    Returns:
        输入实际处理阶段、版本和关联轮次。 未取得对应记录或素材时返回 None；读取故障仍抛出。

    Raises:
        StorageError: 输入处置状态无效。
    """
    row: tuple[object, ...] | None = connection.execute(
        "SELECT version,stage,reason,attention_request_id,admitted_at "
        "FROM input_processing WHERE input_id=?",
        (input_id,),
    ).fetchone()
    if row is None:
        return None
    stage = _string(row[1])
    if stage not in (
        "attention",
        "priority",
        "assessing",
        "submitted",
        "foreground",
        "claimed",
        "processing",
        "processed",
        "failed",
    ):
        raise StorageError("输入处置状态无效。", code="invalid_record")
    return InputProcessing(
        input_id,
        _integer(row[0]),
        stage,
        _string(row[2]),
        _optional_text(row[3]),
        _number(row[4]),
    )


def pending_ids(connection: sqlite3.Connection, stage: InputStage, limit: int) -> tuple[str, ...]:
    """按正式接管顺序返回有限待办，不能凭正文相似合并来源。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        stage: 输入当前或待查询的处理阶段。
        limit: 本次读取或更新的最大条目数。
    """
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT p.input_id FROM pending_inputs p JOIN input_processing s USING(input_id) "
        "WHERE s.stage=? ORDER BY p.sequence LIMIT ?",
        (stage, limit),
    ).fetchall()
    return tuple(_string(row[0]) for row in rows)


def submit_inputs(
    connection: sqlite3.Connection,
    expected: tuple[InputProcessing, ...],
    *,
    minimum_wake: bool,
) -> bool:
    """原子提交一批缓存输入，最低唤起同时保存持久唤醒意图。

    Args:
        connection: Store 专属线程借用的权威连接。
        expected: 尚未提交的 attention/priority 版本快照。
        minimum_wake: 本批是否包含可信高价值输入。

    Returns:
        全部版本匹配才返回 True；冲突不产生部分提交。
    """
    if not expected or any(item.stage not in ("attention", "priority") for item in expected):
        raise ValueError("提交需要尚未交接的非空输入批次。")
    if minimum_wake != any(item.stage == "priority" for item in expected):
        raise ValueError("最低唤起必须由本批可信高价值输入建立。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if any(processing(connection, item.input_id) != item for item in expected):
            return False
        for item in expected:
            connection.execute(
                "UPDATE input_processing SET version=version+1,stage=?,reason=? WHERE input_id=?",
                (
                    "foreground" if minimum_wake else "assessing",
                    "trusted.batch_wake" if minimum_wake else "attention.pending",
                    item.input_id,
                ),
            )
    return True


def take_foreground_inputs(connection: sqlite3.Connection) -> tuple[str, ...]:
    """唤醒时原子取尽已提交输入并消耗唤醒意图。

    Args:
        connection: Store 专属线程借用的权威连接。

    Returns:
        按接管顺序冻结的本轮输入；没有 foreground 唤醒请求时为空。

    Note:
        claimed 在 begin_turn 前已离开收件箱；其后的输入只能进入下一轮。
        未提交的 attention/priority 不属于主脑状态，由协调者先完成交接。
        调用方 Store 持有 BEGIN IMMEDIATE，将睡眠门槛、取批及原文恢复同事务提交。
    """
    if not pending_ids(connection, "foreground", 1):
        return ()
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT p.input_id FROM pending_inputs p JOIN input_processing s USING(input_id) "
        "WHERE s.stage IN ('assessing','submitted','foreground') ORDER BY p.sequence"
    ).fetchall()
    ids = tuple(_string(row[0]) for row in rows)
    for input_id in ids:
        connection.execute(
            "UPDATE input_processing SET version=version+1,stage='claimed' WHERE input_id=?",
            (input_id,),
        )
    return ids


def decide_inputs(
    connection: sqlite3.Connection,
    expected: tuple[InputProcessing, ...],
    level: AttentionLevel,
    reason: str,
    *,
    request_id: str | None,
) -> bool:
    """按提交版本采用整批注意结果，observe 保留收件箱而不请求唤醒。

    Args:
        connection: Store 专属线程借用的权威连接。
        expected: 已提交且尚未被主脑取走的整批版本。
        level: observe、wake 或 urgent。
        reason: 实际判断或明确未完成的原因。
        request_id: 实际模型请求身份；程序处置为 None。

    Returns:
        是否整批采用；迟到判断不更改任何输入。
    """
    if not expected or any(item.stage != "assessing" for item in expected):
        raise ValueError("注意提交需要已进入主脑收件箱的非空批次。")
    if level not in ("observe", "wake", "urgent"):
        raise ValueError("注意级别无效。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if any(processing(connection, item.input_id) != item for item in expected):
            return False
        for item in expected:
            connection.execute(
                "UPDATE input_processing SET version=version+1,stage=?,reason=?,"
                "attention_request_id=? WHERE input_id=?",
                (
                    "submitted" if level == "observe" else "foreground",
                    reason,
                    request_id,
                    item.input_id,
                ),
            )
    return True


def register_minimum_wake_assessment(
    connection: sqlite3.Connection,
    expected: tuple[InputProcessing, ...],
    *,
    owner_id: str,
    observed_generation: int,
) -> bool:
    """为已保底唤醒的整批冻结一次旧生成升级资格。

    Args:
        connection: Store 专属线程借用的权威连接。
        expected: 本批交接后的完整版本；首条身份也是稳定批次身份。
        owner_id: 当前宿主实例，重启不得继承。
        observed_generation: 本批交接时正在处理的旧生成。

    Returns:
        是否首次登记；重放和版本冲突不建立新判断。
    """
    if (
        not expected
        or any(item.stage != "foreground" for item in expected)
        or not owner_id
        or observed_generation <= 0
    ):
        raise ValueError("最低唤起升级必须绑定整批待处理输入和有效旧生成。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if any(processing(connection, item.input_id) != item for item in expected):
            return False
        cursor = connection.execute(
            "INSERT OR IGNORE INTO minimum_wake_assessments"
            "(batch_id,expected_json,owner_id,observed_generation,state) VALUES(?,?,?,?,'pending')",
            (expected[0].input_id, encode({"inputs": expected}), owner_id, observed_generation),
        )
        return cursor.rowcount == 1


def minimum_wake_assessments(
    connection: sqlite3.Connection, owner_id: str, *, limit: int
) -> tuple[MinimumWakeAssessment, ...]:
    """有限读取本宿主的批次升级待办，版本从冻结材料恢复。

    Args:
        connection: Store 专属线程借用的权威连接。
        owner_id: 当前宿主实例。
        limit: 最大批次数。

    Returns:
        按登记顺序排列的冻结批次。
    """
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT batch_id,expected_json,observed_generation FROM minimum_wake_assessments "
        "WHERE owner_id=? AND state='pending' ORDER BY rowid LIMIT ?",
        (owner_id, limit),
    ).fetchall()
    result: list[MinimumWakeAssessment] = []
    for row in rows:
        decoded = decode_object(_string(row[1]).encode("utf-8")).get("inputs")
        if not isinstance(decoded, list) or not decoded:
            raise StorageError("升级判断批次材料损坏。", code="invalid_record")
        expected: list[InputProcessing] = []
        for item in decoded:
            if not isinstance(item, dict) or item.get("stage") != "foreground":
                raise StorageError("升级判断输入版本损坏。", code="invalid_record")
            expected.append(
                InputProcessing(
                    _string(item.get("input_id")),
                    _integer(item.get("version")),
                    "foreground",
                    _string(item.get("reason")),
                    _optional_text(item.get("attention_request_id")),
                    _number(item.get("admitted_at")),
                )
            )
        result.append(
            MinimumWakeAssessment(_string(row[0]), tuple(expected), owner_id, _integer(row[2]))
        )
    return tuple(result)


def finish_minimum_wake_assessment(
    connection: sqlite3.Connection,
    assessment: MinimumWakeAssessment,
    level: AttentionLevel,
    reason: str,
    *,
    request_id: str | None,
) -> bool:
    """永久结案批次判断，保底唤醒不受 observe 降级。

    Args:
        connection: Store 专属线程借用的权威连接。
        assessment: 本宿主登记的原批次与旧生成。
        level: 实际判断结果。
        reason: 实际原因或观察过期说明。
        request_id: 实际请求证据，未调用模型时为 None。

    Returns:
        只有原批次仍完整留在收件箱时返回 True，供协调者决定是否抢占。
    """
    if level not in ("observe", "wake", "urgent"):
        raise ValueError("注意级别无效。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE minimum_wake_assessments SET state='completed',level=?,reason=?,request_id=? "
            "WHERE batch_id=? AND owner_id=? AND observed_generation=? AND expected_json=? "
            "AND state='pending'",
            (
                level,
                reason,
                request_id,
                assessment.batch_id,
                assessment.owner_id,
                assessment.observed_generation,
                encode({"inputs": assessment.expected}),
            ),
        )
        if cursor.rowcount != 1 or any(
            processing(connection, item.input_id) != item for item in assessment.expected
        ):
            return False
        for item in assessment.expected:
            connection.execute(
                "UPDATE input_processing SET version=version+1,reason=?,attention_request_id=? "
                "WHERE input_id=?",
                (reason, request_id, item.input_id),
            )
        return True


def session(connection: sqlite3.Connection, scope: SessionScope) -> SessionRecord:
    """返回尚未建立的空版本或完整已提交历史，不以空值覆盖损坏记录。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        scope: 目标连续会话，foreground 与 reflection 拥有独立历史。

    Returns:
        会话已提交版本与完整检查点，不包含尚未提交的临时历史。
    """
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
    """先接管处理身份与固定会话版本。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        turn_id: 连续会话中本轮处理的身份。
        scope: 目标连续会话，foreground 与 reflection 拥有独立历史。
        expected_version: 提交前观察到的版本，用于拒绝并发覆盖。
        generation: 当前会话或设备的代次，用于拒绝迟到结果更新新状态。
        input_ids: 本轮接管的输入身份集合，保留真实因果关联。

    Returns:
        本轮接管身份、来源输入、生成代次与当前结论。

    Raises:
        StorageError: 会话版本变化或已有在途处理；输入不再具有前台交接资格。

    Note:
        不得与同一会话的在途轮次重叠。
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        current = session(connection, scope)
        if current.version != expected_version or current.active_turn is not None:
            raise StorageError("会话版本变化或已有在途处理。", code="session_conflict")
        for input_id in input_ids:
            item = processing(connection, input_id)
            if item is None or item.stage not in ("priority", "foreground", "claimed"):
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
    """成功检查点和输入结案原子安装。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        turn: 已接管的本轮记录，携带提交前版本与生成代次。
        outcome: 此次操作的真实结论，不由期望效果推断成功。
        checkpoint_json: 完整 Core 检查点的编码；失败轮次可不安装新历史。
        error_json: 编码后的失败证据，供后续交互追溯原轮次。
        accepted_failures: 当前成功交互明确承接的先前失败轮次身份。 默认不附加此类条目。

    Returns:
        会话已提交版本与完整检查点，不包含尚未提交的临时历史。

    Raises:
        ValueError: 参数值或组合不满足本入口的明确约束，错误正文指出具体字段。
        StorageError: 权威记录缺失、版本不一致、载荷无效或提交条件不成立。

    Note:
        失败保留来源和旧历史，不重放行动。
    """
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
        # 结案使用 begin_turn 冻结的权威来源集合，不能让调用方构造的
        # TurnRecord 改写输入归属；本轮运行中不再接管后续刺激。
        actual_turn = get_turn(connection, turn.turn_id)
        if actual_turn is None:
            raise StorageError("活动轮次缺少权威记录。", code="invalid_record")
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
        for input_id in actual_turn.input_ids:
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
    """读取尚未由后续成功交互承接的失败事实，事实永久留在原批次。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        scope: 目标连续会话，foreground 与 reflection 拥有独立历史。

    Returns:
        条目集合：本轮接管身份、来源输入、生成代次与当前结论。

    Raises:
        StorageError: 失败经历引用非失败轮次。
    """
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
    """查询已接管主动处理，取消 begin_turn 等待后仍可核对真正所有权。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        turn_id: 连续会话中本轮处理的身份。

    Returns:
        本轮接管身份、来源输入、生成代次与当前结论。 未取得对应记录或素材时返回 None；
        读取故障仍抛出。

    Raises:
        StorageError: 主体处理身份损坏。
    """
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
    """在启动事务中恢复未开始的唤醒，并将已开始的旧轮次记为中断。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
    """
    connection.execute("SELECT input_id,stage FROM input_processing LIMIT 0")
    connection.execute(
        "SELECT batch_id,expected_json,owner_id,observed_generation,state,level,reason,request_id "
        "FROM minimum_wake_assessments LIMIT 0"
    )
    # 启动事务由 Store 持有；恢复失败不能单独提交任何子系统。
    # 重启没有旧宿主正在运行的生成身份。只撤销升级资格，原最低唤起
    # 继续留在前台；不得根据新进程恰好相同的数字 generation 重新判断。
    connection.execute("UPDATE minimum_wake_assessments SET state='expired' WHERE state='pending'")
    # 尚未 begin_turn 的 claimed 没有执行过模型或行动，可以恢复唤醒；
    # 普通未完成判断重新合批。observe 已完成的 submitted 不自动唤醒。
    connection.execute(
        "UPDATE input_processing SET version=version+1,stage='foreground' WHERE stage='claimed'"
    )
    connection.execute(
        "UPDATE input_processing SET version=version+1,stage='attention' WHERE stage='assessing'"
    )
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
