"""在 Store 唯一线程保存原文快照、分页凭据和行为评审事实。

文档快照幂等登记，旧版本保留供确切引用；创建片段不代表已向模型呈现。
行为评审在原事务确认范围及监管，普通模型文字不能结案。
本模块还保存显式计划，不保存长期认识或调度 Notebook 整理任务。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Sequence
from typing import Literal, cast

from purrcept_core.models import ContentBlock, Message, MessageRole, ToolResultBlock
from pydantic import BaseModel, ValidationError

from purrcept_engine.domain.context.checkpoints import decode_content, decode_message
from purrcept_engine.domain.evidence.models import (
    EvidenceConflict,
    EvidenceDelivery,
    EvidenceInputError,
    EvidencePage,
    EvidenceSlice,
    EvidenceSnapshot,
    FinishSubmission,
    OperationalFinding,
    PendingEvidence,
    RangeUnit,
    ReflectionBatch,
    ReflectionReceipt,
    SupervisionNotice,
    SupervisionRecord,
    WakeupPlan,
)
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite import analysis_work, behavior_review
from purrcept_engine.infrastructure.sqlite.model_requests import (
    intern_core_media,
    restore_core_media,
)
from purrcept_engine.serialization import encode

EvidencePage.model_rebuild()


# ---------------------------------------------------------------------------
# 同库关系与原文快照
# ---------------------------------------------------------------------------

SCHEMA = (
    "CREATE TABLE knowledge_evidence (evidence_id TEXT NOT NULL, version INTEGER NOT NULL, "
    "payload_json TEXT NOT NULL, valid INTEGER NOT NULL CHECK(valid IN(0,1)), created_at REAL "
    "NOT NULL, PRIMARY KEY(evidence_id,version))",
    "CREATE TABLE evidence_media (evidence_id TEXT NOT NULL,version INTEGER NOT NULL, "
    "position INTEGER NOT NULL,media_id TEXT NOT NULL REFERENCES media(media_id), "
    "PRIMARY KEY(evidence_id,version,position), "
    "FOREIGN KEY(evidence_id,version) REFERENCES knowledge_evidence(evidence_id,version))",
    "CREATE TABLE knowledge_slices (slice_ref TEXT PRIMARY KEY, evidence_id TEXT NOT NULL, "
    "version INTEGER NOT NULL, payload_json TEXT NOT NULL, "
    "FOREIGN KEY(evidence_id,version) REFERENCES knowledge_evidence(evidence_id,version))",
    "CREATE TABLE knowledge_cursors (cursor TEXT PRIMARY KEY, evidence_id TEXT NOT NULL, "
    "version INTEGER NOT NULL, start INTEGER NOT NULL, page_chars INTEGER NOT NULL, kind "
    "TEXT NOT NULL)",
    "CREATE TABLE knowledge_review_ranges (slice_ref TEXT NOT NULL REFERENCES "
    "knowledge_slices(slice_ref), "
    "batch_id TEXT NOT NULL,batch_version INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT, "
    "PRIMARY KEY(slice_ref,batch_id,batch_version))",
    "CREATE TABLE reflection_batches (batch_id TEXT NOT NULL, version INTEGER NOT NULL, "
    "payload_json TEXT NOT NULL, state TEXT NOT NULL, receipt_json TEXT, "
    "PRIMARY KEY(batch_id,version))",
    "CREATE TABLE reflection_presented (batch_id TEXT NOT NULL, version INTEGER NOT NULL, "
    "slice_ref TEXT NOT NULL REFERENCES knowledge_slices(slice_ref), "
    "PRIMARY KEY(batch_id,version,slice_ref))",
    "CREATE TABLE reflection_deliveries(delivery_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,"
    "version INTEGER NOT NULL,kind TEXT NOT NULL CHECK(kind IN('initial','tool')),"
    "tool_call_id TEXT,payload_json TEXT NOT NULL,fingerprint TEXT NOT NULL,"
    "FOREIGN KEY(batch_id,version) REFERENCES reflection_batches(batch_id,version))",
    "CREATE INDEX reflection_deliveries_match ON reflection_deliveries"
    "(kind,tool_call_id,fingerprint)",
    "CREATE TABLE reflection_delivery_slices(delivery_id TEXT NOT NULL "
    "REFERENCES reflection_deliveries(delivery_id),ordinal INTEGER NOT NULL,"
    "slice_ref TEXT NOT NULL REFERENCES knowledge_slices(slice_ref),"
    "PRIMARY KEY(delivery_id,ordinal))",
    "CREATE TABLE reflection_delivery_media(delivery_id TEXT NOT NULL "
    "REFERENCES reflection_deliveries(delivery_id),media_id TEXT NOT NULL "
    "REFERENCES media(media_id),"
    "PRIMARY KEY(delivery_id,media_id))",
    "CREATE TABLE knowledge_supervision (id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, "
    "state TEXT NOT NULL, response TEXT, created_at REAL NOT NULL)",
    "CREATE TABLE knowledge_plans (id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)",
    *analysis_work.SCHEMA,
    *behavior_review.SCHEMA,
)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise StorageError("证据存储字符串字段损坏。", code="invalid_record")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StorageError("证据存储版本字段损坏。", code="invalid_record")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise StorageError("证据存储时间字段损坏。", code="invalid_record")
    return float(value)


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _page_limit(value: int) -> None:
    """SQLite 负 LIMIT 表示无限；所有公开分页入口必须先拒绝该形状。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EvidenceInputError("limit 必须是正整数。")


def _notice_state(value: object) -> Literal["open", "accept", "partial", "reject"]:
    if value not in ("open", "accept", "partial", "reject"):
        raise StorageError("监管意见状态损坏。", code="invalid_record")
    return value


def _decode[Record: BaseModel](value: object, model: type[Record]) -> Record:
    """持久化读取是独立边界，坏记录不能降级成空检索结果。"""
    try:
        return model.model_validate_json(_text(value))
    except ValidationError as error:
        raise StorageError("证据存储记录形状损坏。", code="invalid_record") from error


def _one(
    connection: sqlite3.Connection, sql: str, parameters: Sequence[object]
) -> tuple[object, ...] | None:
    row: tuple[object, ...] | None = connection.execute(sql, parameters).fetchone()
    return row


def _evidence(
    connection: sqlite3.Connection, evidence_id: str, version: int | None
) -> EvidenceSnapshot | None:
    """恢复原文记录并核对已保存媒体，权威损坏不能用缺省值掩盖。"""
    row = _one(
        connection,
        "SELECT payload_json,valid FROM knowledge_evidence WHERE evidence_id=? "
        + ("ORDER BY version DESC LIMIT 1" if version is None else "AND version=?"),
        (evidence_id,) if version is None else (evidence_id, version),
    )
    if row is None:
        return None
    if row[1] not in (0, 1):
        raise StorageError("原文有效性字段损坏。", code="invalid_record")
    return _decode(row[0], EvidenceSnapshot).model_copy(update={"valid": row[1] == 1})


