"""在专属 SQLite 线程上保存输入、媒体、行动意图和逐次真实回执。

``Store.open`` 创建单连接所有者；宿主须先持有数据目录实例锁。接管事务同时
检查额度并保存原文、媒体引用和待办。普通请求与处置提交使用独立有界队列，
处置可以先于尚未开始的普通事务，不能抢占已开始的 SQLite 事务。

等待取消不撤销已入队操作；原生来源可通过重投取得第一次接管身份。关闭只
执行一次，停止新的写入后排空已受理操作；连接收回前仍可接管有限只读查询。
权威故障阻止后续写入，保留原库和可读取事实；本模块不调度模型或插件，
不以空库或内存状态替代提交失败。
线程内程序异常原样交回当前和排队的等待者，并停止派发、关闭连接。
"""

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from purrcept_engine.context.maintenance import (
    Reservation,
    SummaryError,
    SummaryProposal,
    SummarySource,
)
from purrcept_engine.errors import StorageError
from purrcept_engine.execution.protocol import (
    ActionProtocolError,
    JsonObject,
    JsonValue,
    decode_object,
)

from . import activities as activity_storage
from . import audio as audio_storage
from . import controls as control_storage
from . import execution as execution_storage
from . import feedback as feedback_storage
from . import knowledge as knowledge_storage
from . import maintenance as maintenance_storage
from . import model_requests as model_storage
from . import projections as projection_storage
from . import retention as retention_storage
from . import subject as subject_storage
from . import time as time_storage
from .activities import ActivityCommand, ActivityFeedback, ActivityRecord
from .audio_port import AudioLedger
from .controls import ControlLedger
from .execution import ActionOutcome, ActionRecord, CallOutcome, CallRecord
from .feedback_port import FeedbackLedger
from .knowledge_port import KnowledgeLedger
from .model_requests import (
    AttemptEvidence,
    AttemptStart,
    RequestEvidence,
    RequestOutcome,
    RequestRecord,
)
from .models import (
    Admission,
    InputContext,
    InputRecord,
    InputSource,
    InputSubmission,
    MediaReference,
)
from .subject import (
    AttentionLevel,
    InputProcessing,
    InputStage,
    MinimumWakeAssessment,
    SessionRecord,
    SessionScope,
    TurnOutcome,
    TurnRecord,
)
from .time_port import TimeLedger

_FORMAT_VERSION = 13
_logger = logging.getLogger(__name__)
_APPLICATION_ID = 0x50555245
_INPUT_COLUMNS = (
    "input_id, adapter_id, account_id, conversation_id, event_kind, native_event_id, "
    "sender_id, display_name, text, occurred_at, received_at, logical_bytes, context_json"
)
_SCHEMA = (
    "CREATE TABLE media (media_id TEXT PRIMARY KEY, content BLOB NOT NULL)",
    "CREATE TABLE inputs ("
    "input_id TEXT PRIMARY KEY, adapter_id TEXT NOT NULL, account_id TEXT NOT NULL, "
    "conversation_id TEXT NOT NULL, event_kind TEXT NOT NULL, native_event_id TEXT, "
    "sender_id TEXT, display_name TEXT, text TEXT NOT NULL, occurred_at TEXT NOT NULL, "
    "received_at TEXT NOT NULL, logical_bytes INTEGER NOT NULL CHECK(logical_bytes >= 0), "
    "context_json TEXT NOT NULL, dedup_key TEXT UNIQUE, fingerprint TEXT NOT NULL)",
    "CREATE TABLE input_media ("
    "input_id TEXT NOT NULL REFERENCES inputs(input_id), ordinal INTEGER NOT NULL, "
    "media_id TEXT NOT NULL REFERENCES media(media_id), media_type TEXT NOT NULL, "
    "name TEXT, PRIMARY KEY(input_id, ordinal))",
    "CREATE TABLE pending_inputs ("
    "sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
    "input_id TEXT UNIQUE NOT NULL REFERENCES inputs(input_id))",
    *execution_storage.SCHEMA,
    *model_storage.SCHEMA,
    *activity_storage.SCHEMA,
    *subject_storage.SCHEMA,
    *knowledge_storage.SCHEMA,
    *maintenance_storage.SCHEMA,
    *time_storage.SCHEMA,
    *feedback_storage.SCHEMA,
    *control_storage.SCHEMA,
    *audio_storage.SCHEMA,
)


