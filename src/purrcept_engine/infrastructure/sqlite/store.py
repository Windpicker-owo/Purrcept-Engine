"""在专属 SQLite 线程上保存输入、媒体、行动意图和逐次真实回执。

``Store.open`` 创建单连接所有者；宿主须先持有数据目录实例锁。接管事务同时
检查额度并保存原文、媒体引用和待办。普通请求与处置提交使用独立有界队列，
处置可以先于尚未开始的普通事务，不能抢占已开始的 SQLite 事务。
睡眠普通输入在同一接管事务仅记历史并释放待办；旧睡眠积压在额度核验前收回，
原文、媒体与反思证据保留，高优先及已取批工作仍受原有额度约束。

等待取消不撤销已入队操作；原生来源可通过重投取得第一次接管身份。关闭只
执行一次，停止新的写入后排空已受理操作；连接收回前仍可接管有限只读查询。
权威故障阻止后续写入，保留原库和可读取事实；本模块不调度模型或插件，
不以空库或内存状态替代提交失败。
线程内程序异常原样交回当前和排队的等待者，并停止派发、关闭连接。
"""

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import purrcept_engine.domain.execution.record_validation as record_validation
import purrcept_engine.domain.knowledge.models as knowledge_models
import purrcept_engine.serialization as serialization
from purrcept_engine.domain.activities.models import (
    ActivityCommand,
    ActivityFeedback,
    ActivityRecord,
)
from purrcept_engine.domain.context.maintenance import (
    Reservation,
    SummaryProposal,
    SummarySource,
)
from purrcept_engine.domain.context.usage import UsageAnchor
from purrcept_engine.domain.execution.models import (
    ActionOutcome,
    ActionRecord,
    CallOutcome,
    CallRecord,
)
from purrcept_engine.domain.inputs.models import (
    Admission,
    InputContext,
    InputRecord,
    InputSource,
    InputSubmission,
    MediaReference,
)
from purrcept_engine.domain.model_requests.models import (
    AttemptEvidence,
    AttemptStart,
    RequestEvidence,
    RequestOutcome,
    RequestRecord,
)
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
from purrcept_engine.domain.time.models import RestState
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite import activities as activity_storage
from purrcept_engine.infrastructure.sqlite import execution as execution_storage
from purrcept_engine.infrastructure.sqlite import feedback as feedback_storage
from purrcept_engine.infrastructure.sqlite import knowledge as knowledge_storage
from purrcept_engine.infrastructure.sqlite import maintenance as maintenance_storage
from purrcept_engine.infrastructure.sqlite import model_requests as model_storage
from purrcept_engine.infrastructure.sqlite import projections as projection_storage
from purrcept_engine.infrastructure.sqlite import retention as retention_storage
from purrcept_engine.infrastructure.sqlite import subject as subject_storage
from purrcept_engine.infrastructure.sqlite import time as time_storage
from purrcept_engine.infrastructure.sqlite.audio_port import AudioLedger
from purrcept_engine.infrastructure.sqlite.controls import ControlLedger
from purrcept_engine.infrastructure.sqlite.feedback_port import FeedbackLedger
from purrcept_engine.infrastructure.sqlite.human_feedback import HumanFeedbackLedger
from purrcept_engine.infrastructure.sqlite.knowledge_port import KnowledgeLedger
from purrcept_engine.infrastructure.sqlite.queue import SqliteExecutor
from purrcept_engine.infrastructure.sqlite.time_port import TimeLedger
from purrcept_engine.values import ActionProtocolError, JsonObject, JsonValue, decode_object

_logger = logging.getLogger(__name__)

_INPUT_COLUMNS = (
    "input_id, adapter_id, account_id, conversation_id, event_kind, native_event_id, "
    "sender_id, display_name, text, occurred_at, received_at, logical_bytes, context_json"
)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise StorageError("权威记录中的字符串字段无效。", code="invalid_record")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _input_context(value: object) -> InputContext:
    """读取持久化可信信号时统一严格核验，睡眠分流不能靠宽松 JSON 真值授权。"""
    try:
        fields = decode_object(_text(value).encode("utf-8"))
        if (
            set(fields)
            != {
                "type",
                "details_json",
                "minimum_wake",
                "interrupt_generation",
                "speech_started",
                "missing_materials",
            }
            or fields["type"] != "InputContext"
        ):
            raise ValueError("持久化输入关系的类型或字段不符合格式。")
        if any(
            type(fields[key]) is not bool
            for key in (
                "minimum_wake",
                "interrupt_generation",
                "speech_started",
            )
        ):
            raise ValueError("持久化程序信号必须是 bool。")
        missing = fields["missing_materials"]
        if not isinstance(missing, list):
            raise ValueError("持久化材料缺口必须是数组。")
        return InputContext(
            details_json=_text(fields["details_json"]),
            minimum_wake=fields["minimum_wake"] is True,
            interrupt_generation=fields["interrupt_generation"] is True,
            speech_started=fields["speech_started"] is True,
            missing_materials=tuple(_text(item) for item in missing),
        )
    except (ActionProtocolError, ValueError, KeyError) as error:
        raise StorageError("输入关系和程序信号损坏。", code="invalid_record") from error


def _sleep_record_only(adapter_id: str, context: InputContext) -> bool:
    """睡眠时只有可信高优先外部输入保留前台资格，旧主动思考不能绕过睡眠。"""
    return adapter_id == "subject.idle" or not (
        context.minimum_wake or context.interrupt_generation or context.speech_started
    )


def _record_sleep_input(connection: sqlite3.Connection, identity: str) -> None:
    """仅结案调度并释放待办名额；原文、媒体、证据和反思待办均不删除。

    processed 在此只表示明确记录，不表示主脑或注意模型已经看过。版本递增
    撤销睡前注意的旧观察；调用者的同一事务同时承担回滚和通知责任。
    """
    connection.execute(
        "UPDATE input_processing SET version=version+1,stage='processed',"
        "reason='subject.sleeping.recorded' WHERE input_id=?",
        (identity,),
    )
    connection.execute("DELETE FROM pending_inputs WHERE input_id=?", (identity,))


def _record_sleep_inputs(connection: sqlite3.Connection) -> RestState | None:
    """当前事务内收回睡眠普通积压；接管额度检查、协调和取批共用同一门槛。"""
    rest = time_storage.get_rest(connection)
    if rest is None:
        return None
    rows: list[tuple[str, str, str]] = connection.execute(
        "SELECT p.input_id,i.adapter_id,i.context_json FROM pending_inputs p "
        "JOIN input_processing s USING(input_id) JOIN inputs i USING(input_id) "
        "WHERE s.stage IN ('attention','assessing','submitted','foreground','priority')"
    ).fetchall()
    for identity, adapter_id, context_json in rows:
        if _sleep_record_only(adapter_id, _input_context(context_json)):
            _record_sleep_input(connection, identity)
    return rest


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StorageError("权威记录中的非负整数字段无效。", code="invalid_record")
    return value


def _datetime(value: object) -> datetime:
    """拒绝无法重建的持久化时间，不以当前时间补齐坏记录。"""
    try:
        result = datetime.fromisoformat(_text(value))
    except ValueError as error:
        raise StorageError("权威记录中的时间无效。", code="invalid_record") from error
    if result.utcoffset() is None:
        raise StorageError("权威记录中的时间缺少时区。", code="invalid_record")
    return result


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _positive(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} 必须是正整数。")


def _active_supervision(
    connection: sqlite3.Connection,
) -> Iterator[knowledge_models.SupervisionRecord]:
    """分块扫描全部未回应意见，逐项核验来源、期限及活动版本。

    32 只是数据库读取块大小，不是主体当前材料的选择上限。此迭代在一个
    Store 作业内结束；不跨异步等待持有游标，也不确认或再次投递意见。
    """
    cursor = connection.execute(
        "SELECT id FROM knowledge_supervision WHERE state='open' ORDER BY created_at,id"
    )
    while rows := cursor.fetchmany(32):
        for row in rows:
            record = knowledge_storage.get_supervision(connection, _text(row[0]))
            if record is None:
                raise StorageError("监管投影引用缺失意见。", code="invalid_record")
            if record.active:
                yield record


def _action_snapshot(
    connection: sqlite3.Connection,
    snapshot: knowledge_models.EvidenceSnapshot,
    *,
    current: ActionRecord | None = None,
) -> ActionRecord:
    """只采用与真实行动及历史回执逐字一致的内部即时快照关系。

    play_action 的即时快照可能早于迟到真实回执；按已保存的回执版本还原
    当时结果，不能要求它与最新结果相等或靠 action_result 身份前缀猜关系。
    原文、类型或来源不符属于权威记录损坏，不静默制造来源资格。
    """
    source = decode_object(snapshot.source_json.encode("utf-8"))
    action_id = source.get("action_id")
    if (
        snapshot.version != 1
        or not isinstance(action_id, str)
        or source != {"source": "actual_action_result", "action_id": action_id}
    ):
        raise StorageError("即时行动快照缺少确切内部来源。", code="invalid_record")
    action = current if current is not None else execution_storage.get_action(connection, action_id)
    if action is None:
        raise StorageError("即时行动快照引用缺失行动。", code="invalid_record")
    if action.action_id != action_id:
        raise StorageError("即时行动快照与给定行动身份不一致。", code="invalid_record")
    payload = decode_object(snapshot.text.encode("utf-8"))
    raw_calls = payload.get("calls")
    if not isinstance(raw_calls, list) or len(raw_calls) != len(action.calls):
        raise StorageError("即时行动快照与实际调用不一致。", code="invalid_record")
    frozen: list[CallRecord] = []
    for original, call in zip(raw_calls, action.calls, strict=True):
        if not isinstance(original, dict):
            raise StorageError("即时行动快照调用形状无效。", code="invalid_record")
        revision = original.get("receipt_revision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or not (1 <= revision <= len(call.history))
        ):
            raise StorageError("即时行动快照回执版本无效。", code="invalid_record")
        receipt = call.history[revision - 1]
        frozen.append(
            replace(
                call,
                state=receipt.state,
                receipt_revision=revision,
                finished_at=receipt.finished_at,
                result_json=receipt.result_json,
                error_type=receipt.error_type,
                error=receipt.error,
                history=call.history[:revision],
                media=receipt.media,
            )
        )
    candidate = replace(action, calls=tuple(frozen))
    if (
        snapshot.text != serialization.encode(candidate)
        or snapshot.occurred_at != action.finished_at
        or snapshot.media
        != tuple(dict.fromkeys(reference for call in candidate.calls for reference in call.media))
    ):
        raise StorageError("即时行动快照与冻结实际结果不一致。", code="invalid_record")
    return candidate


