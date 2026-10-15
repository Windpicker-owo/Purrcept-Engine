"""在同一权威连接中桥接真实反馈，并保存后台覆盖的接管身份。

执行和活动账本仍保存原始事实。本模块把它们交给认识原文与有限前台待办，
两者成功交接后才确认对应消费者。派生唤醒不成为新的独立反思证据；计划的
投递也不代表计划完成。反思游标和尝试范围持久保存，失败不能靠时钟无限重跑。
所有入口由 Store 专属线程调用，不创建连接，不执行模型或设备操作。
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import time
from datetime import UTC, datetime

from pydantic import ValidationError

import purrcept_engine.domain.execution.models as execution_models
import purrcept_engine.domain.knowledge.models as knowledge_models
from purrcept_engine.domain.inputs.models import (
    InputContext,
    InputSource,
    InputSubmission,
    MediaAsset,
    MediaReference,
)
from purrcept_engine.domain.subject.feedback_models import (
    FeedbackBridge,
    FeedbackWork,
    ReflectionSource,
    ReflectionState,
)
from purrcept_engine.domain.subject.models import TurnRecord
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite import activities, execution, knowledge
from purrcept_engine.infrastructure.sqlite.time import AdmitInput
from purrcept_engine.serialization import encode
from purrcept_engine.values import decode_object

SCHEMA = (
    "CREATE TABLE feedback_deliveries (kind TEXT NOT NULL,identity TEXT NOT NULL,"
    "payload_json TEXT NOT NULL,occurred_at REAL NOT NULL,"
    "input_id TEXT REFERENCES inputs(input_id),"
    "PRIMARY KEY(kind,identity))",
    "CREATE TABLE feedback_sources (evidence_id TEXT NOT NULL,version INTEGER NOT NULL,"
    "priority INTEGER NOT NULL CHECK(priority IN(0,1)),PRIMARY KEY(evidence_id,version),"
    "FOREIGN KEY(evidence_id,version) REFERENCES knowledge_evidence(evidence_id,version))",
    "CREATE TABLE feedback_failed_turns (turn_id TEXT PRIMARY KEY "
    "REFERENCES subject_turns(turn_id))",
    "CREATE TABLE feedback_delivery_media(kind TEXT NOT NULL,identity TEXT NOT NULL,"
    "ordinal INTEGER NOT NULL,media_id TEXT NOT NULL REFERENCES media(media_id),"
    "media_type TEXT NOT NULL,name TEXT,PRIMARY KEY(kind,identity,ordinal),"
    "FOREIGN KEY(kind,identity) REFERENCES feedback_deliveries(kind,identity))",
    "CREATE TABLE feedback_supervision_signals(identity TEXT PRIMARY KEY "
    "REFERENCES knowledge_supervision(id),observed_generation INTEGER NOT NULL,"
    "notified INTEGER NOT NULL CHECK(notified IN(0,1)))",
    "CREATE TABLE feedback_reflection_attempts (batch_id TEXT NOT NULL,slice_ref TEXT NOT NULL "
    "REFERENCES knowledge_slices(slice_ref),epoch INTEGER NOT NULL,"
    "successful_turns INTEGER NOT NULL,PRIMARY KEY(slice_ref,epoch))",
    "CREATE TABLE feedback_reflection_epochs(epoch INTEGER PRIMARY KEY,batch_id TEXT NOT NULL,"
    "successful_turns INTEGER NOT NULL)",
    "CREATE TABLE feedback_reflection_cursors (evidence_id TEXT NOT NULL,version INTEGER NOT NULL,"
    "epoch INTEGER NOT NULL,cursor TEXT,exhausted INTEGER NOT NULL CHECK(exhausted IN(0,1)),"
    "PRIMARY KEY(evidence_id,version),FOREIGN KEY(evidence_id,version) "
    "REFERENCES knowledge_evidence(evidence_id,version))",
)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise StorageError("反馈账本文本字段损坏。", code="invalid_record")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError("反馈账本整数字段损坏。", code="invalid_record")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise StorageError("反馈账本时间字段损坏。", code="invalid_record")
    return float(value)


def _decode[Record: knowledge_models.SupervisionNotice | knowledge_models.WakeupPlan](
    value: object, model: type[Record]
) -> Record:
    try:
        return model.model_validate_json(_text(value))
    except ValidationError as error:
        raise StorageError("反馈账本记录损坏。", code="invalid_record") from error


def prepare(connection: sqlite3.Connection, *, now: float, limit: int = 16) -> FeedbackWork:
    """读取有限实际待办，不提前确认任何消费者。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        now: 本次规则计算采用的时刻，便于同一操作保持一致。
        limit: 本次读取或更新的最大条目数。 默认 16。

    Returns:
        同一次读取取得的有限待桥接来源。

    Raises:
        StorageError: 前台失败状态损坏。

    Note:
        无网络和新事实推断。
    """
    calls = execution.pending_call_receipts(connection, limit)
    activity = activities.pending_activity_feedback(connection, "reflection", limit=limit)
    foreground = activities.pending_activity_feedback(connection, "foreground", limit=limit)
    merged = {item.feedback_id: item for item in (*activity, *foreground)}
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT t.turn_id,t.scope,t.session_version,t.generation,t.state,t.started_at,"
        "t.finished_at,t.error_json FROM subject_turns t WHERE t.scope='foreground' AND "
        "t.state IN('failed','cancelled','interrupted') AND NOT EXISTS(SELECT 1 FROM "
        "feedback_failed_turns f WHERE f.turn_id=t.turn_id) ORDER BY t.started_at LIMIT ?",
        (limit,),
    ).fetchall()
    failures: list[TurnRecord] = []
    for row in rows:
        state = row[4]
        if state not in ("failed", "cancelled", "interrupted"):
            raise StorageError("前台失败状态损坏。", code="invalid_record")
        inputs: list[tuple[object, ...]] = connection.execute(
            "SELECT input_id FROM turn_inputs WHERE turn_id=? ORDER BY ordinal", (row[0],)
        ).fetchall()
        failures.append(
            TurnRecord(
                _text(row[0]),
                "foreground",
                _integer(row[2]),
                _integer(row[3]),
                state,
                tuple(_text(item[0]) for item in inputs),
                _number(row[5]),
                None if row[6] is None else _number(row[6]),
                None if row[7] is None else _text(row[7]),
            )
        )
    plan_rows: list[tuple[object, ...]] = connection.execute(
        "SELECT payload_json FROM knowledge_plans p WHERE "
        "json_extract(payload_json,'$.state')='pending' AND "
        "json_extract(payload_json,'$.due')<=? AND NOT EXISTS(SELECT 1 FROM feedback_deliveries d "
        "WHERE d.kind='plan' AND d.identity=p.id) ORDER BY json_extract(payload_json,'$.due'),id "
        "LIMIT ?",
        (now, limit),
    ).fetchall()
    notice_cursor = connection.execute(
        "SELECT id FROM knowledge_supervision s WHERE state='open' AND NOT EXISTS "
        "(SELECT 1 FROM feedback_deliveries d WHERE d.kind='supervision' AND d.identity=s.id) "
        "ORDER BY created_at,id"
    )
    notices: list[knowledge_models.SupervisionRecord] = []
    while notice_rows := notice_cursor.fetchmany(32):
        for notice_row in notice_rows:
            record = knowledge.get_supervision(connection, _text(notice_row[0]))
            if record is not None and record.active:
                notices.append(record)
                if len(notices) == limit:
                    break
        if len(notices) == limit:
            break
    deliveries = (
        connection.execute(
            "SELECT 1 FROM feedback_deliveries WHERE input_id IS NULL LIMIT 1"
        ).fetchone()
        is not None
    )
    return FeedbackWork(
        calls,
        tuple(merged.values()),
        tuple(failures),
        tuple(_decode(row[0], knowledge_models.WakeupPlan) for row in plan_rows),
        tuple(notices),
        deliveries,
    )


def _source(
    connection: sqlite3.Connection, snapshot: knowledge_models.EvidenceSnapshot, *, priority: bool
) -> None:
    """正式证据与宿主异常优先级一起接管，派生唤醒不会再次增加事实代次。"""
    knowledge.put_evidence_in_transaction(connection, snapshot)
    connection.execute(
        "INSERT OR IGNORE INTO feedback_sources VALUES(?,?,?)",
        (snapshot.evidence_id, snapshot.version, int(priority)),
    )


def _delivery(
    connection: sqlite3.Connection,
    kind: str,
    identity: str,
    payload: str,
    occurred_at: float,
    media: tuple[MediaReference, ...] = (),
) -> None:
    """持久 outbox 取得前台责任，原始消费者此后可以可靠确认交接。"""
    connection.execute(
        "INSERT OR IGNORE INTO feedback_deliveries VALUES(?,?,?,?,NULL)",
        (kind, identity, payload, occurred_at),
    )
    connection.executemany(
        "INSERT OR IGNORE INTO feedback_delivery_media VALUES(?,?,?,?,?,?)",
        (
            (kind, identity, position, item.media_id, item.media_type, item.name)
            for position, item in enumerate(media)
        ),
    )


def _admit_deliveries(
    connection: sqlite3.Connection, admit: AdmitInput, *, now: float, limit: int
) -> FeedbackBridge:
    """接管和投递身份同事务提交；额度满只保留 outbox，不丢另一反思责任。"""
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT kind,identity,payload_json,occurred_at FROM feedback_deliveries "
        "WHERE input_id IS NULL ORDER BY occurred_at,kind,identity LIMIT ?",
        (limit,),
    ).fetchall()
    result: list[str] = []
    urgent: list[int] = []
    blocked = False
    for row in rows:
        kind, identity = _text(row[0]), _text(row[1])
        if kind == "supervision":
            notice = knowledge.get_supervision(connection, identity)
            if notice is None or not notice.active:
                connection.execute(
                    "DELETE FROM feedback_delivery_media WHERE kind=? AND identity=?",
                    (kind, identity),
                )
                connection.execute(
                    "DELETE FROM feedback_deliveries WHERE kind=? AND identity=?", (kind, identity)
                )
                continue
        elif kind == "plan":
            current = connection.execute(
                "SELECT payload_json FROM knowledge_plans WHERE id=?", (identity,)
            ).fetchone()
            if (
                current is None
                or _decode(current[0], knowledge_models.WakeupPlan).state != "pending"
            ):
                connection.execute(
                    "DELETE FROM feedback_deliveries WHERE kind=? AND identity=?", (kind, identity)
                )
                continue
        stamp = datetime.fromtimestamp(_number(row[3]), UTC)
        media_rows: list[tuple[object, ...]] = connection.execute(
            "SELECT m.content,d.media_type,d.name,d.media_id FROM feedback_delivery_media d "
            "LEFT JOIN media m USING(media_id) WHERE kind=? AND identity=? ORDER BY ordinal",
            (kind, identity),
        ).fetchall()
        media: list[MediaAsset] = []
        for content, media_type, name, media_id in media_rows:
            if not isinstance(content, bytes) or hashlib.sha256(content).hexdigest() != media_id:
                raise StorageError("反馈媒体缺少权威字节。", code="invalid_record")
            media.append(
                MediaAsset(content, _text(media_type), None if name is None else _text(name))
            )
        submission = InputSubmission(
            InputSource("engine.feedback", "engine", kind, kind, identity),
            _text(row[2]),
            stamp,
            datetime.fromtimestamp(now, UTC),
            media=tuple(media),
            context=InputContext(minimum_wake=True),
        )
        # 保存点只包含派生输入，正式原文和另一消费者的责任交接仍归外层事务。
        connection.execute("SAVEPOINT feedback_input")
        try:
            admission = admit(connection, submission)
        except StorageError as error:
            connection.execute("ROLLBACK TO feedback_input")
            connection.execute("RELEASE feedback_input")
            if error.code != "input_capacity":
                raise
            blocked = True
            break
        connection.execute("RELEASE feedback_input")
        connection.execute(
            "UPDATE feedback_deliveries SET input_id=? WHERE kind=? AND identity=?",
            (admission.input_id, kind, identity),
        )
        # 派生输入用于唤醒前台，不再次证明同一事实；其不可变原文仍完整保留。
        connection.execute(
            "DELETE FROM pending_knowledge WHERE evidence_id=? AND version=1", (admission.input_id,)
        )
        result.append(admission.input_id)
        if kind == "supervision":
            signal = connection.execute(
                "SELECT observed_generation FROM "
                "feedback_supervision_signals WHERE identity=? "
                "AND notified=0",
                (identity,),
            ).fetchone()
            if signal is not None:
                urgent.append(_integer(signal[0]))
                connection.execute(
                    "UPDATE feedback_supervision_signals SET notified=1 WHERE identity=?",
                    (identity,),
                )
    return FeedbackBridge(tuple(result), bool(result), blocked, tuple(urgent))


def bridge(
    connection: sqlite3.Connection,
    admit: AdmitInput,
    work: FeedbackWork,
    *,
    now: float,
    limit: int = 16,
) -> FeedbackBridge:
    """原子桥接已读真实版本，第一版调用不额外开启前台预算，迟到版本只投一次。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        admit: 在调用方当前事务中正式接管输入的同步入口，不另开事务。
        work: 同一权威读取取得的有限反馈来源快照。
        now: 本次规则计算采用的时刻，便于同一操作保持一致。
        limit: 本次读取或更新的最大条目数。 默认 16。

    Returns:
        实际提交的消费者交接与仍需等待容量的状态。

    Raises:
        StorageError: 真实回执缺少行动或完成时刻；失败轮次缺少结束时刻。
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = False
        for call in work.calls:
            row = connection.execute(
                "SELECT input_id FROM actions WHERE action_id=?", (call.action_id,)
            ).fetchone()
            if row is None or call.finished_at is None:
                raise StorageError("真实回执缺少行动或完成时刻。", code="invalid_record")
            identity = f"call:{call.call_id}:{call.receipt_revision}"
            payload = encode(
                {
                    "source": "actual_call_receipt",
                    "call": call_material(call),
                    "causal_input_id": row[0],
                }
            )
            _source(
                connection,
                knowledge_models.EvidenceSnapshot(
                    evidence_id=identity,
                    version=1,
                    kind="call_receipt",
                    text=payload,
                    source_json=encode(
                        {
                            "call_id": call.call_id,
                            "action_id": call.action_id,
                            "receipt_revision": call.receipt_revision,
                        }
                    ),
                    occurred_at=call.finished_at,
                    media=call.media,
                ),
                priority=call.state in ("failed", "unknown"),
            )
            if call.receipt_revision > 1:
                _delivery(connection, "late_call", identity, payload, call.finished_at, call.media)
            connection.execute(
                "DELETE FROM pending_call_receipts WHERE call_id=? AND revision=?",
                (call.call_id, call.receipt_revision),
            )
            changed = True
        for item in work.activity:
            identity = f"activity:{item.feedback_id}"
            payload = encode({"source": "actual_activity_feedback", "feedback": item})
            _source(
                connection,
                knowledge_models.EvidenceSnapshot(
                    evidence_id=identity,
                    version=1,
                    kind="activity_feedback",
                    text=payload,
                    source_json=encode(
                        {"feedback_id": item.feedback_id, "activity_id": item.activity_id}
                    ),
                    occurred_at=item.occurred_at,
                ),
                priority=item.kind in ("completed", "failed", "needs_decision"),
            )
            row = connection.execute(
                "SELECT foreground FROM pending_activity_feedback WHERE feedback_id=?",
                (item.feedback_id,),
            ).fetchone()
            if row is not None and row[0] == 1:
                _delivery(connection, "activity", identity, payload, item.occurred_at)
            connection.execute(
                "UPDATE pending_activity_feedback SET reflection=0,foreground=0 "
                "WHERE feedback_id=?",
                (item.feedback_id,),
            )
            connection.execute(
                "DELETE FROM pending_activity_feedback WHERE feedback_id=? "
                "AND reflection=0 AND foreground=0",
                (item.feedback_id,),
            )
            changed = True
        for turn in work.failures:
            identity = f"foreground_failure:{turn.turn_id}"
            payload = encode({"source": "actual_foreground_failure", "turn": turn})
            stamp = turn.finished_at
            if stamp is None:
                raise StorageError("失败轮次缺少结束时刻。", code="invalid_record")
            _source(
                connection,
                knowledge_models.EvidenceSnapshot(
                    evidence_id=identity,
                    version=1,
                    kind="foreground_failure",
                    text=payload,
                    source_json=encode({"turn_id": turn.turn_id, "input_ids": turn.input_ids}),
                    occurred_at=stamp,
                ),
                priority=_foreground_failure_priority(turn),
            )
            connection.execute(
                "INSERT OR IGNORE INTO feedback_failed_turns VALUES(?)", (turn.turn_id,)
            )
            changed = True
        for plan in work.plans:
            _delivery(
                connection,
                "plan",
                plan.id,
                encode({"source": "due_plan", "plan": plan.model_dump(mode="json")}),
                plan.due,
            )
            changed = True
        for record in work.supervision:
            _delivery(
                connection,
                "supervision",
                record.notice.id,
                encode({"source": "supervision", "notice": record.notice.model_dump(mode="json")}),
                record.created_at,
            )
            changed = True
        result = _admit_deliveries(connection, admit, now=now, limit=limit)
        return FeedbackBridge(
            result.foreground_ids,
            changed or result.changed,
            result.blocked,
            result.urgent_generations,
        )