def _upgrade_minimum_wake(connection: sqlite3.Connection) -> None:
    """在格式12上原子增加升级判断表，不改写原文、关系或业务记录。

    格式12的DDL已冻结。先核对全部原有对象和完整性，再在同一短事务创建
    表及索引。Store在全部初始化通过后推进版本并提交；任何拒绝或SQLite
    失败均留下原版本与原数据。不能用IF NOT EXISTS接受名称冲突。
    """
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
    ).fetchall()
    existing_sql = {_text(row[0]) for row in rows}
    for statement in _SCHEMA:
        if statement in subject_storage.MINIMUM_WAKE_SCHEMA:
            continue
        if statement not in existing_sql:
            raise StorageError("格式12的数据库结构不完整或已被修改。", code="invalid_record")
    for statement in subject_storage.MINIMUM_WAKE_SCHEMA:
        connection.execute(statement)


class _QueuedJob(Protocol):
    """数据库线程执行一次交接，结果通过所属事件循环交回等待者。

    Store 创建并入队，工作线程调用 run；数据库或业务边界错误由线程调用
    fail 回传。结果 Future 只在事件循环修改。该内部契约隐藏结果类型，
    使混合事务队列不必抹除公开方法的返回类型。
    """

    logical_bytes: int
    writing: bool

    def run(self, connection: sqlite3.Connection) -> Callable[[], None]:
        """执行事务并返回结果交付动作；线程先归还交接位置再公开结果。"""
        ...

    def fail(self, error: BaseException) -> None:
        """送回原错误；内部异常不转换成容量拒收或可纠正参数错误。"""
        ...


@dataclass(slots=True)
class _Job[Result]:
    """保留操作与等待者的精确结果类型，不将连接交给异步调用者。"""

    operation: Callable[[sqlite3.Connection], Result]
    future: asyncio.Future[Result]
    loop: asyncio.AbstractEventLoop
    logical_bytes: int
    writing: bool

    def run(self, connection: sqlite3.Connection) -> Callable[[], None]:
        """冻结事务结果，延迟公开到队列名额实际归还后。"""
        result = self.operation(connection)

        def publish() -> None:
            self.loop.call_soon_threadsafe(self.future.set_result, result)

        return publish

    def fail(self, error: BaseException) -> None:
        """在所属循环保留错误，即使原等待者已经取消。"""
        self.loop.call_soon_threadsafe(self.future.set_exception, error)