def _call_original_refs(
    connection: sqlite3.Connection, action: ActionRecord
) -> dict[str, JsonObject]:
    """只给出当前完整调用版本确实匹配的已存原文引用。

    即时行动快照先于可靠反馈桥接即可存在，优先使用它。旧快照仍是有效
    历史，但迟到回执改变版本或实际值后不能用它代替当前结果。次选原文须
    已通过宿主反馈登记；尚未桥接时不返回引用，不手造将来可能出现的身份。
    一份完整行动快照只核验一次，不为每项调用重复重建整份历史。
    """
    originals: dict[str, JsonObject] = {}
    calls = {call.call_id: call for call in action.calls}
    cursor = connection.execute(
        "SELECT evidence_id,version FROM knowledge_evidence WHERE valid=1 AND "
        "json_extract(payload_json,'$.kind')='action_result' AND "
        "json_extract(json_extract(payload_json,'$.source_json'),'$.action_id')=? "
        "ORDER BY created_at,evidence_id,version",
        (action.action_id,),
    )
    while rows := cursor.fetchmany(32):
        for row in rows:
            snapshot = knowledge_storage.get_evidence(
                connection, _text(row[0]), version=_integer(row[1])
            )
            if snapshot is None:
                raise StorageError("调用原文引用缺失有效即时快照。", code="invalid_record")
            frozen = _action_snapshot(connection, snapshot, current=action)
            for original in frozen.calls:
                if original == calls[original.call_id]:
                    originals.setdefault(
                        original.call_id, {"id": snapshot.evidence_id, "version": snapshot.version}
                    )
    for call in action.calls:
        if call.call_id not in originals:
            reference = _call_receipt_ref(connection, action, call)
            if reference is not None:
                originals[call.call_id] = reference
    return originals


def _call_receipt_ref(
    connection: sqlite3.Connection, action: ActionRecord, call: CallRecord
) -> JsonObject | None:
    """核验已由可靠反馈桥接产生的确切当前原文，旧版只保持历史事实。"""

    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT e.evidence_id,e.version FROM knowledge_evidence e "
        "JOIN feedback_sources f USING(evidence_id,version) WHERE e.valid=1 AND "
        "json_extract(e.payload_json,'$.kind')='call_receipt' AND "
        "json_extract(json_extract(e.payload_json,'$.source_json'),'$.call_id')=? AND "
        "json_extract(json_extract(e.payload_json,'$.source_json'),'$.action_id')=? AND "
        "json_extract(json_extract(e.payload_json,'$.source_json'),'$.receipt_revision')=? "
        "ORDER BY e.created_at,e.evidence_id,e.version",
        (call.call_id, action.action_id, call.receipt_revision),
    ).fetchall()
    for row in rows:
        snapshot = knowledge_storage.get_evidence(
            connection, _text(row[0]), version=_integer(row[1])
        )
        if snapshot is None:
            raise StorageError("调用原文引用缺失可靠反馈快照。", code="invalid_record")
        expected_source = {
            "call_id": call.call_id,
            "action_id": action.action_id,
            "receipt_revision": call.receipt_revision,
        }
        expected_text = serialization.encode(
            {
                "source": "actual_call_receipt",
                "call": feedback_storage.call_material(call),
                "causal_input_id": action.input_id,
            }
        )
        if (
            snapshot.version != 1
            or snapshot.source_json != serialization.encode(expected_source)
            or snapshot.text != expected_text
            or snapshot.occurred_at != call.finished_at
            or snapshot.media != call.media
        ):
            raise StorageError("可靠调用原文与当前完整回执不一致。", code="invalid_record")
        return {"id": snapshot.evidence_id, "version": snapshot.version}
    return None


def _evidence_inputs(connection: sqlite3.Connection, references: tuple[str, ...]) -> frozenset[str]:
    """沿真实原文、片段和宿主回执记录取得输入关系，不解析正文中的身份。

    call_receipt 只有带可靠反馈登记的来源才关联行动；即时 action_result 则
    重新核验完整行动快照。这里仅用于未决保护，不授予行动或认识写入资格。
    """
    encoded = json.dumps(references)
    roots = (
        "WITH roots(evidence_id,version) AS ("
        "SELECT e.evidence_id,max(e.version) FROM knowledge_evidence e WHERE e.evidence_id IN "
        "(SELECT value FROM json_each(?)) GROUP BY e.evidence_id UNION "
        "SELECT evidence_id,version FROM knowledge_slices WHERE slice_ref IN "
        "(SELECT value FROM json_each(?))) "
    )
    rows: list[tuple[object, ...]] = connection.execute(
        roots + "SELECT i.input_id FROM roots r JOIN inputs i ON i.input_id=r.evidence_id UNION "
        "SELECT a.input_id FROM roots r JOIN knowledge_evidence e USING(evidence_id,version) "
        "JOIN feedback_sources f USING(evidence_id,version) "
        "JOIN actions a ON a.action_id=json_extract("
        "json_extract(e.payload_json,'$.source_json'),'$.action_id') "
        "WHERE json_extract(e.payload_json,'$.kind')='call_receipt' AND a.input_id IS NOT NULL",
        (encoded, encoded),
    ).fetchall()
    identities = {_text(row[0]) for row in rows}
    snapshots: list[tuple[object, ...]] = connection.execute(
        roots + "SELECT evidence_id,version FROM roots JOIN knowledge_evidence "
        "USING(evidence_id,version) WHERE json_extract(payload_json,'$.kind')='action_result'",
        (encoded, encoded),
    ).fetchall()
    for row in snapshots:
        snapshot = knowledge_storage.get_evidence(
            connection, _text(row[0]), version=_integer(row[1]), include_inactive=True
        )
        if snapshot is None:
            raise StorageError("未决来源引用缺失即时快照。", code="invalid_record")
        identity = _action_snapshot(connection, snapshot).input_id
        if identity is not None:
            identities.add(identity)
    return frozenset(identities)


def _protected_inputs(
    connection: sqlite3.Connection, selected: tuple[str, ...] | None
) -> frozenset[str]:
    """从业务关系选择仍需保护的输入，不从正文、自然语言或身份前缀猜测。

    selected 为历史中的来源时只返回其子集；None 用于完整当前状态投影。
    监管保护同时包含已投递的派生输入及原文片段关联的真实来源。意见已回应
    或失效即不再授予保护，未知调用则须由新的真实回执改变状态。
    """
    # 从仍未解决的业务事实出发，再按输入主键核验身份。若先遍历全部 inputs，
    # 再逐条关联没有 input_id 索引的 turn_inputs，历史增长会形成近似平方扫描；
    # 这会占住唯一提交线程，让本来很快的 WebUI 快照也排队超时。
    selection = "" if selected is None else " WHERE i.input_id IN (SELECT value FROM json_each(?))"
    rows: list[tuple[object, ...]] = connection.execute(
        "WITH unresolved(input_id) AS ("
        "SELECT a.input_id FROM calls c JOIN actions a ON a.action_id=c.action_id "
        "WHERE c.state IN ('prepared','started','unknown') UNION "
        "SELECT a.input_id FROM actions a JOIN calls c ON c.action_id=a.action_id "
        "JOIN activity_commands cmd ON cmd.command_id=c.call_id "
        "JOIN activities act ON act.activity_id=cmd.activity_id "
        "WHERE act.state NOT IN ('ended','completed') UNION "
        "SELECT t.input_id FROM failed_turns f JOIN turn_inputs t ON t.turn_id=f.turn_id "
        "WHERE f.presented_in IS NULL) "
        "SELECT i.input_id FROM unresolved u JOIN inputs i ON i.input_id=u.input_id" + selection,
        () if selected is None else (json.dumps(selected),),
    ).fetchall()
    protected = {_text(row[0]) for row in rows}
    selected_ids = None if selected is None else frozenset(selected)
    cursor = connection.execute(
        "SELECT json_extract(payload_json,'$.evidence') FROM knowledge_plans WHERE "
        "json_extract(payload_json,'$.state')='pending'"
    )
    while plans := cursor.fetchmany(32):
        plan_inputs = _evidence_inputs(connection, tuple(_text(row[0]) for row in plans))
        protected.update(
            identity for identity in plan_inputs if selected_ids is None or identity in selected_ids
        )
    for record in _active_supervision(connection):
        linked: list[tuple[object, ...]] = connection.execute(
            "SELECT input_id FROM feedback_deliveries WHERE kind='supervision' AND identity=? "
            "AND input_id IS NOT NULL",
            (record.notice.id,),
        ).fetchall()
        for identity in (
            *(_text(row[0]) for row in linked),
            *_evidence_inputs(connection, record.notice.evidence),
        ):
            if selected_ids is None or identity in selected_ids:
                protected.add(identity)
    return frozenset(protected)