def _foreground_failure_priority(turn: TurnRecord) -> bool:
    """只由真实终态触发优先反思，整轮预算截止与单请求超时有不同含义。

    category 由会话自己观测整轮 Timeout.expired 后提交；普通 TimeoutError
    仍可能是传输或工具的真实最终故障。VAD/取消和明确预算耗尽保留经历，
    不给它们新的立即反思预算，也不扫描外界正文猜错误类别。
    """
    if turn.state != "failed":
        return False
    if turn.error_json is None:
        return True
    error = decode_object(turn.error_json.encode("utf-8"))
    return error.get("category") != "budget_exhausted" and error.get("error_type") not in (
        "RequestBudgetExceeded",
        "ToolLoopLimitError",
    )


def call_material(call: execution_models.CallRecord) -> dict[str, object]:
    """保存这一版本完整参数和结果，以确切引用关联旧回执而不复制其原文。

    Args:
        call: 当前真实能力或 Core 工具调用的冻结材料。

    Returns:
        业务对象，按实际结果提供 call_id、action_id、sequence、path、target_id、arguments_json、
        state、prepared_at、started_at、finished_at 等 字段。

    Note:
        同连接的可靠反馈桥接与当前请求原文核验共用本表示。调用方提供从执行
        账本取得的冻结 CallRecord；函数不解析外部正文、不决定效果或呈现资格。
        这是事实投影，不伪造一个 history 为空的 CallRecord；原始历史仍属于执行
        账本，各版反思原文身份来自已经核验的真实 CallResultRecord。
    """
    return {
        "call_id": call.call_id,
        "action_id": call.action_id,
        "sequence": call.sequence,
        "path": call.path,
        "target_id": call.target_id,
        "arguments_json": call.arguments_json,
        "state": call.state,
        "prepared_at": call.prepared_at,
        "started_at": call.started_at,
        "finished_at": call.finished_at,
        "receipt_revision": call.receipt_revision,
        "result_json": call.result_json,
        "error_type": call.error_type,
        "error": call.error,
        "media": call.media,
        "receipt_history_refs": tuple(
            f"call:{item.call_id}:{item.revision}" for item in call.history
        ),
    }