def _observe_result[Result](future: asyncio.Future[Result]) -> None:
    """取回无人继续等待的异常，保留 Future 中的结果供正常等待者读取。"""
    if not future.cancelled():
        future.exception()


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise StorageError("权威记录中的字符串字段无效。", code="invalid_record")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


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
) -> Iterator[knowledge_storage.SupervisionRecord]:
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
    snapshot: knowledge_storage.EvidenceSnapshot,
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
        snapshot.text != model_storage.encode(candidate)
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
        expected_text = model_storage.encode(
            {
                "source": "actual_call_receipt",
                "call": feedback_storage.call_material(call),
                "causal_input_id": action.input_id,
            }
        )
        if (
            snapshot.version != 1
            or snapshot.source_json != model_storage.encode(expected_source)
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
    selection = (
        "SELECT input_id FROM inputs" if selected is None else "SELECT value FROM json_each(?)"
    )
    rows: list[tuple[object, ...]] = connection.execute(
        "WITH selected(id) AS (" + selection + ") "
        "SELECT DISTINCT s.id FROM selected s WHERE "
        "EXISTS(SELECT 1 FROM actions a JOIN calls c ON c.action_id=a.action_id "
        "WHERE a.input_id=s.id AND c.state IN ('prepared','started','unknown')) OR "
        "EXISTS(SELECT 1 FROM actions a JOIN calls c ON c.action_id=a.action_id "
        "JOIN activity_commands cmd ON cmd.command_id=c.call_id "
        "JOIN activities act ON act.activity_id=cmd.activity_id "
        "WHERE a.input_id=s.id AND act.state NOT IN ('ended','completed')) OR "
        "EXISTS(SELECT 1 FROM failed_turns f JOIN turn_inputs i ON i.turn_id=f.turn_id "
        "WHERE i.input_id=s.id AND f.presented_in IS NULL)",
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
    """计量每项原文、关系和真实媒体，供外部输入及同库业务交接共用。"""
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

    Store 的外部接管及时间服务共享这一交接：闹铃消费与一次输入保存不能
    分两次提交。调用方拥有额度核验和失败回滚，去重仍按同一原生事实处理。
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
        model_storage.encode(submission.context),
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

    usage: tuple[object, ...] = connection.execute(
        "SELECT count(*), coalesce(sum(i.logical_bytes), 0) "
        "FROM pending_inputs p JOIN inputs i ON i.input_id = p.input_id"
    ).fetchone()
    if _integer(usage[0]) >= max_inputs or _integer(usage[1]) + logical_bytes > max_input_bytes:
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
            model_storage.encode(submission.context),
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
    knowledge_storage.put_evidence_in_transaction(
        connection,
        knowledge_storage.EvidenceSnapshot(
            evidence_id=input_id,
            version=1,
            kind="input",
            text=submission.text,
            source_json=model_storage.encode({"source": source, "context": submission.context}),
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
        """准备未打开实例；公开创建通过 open 取得线程初始化结果。

        路径在交接前固定到当前工作目录，实际文件与 SQLite I/O 全部在线程上。
        """
        self._path = Path(os.path.abspath(path))
        self._max_inputs = max_inputs
        self._max_input_bytes = max_input_bytes
        self._loop = asyncio.get_running_loop()
        self._ready: asyncio.Future[None] = self._loop.create_future()
        self._closed: asyncio.Future[None] = self._loop.create_future()
        self._ready.add_done_callback(_observe_result)
        self._closed.add_done_callback(_observe_result)
        self._condition = threading.Condition()
        self._ordinary: deque[_QueuedJob] = deque()
        self._control: deque[_QueuedJob] = deque()
        self._ordinary_count = 0
        self._ordinary_bytes = 0
        self._control_count = 0
        self._control_bytes = 0
        self._closing = False
        self._connection_closing = False
        self._failure: StorageError | None = None
        self._thread = threading.Thread(target=self._run, name="purrcept-storage", daemon=False)
        self._knowledge = KnowledgeLedger(self._submit)
        self._change_revision = 0
        self._changed = asyncio.Event()

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

        SQL或诊断材料损坏只表示本轮清理未完成；原文、参数、响应及媒体仍
        保留。提交队列/连接本身失效继续保持权威故障，不能在这里吞掉。
        """

        def cleanup(connection: sqlite3.Connection) -> projection_storage.JsonObject:
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
        """返回本进程成功写入通知代次，业务事实仍须从权威记录读取。"""
        return self._change_revision

    async def wait_for_change(self, after_revision: int) -> int:
        """等待新提交或关闭唤醒；通知丢失不能替代持久待办查询。

        工作器先记录代次再查可靠待办，查询期间发生的写入会立即结束本次
        等待。固定工作器可借用此等待，不创建每行订阅或无界消息总线。
        """
        while self._change_revision <= after_revision and not self._closing:
            self._changed.clear()
            if self._change_revision > after_revision:
                break
            await self._changed.wait()
        return self._change_revision

    def _notify_change(self) -> None:
        """数据库线程仅排入本循环通知，事实及其可靠待办已经提交。"""
        self._change_revision += 1
        self._changed.set()

    @classmethod
    async def open(
        cls, path: Path, *, max_inputs: int = 1024, max_input_bytes: int = 256 * 1024 * 1024
    ) -> "Store":
        """打开已知 Engine 格式的权威库，不转换未知数据库。

        空路径首次建立格式，已有库须通过格式和完整性核验。额度约束未处置
        输入；历史记录与同库 BLOB 不因处置而删除。打开失败释放已取得资源；
        打开等待取消也会请求线程关闭，不留下无所有者连接。
        """
        if not isinstance(path, Path):
            raise ValueError("path 必须是 Path。")
        _positive(max_inputs, "max_inputs")
        _positive(max_input_bytes, "max_input_bytes")
        store = cls(path, max_inputs, max_input_bytes)
        store._thread.start()
        try:
            await asyncio.shield(store._ready)
        except asyncio.CancelledError:
            store._begin_close()
            await asyncio.shield(store._closed)
            raise
        except BaseException:
            # 生命周期：失败只有在专属连接实际收回后才能交给目录锁所有者。
            # 这里仅等待清理，原错误仍原样传播。
            await asyncio.shield(store._closed)
            raise
        return store

    async def admit(self, submission: InputSubmission) -> Admission:
        """原子接管输入或返回同一原生事实的第一次确认。

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

    async def get_input(self, input_id: str) -> InputRecord | None:
        """读取接管事实及当前处置状态，查询不会改变待办。"""
        self._validate_id(input_id)
        return await self._submit(lambda connection: self._get_input(connection, input_id))

    async def get_native_input(self, source: InputSource) -> InputRecord | None:
        """只读原生身份的第一次接管事实，不重算上下文或改变处理阶段。

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

        单批不会超过配置的接管条数上限；默认最多读取八条。
        """
        _positive(limit, "limit")
        bounded_limit = min(limit, self._max_inputs)
        return await self._submit(lambda connection: self._pending(connection, bounded_limit))

    async def complete_input(self, input_id: str) -> None:
        """明确提交输入已处置，释放积压额度并保留原文与媒体。

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
        """读取筛选、延后或前台处理的实际版本，不改变交接状态。"""
        self._validate_id(input_id)
        return await self._submit(
            lambda connection: subject_storage.processing(connection, input_id)
        )

    async def staged_inputs(self, stage: InputStage, *, limit: int = 8) -> tuple[InputRecord, ...]:
        """按接管顺序加载有限调度批次；状态只由业务事务推进。"""
        _positive(limit, "limit")

        def read(connection: sqlite3.Connection) -> tuple[InputRecord, ...]:
            records: list[InputRecord] = []
            for input_id in subject_storage.pending_ids(connection, stage, limit):
                record = self._get_input(connection, input_id)
                if record is None:
                    raise StorageError("处理队列引用不存在的原文。", code="invalid_record")
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
        defer_until: float | None = None,
    ) -> bool:
        """整批判断与 pending/defer 交接原子提交；版本过期明确不采用。"""
        return await self._submit(
            lambda connection: subject_storage.decide_inputs(
                connection, expected, level, reason, request_id=request_id, defer_until=defer_until
            ),
            logical_bytes=len(model_storage.encode((expected, reason)).encode("utf-8")),
            control=True,
            writing=True,
        )

    async def deliver_due_inputs(self, now: float) -> tuple[str, ...]:
        """到期直接交前台一次；不重新启动筛选或刷新延后期限。"""
        return await self._submit(
            lambda connection: subject_storage.deliver_due(connection, now),
            control=True,
            writing=True,
        )

    async def register_minimum_wake_assessment(
        self, expected: InputProcessing, *, owner_id: str, observed_generation: int
    ) -> bool:
        """保存最低唤起的一次旧代次升级资格，不创建第二份前台待办。"""
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
        """有限读取本宿主尚未结束的升级资格，重启不能沿用旧生成观察。"""
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
        """保存判断原结果并 CAS 原输入版本，保留最低 WAKE 与单一前台交接。"""
        return await self._submit(
            lambda connection: subject_storage.finish_minimum_wake_assessment(
                connection, assessment, level, reason, request_id=request_id
            ),
            logical_bytes=len(reason.encode("utf-8")),
            control=True,
            writing=True,
        )

    async def next_deferred_input(self) -> float | None:
        """读取最早延后期限以设置单个调度定时器。"""
        return await self._submit(subject_storage.next_deferred)

    async def get_session(self, scope: SessionScope) -> SessionRecord:
        """还原完整已提交检查点媒体，不返回丢失内容的空历史。"""
        return await self._submit(lambda connection: subject_storage.session(connection, scope))

    async def protected_input_ids(self, input_ids: tuple[str, ...]) -> frozenset[str]:
        """查询真实未决调用、活动、计划、有效监管和未承接失败。

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

        一个存储作业读取当前前台版本、全部未完成计划、仍有效未回应监管、
        活动及必要调用事实。数据库分块不限制最终选材；这里不裁剪正文、
        退役未知效果或执行控制。注意背景使用另一份明确有限的投影。
        """

        def query(connection: sqlite3.Connection) -> JsonObject:
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
        """在无工具摘要请求前原子保留同覆盖尝试，失败不会自动重新计预算。"""
        return await self._submit(
            lambda connection: maintenance_storage.reserve_summary(connection, source),
            logical_bytes=len(model_storage.encode(source).encode("utf-8")),
            control=True,
            writing=True,
        )

    async def install_summary(self, proposal: SummaryProposal) -> int | None:
        """idle/版本仍匹配才归档旧原文并安装候选，失败保留原会话和媒体。"""
        return await self._submit(
            lambda connection: maintenance_storage.install_summary(connection, proposal),
            logical_bytes=len(model_storage.encode(proposal).encode("utf-8")),
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
        """先接管主动处理身份，固定输入及会话版本后才借用 Core。"""
        return await self._submit(
            lambda connection: subject_storage.begin_turn(
                connection, turn_id, scope, expected_version, generation, input_ids
            ),
            logical_bytes=len(model_storage.encode(input_ids).encode("utf-8")),
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
        """原子安装成功历史或保留失败经历；取消等待不撤销已经接管提交。"""
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
        """读取待承接的失败经历，实际效果另按行动与调用身份读取。"""
        return await self._submit(lambda connection: subject_storage.failures(connection, scope))

    async def get_turn(self, turn_id: str) -> TurnRecord | None:
        """核对接管后的真实批次，不以调用方等待取消推断事务未发生。"""
        self._validate_id(turn_id)
        return await self._submit(lambda connection: subject_storage.get_turn(connection, turn_id))

    async def get_media(self, media_id: str) -> bytes | None:
        """读取正式媒体并核对内容摘要，不以损坏字节替代已接管证据。"""
        self._validate_id(media_id)
        return await self._submit(lambda connection: self._get_media(connection, media_id))

    async def begin_action(
        self, action_id: str, *, catalog_version: str, source: str, input_id: str | None = None
    ) -> None:
        """执行所有者先保存代码与目录版本；确认返回前不得启动该行动。"""
        self._validate_id(action_id)
        self._validate_id(catalog_version)
        if not isinstance(source, str):
            raise ValueError("行动源码必须是字符串。")
        size = execution_storage.payload_bytes(action_id, catalog_version, source, input_id)
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
        """返回派发前可同步核验的权威资格，不以查询成功替代写入健康。

        故障与关闭门槛由同一线程条件锁读取；调用者在 await 后仍须再次核验，
        该快照不预留提交容量，也不是外部效果已经发生的证明。
        """
        with self._condition:
            return self._failure is None and not self._closing and not self._connection_closing

    async def begin_request(self, request: RequestEvidence) -> None:
        """原子保存编译材料与同库 BLOB 引用，确认前不允许模型派发。"""
        await self._submit(
            lambda connection: model_storage.begin_request(connection, request),
            logical_bytes=len(model_storage.encode(request).encode("utf-8")),
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
        """保存本次真实尝试身份，模型所有者在等待后重验资格和截止。"""
        await self._submit(
            lambda connection: model_storage.begin_attempt(connection, start),
            logical_bytes=len(model_storage.encode(start).encode("utf-8")),
            control=True,
            writing=True,
        )

    async def finish_attempt(self, result: AttemptEvidence) -> None:
        """提交权威响应/原参数，再独立保存有限诊断，不覆盖另一次尝试。"""
        await self._submit(
            lambda connection: model_storage.finish_attempt(connection, result),
            logical_bytes=len(model_storage.encode(result).encode("utf-8")),
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
        """提交逻辑请求结论，已经成功的远端结果不等同于会话已采用。"""
        self._validate_id(request_id)
        await self._submit(
            lambda connection: model_storage.finish_request(connection, request_id, state),
            logical_bytes=len(request_id.encode("utf-8")),
            control=True,
            writing=True,
        )

    async def get_request(self, request_id: str) -> RequestRecord | None:
        """从权威快照还原公开请求与独立尝试；缺失媒体明确失败。"""
        self._validate_id(request_id)
        return await self._submit(
            lambda connection: model_storage.get_request(connection, request_id)
        )

    async def requests_for_turn(self, owner_id: str, turn_id: str) -> tuple[RequestRecord, ...]:
        """按因果身份还原本批各逻辑请求，包括容量依据及独立传输尝试。"""

        def read(connection: sqlite3.Connection) -> tuple[RequestRecord, ...]:
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
        """取得未结束活动的当前快照，查询不创建或恢复驱动。"""
        return await self._submit(activity_storage.activity_records)

    async def get_activity(self, activity_id: str) -> ActivityRecord | None:
        """读取控制、预算和工作当前事实，不自动重放。"""
        self._validate_id(activity_id)
        return await self._submit(
            lambda connection: activity_storage.get_activity(connection, activity_id)
        )

    async def get_activity_command(self, command_id: str) -> ActivityCommand | None:
        """区分命令接管、驱动受理及实际确认。"""
        self._validate_id(command_id)
        return await self._submit(
            lambda connection: activity_storage.get_activity_command(connection, command_id)
        )

    async def get_activity_feedback(self, feedback_id: str) -> ActivityFeedback | None:
        """读取首次反馈的稳定材料，以事实身份处理重投。"""
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
        """CAS 提交当前状态、控制或反馈及可靠交接，事务不等待专业驱动。"""
        size = len(model_storage.encode((record, command, feedback)).encode("utf-8"))
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
        """登记上次运行中断；保留累计，离线时间不扣，不构造驱动。"""
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
        """读取该独立消费者尚未接管的反馈，查询不确认完成。"""
        return await self._submit(
            lambda connection: activity_storage.pending_activity_feedback(connection, consumer)
        )

    async def acknowledge_activity_feedback(
        self,
        feedback_id: str,
        consumer: Literal["foreground", "reflection"],
    ) -> None:
        """只确认一份交接责任，前台与反思互不清除对方待办。"""
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
        """派发前接管完整调用意图；prepared 不证明已进入 Provider。"""
        for identity in (call_id, action_id, path, target_id):
            self._validate_id(identity)
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("调用顺序必须是非负整数。")
        size = execution_storage.payload_bytes(call_id, action_id, path, target_id, arguments_json)
        execution_storage.validate_json(arguments_json, arguments=True)
        await self._submit(
            lambda connection: execution_storage.begin_call(
                connection, call_id, action_id, sequence, path, target_id, arguments_json
            ),
            control=True,
            writing=True,
            logical_bytes=size,
        )

    async def mark_call_started(self, call_id: str) -> None:
        """提交进入 Provider 前的门槛；执行方返回后仍须在锁内核验资格。

        等待取消不撤销已提交门槛；不得把再次调用此方法当作执行许可或重发。
        """
        self._validate_id(call_id)
        size = execution_storage.payload_bytes(call_id)
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

        重复相同结果返回同版本；未知可由迟到真实确认或失败澄清，其旧版本仍
        保留并分别待交接。调用者取消等待不取消结果事务，查询可取回事实。
        """
        self._validate_id(call_id)
        if state not in ("confirmed", "failed", "unknown", "not_started"):
            raise ValueError("调用结果状态无效。")
        size = execution_storage.payload_bytes(call_id, result_json, error_type, error)
        size += len(model_storage.encode(media).encode())
        execution_storage.validate_json(result_json)
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
        """保存行动结束事实，失败与取消都不回滚或覆盖逐次真实回执。"""
        self._validate_id(action_id)
        if state not in ("completed", "failed", "timed_out", "cancelled"):
            raise ValueError("行动结果状态无效。")
        if not isinstance(output, str):
            raise ValueError("行动输出必须是字符串。")
        size = execution_storage.payload_bytes(action_id, result_json, output, error)
        execution_storage.validate_json(result_json)
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
        """查询行动、当前调用及完整回执版本；读取不启动或确认任何工作。"""
        self._validate_id(action_id)
        return await self._submit(
            lambda connection: execution_storage.get_action(connection, action_id)
        )

    async def actions_for_inputs(self, input_ids: tuple[str, ...]) -> tuple[ActionRecord, ...]:
        """按实际持久来源关联查询行动，不从模型结果文本猜测发生关系。"""
        for input_id in input_ids:
            self._validate_id(input_id)

        def read(connection: sqlite3.Connection) -> tuple[ActionRecord, ...]:
            rows: list[tuple[object, ...]] = connection.execute(
                "SELECT action_id FROM actions WHERE input_id IN (SELECT value FROM json_each(?)) "
                "ORDER BY created_at,action_id",
                (model_storage.encode(input_ids),),
            ).fetchall()
            result: list[ActionRecord] = []
            for row in rows:
                action = execution_storage.get_action(connection, _text(row[0]))
                if action is None:
                    raise StorageError("因果行动引用损坏。", code="invalid_record")
                result.append(action)
            return tuple(result)

        return await self._submit(
            read, logical_bytes=len(model_storage.encode(input_ids).encode("utf-8"))
        )

    async def pending_call_receipts(self, *, limit: int = 16) -> tuple[CallRecord, ...]:
        """读取有限可靠交接版本；消费确认须由实际主体接管事务另行提交。"""
        _positive(limit, "limit")
        bounded_limit = min(limit, self._max_inputs)
        return await self._submit(
            lambda connection: execution_storage.pending_call_receipts(connection, bounded_limit)
        )

    async def close(self) -> None:
        """禁止新的写入并等待同一排空与关闭过程。

        取消本次等待不会取消线程收尾；再次调用加入原关闭过程，不再次关闭
        连接。宿主负责整体关闭期限，本入口等待实际完成才返回。
        """
        self._check_loop()
        self._begin_close()
        await asyncio.shield(self._closed)

    def _check_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Store 只能在创建它的事件循环上使用。")

    @staticmethod
    def _validate_id(value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError("记录身份必须是非空字符串。")

    async def _submit[Result](
        self,
        operation: Callable[[sqlite3.Connection], Result],
        *,
        logical_bytes: int = 0,
        control: bool = False,
        writing: bool = False,
    ) -> Result:
        """在取得有限交接位置后提交；shield 保护的是实际提交所有权。"""
        self._check_loop()
        future: asyncio.Future[Result] = self._loop.create_future()
        future.add_done_callback(_observe_result)
        job = _Job(operation, future, self._loop, logical_bytes, writing)
        with self._condition:
            if self._connection_closing or (self._closing and writing):
                raise StorageError("存储已开始关闭。", code="store_closed")
            if writing and self._failure is not None:
                raise StorageError("权威存储已故障，拒绝新的写入。") from self._failure

            # 控制有独立的条数和字节位置，真实回执不借普通输入容量收尾；
            # 参数与结果也须计量，不能在控制路径绕过有界载荷。
            if control:
                if (
                    self._control_count >= self._max_inputs
                    or self._control_bytes + logical_bytes > self._max_input_bytes
                ):
                    raise StorageError("存储控制提交已满。", code="store_busy")
                self._control_count += 1
                self._control_bytes += logical_bytes
                self._control.append(job)
            else:
                if (
                    self._ordinary_count >= self._max_inputs
                    or self._ordinary_bytes + logical_bytes > self._max_input_bytes
                ):
                    raise StorageError("存储普通提交已满。", code="store_busy")
                self._ordinary_count += 1
                self._ordinary_bytes += logical_bytes
                self._ordinary.append(job)
            self._condition.notify()
        return await asyncio.shield(future)

    def _begin_close(self) -> None:
        """关闭资格由同一条件锁串行确认，排队中的提交仍由线程持有。"""
        with self._condition:
            self._closing = True
            self._condition.notify()
        self._loop.call_soon_threadsafe(self._notify_change)

    def _run(self) -> None:
        """专属线程拥有从打开到关闭的全部连接访问与事务执行。"""
        connection: sqlite3.Connection | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._path)
            self._initialize(connection)
        except (OSError, sqlite3.Error, StorageError) as error:
            failure = self._storage_failure(error)
            self._loop.call_soon_threadsafe(self._ready.set_exception, failure)
        except BaseException as error:
            # 跨线程交接必须完成 Future。保留程序异常本身，不让线程退出后
            # 启动等待者永久悬挂，也不把它归入可恢复的 SQLite 错误。
            self._loop.call_soon_threadsafe(self._ready.set_exception, error)
        else:
            self._loop.call_soon_threadsafe(self._ready.set_result, None)
            self._drain(connection)
        finally:
            with self._condition:
                self._connection_closing = True
            try:
                if connection is not None:
                    connection.close()
            except sqlite3.Error as error:
                self._loop.call_soon_threadsafe(
                    self._closed.set_exception, self._storage_failure(error)
                )
            else:
                self._loop.call_soon_threadsafe(self._closed.set_result, None)

    def _drain(self, connection: sqlite3.Connection) -> None:
        """控制优先于未开始事务；已开始事务完成后才归还交接位置。"""
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: bool(self._control or self._ordinary) or self._closing
                )
                if not self._control and not self._ordinary:
                    # 读取与连接关闭使用同一门槛；此后不再接管排队，否则新查询
                    # 可能落在已排空队列之后，永久等待无人执行的任务。
                    self._connection_closing = True
                    return
                control = bool(self._control)
                job = self._control.popleft() if control else self._ordinary.popleft()
            fatal: BaseException | None = None
            error_to_send: BaseException | None = None
            publish: Callable[[], None] | None = None
            changes_before = connection.total_changes
            try:
                # 权威故障也撤销尚未开始的已排队写入资格，防止前一提交失败后
                # 后续派发意图仍被接管并成为能力启动许可；只读事实继续可查询。
                with self._condition:
                    if job.writing and self._failure is not None:
                        raise StorageError("权威存储已故障，拒绝排队写入。") from self._failure
                publish = job.run(connection)
            except StorageError as error:
                if error.code in {"invalid_record", "unsupported_format"}:
                    self._latch_failure(error)
                error_to_send = error
            except knowledge_storage.KnowledgeInputError as error:
                # 来源/批次/版本冲突可在原预算内纠正，不是数据库或程序故障。
                # 该显式业务分类仍原样交回，其他未识别异常继续停止派发。
                error_to_send = error
            except (SummaryError, time_storage.TimeError, control_storage.ControlError) as error:
                error_to_send = error
            except sqlite3.Error as error:
                failure = self._storage_failure(error)
                self._latch_failure(failure)
                error_to_send = failure
            except BaseException as error:
                fatal = error
                error_to_send = error
            finally:
                with self._condition:
                    if control:
                        self._control_count -= 1
                        self._control_bytes -= job.logical_bytes
                    else:
                        self._ordinary_count -= 1
                        self._ordinary_bytes -= job.logical_bytes
            # Ordering: Future 一旦完成，调用方可立即提交下一条操作。名额须
            # 已归还，否则 max_inputs=1 时成功 admit 后的 pending 会随机拒收。
            if error_to_send is not None:
                job.fail(error_to_send)
            else:
                assert publish is not None
                if job.writing and connection.total_changes > changes_before:
                    self._loop.call_soon_threadsafe(self._notify_change)
                publish()
            if fatal is not None:
                self._abort_pending(fatal)
                return

    def _abort_pending(self, error: BaseException) -> None:
        """程序异常停止线程派发，并把所有已接管等待者交回同一个真实故障。"""
        with self._condition:
            self._closing = True
            self._connection_closing = True
            pending = (*self._control, *self._ordinary)
            self._control_count -= len(self._control)
            self._control_bytes -= sum(queued.logical_bytes for queued in self._control)
            self._ordinary_count -= len(self._ordinary)
            self._ordinary_bytes -= sum(queued.logical_bytes for queued in self._ordinary)
            self._control.clear()
            self._ordinary.clear()
            self._loop.call_soon_threadsafe(self._notify_change)
            # 当前任务已归还，只扣尚未开始的队列位置。
            for queued in pending:
                queued.fail(error)

    def _latch_failure(self, failure: StorageError) -> None:
        """权威故障保留为拒写原因，不自动重建连接或替换空数据库。"""
        with self._condition:
            if self._failure is None:
                self._failure = failure
                self._loop.call_soon_threadsafe(self._notify_change)

    @staticmethod
    def _storage_failure(error: BaseException) -> StorageError:
        """翻译真实 I/O 与 SQLite 错误并保留原因，沿用已分类的存储错误。"""
        if isinstance(error, StorageError):
            return error
        result = StorageError("权威 SQLite 操作失败，原数据库已保留。")
        result.__cause__ = error
        return result

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        """初始化空库或升级已知格式12；未知格式和完整性故障保留原库。"""
        connection.execute("PRAGMA foreign_keys = ON")
        version_row: tuple[object, ...] = connection.execute("PRAGMA user_version").fetchone()
        identity_row: tuple[object, ...] = connection.execute("PRAGMA application_id").fetchone()
        version = _integer(version_row[0])
        identity = _integer(identity_row[0])
        tables: list[tuple[object, ...]] = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        # Ordering: 格式、结构、业务解码和上次在途恢复共享一次提交。子系统
        # 初始化借用此事务；任何后置拒绝都不能留下升级或部分中断记录。
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if version == 0 and identity == 0 and not tables:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            elif version == 12 and identity == _APPLICATION_ID:
                _upgrade_minimum_wake(connection)
            elif version != _FORMAT_VERSION or identity != _APPLICATION_ID:
                raise StorageError("数据库不是支持的 Engine 格式。", code="unsupported_format")

            check: tuple[object, ...] = connection.execute("PRAGMA quick_check").fetchone()
            if check != ("ok",):
                raise StorageError("权威数据库完整性检查失败。", code="invalid_record")
            broken_reference: tuple[object, ...] | None = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchone()
            if broken_reference is not None:
                raise StorageError("权威数据库包含失效引用。", code="invalid_record")
            # 明确读取必需列，在装配时暴露缺表/缺列，避免首次输入才触发格式失败。
            connection.execute(
                f"SELECT {_INPUT_COLUMNS}, dedup_key, fingerprint FROM inputs LIMIT 0"
            )
            connection.execute("SELECT media_id, content FROM media LIMIT 0")
            connection.execute(
                "SELECT input_id, ordinal, media_id, media_type, name FROM input_media LIMIT 0"
            )
            connection.execute("SELECT sequence, input_id FROM pending_inputs LIMIT 0")
            execution_storage.initialize(connection)
            model_storage.initialize(connection)
            subject_storage.initialize(connection)
            knowledge_storage.initialize(connection)
            maintenance_storage.initialize(connection)
            time_storage.initialize(connection)
            feedback_storage.initialize(connection)
            control_storage.initialize(connection)
            connection.execute(
                "SELECT sequence,plugin_id,identity,fact,occurred_at,payload_json "
                "FROM audio_facts LIMIT 0"
            )
            activity_storage.activity_records(connection)
            connection.execute(f"PRAGMA user_version = {_FORMAT_VERSION}")

    def _admit(
        self, connection: sqlite3.Connection, submission: InputSubmission, logical_bytes: int
    ) -> Admission:
        """外部接管持有事务；其他业务在自己的事务中复用同一输入保存机制。"""
        with self._condition:
            if self._failure is not None:
                raise StorageError("权威存储已故障，拒绝新的写入。") from self._failure
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
        try:
            context_fields = decode_object(_text(row[12]).encode("utf-8"))
            if (
                set(context_fields)
                != {
                    "type",
                    "details_json",
                    "minimum_wake",
                    "interrupt_generation",
                    "speech_started",
                    "missing_materials",
                }
                or context_fields["type"] != "InputContext"
            ):
                raise ValueError("持久化输入关系的类型或字段不符合格式。")
            signals = tuple(
                context_fields[key]
                for key in ("minimum_wake", "interrupt_generation", "speech_started")
            )
            if any(type(value) is not bool for value in signals):
                raise ValueError("持久化程序信号必须是 bool。")
            missing_values = context_fields["missing_materials"]
            if not isinstance(missing_values, list):
                raise ValueError("持久化材料缺口必须是数组。")
            missing = tuple(_text(item) for item in missing_values)
            context = InputContext(
                details_json=_text(context_fields["details_json"]),
                minimum_wake=context_fields["minimum_wake"] is True,
                interrupt_generation=context_fields["interrupt_generation"] is True,
                speech_started=context_fields["speech_started"] is True,
                missing_materials=missing,
            )
        except (ActionProtocolError, ValueError, KeyError) as error:
            raise StorageError("输入关系和程序信号损坏。", code="invalid_record") from error
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
        with self._condition:
            if self._failure is not None:
                raise StorageError("权威存储已故障，拒绝新的写入。") from self._failure
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