def put_evidence(
    connection: sqlite3.Connection, snapshot: EvidenceSnapshot, *, needs_reflection: bool = True
) -> EvidenceSnapshot:
    """原文与待复核身份同事务接管。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        snapshot: 完整不可变原文快照，含来源、确切版本及媒体引用。
        needs_reflection: 是否为新原文登记可靠的待反思关系。 默认 True。

    Returns:
        完整保存的原文快照，包含确切来源、版本与媒体关系。

    Note:
        相同事实幂等，重复保存不复活撤销来源。
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        return put_evidence_in_transaction(connection, snapshot, needs_reflection=needs_reflection)


def put_evidence_in_transaction(
    connection: sqlite3.Connection, snapshot: EvidenceSnapshot, *, needs_reflection: bool = True
) -> EvidenceSnapshot:
    """借用调用方事务接管原文、BLOB 关系和待办，不自行开始或结束事务。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        snapshot: 完整不可变原文快照，含来源、确切版本及媒体引用。
        needs_reflection: 是否为新原文登记可靠的待反思关系。 默认 True。

    Returns:
        完整保存的原文快照，包含确切来源、版本与媒体关系。

    Raises:
        RuntimeError: 原文接管必须处于调用方拥有的事务中。
        EvidenceConflict: 同一原文身份和版本已有不同内容。
        StorageError: 原文引用的权威媒体缺失；原文引用的权威媒体摘要或长度不匹配。

    Note:
        输入和轮次事实由 Store 先保存同库媒体，再在同一事务调用此入口；任一步
        失败由外层回滚全部接管。独立调用使用 put_evidence，不能依赖提交后通知。
    """
    if not connection.in_transaction:
        raise RuntimeError("原文接管必须处于调用方拥有的事务中。")
    for reference in snapshot.media:
        row = _one(connection, "SELECT content FROM media WHERE media_id=?", (reference.media_id,))
        if row is None or not isinstance(row[0], bytes):
            raise StorageError("原文引用的权威媒体缺失。", code="invalid_record")
        if (
            len(row[0]) != reference.byte_length
            or hashlib.sha256(row[0]).hexdigest() != reference.media_id
        ):
            raise StorageError("原文引用的权威媒体摘要或长度不匹配。", code="invalid_record")
    existing = _evidence(connection, snapshot.evidence_id, snapshot.version)
    if existing is not None:
        if existing.model_dump(exclude={"valid"}) != snapshot.model_dump(exclude={"valid"}):
            raise EvidenceConflict("原文版本已有不同内容。")
        return existing
    now = time.time()
    connection.execute(
        "INSERT INTO knowledge_evidence VALUES(?,?,?,?,?)",
        (
            snapshot.evidence_id,
            snapshot.version,
            snapshot.model_dump_json(),
            int(snapshot.valid),
            now,
        ),
    )
    connection.executemany(
        "INSERT INTO evidence_media VALUES(?,?,?,?)",
        (
            (snapshot.evidence_id, snapshot.version, position, item.media_id)
            for position, item in enumerate(snapshot.media)
        ),
    )
    if needs_reflection and snapshot.valid:
        analysis_work.enqueue(connection, snapshot.evidence_id, snapshot.version, now)
    return snapshot


def get_evidence(
    connection: sqlite3.Connection,
    evidence_id: str,
    *,
    version: int | None = None,
    include_inactive: bool = False,
) -> EvidenceSnapshot | None:
    """按确切原文版本查询。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        evidence_id: 已保存原文快照的稳定身份。
        version: 目标记录的确切版本，区别于可变的当前版本。 None 首读当前版本，
            续读仍绑定游标的原版本。
        include_inactive: 是否同时返回已失效的历史记录，并保留失效原因。 默认 False。

    Returns:
        完整保存的原文快照，包含确切来源、版本与媒体关系。 未取得对应记录或素材时返回 None；
        读取故障仍抛出。

    Note:
        显式历史查询保留撤销事实，不赋予引用资格。
    """
    snapshot = _evidence(connection, evidence_id, version)
    return snapshot if snapshot is not None and (snapshot.valid or include_inactive) else None


def get_slice(connection: sqlite3.Connection, slice_ref: str) -> EvidenceSlice | None:
    """展开宿主生成的范围身份，不接受模型手填坐标代替真实片段。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        slice_ref: 已保存原文片段的不可变身份。

    Returns:
        同一原文版本中已保存的精确范围及不可变片段身份。 未取得对应记录或素材时返回 None；
        读取故障仍抛出。
    """
    row = _one(
        connection, "SELECT payload_json FROM knowledge_slices WHERE slice_ref=?", (slice_ref,)
    )
    return None if row is None else _decode(row[0], EvidenceSlice)


def _slice(
    connection: sqlite3.Connection,
    snapshot: EvidenceSnapshot,
    start: int,
    end: int,
    unit: RangeUnit,
    media_ids: tuple[str, ...],
) -> EvidenceSlice:
    """同版本和完整范围生成稳定身份，读取重放不会制造新的引用资格。"""
    identity = json.dumps(
        (snapshot.evidence_id, snapshot.version, unit, start, end, media_ids),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    reference = "slice:" + hashlib.sha256(identity.encode()).hexdigest()
    result = EvidenceSlice(
        slice_ref=reference,
        evidence_id=snapshot.evidence_id,
        version=snapshot.version,
        start=start,
        end=end,
        unit=unit,
        media_ids=media_ids,
    )
    connection.execute(
        "INSERT OR IGNORE INTO knowledge_slices VALUES(?,?,?,?)",
        (
            reference,
            snapshot.evidence_id,
            snapshot.version,
            result.model_dump_json(),
        ),
    )
    return result


def _cursor(
    connection: sqlite3.Connection,
    identity: str,
    version: int,
    start: int,
    page_chars: int,
    kind: str,
) -> str:
    """续读资格绑定完整来源、版本及起点，同一页长的重放得到同一范围。"""
    encoded = json.dumps((kind, identity, version, start, page_chars), separators=(",", ":"))
    cursor = "cursor:" + hashlib.sha256(encoded.encode()).hexdigest()
    connection.execute(
        "INSERT OR IGNORE INTO knowledge_cursors VALUES(?,?,?,?,?,?)",
        (cursor, identity, version, start, page_chars, kind),
    )
    return cursor


def read_evidence(
    connection: sqlite3.Connection,
    evidence_id: str,
    *,
    level: str = "evidence",
    cursor: str | None = None,
    max_chars: int = 8000,
    version: int | None = None,
) -> EvidencePage:
    """同快照分页，结构化条目不切断。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        evidence_id: 已保存原文快照的稳定身份。
        level: evidence 展开原文，summary 或 overview 读取概要；与目标材料种类一并核验。 默认
            'evidence'。
        cursor: 宿主签发并保存的不透明续读游标，续读同一快照版本。 None 表示首次读取。
        max_chars: 本页正文的字符上限；分页游标继续绑定原快照。 默认 8000。
        version: 目标记录的确切版本，区别于可变的当前版本。 None 首读当前版本，
            续读仍绑定游标的原版本。

    Returns:
        当前版本的一页正文、确切范围和下一游标；取得页面不等于已呈现给模型。

    Raises:
        EvidenceInputError: 来源、版本、时间范围或材料引用不满足当前操作要求。
        StorageError: 保存的续读资格损坏。
        EvidenceConflict: 原文版本不存在或引用资格已撤销。

    Note:
        摘要和概览页面不生成原文引用资格。
    """
    if level not in ("summary", "overview", "evidence"):
        raise EvidenceInputError("level 只接受 summary、overview 或 evidence。")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise EvidenceInputError("max_chars 必须是正整数。")
    if version is not None and (
        isinstance(version, bool) or not isinstance(version, int) or version < 1
    ):
        raise EvidenceInputError("version 必须是正整数。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        start = 0
        if cursor is not None:
            row = _one(
                connection,
                "SELECT evidence_id,version,start,page_chars,kind FROM knowledge_cursors "
                "WHERE cursor=?",
                (cursor,),
            )
            if row is None or row[0] != evidence_id or level != "evidence":
                raise EvidenceInputError("续读必须对应同一来源、版本及层次。")
            if version is not None and version != row[1]:
                raise EvidenceInputError("续读不能替换原游标的确切版本。")
            if (
                isinstance(row[2], bool)
                or not isinstance(row[2], int)
                or row[2] < 0
                or row[4] != "evidence"
            ):
                raise StorageError("保存的续读资格损坏。", code="invalid_record")
            start, version = row[2], _integer(row[1])
        snapshot = _evidence(connection, evidence_id, version)
        if snapshot is None:
            raise EvidenceConflict("原文版本不存在；旧认识引用不可用。")
        if not snapshot.valid:
            raise EvidenceConflict("原文已撤销。")
        if level != "evidence":
            text = (
                (snapshot.summary if level == "summary" else snapshot.overview)
                or snapshot.items_json
                or snapshot.text
            )
            return EvidencePage(snapshot=snapshot, slice=None, text=text, level=level)
        unit: RangeUnit = "characters"
        if snapshot.items_json is None:
            total = len(snapshot.text)
            end = min(total, start + max_chars)
            text = snapshot.text[start:end]
        else:
            unit = "items"
            items = cast(list[object], json.loads(snapshot.items_json))
            total, end = len(items), start
            parts: list[str] = []
            page_chars = 2
            # 每个条目只编码一次，页长包含括号和分隔符，仍不切断一个对象。
            while end < total:
                encoded = json.dumps(
                    items[end],
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                proposed_chars = page_chars + len(encoded) + int(bool(parts))
                if proposed_chars > max_chars:
                    if end == start:
                        raise EvidenceInputError("单个结构化条目超过页长，原文已保留且未登记交付。")
                    break
                parts.append(encoded)
                page_chars, end = proposed_chars, end + 1
            text = "[" + ",".join(parts) + "]"
        media_ids = tuple(item.media_id for item in snapshot.media) if start == 0 else ()
        fragment = _slice(connection, snapshot, start, end, unit, media_ids)
        next_cursor = (
            _cursor(connection, evidence_id, snapshot.version, end, max_chars, "evidence")
            if end < total
            else None
        )
        return EvidencePage(
            snapshot=snapshot, slice=fragment, text=text, level="evidence", next_cursor=next_cursor
        )


def read_evidence_cursor(
    connection: sqlite3.Connection, cursor: str, *, max_chars: int = 8000
) -> EvidencePage:
    """只按已保存的游标恢复确切来源，Notebook 可以核验返回快照的路径元数据。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        cursor: 宿主签发并保存的不透明续读游标，续读同一快照版本。
        max_chars: 本页正文的字符上限；分页游标继续绑定原快照。 默认 8000。

    Returns:
        当前版本的一页正文、确切范围和下一游标；取得页面不等于已呈现给模型。

    Raises:
        EvidenceInputError: 续读游标不存在。
    """
    row = _one(connection, "SELECT evidence_id FROM knowledge_cursors WHERE cursor=?", (cursor,))
    if row is None:
        raise EvidenceInputError("续读游标不存在。")
    return read_evidence(connection, _text(row[0]), cursor=cursor, max_chars=max_chars)


def _require_slices(
    connection: sqlite3.Connection, references: Sequence[str]
) -> tuple[EvidenceSlice, ...]:
    """在同一提交事务核验确切版本和原文范围，不以缓存资格代替当前来源。"""
    result: list[EvidenceSlice] = []
    for reference in dict.fromkeys(references):
        fragment = get_slice(connection, reference)
        if fragment is None:
            raise EvidenceConflict("引用的原文片段不存在。")
        snapshot = _evidence(connection, fragment.evidence_id, fragment.version)
        if snapshot is None or not snapshot.valid:
            raise EvidenceConflict("引用的来源版本已失效。")
        total = (
            len(snapshot.text)
            if fragment.unit == "characters"
            else len(cast(list[object], json.loads(snapshot.items_json or "[]")))
        )
        if (
            ((snapshot.items_json is None) != (fragment.unit == "characters"))
            or fragment.end > total
            or not set(fragment.media_ids) <= {item.media_id for item in snapshot.media}
        ):
            raise StorageError("保存的来源范围与原文不一致。", code="invalid_record")
        result.append(fragment)
    return tuple(result)


# ---------------------------------------------------------------------------
# 反思代次与实际呈现资格
# ---------------------------------------------------------------------------


def begin_reflection(
    connection: sqlite3.Connection,
    batch_id: str,
    required_slice_refs: tuple[str, ...],
    findings: tuple[OperationalFinding, ...] = (),
    *,
    purpose: Literal["behavior"] = "behavior",
) -> ReflectionBatch:
    """冻结本批范围。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        batch_id: 独立反思批次的身份，须与批次版本配对使用。
        required_slice_refs: 本批必须完成复核的冻结原文范围。
        findings: 本批需要核验的实际异常或运行观察。 默认不附加此类条目。

    Returns:
        冻结的反思范围与批次版本，后续修订须携带同一资格。

    Raises:
        EvidenceConflict: 反思批次身份与既有范围冲突。

    Note:
        中断恢复以新版本取代资格，已完成批次直接返回回执。
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_slices(connection, required_slice_refs)
        row = _one(
            connection,
            "SELECT version,payload_json,state,receipt_json FROM reflection_batches WHERE "
            "batch_id=? ORDER BY version DESC LIMIT 1",
            (batch_id,),
        )
        version = 1
        if row is not None:
            current = _decode(row[1], ReflectionBatch)
            if (
                current.required_slice_refs != required_slice_refs
                or current.findings != findings
                or current.purpose != purpose
            ):
                raise EvidenceConflict("反思批次身份与既有范围冲突。")
            if row[2] != "interrupted":
                return get_reflection(connection, batch_id, _integer(row[0]))
            version = _integer(row[0]) + 1
        batch = ReflectionBatch(
            batch_id=batch_id,
            version=version,
            required_slice_refs=required_slice_refs,
            findings=findings,
            purpose=purpose,
            created_at=time.time(),
        )
        connection.execute(
            "INSERT INTO reflection_batches VALUES(?,?,?,'running',NULL)",
            (batch_id, version, batch.model_dump_json()),
        )
        return batch