def dispatch_supervision(
    connection: sqlite3.Connection,
    admit: AdmitInput,
    record: knowledge_models.SupervisionRecord,
    *,
    observed_generation: int,
) -> FeedbackBridge:
    """交付前复核当前有效资格。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        admit: 在调用方当前事务中正式接管输入的同步入口，不另开事务。
        record: 当前业务记录的不可变快照。
        observed_generation: 接管或判断开始时观察到的前台生成代次。

    Returns:
        实际提交的消费者交接与仍需等待容量的状态。

    Note:
        已交接意见不再取得紧急抢占身份。
    """
    current = knowledge.get_supervision(connection, record.notice.id)
    if current is None or not current.active or not record.active or record.duplicate:
        return FeedbackBridge()
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT input_id FROM feedback_deliveries WHERE kind='supervision' AND identity=?",
            (record.notice.id,),
        ).fetchone()
        if record.notice.level == "urgent":
            connection.execute(
                "INSERT OR IGNORE INTO feedback_supervision_signals VALUES(?,?,0)",
                (record.notice.id, observed_generation),
            )
        _delivery(
            connection,
            "supervision",
            record.notice.id,
            encode({"source": "supervision", "notice": record.notice.model_dump(mode="json")}),
            record.created_at,
        )
        result = _admit_deliveries(connection, admit, now=time.time(), limit=16)
        row = connection.execute(
            "SELECT input_id FROM feedback_deliveries WHERE kind='supervision' AND identity=?",
            (record.notice.id,),
        ).fetchone()
        if existing is not None and row is not None and row[0] is not None:
            signal = connection.execute(
                "SELECT observed_generation FROM "
                "feedback_supervision_signals WHERE identity=? "
                "AND notified=0",
                (record.notice.id,),
            ).fetchone()
            if signal is not None:
                connection.execute(
                    "UPDATE feedback_supervision_signals SET notified=1 WHERE identity=?",
                    (record.notice.id,),
                )
                return FeedbackBridge(
                    urgent_generations=(*result.urgent_generations, _integer(signal[0]))
                )
        return result