def input_logical_bytes(submission: InputSubmission) -> int:
    """计量每项原文、关系和真实媒体，供外部输入及同库业务交接共用。

    Args:
        submission: 待正式接管的来源、原文、媒体与可信信号。

    Raises:
        StorageError: 输入正文不能表示为 UTF-8。
    """
    try:
        text_bytes = len(submission.text.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise StorageError("输入正文不能表示为 UTF-8。", code="invalid_input") from error
    context_bytes = (
        0
        if submission.context.details_json == "{}"
        else len(submission.context.details_json.encode("utf-8"))
    )
    context_bytes += sum(
        len(reason.encode("utf-8")) for reason in submission.context.missing_materials
    )
    logical_bytes = (
        text_bytes + context_bytes + sum(len(asset.content) for asset in submission.media)
    )
    return logical_bytes


def admit_in_transaction(
    connection: sqlite3.Connection,
    submission: InputSubmission,
    *,
    max_inputs: int,
    max_input_bytes: int,
    logical_bytes: int,
) -> Admission:
    """由调用事务原子保存完整输入、待办和来源，不开始或提交另一个事务。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        submission: 待正式接管的来源、原文、媒体与可信信号。
        max_inputs: 有界提交队列允许接管的条目上限。
        max_input_bytes: 有界提交队列允许接管的逻辑字节上限。
        logical_bytes: 本次操作占用的逻辑载荷字节数，用于接管前背压。

    Returns:
        正式接管凭证，包含 input_id 与重复来源标记；不表示模型已处理或对外回复。

    Raises:
        StorageError: 原生输入身份与已接管事实冲突；未处置输入的接管额度已满。

    Note:
        Store 的外部接管及时间服务共享这一交接：闹铃消费与一次输入保存不能
        分两次提交。调用方拥有额度核验和失败回滚，去重仍按同一原生事实处理。
        同事务采用睡眠规则，普通输入只记历史；单条大小和瞬时提交仍有界，
        不通过无界待办或扩大高优先额度来解决睡眠积压。
    """
    source = submission.source
    media_ids = tuple(hashlib.sha256(asset.content).hexdigest() for asset in submission.media)
    source_facts = (
        source.adapter_id,
        source.account_id,
        source.conversation_id,
        source.event_kind,
        source.native_event_id,
        source.sender_id,
        source.display_name,
    )
    dedup_key = None
    if source.native_event_id is not None:
        dedup_key = json.dumps(source_facts[:5], separators=(",", ":"))
    # 接收时间属于这次传输；发生事实与媒体顺序才决定原生身份是否冲突。
    facts = (
        source_facts,
        submission.text,
        serialization.encode(submission.context),
        _timestamp(submission.occurred_at),
        tuple(
            (media_id, asset.media_type, asset.name)
            for media_id, asset in zip(media_ids, submission.media, strict=True)
        ),
    )
    fingerprint = hashlib.sha256(
        json.dumps(facts, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if dedup_key is not None:
        existing: tuple[object, ...] | None = connection.execute(
            "SELECT input_id, fingerprint FROM inputs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        if existing is not None:
            if _text(existing[1]) != fingerprint:
                raise StorageError("原生输入身份与已接管事实冲突。", code="source_conflict")
            return Admission(_text(existing[0]), duplicate=True)

    # 先采用当前睡眠事实再核额度，旧版本已堆满的普通积压也不能拒绝唤醒输入。
    # 新普通输入直接记历史，不能先占待办再等待协调者，否则仍有满队列竞争窗口。
    rest = _record_sleep_inputs(connection)
    record_only = rest is not None and _sleep_record_only(source.adapter_id, submission.context)
    usage: tuple[object, ...] = connection.execute(
        "SELECT count(*), coalesce(sum(i.logical_bytes), 0) "
        "FROM pending_inputs p JOIN inputs i ON i.input_id = p.input_id"
    ).fetchone()
    if logical_bytes > max_input_bytes or (
        not record_only
        and (
            _integer(usage[0]) >= max_inputs or _integer(usage[1]) + logical_bytes > max_input_bytes
        )
    ):
        raise StorageError("未处置输入的接管额度已满。", code="input_capacity")

    input_id = uuid4().hex
    connection.execute(
        "INSERT INTO inputs (" + _INPUT_COLUMNS + ", dedup_key, fingerprint) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            input_id,
            *source_facts,
            submission.text,
            _timestamp(submission.occurred_at),
            _timestamp(submission.received_at),
            logical_bytes,
            serialization.encode(submission.context),
            dedup_key,
            fingerprint,
        ),
    )
    for ordinal, (media_id, asset) in enumerate(zip(media_ids, submission.media, strict=True)):
        connection.execute(
            "INSERT OR IGNORE INTO media (media_id, content) VALUES (?, ?)",
            (media_id, asset.content),
        )
        connection.execute(
            "INSERT INTO input_media (input_id, ordinal, media_id, media_type, name) "
            "VALUES (?, ?, ?, ?, ?)",
            (input_id, ordinal, media_id, asset.media_type, asset.name),
        )
    connection.execute("INSERT INTO pending_inputs (input_id) VALUES (?)", (input_id,))
    subject_storage.register_input(
        connection,
        input_id,
        minimum_wake=(
            submission.context.minimum_wake
            or submission.context.interrupt_generation
            or submission.context.speech_started
        ),
    )
    if record_only:
        _record_sleep_input(connection, input_id)
    knowledge_storage.put_evidence_in_transaction(
        connection,
        knowledge_models.EvidenceSnapshot(
            evidence_id=input_id,
            version=1,
            kind="input",
            text=submission.text,
            source_json=serialization.encode({"source": source, "context": submission.context}),
            occurred_at=submission.occurred_at.timestamp(),
            media=tuple(
                MediaReference(media_id, asset.media_type, len(asset.content), asset.name)
                for media_id, asset in zip(media_ids, submission.media, strict=True)
            ),
        ),
    )
    return Admission(input_id, duplicate=False)


class Store:
    """拥有专属连接与工作线程的权威存储服务。

    宿主在取得实例锁后创建并在所有借用者停止后关闭。公开方法只在创建它的
    事件循环使用；数据库连接从不离开专属线程。调用者可并发提交，但饱和时
    明确返回 ``store_busy``，不在内部创建无限等待任务。
    """

    def __init__(self, path: Path, max_inputs: int, max_input_bytes: int) -> None:
        """创建同连接账本门面。

        Args:
            path: 此操作定位的明确路径。
            max_inputs: 有界提交队列允许接管的条目上限。
            max_input_bytes: 有界提交队列允许接管的逻辑字节上限。

        Note:
            公开创建仍通过 open 等待连接就绪。
        """
        self._max_inputs = max_inputs
        self._max_input_bytes = max_input_bytes
        self._queue = SqliteExecutor(path, max_inputs, max_input_bytes)
        self._submit = self._queue.submit
        self._knowledge = KnowledgeLedger(self._submit)

    @property
    def knowledge(self) -> KnowledgeLedger:
        """借出同连接的认识账本端口，其关闭和权威资格始终归本 Store。"""
        return self._knowledge

    @property
    def time(self) -> TimeLedger:
        """借出同队列时钟/休息端口，闹铃消费与输入交接共用一个事务。"""

        return TimeLedger(self._submit, self._admit_business)

    @property
    def feedback(self) -> FeedbackLedger:
        """借出真实回执、活动和计划的可靠交接端口，不建立另一连接。"""
        return FeedbackLedger(self._submit, self._admit_business)

    @property
    def controls(self) -> ControlLedger:
        """借出同连接的页面控制身份账本，不增加设备执行入口。"""
        return ControlLedger(self._submit)

    @property
    def human_feedback(self) -> HumanFeedbackLedger:
        """借出同库人工审查端口，评价复用原证据及双消费者反馈账本。"""
        return HumanFeedbackLedger(self._submit)

    @property
    def audio(self) -> AudioLedger:
        """借出同连接音频阶段事实，不取得或恢复播放器/合成器。"""
        return AudioLedger(self._submit)

    async def runtime_projection(self) -> projection_storage.JsonObject:
        """读取同连接的有限运行概况，不消耗待办或暴露完整模型材料。"""
        return await self._submit(projection_storage.runtime)

    async def expire_diagnostics(
        self, *, before: float, expired_at: float, limit: int = 128
    ) -> projection_storage.JsonObject:
        """在同连接淘汰一批诊断，明确返回非权威清理故障，不修改业务资格。

        Args:
            before: 本次保留规则采用的 Unix 时间上界。
            expired_at: 此前已确认的诊断失效时刻，用于保留过期查询标记。
            limit: 本次读取或更新的最大条目数。 默认 128。

        Note:
            SQL或诊断材料损坏只表示本轮清理未完成；原文、参数、响应及媒体仍
            保留。提交队列/连接本身失效继续保持权威故障，不能在这里吞掉。
        """

        def cleanup(connection: sqlite3.Connection) -> projection_storage.JsonObject:
            """在当前同库操作中清理有限诊断正文并返回实际统计。"""
            try:
                result = retention_storage.expire_diagnostics(
                    connection, before=before, expired_at=expired_at, limit=limit
                )
            except (sqlite3.Error, StorageError) as error:
                if isinstance(error, StorageError) and error.code != "invalid_diagnostics":
                    raise
                return {"state": "failed", "error_type": type(error).__name__}
            return {
                "state": "confirmed",
                "expired_attempts": result.expired_attempts,
                "removed_events": result.removed_events,
                "removed_bytes": result.removed_bytes,
                "has_more": result.has_more,
            }

        return await self._submit(cleanup, writing=True)

    def _admit_business(
        self, connection: sqlite3.Connection, submission: InputSubmission
    ) -> Admission:
        """业务自己的短事务复用同一输入额度、原文及去重，不额外提交。"""
        return admit_in_transaction(
            connection,
            submission,
            max_inputs=self._max_inputs,
            max_input_bytes=self._max_input_bytes,
            logical_bytes=input_logical_bytes(submission),
        )

    @property
    def change_revision(self) -> int:
        """返回提交器已发布的事务通知代次，不替代权威查询。"""
        return self._queue.change_revision

    async def wait_for_change(self, after_revision: int) -> int:
        """等待事务或关闭通知，所有账本共享同一通知源。

        Args:
            after_revision: 查询待办前取得的通知序号，避免查询与等待之间漏唤醒。
        """
        return await self._queue.wait_for_change(after_revision)

    @classmethod
    async def open(
        cls, path: Path, *, max_inputs: int = 1024, max_input_bytes: int = 256 * 1024 * 1024
    ) -> "Store":
        """打开已知 Engine 格式的权威库，不转换未知数据库。

        Args:
            path: 此操作定位的明确路径。
            max_inputs: 有界提交队列允许接管的条目上限。 默认 1024。
            max_input_bytes: 有界提交队列允许接管的逻辑字节上限。

        Returns:
            已初始化的唯一连接所有者；后续方法只能在创建循环调用。

        Raises:
            ValueError: path 必须是 Path。

        Note:
            空路径首次建立格式，已有库须通过格式和完整性核验。额度约束未处置
            输入；历史记录与同库 BLOB 不因处置而删除。打开失败释放已取得资源；
            打开等待取消也会请求线程关闭，不留下无所有者连接。
        """
        if not isinstance(path, Path):
            raise ValueError("path 必须是 Path。")
        _positive(max_inputs, "max_inputs")
        _positive(max_input_bytes, "max_input_bytes")
        store = cls(path, max_inputs, max_input_bytes)
        await store._queue.start()
        return store

    async def admit(self, submission: InputSubmission) -> Admission:
        """原子接管输入或返回同一原生事实的第一次确认。

        Args:
            submission: 待正式接管的来源、原文、媒体与可信信号。

        Returns:
            正式接管凭证，包含 input_id 与重复来源标记；不表示模型已处理或对外回复。

        Raises:
            ValueError: submission 必须是 InputSubmission。
            StorageError: 单条输入超过逻辑负载额度。

        Note:
            来源冲突与额度不足均不写入部分原文或媒体。接收时间变化可以重放；
            来源发生事实、正文、发生时间或媒体变化必须使用新的事件身份。
            已排队操作不因等待取消回滚，接入方可用原生身份重投查询真实结果。
        """
        if not isinstance(submission, InputSubmission):
            raise ValueError("submission 必须是 InputSubmission。")
        logical_bytes = input_logical_bytes(submission)
        if logical_bytes > self._max_input_bytes:
            raise StorageError("单条输入超过逻辑负载额度。", code="input_capacity")
        admission = await self._submit(
            lambda connection: self._admit(connection, submission, logical_bytes),
            logical_bytes=logical_bytes,
            writing=True,
        )
        if not admission.duplicate:
            _logger.info("输入已正式接管。", extra={"input_id": admission.input_id})
        return admission

    async def idle_input_state(self) -> tuple[int, bool]:
        """取得空闲计时观察，不以高频日志、设备状态写入或重复接收重置时钟。

        Returns:
            最新 inputs rowid（空库为0）及是否有尚未完成调度的输入。

        Note:
            原始输入不删除，rowid 只作宿主内部接管游标，不作为平台时间或公开身份。
        """
        return await self._submit(self._idle_input_state)

    async def apply_sleep_policy(self, owner_id: str) -> RestState | None:
        """同一事务读取休息并将普通输入仅记历史，保留原文与高优先交接。

        Args:
            owner_id: 当前注意评估所有者，不修改其他宿主的旧判断记录。

        Returns:
            明确休息事实；清醒时不修改调度状态。

        Note:
            普通缓存、未决注意、已暂存及睡前普通唤醒统一仅记历史并释放额度；
            已有高优先意图仍可处理。定时主动思考即使刚在睡前接管也仅记录。
            当前已 claimed/processing 的轮次不撤销，原文和反思证据保留。
            无变化时不写行，因此不会因本查询通知自身而形成调度热循环。
        """
        self._validate_id(owner_id)

        def apply(connection: sqlite3.Connection) -> RestState | None:
            """只在短事务里采用当前休息事实，模型和设备等待留在事务外。"""
            self._queue.check_writing()
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                rest = _record_sleep_inputs(connection)
                if rest is None:
                    return None
                connection.execute(
                    "UPDATE minimum_wake_assessments SET state='completed',level='wake',"
                    "reason='subject.sleeping' WHERE owner_id=? AND state='pending'",
                    (owner_id,),
                )
                return rest

        return await self._submit(apply, control=True, writing=True)

    async def claim_sleep_compaction(self, expected: RestState) -> bool:
        """领取一次睡眠压缩，领取事实跨重启保留，不表示摘要成功。

        Args:
            expected: 本次休息的确切观察版本。

        Returns:
            是否首次领取，离床或版本冲突时为 False。
        """
        return await self._submit(
            lambda connection: time_storage.claim_rest_compaction(connection, expected),
            control=True,
            writing=True,
        )

    @staticmethod
    def _idle_input_state(connection: sqlite3.Connection) -> tuple[int, bool]:
        """只把需要处理的阶段视为忙碌，observe 积累仍可由空闲刺激唤醒。"""
        row: tuple[int, int] = connection.execute(
            "SELECT COALESCE((SELECT MAX(rowid) FROM inputs),0), "
            "EXISTS(SELECT 1 FROM input_processing "
            "WHERE stage NOT IN ('submitted','processed','failed'))"
        ).fetchone()
        return row[0], bool(row[1])

    async def admit_idle_input(
        self,
        submission: InputSubmission,
        *,
        expected_sequence: int,
        foreground_overdue: bool = False,
    ) -> Admission | None:
        """原子复查静默或主脑到期条件，沿正式接管逻辑保存主动思考刺激。

        Args:
            submission: 调度器的完整空闲事实，必须保底唤起且不能直接停音/抢占。
            expected_sequence: 最近一次空闲观察的非负 inputs 接管游标。
            foreground_overdue: 协调者确认主脑期限已到；允许新增普通刺激及注意
                待办，不允许已有优先唤醒、已取批或正在处理的主脑工作。

        Returns:
            实际接管凭证；所选触发条件已失效时返回 None。

        Raises:
            ValueError: 游标、到期标记或空闲刺激信号不满足入口约束。
            StorageError: 接管额度不足或权威存储故障。

        Note:
            复查与原文/待办插入在同一事务。静默分支中新输入优先；主脑到期分支
            则允许新背景输入一并唤醒，否则连续背景流量仍会使主脑饥饿。
        """
        if (
            isinstance(expected_sequence, bool)
            or not isinstance(expected_sequence, int)
            or expected_sequence < 0
        ):
            raise ValueError("接管游标必须是非负整数。")
        if not isinstance(foreground_overdue, bool):
            raise ValueError("主脑到期标记必须是布尔值。")
        if not isinstance(submission, InputSubmission) or (
            not submission.context.minimum_wake
            or submission.context.interrupt_generation
            or submission.context.speech_started
        ):
            raise ValueError("空闲刺激必须保底唤起，且不能停音或直接抢占。")
        logical_bytes = input_logical_bytes(submission)

        def admit(connection: sqlite3.Connection) -> Admission | None:
            """同一提交段复查对应分支；不能用定时刺激重复已有主脑唤醒。"""
            self._queue.check_writing()
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if time_storage.get_rest(connection) is not None:
                    return None
                if foreground_overdue:
                    scheduled = connection.execute(
                        "SELECT 1 FROM input_processing "
                        "WHERE stage IN ('priority','foreground','claimed','processing') LIMIT 1"
                    ).fetchone()
                    if scheduled is not None:
                        return None
                else:
                    sequence, pending = self._idle_input_state(connection)
                    if sequence != expected_sequence or pending:
                        return None
                return admit_in_transaction(
                    connection,
                    submission,
                    max_inputs=self._max_inputs,
                    max_input_bytes=self._max_input_bytes,
                    logical_bytes=logical_bytes,
                )

        return await self._submit(admit, logical_bytes=logical_bytes, writing=True)

    async def get_input(self, input_id: str) -> InputRecord | None:
        """读取接管事实及当前处置状态，查询不会改变待办。

        Args:
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。

        Returns:
            首次接管的完整输入原文、来源与媒体引用。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
        self._validate_id(input_id)
        return await self._submit(lambda connection: self._get_input(connection, input_id))

    async def get_native_input(self, source: InputSource) -> InputRecord | None:
        """只读原生身份的第一次接管事实，不重算上下文或改变处理阶段。

        Args:
            source: 含适配器、账号、会话和原生事件身份的可信来源。

        Returns:
            首次接管的完整输入原文、来源与媒体引用。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。

        Raises:
            ValueError: source 必须是 InputSource。

        Note:
            没有稳定原生事件身份时无法保证跨接收去重，返回 None。接入方
            仍须核验原生事实是否相同；本方法不把查询成功当作再次接管确认。
        """
        if not isinstance(source, InputSource):
            raise ValueError("source 必须是 InputSource。")
        if source.native_event_id is None:
            return None
        scope = (
            source.adapter_id,
            source.account_id,
            source.conversation_id,
            source.event_kind,
            source.native_event_id,
        )
        key = json.dumps(scope, separators=(",", ":"))

        def read(connection: sqlite3.Connection) -> InputRecord | None:
            """在同一数据库读取中恢复完整记录与其已保存关联。"""
            row: tuple[object, ...] | None = connection.execute(
                "SELECT input_id FROM inputs WHERE dedup_key=?", (key,)
            ).fetchone()
            if row is None:
                return None
            record = self._get_input(connection, _text(row[0]))
            if (
                record is None
                or (
                    record.source.adapter_id,
                    record.source.account_id,
                    record.source.conversation_id,
                    record.source.event_kind,
                    record.source.native_event_id,
                )
                != scope
            ):
                raise StorageError("原生身份索引与首次事实不符。", code="invalid_record")
            return record

        return await self._submit(read)

    async def pending(self, *, limit: int = 8) -> tuple[InputRecord, ...]:
        """按接管顺序取得一小批待办，不创建逐条等待任务或确认已处理。

        Args:
            limit: 本次读取或更新的最大条目数。 默认 8。

        Returns:
            条目集合：首次接管的完整输入原文、来源与媒体引用。

        Note:
            单批不会超过配置的接管条数上限；默认最多读取八条。
        """
        _positive(limit, "limit")
        bounded_limit = min(limit, self._max_inputs)
        return await self._submit(lambda connection: self._pending(connection, bounded_limit))

    async def complete_input(self, input_id: str) -> None:
        """明确提交输入已处置，释放积压额度并保留原文与媒体。

        Args:
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。

        Note:
            注意或前台所有者在完成处置后调用；重复提交同一身份没有附加效果。
            该控制提交不占普通排队位置，也不把释放额度等同于对外回复。
        """
        self._validate_id(input_id)
        await self._submit(
            lambda connection: self._complete_input(connection, input_id),
            control=True,
            writing=True,
        )

    async def input_processing(self, input_id: str) -> InputProcessing | None:
        """读取筛选、延后或前台处理的实际版本，不改变交接状态。

        Args:
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。

        Returns:
            输入实际处理阶段、版本和关联轮次。 未取得对应记录或素材时返回 None；读取故障仍抛出。
        """
        self._validate_id(input_id)
        return await self._submit(
            lambda connection: subject_storage.processing(connection, input_id)
        )

    async def staged_inputs(self, stage: InputStage, *, limit: int = 8) -> tuple[InputRecord, ...]:
        """按接管顺序加载有限调度批次。

        Args:
            stage: 输入当前或待查询的处理阶段。
            limit: 本次读取或更新的最大条目数。 默认 8。

        Returns:
            条目集合：首次接管的完整输入原文、来源与媒体引用。

        Note:
            状态只由业务事务推进。
        """
        _positive(limit, "limit")

        def read(connection: sqlite3.Connection) -> tuple[InputRecord, ...]:
            """在同一数据库读取中恢复完整记录与其已保存关联。"""
            records: list[InputRecord] = []
            for input_id in subject_storage.pending_ids(connection, stage, limit):
                record = self._get_input(connection, input_id)
                if record is None:
                    raise StorageError("处理队列引用不存在的原文。", code="invalid_record")
                records.append(record)
            return tuple(records)

        return await self._submit(read)

    async def submit_inputs(
        self, expected: tuple[InputProcessing, ...], *, minimum_wake: bool
    ) -> bool:
        """CAS 提交缓存批次，最低唤起同时保存唤醒意图。

        Args:
            expected: 当前缓存批次的完整版本。
            minimum_wake: 是否包含可信高价值输入。

        Returns:
            是否整批提交；版本冲突不部分采用。
        """
        return await self._submit(
            lambda connection: subject_storage.submit_inputs(
                connection, expected, minimum_wake=minimum_wake
            ),
            control=True,
            writing=True,
        )

    async def take_foreground_inputs(self) -> tuple[InputRecord, ...]:
        """原子消耗持久唤醒意图并取尽已提交收件箱。

        Returns:
            本轮独占的完整输入快照；无唤醒意图时为空。
        """

        def take(connection: sqlite3.Connection) -> tuple[InputRecord, ...]:
            """睡眠复查与取批共用事务，排队期间上床也不能被旧普通唤醒绕过。"""
            records: list[InputRecord] = []
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                _record_sleep_inputs(connection)
                for input_id in subject_storage.take_foreground_inputs(connection):
                    record = self._get_input(connection, input_id)
                    if record is None:
                        raise StorageError("处理队列引用不存在的原文。", code="invalid_record")
                    records.append(record)
            return tuple(records)

        return await self._submit(take, control=True, writing=True)

    async def buffered_inputs(self) -> tuple[InputRecord, ...]:
        """按接管顺序读取全部尚未提交的缓存输入。

        Returns:
            attention/priority 原文快照；容量由接管额度限制，查询不取得提交资格。
        """

        def read(connection: sqlite3.Connection) -> tuple[InputRecord, ...]:
            """跨阶段仍按 pending 序号排序，避免高价值批次改变来源顺序。"""
            rows: list[tuple[str]] = connection.execute(
                "SELECT p.input_id FROM pending_inputs p JOIN input_processing s USING(input_id) "
                "WHERE s.stage IN ('attention','priority') ORDER BY p.sequence"
            ).fetchall()
            records: list[InputRecord] = []
            for (input_id,) in rows:
                record = self._get_input(connection, input_id)
                if record is None:
                    raise StorageError("缓存引用不存在的原文。", code="invalid_record")
                records.append(record)
            return tuple(records)

        return await self._submit(read)

    async def decide_inputs(
        self,
        expected: tuple[InputProcessing, ...],
        level: AttentionLevel,
        reason: str,
        *,
        request_id: str | None = None,
    ) -> bool:
        """CAS 采用已提交批次的判断，observe 只保留待处理输入。

        Args:
            expected: 已提交批次的原版本。
            level: observe、wake 或 urgent。
            reason: 实际处置原因。
            request_id: 实际请求证据，程序处置为 None。

        Returns:
            是否整批采用；迟到结果不覆盖已取走输入。
        """
        return await self._submit(
            lambda connection: subject_storage.decide_inputs(
                connection, expected, level, reason, request_id=request_id
            ),
            logical_bytes=len(serialization.encode((expected, reason)).encode("utf-8")),
            control=True,
            writing=True,
        )

    async def register_minimum_wake_assessment(
        self, expected: tuple[InputProcessing, ...], *, owner_id: str, observed_generation: int
    ) -> bool:
        """保存最低唤起的一次旧代次升级资格，不创建第二份前台待办。

        Args:
            expected: 此次比较或提交必须仍匹配的原始观察值。
            owner_id: 发起本次工作并持有提交资格的所有者身份。
            observed_generation: 接管或判断开始时观察到的前台生成代次。
        """
        return await self._submit(
            lambda connection: subject_storage.register_minimum_wake_assessment(
                connection, expected, owner_id=owner_id, observed_generation=observed_generation
            ),
            control=True,
            writing=True,
        )

    async def minimum_wake_assessments(
        self, owner_id: str, *, limit: int = 8
    ) -> tuple[MinimumWakeAssessment, ...]:
        """有限读取本宿主尚未结束的升级资格，重启不能沿用旧生成观察。

        Args:
            owner_id: 发起本次工作并持有提交资格的所有者身份。
            limit: 本次读取或更新的最大条目数。 默认 8。

        Returns:
            属于当前宿主的有限紧急升级判断待办。
        """
        _positive(limit, "limit")
        return await self._submit(
            lambda connection: subject_storage.minimum_wake_assessments(
                connection, owner_id, limit=limit
            )
        )

    async def finish_minimum_wake_assessment(
        self,
        assessment: MinimumWakeAssessment,
        level: AttentionLevel,
        reason: str,
        *,
        request_id: str | None = None,
    ) -> bool:
        """保存判断原结果并 CAS 原输入版本，保留最低 WAKE 与单一前台交接。

        Args:
            assessment: 已取得并需按原版本核验的判断结果。
            level: 本批明确的注意处置等级。
            reason: 供后续核对的明确处置原因。
            request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。
        """
        return await self._submit(
            lambda connection: subject_storage.finish_minimum_wake_assessment(
                connection, assessment, level, reason, request_id=request_id
            ),
            logical_bytes=len(reason.encode("utf-8")),
            control=True,
            writing=True,
        )

    async def get_session(self, scope: SessionScope) -> SessionRecord:
        """还原完整已提交检查点媒体，不返回丢失内容的空历史。

        Args:
            scope: 目标连续会话，foreground 与 reflection 拥有独立历史。

        Returns:
            会话已提交版本与完整检查点，不包含尚未提交的临时历史。
        """
        return await self._submit(lambda connection: subject_storage.session(connection, scope))

    async def protected_input_ids(self, input_ids: tuple[str, ...]) -> frozenset[str]:
        """查询真实未决调用、活动、计划、有效监管和未承接失败。

        Args:
            input_ids: 本轮接管的输入身份集合，保留真实因果关联。

        Returns:
            由完整请求材料确认的不可变身份集合，不从正文标记推断资格。

        Note:
            来源是 Engine 保存的明确身份关系；不从正文或任意工具 JSON 猜测
            承诺完成，不读取全部历史建立另一份内存状态。
        """
        for identity in input_ids:
            self._validate_id(identity)

        return await self._submit(
            lambda connection: _protected_inputs(connection, input_ids),
            logical_bytes=sum(len(value.encode()) for value in input_ids),
        )

    async def subject_work(self) -> JsonObject:
        """取得完整当前未决材料，由实际请求容量门槛决定能否交付。

        Note:
            一个存储作业读取当前前台版本、全部未完成计划、仍有效未回应监管、
            活动及必要调用事实。数据库分块不限制最终选材；这里不裁剪正文、
            退役未知效果或执行控制。注意背景使用另一份明确有限的投影。
        """

        def query(connection: sqlite3.Connection) -> JsonObject:
            """枚举原生捕获库报告的输入设备及默认采样率。"""
            session = subject_storage.session(connection, "foreground")
            turn = (
                None
                if session.active_turn is None
                else subject_storage.get_turn(connection, session.active_turn)
            )
            if session.active_turn is not None and turn is None:
                raise StorageError("当前会话引用缺失轮次。", code="invalid_record")
            count = _integer(
                connection.execute(
                    "SELECT count(*) FROM knowledge_plans WHERE "
                    "json_extract(payload_json,'$.state')='pending'"
                ).fetchone()[0]
            )
            plans = () if count == 0 else knowledge_storage.pending_plans(connection, limit=count)
            supervision = tuple(_active_supervision(connection))
            activities = activity_storage.activity_records(connection)
            protected = _protected_inputs(connection, None)
            cursor = connection.execute(
                "SELECT action_id FROM actions a WHERE a.state='running' OR "
                "EXISTS(SELECT 1 FROM calls c WHERE c.action_id=a.action_id AND "
                "c.state IN ('prepared','started','unknown')) OR "
                "a.input_id IN (SELECT value FROM json_each(?)) ORDER BY created_at,action_id",
                (json.dumps(tuple(sorted(protected))),),
            )
            calls: list[JsonValue] = []
            while rows := cursor.fetchmany(32):
                for row in rows:
                    action = execution_storage.get_action(connection, _text(row[0]))
                    if action is None:
                        raise StorageError("当前调用投影引用缺失行动。", code="invalid_record")
                    original_refs = _call_original_refs(connection, action)
                    for call in action.calls:
                        # 已成功的效果只在其关联事项仍未结案时保持当前保护；未知
                        # 调用不靠时间、行动已结束或邮箱已交付自动退役。
                        if call.state not in ("prepared", "started", "unknown") and (
                            action.state != "running" and action.input_id not in protected
                        ):
                            continue
                        calls.append(
                            {
                                "action_id": action.action_id,
                                "action_state": action.state,
                                "input_id": action.input_id,
                                "call_id": call.call_id,
                                "path": call.path,
                                "target_id": call.target_id,
                                "arguments_json": call.arguments_json,
                                "state": call.state,
                                "receipt_revision": call.receipt_revision,
                                "started_at": call.started_at,
                                "finished_at": call.finished_at,
                                "result_json": call.result_json,
                                "error_type": call.error_type,
                                "error": call.error,
                                "original_ref": original_refs.get(call.call_id),
                            }
                        )
            return {
                "coverage": "all_current_pending_plans; active_supervision; active_activities; "
                "unresolved_calls_and_effects_linked_to_unfinished_work",
                "assembled_at": time.time(),
                "foreground_session_version": session.version,
                "current_turn": None
                if turn is None
                else {
                    "turn_id": turn.turn_id,
                    "input_ids": list(turn.input_ids),
                    "started_at": turn.started_at,
                    "generation": turn.generation,
                },
                "plans": [item.model_dump(mode="json") for item in plans],
                "supervision": [item.model_dump(mode="json") for item in supervision],
                "activities": [
                    {
                        "activity_id": item.activity_id,
                        "version": item.version,
                        "goal": item.goal,
                        "state": item.state,
                        "run_generation": item.run_generation,
                        "remaining_seconds": item.remaining_seconds,
                        "reconciliation_required": item.needs_reconciliation,
                    }
                    for item in activities
                ],
                "calls": calls,
            }

        return await self._submit(query)

    async def reserve_summary(self, source: SummarySource) -> Reservation:
        """在无工具摘要请求前原子保留同覆盖尝试，失败不会自动重新计预算。

        Args:
            source: 已冻结的历史范围及所属会话版本。

        Returns:
            reserved 表示已取得独占占位；already_attempted 表示覆盖在途或已安装；stale 或
            busy 表示当前版本或空闲条件不允许维护。
        """
        return await self._submit(
            lambda connection: maintenance_storage.reserve_summary(connection, source),
            logical_bytes=len(serialization.encode(source).encode("utf-8")),
            control=True,
            writing=True,
        )

    async def get_usage_anchor(self, binding: str) -> UsageAnchor | None:
        """恢复指定端点/用途绑定的实际用量指纹，不加载历史正文或媒体。

        Args:
            binding: 应用生成的用途、端点、模型与协议绑定摘要。

        Raises:
            StorageError: 保存的锚点字段损坏，不能静默退回估算。
        """

        def read(connection: sqlite3.Connection) -> UsageAnchor | None:
            row: tuple[object, ...] | None = connection.execute(
                "SELECT request_id,anchor_json FROM capacity_usage WHERE binding=?", (binding,)
            ).fetchone()
            if row is None:
                return None
            try:
                value = decode_object(_text(row[1]).encode("utf-8"))
            except ValueError as error:
                raise StorageError("用量锚点编码损坏。", code="invalid_record") from error
            if value.get("type") != "UsageAnchor" or value.get("request_id") != row[0]:
                raise StorageError("用量锚点来源身份不一致。", code="invalid_record")
            prefix = value.get("prefix")
            if not isinstance(prefix, list):
                raise StorageError("用量锚点前缀形状无效。", code="invalid_record")
            try:
                return UsageAnchor(
                    _text(value.get("request_id")),
                    _text(value.get("compiler")),
                    _text(value.get("static_digest")),
                    _text(value.get("field")),
                    tuple(_text(item) for item in prefix),
                    _integer(value.get("input_tokens")),
                )
            except ValueError as error:
                raise StorageError("用量锚点字段损坏。", code="invalid_record") from error

        return await self._submit(read)

    async def put_usage_anchor(self, binding: str, anchor: UsageAnchor) -> None:
        """仅为已提交成功请求保存实际输入用量，原材料仍由请求账本持有。

        Args:
            binding: 应用生成的独立用途及模型端点绑定摘要。
            anchor: 已按实际 SDK 输入冻结的不可变用量和指纹。

        Raises:
            StorageError: 来源请求不存在或尚未成功提交。
        """
        encoded = serialization.encode(anchor)

        def save(connection: sqlite3.Connection) -> None:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row: tuple[object, ...] | None = connection.execute(
                    "SELECT state FROM model_requests WHERE request_id=?", (anchor.request_id,)
                ).fetchone()
                if row != ("succeeded",):
                    raise StorageError("用量锚点缺少真实成功请求。", code="invalid_record")
                connection.execute(
                    "INSERT INTO capacity_usage(binding,request_id,anchor_json) VALUES(?,?,?) "
                    "ON CONFLICT(binding) DO UPDATE SET request_id=excluded.request_id, "
                    "anchor_json=excluded.anchor_json",
                    (binding, anchor.request_id, encoded),
                )

        await self._submit(save, logical_bytes=len(encoded.encode("utf-8")), writing=True)

    async def release_summary(self, source: SummarySource) -> None:
        """归还未安装摘要的临时占位，后续独立维护仍使用其自身原始预算。"""
        await self._submit(
            lambda connection: maintenance_storage.release_summary(connection, source),
            control=True,
            writing=True,
        )

    async def install_summary(self, proposal: SummaryProposal) -> int | None:
        """idle/版本仍匹配才归档旧原文并安装候选，失败保留原会话和媒体。

        Args:
            proposal: 已冻结来源和内容的候选提案，提交前仍须验证。

        Returns:
            实际提交的新会话版本；None 表示来源过期或目标不再空闲，旧检查点保留。
        """
        return await self._submit(
            lambda connection: maintenance_storage.install_summary(connection, proposal),
            logical_bytes=len(serialization.encode(proposal).encode("utf-8")),
            control=True,
            writing=True,
        )

    async def begin_turn(
        self,
        turn_id: str,
        scope: SessionScope,
        expected_version: int,
        generation: int,
        input_ids: tuple[str, ...],
    ) -> TurnRecord:
        """先接管主动处理身份和初始输入，固定会话版本后才借用 Core。

        Args:
            turn_id: 连续会话中本轮处理的身份。
            scope: 目标连续会话，foreground 与 reflection 拥有独立历史。
            expected_version: 提交前观察到的版本，用于拒绝并发覆盖。
            generation: 当前会话或设备的代次，用于拒绝迟到结果更新新状态。
            input_ids: 本轮接管的输入身份集合，保留真实因果关联。

        Returns:
            本轮接管身份、来源输入、生成代次与当前结论。

        Raises:
            SessionVersionChanged: 事务发现空闲会话历史已更新，尚未接管本轮。
            StorageError: 会话已有在途轮次或输入不再具有交接资格。
        """
        return await self._submit(
            lambda connection: subject_storage.begin_turn(
                connection, turn_id, scope, expected_version, generation, input_ids
            ),
            logical_bytes=len(serialization.encode(input_ids).encode("utf-8")),
            control=True,
            writing=True,
        )

    async def finish_turn(
        self,
        turn: TurnRecord,
        outcome: TurnOutcome,
        *,
        checkpoint_json: str | None = None,
        error_json: str | None = None,
        accepted_failures: tuple[str, ...] = (),
    ) -> SessionRecord:
        """原子安装成功历史或保留失败经历。

        Args:
            turn: 已接管的本轮记录，携带提交前版本与生成代次。
            outcome: 此次操作的真实结论，不由期望效果推断成功。
            checkpoint_json: 完整 Core 检查点的编码；失败轮次可不安装新历史。
            error_json: 编码后的失败证据，供后续交互追溯原轮次。
            accepted_failures: 当前成功交互明确承接的先前失败轮次身份。 默认不附加此类条目。

        Returns:
            会话已提交版本与完整检查点，不包含尚未提交的临时历史。

        Note:
            取消等待不撤销已经接管提交。
        """
        return await self._submit(
            lambda connection: subject_storage.finish_turn(
                connection,
                turn,
                outcome,
                checkpoint_json=checkpoint_json,
                error_json=error_json,
                accepted_failures=accepted_failures,
            ),
            logical_bytes=len((checkpoint_json or "").encode("utf-8"))
            + len((error_json or "").encode("utf-8")),
            control=True,
            writing=True,
        )

    async def failed_turns(self, scope: SessionScope) -> tuple[TurnRecord, ...]:
        """读取待承接的失败经历，实际效果另按行动与调用身份读取。

        Args:
            scope: 目标连续会话，foreground 与 reflection 拥有独立历史。

        Returns:
            条目集合：本轮接管身份、来源输入、生成代次与当前结论。
        """
        return await self._submit(lambda connection: subject_storage.failures(connection, scope))

    async def get_turn(self, turn_id: str) -> TurnRecord | None:
        """核对接管后的真实批次，不以调用方等待取消推断事务未发生。

        Args:
            turn_id: 连续会话中本轮处理的身份。

        Returns:
            本轮接管身份、来源输入、生成代次与当前结论。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
        self._validate_id(turn_id)
        return await self._submit(lambda connection: subject_storage.get_turn(connection, turn_id))

    async def get_media(self, media_id: str) -> bytes | None:
        """读取正式媒体并核对内容摘要，不以损坏字节替代已接管证据。

        Args:
            media_id: 权威库内由内容摘要标识的完整媒体身份。
        """
        self._validate_id(media_id)
        return await self._submit(lambda connection: self._get_media(connection, media_id))

    async def begin_action(
        self, action_id: str, *, catalog_version: str, source: str, input_id: str | None = None
    ) -> None:
        """执行所有者先保存代码与目录版本。

        Args:
            action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
            catalog_version: 本次行动冻结的目录内容摘要，执行期间保持不变。
            source: 行动原始材料，保存受限源码或冻结的 structured_call JSON。
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。 None
                表示无单一输入因果关联。

        Raises:
            ValueError: 行动源码必须是字符串。

        Note:
            确认返回前不得启动该行动。
        """
        self._validate_id(action_id)
        self._validate_id(catalog_version)
        if not isinstance(source, str):
            raise ValueError("行动源码必须是字符串。")
        size = record_validation.payload_bytes(action_id, catalog_version, source, input_id)
        await self._submit(
            lambda connection: execution_storage.begin_action(
                connection, action_id, catalog_version, source, input_id
            ),
            control=True,
            writing=True,
            logical_bytes=size,
        )
        _logger.info(
            "行动意图已接管，尚不证明能力效果。",
            extra={"action_id": action_id, "input_id": input_id},
        )

    @property
    def authority_available(self) -> bool:
        """同步读取提交器的写入资格。

        Note:
            调用方在等待后仍须复核。
        """
        return self._queue.authority_available

    async def begin_request(self, request: RequestEvidence) -> None:
        """原子保存编译材料与同库 BLOB 引用，确认前不允许模型派发。

        Args:
            request: 本次逻辑请求冻结的完整公开材料与因果身份。
        """
        await self._submit(
            lambda connection: model_storage.begin_request(connection, request),
            logical_bytes=len(serialization.encode(request).encode("utf-8")),
            control=True,
            writing=True,
        )
        _logger.info(
            "逻辑模型请求已冻结。",
            extra={
                "request_id": request.request_id,
                "turn_id": request.scope.turn_id,
                "scope": request.scope.owner_id,
            },
        )

    async def begin_attempt(self, start: AttemptStart) -> None:
        """保存本次真实尝试身份，模型所有者在等待后重验资格和截止。

        Args:
            start: 真实尝试开始前须持久化的编号、身份和截止事实。
        """
        await self._submit(
            lambda connection: model_storage.begin_attempt(connection, start),
            logical_bytes=len(serialization.encode(start).encode("utf-8")),
            control=True,
            writing=True,
        )

    async def finish_attempt(self, result: AttemptEvidence) -> None:
        """提交权威响应/原参数，再独立保存有限诊断，不覆盖另一次尝试。

        Args:
            result: 本次尝试的实际响应、失败分类与有限诊断。
        """
        await self._submit(
            lambda connection: model_storage.finish_attempt(connection, result),
            logical_bytes=len(serialization.encode(result).encode("utf-8")),
            control=True,
            writing=True,
        )
        _logger.info(
            "模型尝试已结束。",
            extra={
                "request_id": result.start.request_id,
                "attempt_id": result.start.attempt_id,
                "outcome": result.state,
                "error_type": None if result.failure is None else result.failure.error_type,
            },
        )

    async def finish_request(self, request_id: str, state: RequestOutcome) -> None:
        """提交逻辑请求结论，已经成功的远端结果不等同于会话已采用。

        Args:
            request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。
            state: 逻辑请求最终状态，独立于各次传输尝试。
        """
        self._validate_id(request_id)
        await self._submit(
            lambda connection: model_storage.finish_request(connection, request_id, state),
            logical_bytes=len(request_id.encode("utf-8")),
            control=True,
            writing=True,
        )

    async def get_request(self, request_id: str) -> RequestRecord | None:
        """从权威快照还原公开请求与独立尝试。

        Args:
            request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。

        Returns:
            一个逻辑模型请求的冻结材料及按实际顺序保存的尝试。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。

        Note:
            缺失媒体明确失败。
        """
        self._validate_id(request_id)
        return await self._submit(
            lambda connection: model_storage.get_request(connection, request_id)
        )

    async def requests_for_turn(self, owner_id: str, turn_id: str) -> tuple[RequestRecord, ...]:
        """按因果身份还原本批各逻辑请求，包括容量依据及独立传输尝试。

        Args:
            owner_id: 发起本次工作并持有提交资格的所有者身份。
            turn_id: 连续会话中本轮处理的身份。

        Returns:
            条目集合：一个逻辑模型请求的冻结材料及按实际顺序保存的尝试。
        """

        def read(connection: sqlite3.Connection) -> tuple[RequestRecord, ...]:
            """在同一数据库读取中恢复完整记录与其已保存关联。"""
            rows: list[tuple[object, ...]] = connection.execute(
                "SELECT request_id FROM model_requests "
                "WHERE json_extract(evidence_json,'$.scope.owner_id')=? "
                "AND json_extract(evidence_json,'$.scope.turn_id')=? "
                "ORDER BY created_at,request_id",
                (owner_id, turn_id),
            ).fetchall()
            records: list[RequestRecord] = []
            for row in rows:
                record = model_storage.get_request(connection, _text(row[0]))
                if record is None:
                    raise StorageError("批次请求身份损坏。", code="invalid_record")
                records.append(record)
            return tuple(records)

        return await self._submit(read)

    async def activity_records(self) -> tuple[ActivityRecord, ...]:
        """取得未结束活动的当前快照，查询不创建或恢复驱动。

        Returns:
            条目集合：活动的控制、预算、运行代次与工作快照。
        """
        return await self._submit(activity_storage.activity_records)

    async def get_activity(self, activity_id: str) -> ActivityRecord | None:
        """读取控制、预算和工作当前事实，不自动重放。

        Args:
            activity_id: 已创建活动的稳定身份。

        Returns:
            活动的控制、预算、运行代次与工作快照。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
        self._validate_id(activity_id)
        return await self._submit(
            lambda connection: activity_storage.get_activity(connection, activity_id)
        )

    async def get_activity_command(self, command_id: str) -> ActivityCommand | None:
        """区分命令接管、驱动受理及实际确认。

        Args:
            command_id: 控制命令的稳定身份；重复提交须沿用原身份和内容。

        Returns:
            命令接管与实际确认记录；受理成功不等于驱动已完成。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
        self._validate_id(command_id)
        return await self._submit(
            lambda connection: activity_storage.get_activity_command(connection, command_id)
        )

    async def get_activity_feedback(self, feedback_id: str) -> ActivityFeedback | None:
        """读取首次反馈的稳定材料，以事实身份处理重投。

        Args:
            feedback_id: 驱动反馈的稳定身份，用于去重和查询实际确认。

        Returns:
            已经按身份去重并保存的真实驱动反馈。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
        self._validate_id(feedback_id)
        return await self._submit(
            lambda connection: activity_storage.get_activity_feedback(connection, feedback_id)
        )

    async def commit_activity(
        self,
        record: ActivityRecord,
        *,
        expected_version: int | None,
        command: ActivityCommand | None = None,
        feedback: ActivityFeedback | None = None,
    ) -> None:
        """CAS 提交当前状态、控制或反馈及可靠交接，事务不等待专业驱动。

        Args:
            record: 待查询、核对或提交的当前活动快照。
            expected_version: 提交前观察到的版本，用于拒绝并发覆盖。
            command: 与状态变化同事务保存的控制命令；None 表示本次没有命令。 None
                表示本次没有伴随命令。
            feedback: 与活动状态同事务保存的驱动反馈；None 表示没有新反馈。
        """
        size = len(serialization.encode((record, command, feedback)).encode("utf-8"))
        await self._submit(
            lambda connection: activity_storage.commit_activity(
                connection,
                record,
                expected_version=expected_version,
                command=command,
                feedback=feedback,
            ),
            logical_bytes=size,
            control=True,
            writing=True,
        )
        if command is not None:
            _logger.info(
                "活动控制已入账，实际状态见确认。",
                extra={
                    "activity_id": record.activity_id,
                    "command_id": command.command_id,
                    "outcome": command.status,
                },
            )

    async def recover_activities(self) -> tuple[ActivityRecord, ...]:
        """登记上次运行中断。

        Returns:
            条目集合：活动的控制、预算、运行代次与工作快照。

        Note:
            保留累计，离线时间不扣，不构造驱动。
        """
        return await self._submit(
            lambda connection: activity_storage.recover_activities(
                connection, now=datetime.now(UTC).timestamp()
            ),
            control=True,
            writing=True,
        )

    async def pending_activity_feedback(
        self,
        consumer: Literal["foreground", "reflection"],
    ) -> tuple[ActivityFeedback, ...]:
        """读取该独立消费者尚未接管的反馈，查询不确认完成。

        Args:
            consumer: 本次可靠反馈交接的消费者，前台与反思分别维护游标。

        Returns:
            条目集合：已经按身份去重并保存的真实驱动反馈。
        """
        return await self._submit(
            lambda connection: activity_storage.pending_activity_feedback(connection, consumer)
        )

    async def acknowledge_activity_feedback(
        self,
        feedback_id: str,
        consumer: Literal["foreground", "reflection"],
    ) -> None:
        """只确认一份交接责任，前台与反思互不清除对方待办。

        Args:
            feedback_id: 驱动反馈的稳定身份，用于去重和查询实际确认。
            consumer: 本次可靠反馈交接的消费者，前台与反思分别维护游标。
        """
        self._validate_id(feedback_id)
        await self._submit(
            lambda connection: activity_storage.acknowledge_activity_feedback(
                connection, feedback_id, consumer
            ),
            control=True,
            writing=True,
        )

    async def begin_call(
        self,
        call_id: str,
        *,
        action_id: str,
        sequence: int,
        path: str,
        target_id: str,
        arguments_json: str,
    ) -> None:
        """派发前接管完整调用意图。

        Args:
            call_id: 实际能力调用的唯一身份，用于保存启动门槛及结果版本。
            action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
            sequence: 行动内从 1 开始的调用顺序号。
            path: 此操作定位的明确路径。
            target_id: 能力绑定的目标对象身份。
            arguments_json: 已编码为严格 JSON 对象的原始能力参数。

        Raises:
            ValueError: 调用顺序必须是非负整数。

        Note:
            prepared 不证明已进入 Provider。
        """
        for identity in (call_id, action_id, path, target_id):
            self._validate_id(identity)
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("调用顺序必须是非负整数。")
        size = record_validation.payload_bytes(call_id, action_id, path, target_id, arguments_json)
        record_validation.validate_json(arguments_json, arguments=True)
        await self._submit(
            lambda connection: execution_storage.begin_call(
                connection, call_id, action_id, sequence, path, target_id, arguments_json
            ),
            control=True,
            writing=True,
            logical_bytes=size,
        )

    async def mark_call_started(self, call_id: str) -> None:
        """提交进入 Provider 前的门槛。

        Args:
            call_id: 实际能力调用的唯一身份，用于保存启动门槛及结果版本。

        Note:
            执行方返回后仍须在锁内核验资格。

            等待取消不撤销已提交门槛；不得把再次调用此方法当作执行许可或重发。
        """
        self._validate_id(call_id)
        size = record_validation.payload_bytes(call_id)
        await self._submit(
            lambda connection: execution_storage.mark_call_started(connection, call_id),
            control=True,
            writing=True,
            logical_bytes=size,
        )

    async def record_call_result(
        self,
        call_id: str,
        *,
        state: CallOutcome,
        result_json: str | None,
        error_type: str | None,
        error: str | None,
        media: tuple[MediaReference, ...] = (),
    ) -> CallRecord:
        """独立提交真实结果、回执历史和可靠待办，不依赖会话提交。

        Args:
            call_id: 实际能力调用的唯一身份，用于保存启动门槛及结果版本。
            state: 本次实际调用的确认、失败或未知结论。
            result_json: 已编码的实际结果；无可用结果时为 None。
            error_type: 回执中稳定的错误类别，无错误时为 None。
            error: 当前流程已经取得的真实故障或失败说明。
            media: 已接管媒体的引用集合，展开时必须取得原始 BLOB。 默认不附加此类条目。

        Returns:
            原调用当前的权威结果及不可变版本历史。

        Raises:
            ValueError: 调用结果状态无效。

        Note:
            重复相同结果返回同版本；未知可由迟到真实确认或失败澄清，其旧版本仍
            保留并分别待交接。调用者取消等待不取消结果事务，查询可取回事实。
        """
        self._validate_id(call_id)
        if state not in ("confirmed", "failed", "unknown", "not_started"):
            raise ValueError("调用结果状态无效。")
        size = record_validation.payload_bytes(call_id, result_json, error_type, error)
        size += len(serialization.encode(media).encode())
        record_validation.validate_json(result_json)
        record = await self._submit(
            lambda connection: execution_storage.record_call_result(
                connection, call_id, state, result_json, error_type, error, media
            ),
            control=True,
            writing=True,
            logical_bytes=size,
        )
        _logger.info(
            "能力回执已入账。",
            extra={
                "action_id": record.action_id,
                "call_id": record.call_id,
                "path": record.path,
                "outcome": record.state,
                "receipt_revision": record.receipt_revision,
                "error_type": record.error_type,
            },
        )
        return record

    async def finish_action(
        self,
        action_id: str,
        *,
        state: ActionOutcome,
        result_json: str | None,
        output: str,
        error: str | None,
    ) -> None:
        """保存行动结束事实，失败与取消都不回滚或覆盖逐次真实回执。

        Args:
            action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
            state: 行动结束状态，不能抹除已经发生的逐次效果。
            result_json: 已编码的实际结果；无可用结果时为 None。
            output: 此次 Worker 实际产生的有界标准输出。
            error: 当前流程已经取得的真实故障或失败说明。

        Raises:
            ValueError: 行动结果状态无效；行动输出必须是字符串。
        """
        self._validate_id(action_id)
        if state not in ("completed", "failed", "timed_out", "cancelled"):
            raise ValueError("行动结果状态无效。")
        if not isinstance(output, str):
            raise ValueError("行动输出必须是字符串。")
        size = record_validation.payload_bytes(action_id, result_json, output, error)
        record_validation.validate_json(result_json)
        await self._submit(
            lambda connection: execution_storage.finish_action(
                connection, action_id, state, result_json, output, error
            ),
            control=True,
            writing=True,
            logical_bytes=size,
        )
        _logger.info(
            "行动已结束；逐调用回执分别保留确认、未知与失败。",
            extra={"action_id": action_id, "outcome": state},
        )

    async def get_action(self, action_id: str) -> ActionRecord | None:
        """查询行动、当前调用及完整回执版本。

        Args:
            action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。

        Returns:
            已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。
            未取得对应记录或素材时返回 None；读取故障仍抛出。

        Note:
            读取不启动或确认任何工作。
        """
        self._validate_id(action_id)
        return await self._submit(
            lambda connection: execution_storage.get_action(connection, action_id)
        )

    async def actions_for_inputs(self, input_ids: tuple[str, ...]) -> tuple[ActionRecord, ...]:
        """按实际持久来源关联查询行动，不从模型结果文本猜测发生关系。

        Args:
            input_ids: 本轮接管的输入身份集合，保留真实因果关联。

        Returns:
            条目集合：已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。
        """
        for input_id in input_ids:
            self._validate_id(input_id)

        def read(connection: sqlite3.Connection) -> tuple[ActionRecord, ...]:
            """在同一数据库读取中恢复完整记录与其已保存关联。"""
            rows: list[tuple[object, ...]] = connection.execute(
                "SELECT action_id FROM actions WHERE input_id IN (SELECT value FROM json_each(?)) "
                "ORDER BY created_at,action_id",
                (serialization.encode(input_ids),),
            ).fetchall()
            result: list[ActionRecord] = []
            for row in rows:
                action = execution_storage.get_action(connection, _text(row[0]))
                if action is None:
                    raise StorageError("因果行动引用损坏。", code="invalid_record")
                result.append(action)
            return tuple(result)

        return await self._submit(
            read, logical_bytes=len(serialization.encode(input_ids).encode("utf-8"))
        )

    async def pending_call_receipts(self, *, limit: int = 16) -> tuple[CallRecord, ...]:
        """读取有限可靠交接版本。

        Args:
            limit: 本次读取或更新的最大条目数。 默认 16。

        Returns:
            条目集合：原调用当前的权威结果及不可变版本历史。

        Note:
            消费确认须由实际主体接管事务另行提交。
        """
        _positive(limit, "limit")
        bounded_limit = min(limit, self._max_inputs)
        return await self._submit(
            lambda connection: execution_storage.pending_call_receipts(connection, bounded_limit)
        )

    async def close(self) -> None:
        """等待唯一提交器实际排空和关闭，观察取消不撤销收尾。"""
        await self._queue.close()

    @staticmethod
    def _validate_id(value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError("记录身份必须是非空字符串。")

    def _admit(
        self, connection: sqlite3.Connection, submission: InputSubmission, logical_bytes: int
    ) -> Admission:
        """外部接管持有事务；其他业务在自己的事务中复用同一输入保存机制。"""
        self._queue.check_writing()
        connection.execute("BEGIN IMMEDIATE")
        try:
            admission = admit_in_transaction(
                connection,
                submission,
                max_inputs=self._max_inputs,
                max_input_bytes=self._max_input_bytes,
                logical_bytes=logical_bytes,
            )
            connection.commit()
            return admission
        except (sqlite3.Error, StorageError):
            connection.rollback()
            raise
        except UnicodeEncodeError as error:
            # Python 孤立代理项无法保存为 UTF-8；接管前输入错误不关闭权威线程。
            connection.rollback()
            raise StorageError(
                "输入来源或媒体信息不能表示为 UTF-8。", code="invalid_input"
            ) from error

    def _get_input(self, connection: sqlite3.Connection, input_id: str) -> InputRecord | None:
        """由唯一待办关系推导状态，不维护第二份可写状态字段。"""
        row: tuple[object, ...] | None = connection.execute(
            f"SELECT {_INPUT_COLUMNS} FROM inputs WHERE input_id = ?", (input_id,)
        ).fetchone()
        if row is None:
            return None
        media_rows: list[tuple[object, ...]] = connection.execute(
            "SELECT r.media_id, r.media_type, length(m.content), r.name FROM input_media r "
            "LEFT JOIN media m ON m.media_id = r.media_id WHERE r.input_id = ? ORDER BY r.ordinal",
            (input_id,),
        ).fetchall()
        pending: tuple[object, ...] | None = connection.execute(
            "SELECT 1 FROM pending_inputs WHERE input_id = ?", (input_id,)
        ).fetchone()
        try:
            source = InputSource(
                _text(row[1]),
                _text(row[2]),
                _text(row[3]),
                _text(row[4]),
                _optional_text(row[5]),
                _optional_text(row[6]),
                _optional_text(row[7]),
            )
        except ValueError as error:
            raise StorageError("权威记录中的输入来源无效。", code="invalid_record") from error
        media = tuple(
            MediaReference(
                _text(item[0]), _text(item[1]), _integer(item[2]), _optional_text(item[3])
            )
            for item in media_rows
        )
        context = _input_context(row[12])
        return InputRecord(
            _text(row[0]),
            source,
            _text(row[8]),
            _datetime(row[9]),
            _datetime(row[10]),
            media,
            "pending" if pending is not None else "processed",
            _integer(row[11]),
            context,
        )

    def _pending(self, connection: sqlite3.Connection, limit: int) -> tuple[InputRecord, ...]:
        """数据库保留所有身份，只加载调用者明确请求的有限批次。"""
        rows: list[tuple[object, ...]] = connection.execute(
            "SELECT input_id FROM pending_inputs ORDER BY sequence LIMIT ?", (limit,)
        ).fetchall()
        records: list[InputRecord] = []
        for row in rows:
            record = self._get_input(connection, _text(row[0]))
            if record is None:
                raise StorageError("待办引用不存在的输入。", code="invalid_record")
            records.append(record)
        return tuple(records)

    def _complete_input(self, connection: sqlite3.Connection, input_id: str) -> None:
        """显式处置只移除积压关系；原文和媒体继续作为权威历史保留。"""
        self._queue.check_writing()
        with connection:
            existing: tuple[object, ...] | None = connection.execute(
                "SELECT 1 FROM inputs WHERE input_id = ?", (input_id,)
            ).fetchone()
            if existing is None:
                raise StorageError("待处置输入不存在。", code="input_missing")
            connection.execute("DELETE FROM pending_inputs WHERE input_id = ?", (input_id,))
            connection.execute(
                "UPDATE input_processing SET version=version+1,stage='processed',"
                "reason='explicit.complete' WHERE input_id=?",
                (input_id,),
            )

    @staticmethod
    def _get_media(connection: sqlite3.Connection, media_id: str) -> bytes | None:
        """内容寻址只在摘要仍匹配时成立，坏 BLOB 必须作为权威故障暴露。"""
        row: tuple[object, ...] | None = connection.execute(
            "SELECT content FROM media WHERE media_id = ?", (media_id,)
        ).fetchone()
        if row is None:
            return None
        content = row[0]
        if not isinstance(content, bytes) or hashlib.sha256(content).hexdigest() != media_id:
            raise StorageError("正式媒体内容与摘要不符。", code="invalid_record")
        return content