def get_reflection(connection: sqlite3.Connection, batch_id: str, version: int) -> ReflectionBatch:
    """读取确切批次代次，普通最终文字不会改变此状态。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        batch_id: 独立反思批次的身份，须与批次版本配对使用。
        version: 当前反思批次的确切版本，旧代次不再拥有写入资格。

    Returns:
        冻结的反思范围与批次版本，后续修订须携带同一资格。

    Raises:
        EvidenceConflict: 反思批次不存在。
        StorageError: 反思批次状态损坏。
    """
    row = _one(
        connection,
        "SELECT payload_json,state,receipt_json FROM reflection_batches WHERE batch_id=? "
        "AND version=?",
        (batch_id, version),
    )
    if row is None:
        raise EvidenceConflict("反思批次不存在。")
    if row[1] not in ("running", "completed", "interrupted"):
        raise StorageError("反思批次状态损坏。", code="invalid_record")
    return _decode(row[0], ReflectionBatch).model_copy(
        update={
            "state": row[1],
            "receipt": None if row[2] is None else _decode(row[2], ReflectionReceipt),
        }
    )


def abandon_reflection(
    connection: sqlite3.Connection, batch_id: str, version: int, reason: str
) -> ReflectionBatch:
    """撤销失败批次的后续写资格，保留呈现、逐项修订及未完成来源。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        batch_id: 独立反思批次的身份，须与批次版本配对使用。
        version: 当前反思批次的确切版本，旧代次不再拥有写入资格。
        reason: 供后续核对的明确处置原因。

    Returns:
        冻结的反思范围与批次版本，后续修订须携带同一资格。

    Raises:
        EvidenceInputError: 中断批次需要明确原因。

    Note:
        首次中断原因保存在该代次 payload。完成事实不可退回；重复中断不覆盖
        原因。下一批是否可开始由主体依据新事实决定，此入口不产生重试或唤醒。
    """
    if not reason.strip():
        raise EvidenceInputError("中断批次需要明确原因。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        batch = get_reflection(connection, batch_id, version)
        if batch.state != "running":
            return batch
        _active_batch(connection, batch_id, version)
        interrupted = batch.model_copy(
            update={"state": "interrupted", "interrupted_reason": reason}
        )
        connection.execute(
            "UPDATE reflection_batches SET state='interrupted',payload_json=? "
            "WHERE batch_id=? AND version=?",
            (interrupted.model_dump_json(), batch_id, version),
        )
        analysis_work.settle(connection, batch_id, failure=reason)
        return interrupted


def _active_batch(connection: sqlite3.Connection, batch_id: str, version: int) -> ReflectionBatch:
    """写入口仅借用当前仍运行的代次，旧批次不能沿用已撤销的写资格。"""
    batch = get_reflection(connection, batch_id, version)
    latest = _one(
        connection, "SELECT MAX(version) FROM reflection_batches WHERE batch_id=?", (batch_id,)
    )
    if batch.state != "running" or latest != (version,):
        raise EvidenceConflict("反思批次已结束或处理代次过期。")
    return batch


def mark_presented(
    connection: sqlite3.Connection, batch_id: str, version: int, slice_refs: tuple[str, ...]
) -> None:
    """为实际请求中仍有效的完整片段登记资格，取得片段不属于成功门槛。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        batch_id: 独立反思批次的身份，须与批次版本配对使用。
        version: 目标记录的确切版本，区别于可变的当前版本。
        slice_refs: 当前材料实际覆盖的原文片段身份。

    Raises:
        EvidenceConflict: 呈现候选的原文片段不存在。
        StorageError: 呈现候选缺少原文版本。

    Note:
        已提交连续历史可以保留后来撤销的来源。它仍是曾经交付的事实，但不再
        取得行为评审引用资格；这一项不能阻断同请求里的有效新来源。缺失关系和有效
        候选的坏范围仍是明确故障，revise/finish 继续核验真正引用的来源和版本。
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if get_reflection(connection, batch_id, version).state == "completed":
            # 完成工具后的普通模型收尾仍使用同一批次；它不再扩展已完成覆盖。
            return
        _active_batch(connection, batch_id, version)
        active: list[str] = []
        for reference in dict.fromkeys(slice_refs):
            fragment = get_slice(connection, reference)
            if fragment is None:
                raise EvidenceConflict("呈现候选的原文片段不存在。")
            snapshot = _evidence(connection, fragment.evidence_id, fragment.version)
            if snapshot is None:
                raise StorageError("呈现候选缺少原文版本。", code="invalid_record")
            if snapshot.valid:
                active.append(reference)
        _require_slices(connection, active)
        connection.executemany(
            "INSERT OR IGNORE INTO reflection_presented VALUES(?,?,?)",
            ((batch_id, version, reference) for reference in active),
        )


def _delivery_fingerprint(encoded: str) -> str:
    """字典顺序不改变 Core 值相等性；索引只缩小候选，完整值仍必须核验。"""
    value: object = json.loads(encoded)
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _delivery_identity(
    batch_id: str, version: int, kind: str, tool_call_id: str | None, fingerprint: str
) -> str:
    return hashlib.sha256(
        encode((batch_id, version, kind, tool_call_id, fingerprint)).encode("utf-8")
    ).hexdigest()


def _delivery_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """媒体恢复会重新编码 JSON，必须提前拒绝会被重新编码抹掉的歧义字段。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StorageError("反思交付 JSON 包含重复字段。", code="invalid_record")
        result[key] = value
    return result


def _delivery(connection: sqlite3.Connection, row: tuple[object, ...]) -> EvidenceDelivery:
    """从持久关系恢复完整值，并核验摘要；来源撤销不删除发生过的交付事实。"""
    identity, batch_id, version = _text(row[0]), _text(row[1]), _integer(row[2])
    raw_kind, tool_call_id = row[3], _optional_text(row[4])
    stored = _text(row[5])
    try:
        json.loads(stored, object_pairs_hook=_delivery_fields)
    except (ValueError, TypeError, RecursionError) as error:
        raise StorageError("反思交付 JSON 损坏。", code="invalid_record") from error
    encoded = restore_core_media(connection, stored)
    material: Message | tuple[ContentBlock, ...]
    kind: Literal["initial", "tool"]
    if raw_kind == "initial" and tool_call_id is None:
        kind = "initial"
        material = decode_message(encoded)
        if material.role is not MessageRole.USER:
            raise StorageError("反思初始交付角色损坏。", code="invalid_record")
    elif raw_kind == "tool" and tool_call_id:
        kind = "tool"
        material = decode_content(encoded)
    else:
        raise StorageError("反思交付类别与调用身份不一致。", code="invalid_record")
    fingerprint = _delivery_fingerprint(encode(material))
    if fingerprint != row[6] or identity != _delivery_identity(
        batch_id, version, kind, tool_call_id, fingerprint
    ):
        raise StorageError("反思交付完整材料与登记摘要不一致。", code="invalid_record")
    ranges: list[tuple[object, ...]] = connection.execute(
        "SELECT slice_ref FROM reflection_delivery_slices WHERE delivery_id=? ORDER BY ordinal",
        (identity,),
    ).fetchall()
    references = tuple(_text(item[0]) for item in ranges)
    if not references or len(references) != len(set(references)):
        raise StorageError("反思交付缺少唯一切片关系。", code="invalid_record")
    return EvidenceDelivery(identity, batch_id, version, kind, tool_call_id, material, references)


def record_delivery(
    connection: sqlite3.Connection,
    batch_id: str,
    version: int,
    *,
    tool_call_id: str | None,
    material: Message | tuple[ContentBlock, ...],
    slice_refs: tuple[str, ...],
) -> EvidenceDelivery:
    """原子保存真实交付及原批次，重复相同登记返回原记录，不提前确认呈现。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        batch_id: 独立反思批次的身份，须与批次版本配对使用。
        version: 目标记录的确切版本，区别于可变的当前版本。
        tool_call_id: Core 分配的工具调用身份，用于匹配完整工具结果。
        material: 真正交付的完整消息或工具内容，供后续请求逐项匹配。
        slice_refs: 当前材料实际覆盖的原文片段身份。

    Returns:
        本次完整材料与精确原文范围的持久交付登记。

    Raises:
        EvidenceInputError: 来源、版本、时间范围或材料引用不满足当前操作要求。
        EvidenceConflict: 相同交付身份已有不同完整材料或切片关系。

    Note:
        本入口由 ReflectionTools 在构造完整初始输入或原文工具结果后调用。
        工具登记只接受成功结果的完整 content，调用 ID 和 read_evidence 名称由
        Core 实际工具上下文提供。媒体缩为同库 BLOB，关系与材料同事务保存。
    """
    if not slice_refs or len(slice_refs) != len(set(slice_refs)):
        raise EvidenceInputError("真实交付需要非空且唯一的切片关系。")
    if isinstance(material, Message):
        if material.role is not MessageRole.USER or tool_call_id is not None:
            raise EvidenceInputError("初始交付需要完整 user Message，不能携带调用身份。")
        kind = "initial"
        encoded = encode(material)
        decode_message(encoded)
    else:
        if not tool_call_id or not tool_call_id.strip():
            raise EvidenceInputError("原文工具交付需要真实调用身份。")
        kind = "tool"
        encoded = encode(material)
        decode_content(encoded)
    fingerprint = _delivery_fingerprint(encoded)
    identity = _delivery_identity(batch_id, version, kind, tool_call_id, fingerprint)

    with connection:
        connection.execute("BEGIN IMMEDIATE")
        _active_batch(connection, batch_id, version)
        _require_slices(connection, slice_refs)
        row = _one(
            connection,
            "SELECT delivery_id,batch_id,version,kind,tool_call_id,payload_json,fingerprint "
            "FROM reflection_deliveries WHERE delivery_id=?",
            (identity,),
        )
        if row is not None:
            existing = _delivery(connection, row)
            if existing.material != material or existing.slice_refs != slice_refs:
                raise EvidenceConflict("相同交付身份已有不同完整材料或切片关系。")
            return existing
        stored, media_ids = intern_core_media(connection, encoded)
        connection.execute(
            "INSERT INTO reflection_deliveries VALUES(?,?,?,?,?,?,?)",
            (identity, batch_id, version, kind, tool_call_id, stored, fingerprint),
        )
        connection.executemany(
            "INSERT INTO reflection_delivery_slices VALUES(?,?,?)",
            ((identity, ordinal, reference) for ordinal, reference in enumerate(slice_refs)),
        )
        connection.executemany(
            "INSERT INTO reflection_delivery_media VALUES(?,?)",
            ((identity, media_id) for media_id in media_ids),
        )
        return EvidenceDelivery(
            identity, batch_id, version, kind, tool_call_id, material, slice_refs
        )


def deliveries_for_history(
    connection: sqlite3.Connection, messages: tuple[Message, ...]
) -> tuple[EvidenceDelivery, ...]:
    """只查真实历史的完整值候选，恢复原始批次和切片，不赋予本批写资格。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        messages: 按实际先后排列的完整 Core 消息，不从正文猜测交付身份。

    Returns:
        条目集合：本次完整材料与精确原文范围的持久交付登记。

    Note:
        初始 Message 的全部字段必须一致；工具历史需要 read_evidence 名称、相同
        tool_call_id、成功标记以及原结果的全部有序内容。摘要、局部媒体、伪造身份
        字符串不能匹配。旧批次是否结束与历史事实能否再次呈现是独立判断。
    """
    result: dict[str, EvidenceDelivery] = {}
    candidates: list[tuple[str, str | None, Message | tuple[ContentBlock, ...]]] = []
    for message in messages:
        if message.role is MessageRole.USER:
            candidates.append(("initial", None, message))
        elif message.role is MessageRole.TOOL and message.name == "read_evidence":
            candidates.extend(
                ("tool", block.tool_call_id, block.content)
                for block in message.content
                if isinstance(block, ToolResultBlock) and not block.is_error
            )
    for kind, tool_call_id, material in candidates:
        rows: list[tuple[object, ...]] = connection.execute(
            "SELECT delivery_id,batch_id,version,kind,tool_call_id,payload_json,fingerprint "
            "FROM reflection_deliveries WHERE kind=? AND tool_call_id IS ? AND fingerprint=?",
            (kind, tool_call_id, _delivery_fingerprint(encode(material))),
        ).fetchall()
        for row in rows:
            delivery = _delivery(connection, row)
            if delivery.material == material:
                result.setdefault(delivery.delivery_id, delivery)
    return tuple(result.values())


def _covered(required: EvidenceSlice, slices: Sequence[EvidenceSlice]) -> bool:
    """按同源同版本范围并集检查，第一页或一个局部来源不冒充整份覆盖。"""
    matching = [
        item
        for item in slices
        if (item.evidence_id, item.version, item.unit)
        == (required.evidence_id, required.version, required.unit)
    ]
    media = {identity for item in matching for identity in item.media_ids}
    if not set(required.media_ids) <= media:
        return False
    if required.start == required.end:
        return any(item.start <= required.start <= item.end for item in matching)
    frontier = required.start
    for item in sorted(matching, key=lambda item: (item.start, item.end)):
        if item.start > frontier:
            break
        frontier = max(frontier, item.end)
        if frontier >= required.end:
            return True
    return False


def _presented(
    connection: sqlite3.Connection, batch_id: str, version: int, references: Sequence[str]
) -> tuple[EvidenceSlice, ...]:
    """核对本批已实际呈现的原文范围，不从片段存在推断已读。"""
    wanted = _require_slices(connection, references)
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT slice_ref FROM reflection_presented WHERE batch_id=? AND version=?",
        (batch_id, version),
    ).fetchall()
    seen: list[EvidenceSlice] = []
    for row in rows:
        fragment = get_slice(connection, _text(row[0]))
        if fragment is None:
            raise StorageError("呈现记录缺少原文片段。", code="invalid_record")
        snapshot = _evidence(connection, fragment.evidence_id, fragment.version)
        if snapshot is None:
            raise StorageError("呈现记录缺少原文版本。", code="invalid_record")
        if snapshot.valid:
            seen.append(fragment)
    if any(not _covered(item, seen) for item in wanted):
        raise EvidenceConflict("原文范围尚未实际呈现给本批模型。")
    return wanted