def reflection_state(connection: sqlite3.Connection) -> ReflectionState:
    """按独立原文行序与真正成功轮次计算节奏，不把读取/监管回应当新事实。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。

    Returns:
        反思来源序号与实际成功前台轮数。
    """
    row = connection.execute(
        "SELECT coalesce(max(e.rowid),0),min(CASE WHEN p.state='pending' AND e.rowid>"
        "(SELECT coalesce(max(epoch),0) FROM feedback_reflection_epochs) "
        "THEN p.queued_at END) "
        "FROM knowledge_evidence e JOIN pending_knowledge p USING(evidence_id,version) "
        "WHERE e.valid=1"
    ).fetchone()
    count = connection.execute(
        "SELECT count(*) FROM subject_turns WHERE scope='foreground' AND state='succeeded'"
    ).fetchone()
    taken = connection.execute(
        "SELECT coalesce(max(successful_turns),0) FROM feedback_reflection_epochs"
    ).fetchone()
    priority = (
        connection.execute(
            "SELECT 1 FROM feedback_sources f JOIN pending_knowledge p "
            "USING(evidence_id,version) WHERE priority=1 "
            "AND p.state='pending' "
            "AND EXISTS(SELECT 1 FROM knowledge_evidence e WHERE "
            "e.evidence_id=f.evidence_id AND e.version=f.version AND e.rowid>"
            "(SELECT coalesce(max(epoch),0) FROM feedback_reflection_epochs)) "
            "LIMIT 1"
        ).fetchone()
        is not None
    )
    plan = connection.execute(
        "SELECT min(json_extract(payload_json,'$.due')) FROM "
        "knowledge_plans p WHERE "
        "json_extract(payload_json,'$.state')='pending' "
        "AND NOT EXISTS(SELECT 1 FROM feedback_deliveries d WHERE "
        "d.kind='plan' AND d.identity=p.id)"
    ).fetchone()
    return ReflectionState(
        _integer(row[0]),
        _integer(count[0]),
        _integer(taken[0]),
        None if row[1] is None else _number(row[1]),
        priority,
        None if plan[0] is None else _number(plan[0]),
    )


def _resume_cursor(connection: sqlite3.Connection, evidence_id: str, version: int) -> str | None:
    """从真实已复核范围的连续前缀续读；失败或暂缓的尝试不能跳过原页。

    reserve 保存的 next_cursor 表示取得位置，不能表示理解。只采用完成回执中
    当前有效的 reviewed 范围；之前 read_evidence 已原子创建每个分页边界游标。
    媒体没有完成复核时仍从首段开始，以便下个有新事实的批次获得完整媒体。
    """
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT r.slice_ref,r.state FROM knowledge_review_ranges r JOIN knowledge_slices s "
        "USING(slice_ref) JOIN reflection_batches b ON r.batch_id=b.batch_id AND "
        "r.batch_version=b.version WHERE s.evidence_id=? AND s.version=? AND "
        "b.state='completed' ORDER BY json_extract(b.receipt_json,'$.completed_at')",
        (evidence_id, version),
    ).fetchall()
    effective = {_text(row[0]): _text(row[1]) for row in rows}
    fragments: list[knowledge_models.EvidenceSlice] = []
    deferred_start: int | None = None
    for reference, state in effective.items():
        fragment = knowledge.get_slice(connection, reference)
        if fragment is None:
            raise StorageError("反思覆盖缺少原文范围。", code="invalid_record")
        if state == "reviewed":
            fragments.append(fragment)
        elif state == "deferred":
            deferred_start = (
                fragment.start if deferred_start is None else min(deferred_start, fragment.start)
            )
        else:
            raise StorageError("反思范围结论损坏。", code="invalid_record")
    snapshot = knowledge.get_evidence(connection, evidence_id, version=version)
    if snapshot is None:
        raise StorageError("反思候选缺少有效原文版本。", code="invalid_record")
    reviewed_media = {identity for fragment in fragments for identity in fragment.media_ids}
    if not {media.media_id for media in snapshot.media} <= reviewed_media:
        return None
    frontier = 0
    for fragment in sorted(fragments, key=lambda item: (item.start, item.end)):
        if fragment.start > frontier:
            break
        frontier = max(frontier, fragment.end)
    if deferred_start is not None:
        frontier = min(frontier, deferred_start)
    if frontier == 0:
        return None
    row = connection.execute(
        "SELECT cursor FROM knowledge_cursors WHERE evidence_id=? AND version=? AND start=? "
        "AND kind='evidence' ORDER BY page_chars,cursor LIMIT 1",
        (evidence_id, version, frontier),
    ).fetchone()
    if row is None:
        raise StorageError("反思已复核前缀缺少持久分页边界。", code="invalid_record")
    return _text(row[0])