# ---------------------------------------------------------------------------
# 原文引用资格撤销
# ---------------------------------------------------------------------------


def revoke_evidence(connection: sqlite3.Connection, evidence_id: str, version: int) -> None:
    """立即撤销该来源资格，保留原文、片段和已有引用以供审计。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        evidence_id: 已保存原文快照的稳定身份。
        version: 目标记录的确切版本，区别于可变的当前版本。

    Raises:
        EvidenceConflict: 撤销的原文版本不存在。
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if _evidence(connection, evidence_id, version) is None:
            raise EvidenceConflict("撤销的原文版本不存在。")
        connection.execute(
            "UPDATE knowledge_evidence SET valid=0 WHERE evidence_id=? AND version=?",
            (evidence_id, version),
        )


# ---------------------------------------------------------------------------
# 完成回执与逐范围邮箱确认
# ---------------------------------------------------------------------------


def _overlap(left: EvidenceSlice, right: EvidenceSlice) -> bool:
    """文本半开区间与实际媒体共同决定范围冲突，不按引用字符串判断。"""
    if (left.evidence_id, left.version, left.unit) != (
        right.evidence_id,
        right.version,
        right.unit,
    ):
        return False
    if set(left.media_ids) & set(right.media_ids):
        return True
    if left.start == left.end == right.start == right.end:
        return True
    return max(left.start, right.start) < min(left.end, right.end)


def finish_reflection(
    connection: sqlite3.Connection, batch_id: str, version: int, submission: FinishSubmission
) -> ReflectionReceipt:
    """核验实际呈现、范围覆盖及真实修订后，原子结束批次并确认对应邮箱。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        batch_id: 独立反思批次的身份，须与批次版本配对使用。
        version: 当前反思批次的确切版本，旧代次不再拥有写入资格。
        submission: 本批已复核与明确延期的范围及完成结论。

    Returns:
        实际已提交的结案回执，重复完成复用原结果。

    Raises:
        EvidenceInputError: 来源、版本、时间范围或材料引用不满足当前操作要求。
        StorageError: 批次来源缺失。
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        batch = get_reflection(connection, batch_id, version)
        if batch.receipt is not None:
            return batch.receipt
        _active_batch(connection, batch_id, version)
        required = _require_slices(connection, batch.required_slice_refs)
        reviewed = _presented(
            connection,
            batch_id,
            version,
            tuple(ref for review in submission.reviews for ref in review.evidence),
        )
        deferred = _require_slices(
            connection, tuple(ref for item in submission.deferred for ref in item.evidence)
        )
        # 尚未读的材料可以明确暂缓，不能据此引用具体内容或提出有据监管。
        if any(_overlap(left, right) for left in reviewed for right in deferred):
            raise EvidenceInputError("同一范围不能同时复核和暂缓。")
        if any(not _covered(fragment, (*reviewed, *deferred)) for fragment in required):
            raise EvidenceInputError("本批每个分配范围必须复核或明确暂缓。")
        expected = {finding.id for finding in batch.findings}
        if {finding.id for finding in submission.findings} != expected or len(
            submission.findings
        ) != len(expected):
            raise EvidenceInputError("本批程序异常必须逐项判断。")
        if submission.behavior is None:
            if submission.reviews or not submission.deferred:
                raise EvidenceInputError("行为评审必须提交明确结论或明确暂缓全部范围。")
        else:
            _presented(connection, batch_id, version, submission.behavior.evidence)
            for observation in submission.behavior.observations:
                _presented(connection, batch_id, version, observation.evidence)
            behavior_review.finish(connection, batch, submission.behavior)
        receipt = ReflectionReceipt(
            batch_id=batch_id,
            version=version,
            completed_at=time.time(),
            submission=submission,
            coverage=tuple(
                dict.fromkeys(
                    (
                        *batch.required_slice_refs,
                        *(item.slice_ref for item in reviewed),
                        *(item.slice_ref for item in deferred),
                    )
                )
            ),
        )
        connection.execute(
            "UPDATE reflection_batches SET state='completed',receipt_json=? WHERE "
            "batch_id=? AND version=?",
            (receipt.model_dump_json(), batch_id, version),
        )
        for review in submission.reviews:
            connection.executemany(
                # 一个范围可承载多个一致取舍的问题；完整结论保存在回执，覆盖关系只登记一次。
                "INSERT OR IGNORE INTO knowledge_review_ranges VALUES(?,?,?,'reviewed',?)",
                (
                    (reference, batch_id, version, review.reason)
                    for reference in dict.fromkeys(review.evidence)
                ),
            )
        for item in submission.deferred:
            connection.executemany(
                "INSERT OR IGNORE INTO knowledge_review_ranges VALUES(?,?,?,'deferred',?)",
                (
                    (reference, batch_id, version, item.reason)
                    for reference in dict.fromkeys(item.evidence)
                ),
            )
        analysis_work.settle(connection, batch_id)
        return receipt