def reflection_sources(
    connection: sqlite3.Connection, *, epoch: int, limit: int = 32, after: int = 0
) -> tuple[ReflectionSource, ...]:
    """新 epoch 可选旧未覆盖来源，扫描和分页不会自行开启另一个批次预算。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        epoch: 后台可靠来源的已提交单调序号，不等同于模型输出次数。
        limit: 本次读取或更新的最大条目数。 默认 32。
        after: 已观察的变更序号。 默认 0。

    Returns:
        条目集合：待复核来源的确切版本与可靠游标。
    """
    if (
        connection.execute(
            "SELECT 1 FROM feedback_reflection_epochs WHERE epoch=?", (epoch,)
        ).fetchone()
        is not None
    ):
        return ()
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT e.rowid,e.evidence_id,e.version,p.queued_at,"
        "coalesce(f.priority,0) FROM knowledge_evidence e JOIN pending_knowledge p "
        "USING(evidence_id,version) LEFT JOIN feedback_reflection_cursors c "
        "USING(evidence_id,version) LEFT JOIN feedback_sources f USING(evidence_id,version) "
        "WHERE e.valid=1 AND p.state IN('pending','deferred') AND e.rowid>? AND e.rowid<=? "
        "ORDER BY coalesce(f.priority,0) DESC,(c.epoch IS NOT NULL),e.rowid LIMIT ?",
        (after, epoch, limit),
    ).fetchall()
    return tuple(
        ReflectionSource(
            _integer(row[0]),
            _text(row[1]),
            _integer(row[2]),
            _number(row[3]),
            _resume_cursor(connection, _text(row[1]), _integer(row[2])),
            bool(row[4]),
        )
        for row in rows
    )


def reserve_reflection(
    connection: sqlite3.Connection,
    *,
    batch_id: str,
    epoch: int,
    pages: tuple[knowledge_models.EvidencePage, ...],
    successful_turns: int,
) -> bool:
    """在首次派发前保存覆盖尝试与下一页。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        batch_id: 独立反思批次的身份，须与批次版本配对使用。
        epoch: 后台可靠来源的已提交单调序号，不等同于模型输出次数。
        pages: 当前批次实际取得的完整原文页面。
        successful_turns: 已提交成功的前台轮数，用于后台调度判断。

    Raises:
        ValueError: 反思只能接管真实原文片段。
        AssertionError: 已核对原文片段。

    Note:
        失败不撤销接管，也不确认理解。
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if (
            connection.execute(
                "SELECT 1 FROM feedback_reflection_epochs WHERE epoch=?", (epoch,)
            ).fetchone()
            is not None
        ):
            return False
        for page in pages:
            fragment = page.slice
            if fragment is None:
                raise ValueError("反思只能接管真实原文片段。")
            duplicate = connection.execute(
                "SELECT 1 FROM feedback_reflection_attempts WHERE slice_ref=? AND epoch=?",
                (fragment.slice_ref, epoch),
            ).fetchone()
            if duplicate is not None:
                return False
        for page in pages:
            fragment = page.slice
            if fragment is None:
                raise AssertionError("已核对原文片段。")
            connection.execute(
                "INSERT INTO feedback_reflection_attempts VALUES(?,?,?,?)",
                (batch_id, fragment.slice_ref, epoch, successful_turns),
            )
            connection.execute(
                "INSERT INTO feedback_reflection_cursors VALUES(?,?,?,?,?) "
                "ON CONFLICT(evidence_id,version) DO UPDATE SET "
                "epoch=excluded.epoch,cursor=excluded.cursor,exhausted=excluded.exhausted",
                (
                    fragment.evidence_id,
                    fragment.version,
                    epoch,
                    page.next_cursor,
                    int(page.next_cursor is None),
                ),
            )
        connection.execute(
            "INSERT INTO feedback_reflection_epochs VALUES(?,?,?)",
            (epoch, batch_id, successful_turns),
        )
        return True


def initialize(connection: sqlite3.Connection) -> None:
    """启动时核对实际查询列。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。

    Note:
        接管身份保留，不自动重放中断批次或信号。
    """
    for columns, table in (
        ("kind,identity,payload_json,occurred_at,input_id", "feedback_deliveries"),
        ("kind,identity,ordinal,media_id,media_type,name", "feedback_delivery_media"),
        ("evidence_id,version,priority", "feedback_sources"),
        ("turn_id", "feedback_failed_turns"),
        ("identity,observed_generation,notified", "feedback_supervision_signals"),
        ("batch_id,slice_ref,epoch,successful_turns", "feedback_reflection_attempts"),
        ("epoch,batch_id,successful_turns", "feedback_reflection_epochs"),
        ("evidence_id,version,epoch,cursor,exhausted", "feedback_reflection_cursors"),
    ):
        connection.execute(f"SELECT {columns} FROM {table} LIMIT 0")