# ---------------------------------------------------------------------------
# 监管交付与前台计划
# ---------------------------------------------------------------------------


def _notice_active(connection: sqlite3.Connection, notice: SupervisionNotice) -> bool:
    """交付前复核来源和活动版本；业务撤销令意见失效，内部错误继续传播。"""
    if notice.expires_at is not None and notice.expires_at <= time.time():
        return False
    observation = connection.execute(
        "SELECT opportunities FROM supervision_outcomes WHERE notice_id=?", (notice.id,)
    ).fetchone()
    if observation is not None and observation[0] >= 20:
        return False
    if notice.activity_id is not None:
        row = _one(
            connection, "SELECT version FROM activities WHERE activity_id=?", (notice.activity_id,)
        )
        if row != (notice.activity_version,):
            return False
    try:
        _require_slices(connection, notice.evidence)
    except EvidenceConflict:
        return False
    return True


def supervise(
    connection: sqlite3.Connection, batch_id: str, version: int, notice: SupervisionNotice
) -> SupervisionRecord:
    """立即提交有据监管意见，完成批次失败不会撤销已经提交的意见。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        batch_id: 独立反思批次的身份，须与批次版本配对使用。
        version: 目标记录的确切版本，区别于可变的当前版本。
        notice: 有原文依据的监管意见，紧急意见仍须显式交接前台。

    Returns:
        包含原始依据、活动版本约束和当前回应的监管记录。

    Raises:
        EvidenceConflict: 监管来源、期限或活动版本已失效；监管身份已有不同意见。
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        batch = _active_batch(connection, batch_id, version)
        fragments = _presented(connection, batch_id, version, notice.evidence)
        if batch.purpose == "behavior":
            notice = behavior_review.normalize_notice(batch, notice)
            if notice.generalized and len({fragment.evidence_id for fragment in fragments}) < 2:
                raise EvidenceInputError("泛化纠偏至少需要两项独立来源。")
            same = connection.execute(
                "SELECT n.id FROM supervision_outcomes o JOIN "
                "knowledge_supervision n ON n.id=o.notice_id WHERE o.dedup_key=? "
                "AND json_extract(n.payload_json,'$.expires_at')>? AND "
                "o.opportunities<20 ORDER BY n.created_at LIMIT 1",
                (behavior_review.notice_key(notice), time.time()),
            ).fetchone()
            if same is not None:
                existing = get_supervision(connection, same[0])
                assert existing is not None
                connection.execute(
                    "INSERT OR IGNORE INTO behavior_suggestions VALUES(?,?)",
                    (batch_id, existing.notice.id),
                )
                return existing.model_copy(update={"duplicate": True})
        if not _notice_active(connection, notice):
            raise EvidenceConflict("监管来源、期限或活动版本已失效。")
        row = _one(
            connection,
            "SELECT payload_json,state,response,created_at FROM knowledge_supervision WHERE id=?",
            (notice.id,),
        )
        if row is not None:
            if _decode(row[0], SupervisionNotice) != notice:
                raise EvidenceConflict("监管身份已有不同意见。")
            return SupervisionRecord(
                notice=notice,
                state=_notice_state(row[1]),
                response=_optional_text(row[2]),
                created_at=_number(row[3]),
                active=row[1] == "open",
                duplicate=True,
            )
        now = time.time()
        connection.execute(
            "INSERT INTO knowledge_supervision VALUES(?,?,'open',NULL,?)",
            (notice.id, notice.model_dump_json(), now),
        )
        if batch.purpose == "behavior":
            connection.execute(
                "INSERT INTO supervision_outcomes(notice_id,batch_id,dedup_key) VALUES(?,?,?)",
                (notice.id, batch_id, behavior_review.notice_key(notice)),
            )
        if batch.purpose == "behavior":
            connection.execute(
                "INSERT OR IGNORE INTO behavior_suggestions VALUES(?,?)", (batch_id, notice.id)
            )
        return SupervisionRecord(notice=notice, state="open", created_at=now, active=True)


def get_supervision(connection: sqlite3.Connection, identity: str) -> SupervisionRecord | None:
    """读取确切意见并复核当前资格，交接事务不能借用先前扫描的有效性。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        identity: 待查询或回应的监管意见身份。

    Returns:
        包含原始依据、活动版本约束和当前回应的监管记录。 未取得对应记录或素材时返回 None；
        读取故障仍抛出。

    Note:
        查询不改回应或删除历史；active 表达当前未回应、来源仍有效、期限及活动
        版本匹配。调用方负责自己事务中的交接，失效意见仍可查到原记录。
    """
    row = _one(
        connection,
        "SELECT payload_json,state,response,created_at FROM knowledge_supervision WHERE id=?",
        (identity,),
    )
    if row is None:
        return None
    notice = _decode(row[0], SupervisionNotice)
    state = _notice_state(row[1])
    return SupervisionRecord(
        notice=notice,
        state=state,
        response=_optional_text(row[2]),
        created_at=_number(row[3]),
        active=state == "open" and _notice_active(connection, notice),
    )


def pending_supervision(
    connection: sqlite3.Connection, *, limit: int = 32
) -> tuple[SupervisionRecord, ...]:
    """交付前再次核验期限、原文及可选活动版本，不交付已失效旧意见。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        limit: 本次读取或更新的最大条目数。 默认 32。

    Returns:
        条目集合：包含原始依据、活动版本约束和当前回应的监管记录。
    """
    _page_limit(limit)
    cursor = connection.execute(
        "SELECT payload_json,response,created_at FROM knowledge_supervision WHERE "
        "state='open' ORDER BY created_at,id"
    )
    result: list[SupervisionRecord] = []
    # 来源失效不消耗交付额度；分块扫描保留历史而不把整个邮箱载入内存。
    while rows := cursor.fetchmany(32):
        for row in rows:
            notice = _decode(row[0], SupervisionNotice)
            if _notice_active(connection, notice):
                result.append(
                    SupervisionRecord(
                        notice=notice,
                        state="open",
                        response=_optional_text(row[1]),
                        created_at=_number(row[2]),
                        active=True,
                    )
                )
                if len(result) == limit:
                    return tuple(result)
    return tuple(result)


def respond_supervision(
    connection: sqlite3.Connection, identity: str, decision: str, reason: str
) -> SupervisionRecord:
    """前台明确回应监管，回应不能被用作反思结论的独立证明。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        identity: 待查询或回应的监管意见身份。
        decision: accept、partial 或 reject，分别表示接受、部分接受或拒绝。
        reason: 供后续核对的明确处置原因。

    Returns:
        包含原始依据、活动版本约束和当前回应的监管记录。

    Raises:
        EvidenceInputError: 监管回应需 accept/partial/reject 与明确理由。
        EvidenceConflict: 批次资格或原文来源状态已变化，不能采用本次提交。
    """
    if decision not in ("accept", "partial", "reject") or not reason.strip():
        raise EvidenceInputError("监管回应需 accept/partial/reject 与明确理由。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _one(
            connection,
            "SELECT payload_json,state,response,created_at FROM knowledge_supervision WHERE id=?",
            (identity,),
        )
        if row is None:
            raise EvidenceConflict("监管意见不存在。")
        notice = _decode(row[0], SupervisionNotice)
        if row[1] != "open":
            if (row[1], row[2]) != (decision, reason):
                raise EvidenceConflict("监管意见已经回应。")
        else:
            if not _notice_active(connection, notice):
                raise EvidenceConflict("监管意见已失效。")
            connection.execute(
                "UPDATE knowledge_supervision SET state=?,response=? WHERE id=?",
                (decision, reason, identity),
            )
        return SupervisionRecord(
            notice=notice, state=decision, response=reason, created_at=_number(row[3]), active=False
        )


def _require_plan_evidence(connection: sqlite3.Connection, identity: str) -> None:
    """前台原始来源可直接引用；片段身份仍按原文版本核验，不要求反思已读登记。"""
    snapshot = _evidence(connection, identity, None)
    if snapshot is not None:
        if not snapshot.valid:
            raise EvidenceConflict("计划来源已失效。")
        return
    _require_slices(connection, (identity,))


def plan_wakeup(connection: sqlite3.Connection, plan: WakeupPlan) -> WakeupPlan:
    """持久保存未来唤起，证据与 deadline 不从注意 defer 的默认值推导。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        plan: 带来源与到期时刻的唤醒计划，不直接执行外部行动。

    Returns:
        已保存的计划及其当前结论，不直接代表外部行动完成。

    Raises:
        EvidenceInputError: 计划需要未来期限与未完成状态。
        EvidenceConflict: 计划身份已有不同内容。
    """
    if plan.due <= time.time() or plan.state != "pending":
        raise EvidenceInputError("计划需要未来期限与未完成状态。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_plan_evidence(connection, plan.evidence)
        row = _one(connection, "SELECT payload_json FROM knowledge_plans WHERE id=?", (plan.id,))
        if row is not None:
            existing = _decode(row[0], WakeupPlan)
            if existing != plan:
                raise EvidenceConflict("计划身份已有不同内容。")
            return existing
        connection.execute(
            "INSERT INTO knowledge_plans VALUES(?,?)", (plan.id, plan.model_dump_json())
        )
        return plan


def resolve_plan(
    connection: sqlite3.Connection, identity: str, outcome: str, evidence: str
) -> WakeupPlan:
    """以真实证据结束计划，文字声称完成不能替代来源存在性。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        identity: 需要明确结案的未完成计划身份。
        outcome: 本次计划的明确结案结论。
        evidence: 结束计划或确认事实所依据的实际证据文本。

    Returns:
        已保存的计划及其当前结论，不直接代表外部行动完成。

    Raises:
        EvidenceInputError: 计划完成需要实际结果说明。
        EvidenceConflict: 计划不存在；计划已有不同完成事实。
    """
    if not outcome.strip():
        raise EvidenceInputError("计划完成需要实际结果说明。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_plan_evidence(connection, evidence)
        row = _one(connection, "SELECT payload_json FROM knowledge_plans WHERE id=?", (identity,))
        if row is None:
            raise EvidenceConflict("计划不存在。")
        plan = _decode(row[0], WakeupPlan)
        if plan.state == "completed":
            if (plan.outcome, plan.result_evidence) != (outcome, evidence):
                raise EvidenceConflict("计划已有不同完成事实。")
            return plan
        completed = plan.model_copy(
            update={"state": "completed", "outcome": outcome, "result_evidence": evidence}
        )
        connection.execute(
            "UPDATE knowledge_plans SET payload_json=? WHERE id=?",
            (completed.model_dump_json(), identity),
        )
        return completed


def pending_plans(
    connection: sqlite3.Connection, *, due_before: float | None = None, limit: int = 32
) -> tuple[WakeupPlan, ...]:
    """读取有限未完成计划，取出到期计划不自动执行或重复确认。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        due_before: 仅返回此 Unix 时刻前到期的计划；None 不限制到期时间。
        limit: 本次读取或更新的最大条目数。 默认 32。

    Returns:
        条目集合：已保存的计划及其当前结论，不直接代表外部行动完成。

    Raises:
        ValueError: 期限必须是有限时间。
    """
    _page_limit(limit)
    if due_before is not None and not math.isfinite(due_before):
        raise ValueError("期限必须是有限时间。")
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT payload_json FROM knowledge_plans WHERE "
        "json_extract(payload_json,'$.state')='pending' "
        + ("" if due_before is None else "AND json_extract(payload_json,'$.due')<=? ")
        + "ORDER BY json_extract(payload_json,'$.due'),id LIMIT ?",
        (limit,) if due_before is None else (due_before, limit),
    ).fetchall()
    return tuple(_decode(row[0], WakeupPlan) for row in rows)


def initialize(connection: sqlite3.Connection) -> None:
    """借用启动事务核验共享证据表并撤旧批次资格，不自行提交事务。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
    """
    for table in (
        "knowledge_evidence",
        "evidence_media",
        "knowledge_slices",
        "reflection_batches",
        "reflection_presented",
        "knowledge_cursors",
        "knowledge_supervision",
        "knowledge_plans",
        "knowledge_review_ranges",
    ):
        connection.execute(f"SELECT * FROM {table} LIMIT 0")
    for columns, table in (
        (
            "delivery_id,batch_id,version,kind,tool_call_id,payload_json,fingerprint",
            "reflection_deliveries",
        ),
        ("delivery_id,ordinal,slice_ref", "reflection_delivery_slices"),
        ("delivery_id,media_id", "reflection_delivery_media"),
    ):
        connection.execute(f"SELECT {columns} FROM {table} LIMIT 0")
    # 启动事务由 Store 持有；恢复失败不能单独提交任何子系统。
    connection.execute(
        "UPDATE reflection_batches SET state='interrupted',payload_json="
        "json_set(payload_json,'$.interrupted_reason','进程恢复撤销旧代次。') "
        "WHERE state='running'"
    )


def record_document(connection: sqlite3.Connection, snapshot: EvidenceSnapshot) -> EvidenceSnapshot:
    """保存同路径文档新版本，重复读取返回原快照；历史版本不因编辑而撤销。

    Args:
        connection: Store 唯一线程中的连接。
        snapshot: 调用者稳定读取的完整文档，版本由此事务分配。

    Returns:
        与实际内容对应的不可变快照。
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        latest = _evidence(connection, snapshot.evidence_id, None)
        if latest is not None and all(
            getattr(latest, key) == getattr(snapshot, key)
            for key in ("kind", "text", "source_json", "summary", "overview")
        ):
            return latest
        version = 1 if latest is None else latest.version + 1
        return put_evidence_in_transaction(
            connection, snapshot.model_copy(update={"version": version}), needs_reflection=False
        )


def read_slice(connection: sqlite3.Connection, reference: str) -> EvidencePage:
    """按已保存范围读取原文，不扩大范围或将外界字符串当作阅读证明。"""
    fragment = get_slice(connection, reference)
    if fragment is None:
        raise EvidenceInputError("引用的原文片段不存在。")
    snapshot = get_evidence(connection, fragment.evidence_id, version=fragment.version)
    if snapshot is None:
        raise EvidenceConflict("引用来源缺失或已撤销。")
    text = snapshot.text[fragment.start : fragment.end]
    if fragment.unit == "items":
        text = json.dumps(
            json.loads(snapshot.items_json or "[]")[fragment.start : fragment.end],
            ensure_ascii=False,
            allow_nan=False,
        )
    return EvidencePage(snapshot=snapshot, slice=fragment, text=text, level="evidence")


def pending_review(
    connection: sqlite3.Connection, *, limit: int = 32
) -> tuple[PendingEvidence, ...]:
    """读取尚未结案的行为复核来源；不建立任何独立记忆队列。"""
    _page_limit(limit)
    return tuple(
        PendingEvidence(evidence_id=row[0], version=row[1], queued_at=row[2])
        for row in connection.execute(
            "SELECT evidence_id,version,queued_at FROM analysis_work "
            "WHERE purpose='behavior' AND state<>'comple"
            "ted' ORDER BY queued_at,evidence_id LIMIT ?",
            (limit,),
        )
    )
