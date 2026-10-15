"""在 Store 的唯一连接中保存原文、认识版本与独立反思提交。

原文快照和片段身份不可覆盖；创建片段与模型实际呈现分开记录。认识修订
核验当前批次、来源覆盖和 base_version，提交即生效，索引更新不决定有效性。
finish_reflection 原子确认范围、真实修订及邮箱；普通模型文字没有确认作用。
本模块只提供不可变值、SCHEMA 和短事务，不打开连接或调用模型。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from collections.abc import Sequence
from typing import Literal, cast

from purrcept_core.models import ContentBlock, Message, MessageRole, ToolResultBlock
from pydantic import BaseModel, ValidationError

from purrcept_engine.domain.context.checkpoints import decode_content, decode_message
from purrcept_engine.domain.knowledge.models import (
    EvidenceDelivery,
    EvidencePage,
    EvidenceSlice,
    EvidenceSnapshot,
    FinishSubmission,
    IndexJob,
    KnowledgeCandidate,
    KnowledgeConflict,
    KnowledgeInputError,
    KnowledgeReference,
    MemoryConflict,
    MemoryPatch,
    MemoryRecord,
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
    "CREATE TABLE pending_knowledge (evidence_id TEXT NOT NULL, version INTEGER NOT NULL, "
    "queued_at REAL NOT NULL, state TEXT NOT NULL CHECK(state "
    "IN('pending','reviewed','deferred')), "
    "reason TEXT, PRIMARY KEY(evidence_id,version))",
    "CREATE TABLE memories (memory_id TEXT NOT NULL, version INTEGER NOT NULL, payload_json "
    "TEXT NOT NULL, learned_at REAL NOT NULL, retired TEXT, PRIMARY KEY(memory_id,version))",
    "CREATE TABLE memory_sources (memory_id TEXT NOT NULL, version INTEGER NOT NULL, slice_ref "
    "TEXT NOT NULL REFERENCES knowledge_slices(slice_ref), PRIMARY "
    "KEY(memory_id,version,slice_ref), "
    "FOREIGN KEY(memory_id,version) REFERENCES memories(memory_id,version))",
    "CREATE TABLE memory_conflicts (left_id TEXT NOT NULL, left_version INTEGER NOT NULL, "
    "right_id TEXT NOT NULL, right_version INTEGER NOT NULL, "
    "PRIMARY KEY(left_id,left_version,right_id,right_version))",
    "CREATE TABLE knowledge_index (kind TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT "
    "NULL, terms TEXT NOT NULL, vector_json TEXT, model TEXT, PRIMARY KEY(kind,id,version))",
    "CREATE TABLE pending_knowledge_index (kind TEXT NOT NULL, id TEXT NOT NULL, version INTEGER "
    "NOT NULL, queued_at REAL NOT NULL, route TEXT NOT NULL, PRIMARY KEY(kind,id,version,route))",
    "CREATE VIRTUAL TABLE knowledge_search USING fts5(kind UNINDEXED,id UNINDEXED,version "
    "UNINDEXED,terms)",
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
    "CREATE TABLE reflection_writes (batch_id TEXT NOT NULL, batch_version INTEGER NOT NULL, "
    "memory_id TEXT NOT NULL, memory_version INTEGER NOT NULL, "
    "PRIMARY KEY(batch_id,batch_version,memory_id,memory_version))",
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
)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise StorageError("认识账本字符串字段损坏。", code="invalid_record")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StorageError("认识账本版本字段损坏。", code="invalid_record")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise StorageError("认识账本时间字段损坏。", code="invalid_record")
    return float(value)


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _page_limit(value: int) -> None:
    """SQLite 负 LIMIT 表示无限；所有公开分页入口必须先拒绝该形状。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise KnowledgeInputError("limit 必须是正整数。")


def _kind(value: object) -> Literal["evidence", "memory"]:
    if value not in ("evidence", "memory"):
        raise StorageError("认识索引类型损坏。", code="invalid_record")
    return value


def _notice_state(value: object) -> Literal["open", "accept", "partial", "reject"]:
    if value not in ("open", "accept", "partial", "reject"):
        raise StorageError("监管意见状态损坏。", code="invalid_record")
    return value


def _queue_index(
    connection: sqlite3.Connection, kind: str, identity: str, version: int, now: float
) -> None:
    """两条派生路线独立排队，关键词提交不能吞掉尚未计算的语义更新。"""
    connection.executemany(
        "INSERT INTO pending_knowledge_index VALUES(?,?,?,?,?)",
        ((kind, identity, version, now, route) for route in ("keyword", "semantic")),
    )


def _decode[Record: BaseModel](value: object, model: type[Record]) -> Record:
    """持久化读取是独立边界，坏记录不能降级成空检索结果。"""
    try:
        return model.model_validate_json(_text(value))
    except ValidationError as error:
        raise StorageError("认识账本记录形状损坏。", code="invalid_record") from error


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
        KnowledgeConflict: 原文身份与认识身份冲突；原文版本已有不同内容。
        StorageError: 原文引用的权威媒体缺失；原文引用的权威媒体摘要或长度不匹配。

    Note:
        输入和轮次事实由 Store 先保存同库媒体，再在同一事务调用此入口；任一步
        失败由外层回滚全部接管。独立调用使用 put_evidence，不能依赖提交后通知。
    """
    if not connection.in_transaction:
        raise RuntimeError("原文接管必须处于调用方拥有的事务中。")
    if _one(connection, "SELECT 1 FROM memories WHERE memory_id=?", (snapshot.evidence_id,)):
        raise KnowledgeConflict("原文身份与认识身份冲突。")
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
            raise KnowledgeConflict("原文版本已有不同内容。")
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
    _queue_index(connection, "evidence", snapshot.evidence_id, snapshot.version, now)
    if needs_reflection and snapshot.valid:
        connection.execute(
            "INSERT INTO pending_knowledge VALUES(?,?,?,'pending',NULL)",
            (snapshot.evidence_id, snapshot.version, now),
        )
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
        KnowledgeInputError: 来源、版本、时间范围或材料引用不满足当前操作要求。
        StorageError: 保存的续读资格损坏。
        KnowledgeConflict: 原文或认识版本不存在；原文已撤销。

    Note:
        认识/摘要页面不生成原文引用资格。
    """
    if level not in ("summary", "overview", "evidence"):
        raise KnowledgeInputError("level 只接受 summary、overview 或 evidence。")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise KnowledgeInputError("max_chars 必须是正整数。")
    if version is not None and (
        isinstance(version, bool) or not isinstance(version, int) or version < 1
    ):
        raise KnowledgeInputError("version 必须是正整数。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        start, kind = 0, None
        if cursor is not None:
            row = _one(
                connection,
                "SELECT evidence_id,version,start,page_chars,kind FROM knowledge_cursors "
                "WHERE cursor=?",
                (cursor,),
            )
            if row is None or row[0] != evidence_id or level != "evidence":
                raise KnowledgeInputError("续读必须对应同一来源、版本及层次。")
            if version is not None and version != row[1]:
                raise KnowledgeInputError("续读不能替换原游标的确切版本。")
            if (
                isinstance(row[2], bool)
                or not isinstance(row[2], int)
                or row[2] < 0
                or row[4] not in ("evidence", "memory")
            ):
                raise StorageError("保存的续读资格损坏。", code="invalid_record")
            start, version, kind = row[2], _integer(row[1]), row[4]
        snapshot = None if kind == "memory" else _evidence(connection, evidence_id, version)
        if snapshot is None:
            memory = _memory(connection, evidence_id, version, time.time())
            if memory is None:
                raise KnowledgeConflict("原文或认识版本不存在。")
            content = memory.patch.content
            if level != "evidence":
                text = (
                    memory.patch.summary if level == "summary" else memory.patch.overview
                ) or content
                return EvidencePage(memory=memory, slice=None, text=text, level=level)
            end = min(len(content), start + max_chars)
            next_cursor = (
                _cursor(connection, evidence_id, memory.version, end, max_chars, "memory")
                if end < len(content)
                else None
            )
            return EvidencePage(
                memory=memory,
                slice=None,
                text=content[start:end],
                level="evidence",
                next_cursor=next_cursor,
            )
        if not snapshot.valid:
            raise KnowledgeConflict("原文已撤销。")
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
                        raise KnowledgeInputError(
                            "单个结构化条目超过页长，原文已保留且未登记交付。"
                        )
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
        KnowledgeInputError: 续读游标不存在。
    """
    row = _one(connection, "SELECT evidence_id FROM knowledge_cursors WHERE cursor=?", (cursor,))
    if row is None:
        raise KnowledgeInputError("续读游标不存在。")
    return read_evidence(connection, _text(row[0]), cursor=cursor, max_chars=max_chars)


def _require_slices(
    connection: sqlite3.Connection, references: Sequence[str]
) -> tuple[EvidenceSlice, ...]:
    """在同一提交事务核验确切版本和原文范围，不以缓存资格代替当前来源。"""
    result: list[EvidenceSlice] = []
    for reference in dict.fromkeys(references):
        fragment = get_slice(connection, reference)
        if fragment is None:
            raise KnowledgeConflict("引用的原文片段不存在。")
        snapshot = _evidence(connection, fragment.evidence_id, fragment.version)
        if snapshot is None or not snapshot.valid:
            raise KnowledgeConflict("引用的来源版本已失效。")
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


def pending_reflection(
    connection: sqlite3.Connection, *, limit: int = 32
) -> tuple[PendingEvidence, ...]:
    """读取有限可靠来源及已有范围结论，局部一页不冒充整份复核。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        limit: 本次读取或更新的最大条目数。 默认 32。

    Returns:
        本次有限读取的可靠待反思来源及已有范围结论。
    """
    _page_limit(limit)
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT p.evidence_id,p.version,p.queued_at FROM pending_knowledge p JOIN "
        "knowledge_evidence e "
        "ON p.evidence_id=e.evidence_id AND p.version=e.version WHERE p.state='pending' "
        "AND e.valid=1 "
        "ORDER BY p.queued_at,p.evidence_id LIMIT ?",
        (limit,),
    ).fetchall()
    result: list[PendingEvidence] = []
    for row in rows:
        ranges: list[tuple[object, ...]] = connection.execute(
            "SELECT r.slice_ref,r.state FROM knowledge_review_ranges r JOIN "
            "knowledge_slices s ON r.slice_ref=s.slice_ref "
            "JOIN reflection_batches b ON r.batch_id=b.batch_id AND r.batch_version=b.version "
            "WHERE s.evidence_id=? AND s.version=? ORDER BY "
            "json_extract(b.receipt_json,'$.completed_at')",
            (row[0], row[1]),
        ).fetchall()
        effective = {_text(item[0]): _text(item[1]) for item in ranges}
        result.append(
            PendingEvidence(
                evidence_id=_text(row[0]),
                version=_integer(row[1]),
                queued_at=_number(row[2]),
                reviewed_slice_refs=tuple(
                    reference for reference, state in effective.items() if state == "reviewed"
                ),
                deferred_slice_refs=tuple(
                    reference for reference, state in effective.items() if state == "deferred"
                ),
            )
        )
    return tuple(result)


# ---------------------------------------------------------------------------
# 反思代次与实际呈现资格
# ---------------------------------------------------------------------------


def begin_reflection(
    connection: sqlite3.Connection,
    batch_id: str,
    required_slice_refs: tuple[str, ...],
    findings: tuple[OperationalFinding, ...] = (),
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
        KnowledgeConflict: 反思批次身份与既有范围冲突。

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
            if current.required_slice_refs != required_slice_refs or current.findings != findings:
                raise KnowledgeConflict("反思批次身份与既有范围冲突。")
            if row[2] != "interrupted":
                return get_reflection(connection, batch_id, _integer(row[0]))
            version = _integer(row[0]) + 1
        batch = ReflectionBatch(
            batch_id=batch_id,
            version=version,
            required_slice_refs=required_slice_refs,
            findings=findings,
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
        KnowledgeConflict: 反思批次不存在。
        StorageError: 反思批次状态损坏。
    """
    row = _one(
        connection,
        "SELECT payload_json,state,receipt_json FROM reflection_batches WHERE batch_id=? "
        "AND version=?",
        (batch_id, version),
    )
    if row is None:
        raise KnowledgeConflict("反思批次不存在。")
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
        KnowledgeInputError: 中断批次需要明确原因。

    Note:
        首次中断原因保存在该代次 payload。完成事实不可退回；重复中断不覆盖
        原因。下一批是否可开始由主体依据新事实决定，此入口不产生重试或唤醒。
    """
    if not reason.strip():
        raise KnowledgeInputError("中断批次需要明确原因。")
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
        return interrupted


def _active_batch(connection: sqlite3.Connection, batch_id: str, version: int) -> ReflectionBatch:
    """写入口仅借用当前仍运行的代次，旧批次不能沿用已撤销的写资格。"""
    batch = get_reflection(connection, batch_id, version)
    latest = _one(
        connection, "SELECT MAX(version) FROM reflection_batches WHERE batch_id=?", (batch_id,)
    )
    if batch.state != "running" or latest != (version,):
        raise KnowledgeConflict("反思批次已结束或处理代次过期。")
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
        KnowledgeConflict: 呈现候选的原文片段不存在。
        StorageError: 呈现候选缺少原文版本。

    Note:
        已提交连续历史可以保留后来撤销的来源。它仍是曾经交付的事实，但不再
        取得认识引用资格；这一项不能阻断同请求里的有效新来源。缺失关系和有效
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
                raise KnowledgeConflict("呈现候选的原文片段不存在。")
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
        KnowledgeInputError: 来源、版本、时间范围或材料引用不满足当前操作要求。
        KnowledgeConflict: 相同交付身份已有不同完整材料或切片关系。

    Note:
        本入口由 ReflectionTools 在构造完整初始输入或原文工具结果后调用。
        工具登记只接受成功结果的完整 content，调用 ID 和 read_evidence 名称由
        Core 实际工具上下文提供。媒体缩为同库 BLOB，关系与材料同事务保存。
    """
    if not slice_refs or len(slice_refs) != len(set(slice_refs)):
        raise KnowledgeInputError("真实交付需要非空且唯一的切片关系。")
    if isinstance(material, Message):
        if material.role is not MessageRole.USER or tool_call_id is not None:
            raise KnowledgeInputError("初始交付需要完整 user Message，不能携带调用身份。")
        kind = "initial"
        encoded = encode(material)
        decode_message(encoded)
    else:
        if not tool_call_id or not tool_call_id.strip():
            raise KnowledgeInputError("原文工具交付需要真实调用身份。")
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
                raise KnowledgeConflict("相同交付身份已有不同完整材料或切片关系。")
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
        raise KnowledgeConflict("原文范围尚未实际呈现给本批模型。")
    return wanted


# ---------------------------------------------------------------------------
# 认识版本、替代与未决关系
# ---------------------------------------------------------------------------


def _memory_version(
    connection: sqlite3.Connection, memory_id: str, version: int | None, at: float
) -> MemoryRecord | None:
    """计算单个确切版本的资格，不递归展开相邻冲突图。

    相邻版本也用这套来源和有效性规则；将关系装配留给 _memory，避免双向
    冲突形成递归读取或将对方完整正文嵌入当前认识。
    """
    row = _one(
        connection,
        "SELECT version,payload_json,learned_at,retired FROM memories WHERE memory_id=? "
        + ("ORDER BY version DESC LIMIT 1" if version is None else "AND version=?"),
        (memory_id,) if version is None else (memory_id, version),
    )
    if row is None:
        return None
    patch = _decode(row[1], MemoryPatch)
    revision = _integer(row[0])
    latest = _one(connection, "SELECT MAX(version) FROM memories WHERE memory_id=?", (memory_id,))
    reason = None if row[3] is None else _text(row[3])
    if latest != (revision,):
        reason = "revised"
    if reason is None:
        for source in patch.sources:
            fragment = get_slice(connection, source)
            snapshot = (
                None
                if fragment is None
                else _evidence(connection, fragment.evidence_id, fragment.version)
            )
            if fragment is None or snapshot is None:
                raise StorageError("认识缺少对应的原文关系。", code="invalid_record")
            if not snapshot.valid:
                reason = "source_revoked"
                break
    if reason is None and patch.valid_from is not None and at < patch.valid_from:
        reason = "not_yet_valid"
    if reason is None and patch.valid_until is not None and at >= patch.valid_until:
        reason = "expired"
    return MemoryRecord(
        patch=patch,
        version=revision,
        learned_at=_number(row[2]),
        active=reason is None,
        inactive_reason=reason,
    )


def _memory(
    connection: sqlite3.Connection, memory_id: str, version: int | None, at: float
) -> MemoryRecord | None:
    """从权威版本装配双向未决关系，双方资格使用同一查询时点。

    对方失效只改变资格投影，不能按时间自动删除关系或改指较新版本。
    关系引用的版本缺失属于权威记录损坏，不伪装为正常失效。
    """
    record = _memory_version(connection, memory_id, version, at)
    if record is None:
        return None
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT right_id,right_version FROM memory_conflicts WHERE left_id=? AND "
        "left_version=? UNION SELECT left_id,left_version FROM memory_conflicts WHERE "
        "right_id=? AND right_version=? ORDER BY 1,2",
        (memory_id, record.version, memory_id, record.version),
    ).fetchall()
    conflicts: list[MemoryConflict] = []
    for item in rows:
        other_id, other_version = _text(item[0]), _integer(item[1])
        other = _memory_version(connection, other_id, other_version, at)
        if other is None:
            raise StorageError("未决冲突缺少对应的认识版本。", code="invalid_record")
        conflicts.append(
            MemoryConflict(
                kind="memory",
                id=other_id,
                version=other_version,
                active=other.active,
                inactive_reason=other.inactive_reason,
            )
        )
    return record.model_copy(update={"conflicts": tuple(conflicts)})


def get_memory(
    connection: sqlite3.Connection,
    memory_id: str,
    *,
    version: int | None = None,
    include_inactive: bool = False,
    at: float | None = None,
) -> MemoryRecord | None:
    """显式历史查询保留版本和失效理由，默认只返回当前有效认识。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        memory_id: 已保存认识的稳定身份。
        version: 目标记录的确切版本，区别于可变的当前版本。 None 首读当前版本，
            续读仍绑定游标的原版本。
        include_inactive: 是否同时返回已失效的历史记录，并保留失效原因。 默认 False。
        at: 核验记录有效性的 Unix 时间戳；None 使用当前时间。

    Returns:
        包含原始依据、关系、有效期与确切版本的认识记录。 未取得对应记录或素材时返回 None；
        读取故障仍抛出。
    """
    result = _memory(connection, memory_id, version, time.time() if at is None else at)
    return result if result is not None and (result.active or include_inactive) else None


def revise_memory(
    connection: sqlite3.Connection, batch_id: str, version: int, patch: MemoryPatch
) -> MemoryRecord:
    """原子提交版本、来源、替代、未决关系、索引及本批真实整合回执。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        batch_id: 独立反思批次的身份，须与批次版本配对使用。
        version: 目标记录的确切版本，区别于可变的当前版本。
        patch: 含原始证据、版本、关系与有效期的认识修订提案。

    Returns:
        包含原始依据、关系、有效期与确切版本的认识记录。

    Raises:
        KnowledgeConflict: 批次、来源或认识版本已变化，不能采用本次提交。
        StorageError: 新认识缺少事务内记录。

    Note:
        关系目标必须仍是有效确切版本；拒绝迟到提案而非改指最新版本。对方
        认识不是新原文来源，不将关系或认识正文登记成已读原文。
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        _active_batch(connection, batch_id, version)
        _presented(connection, batch_id, version, patch.sources)
        if _one(connection, "SELECT 1 FROM knowledge_evidence WHERE evidence_id=?", (patch.id,)):
            raise KnowledgeConflict("认识身份与原文身份冲突。")
        row = _one(connection, "SELECT MAX(version) FROM memories WHERE memory_id=?", (patch.id,))
        current_version = 0 if row is None or row[0] is None else _integer(row[0])
        if current_version != patch.base_version:
            raise KnowledgeConflict("base_version 与当前认识版本不一致。")
        now = time.time()
        for reference in patch.conflicts:
            other = _memory_version(connection, reference.id, reference.version, now)
            if other is None or not other.active:
                raise KnowledgeConflict("冲突认识版本不存在或已经失效，必须重新核对。")
        if patch.supersedes is not None:
            old = _memory_version(connection, patch.supersedes, None, now)
            if old is None or not old.active:
                raise KnowledgeConflict("要替代的认识不存在或已经失效。")
            connection.execute(
                "UPDATE memories SET retired='superseded' WHERE memory_id=? AND version=?",
                (patch.supersedes, old.version),
            )
        next_version = patch.base_version + 1
        connection.execute(
            "INSERT INTO memories VALUES(?,?,?,?,NULL)",
            (patch.id, next_version, patch.model_dump_json(), now),
        )
        connection.executemany(
            "INSERT INTO memory_sources VALUES(?,?,?)",
            ((patch.id, next_version, source) for source in dict.fromkeys(patch.sources)),
        )
        connection.executemany(
            "INSERT INTO memory_conflicts VALUES(?,?,?,?)",
            ((patch.id, next_version, other.id, other.version) for other in patch.conflicts),
        )
        _queue_index(connection, "memory", patch.id, next_version, now)
        connection.execute(
            "INSERT INTO reflection_writes VALUES(?,?,?,?)",
            (batch_id, version, patch.id, next_version),
        )
        result = _memory(connection, patch.id, next_version, now)
        if result is None:
            raise StorageError("新认识缺少事务内记录。", code="invalid_record")
        return result


def revoke_evidence(connection: sqlite3.Connection, evidence_id: str, version: int) -> None:
    """立即撤销该来源资格，原文、片段、认识历史及已有索引全部保留。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        evidence_id: 已保存原文快照的稳定身份。
        version: 目标记录的确切版本，区别于可变的当前版本。

    Raises:
        KnowledgeConflict: 撤销的原文版本不存在。
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if _evidence(connection, evidence_id, version) is None:
            raise KnowledgeConflict("撤销的原文版本不存在。")
        connection.execute(
            "UPDATE knowledge_evidence SET valid=0 WHERE evidence_id=? AND version=?",
            (evidence_id, version),
        )


# ---------------------------------------------------------------------------
# 可重建索引与有限召回
# ---------------------------------------------------------------------------


def pending_index(
    connection: sqlite3.Connection, *, route: str | None = None, limit: int = 32
) -> tuple[IndexJob, ...]:
    """分页读取独立关键词/语义任务，消费一条路线不丢另一条更新。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        route: 派生索引路线名称，用于区分关键词与语义任务。 None 按此账本的全部可用路线读取。
        limit: 本次读取或更新的最大条目数。 默认 32。

    Returns:
        本次有限读取的可重建索引任务。

    Raises:
        ValueError: 未知索引路线。
        StorageError: 索引任务路线损坏。
    """
    _page_limit(limit)
    if route not in (None, "keyword", "semantic"):
        raise ValueError("未知索引路线。")
    sql = "SELECT kind,id,version,queued_at,route FROM pending_knowledge_index "
    parameters: tuple[object, ...] = (limit,)
    if route is not None:
        sql += "WHERE route=? "
        parameters = (route, limit)
    rows: list[tuple[object, ...]] = connection.execute(
        sql + "ORDER BY queued_at,kind,id,version LIMIT ?", parameters
    ).fetchall()
    result: list[IndexJob] = []
    for row in rows:
        if row[4] not in ("keyword", "semantic"):
            raise StorageError("索引任务路线损坏。", code="invalid_record")
        result.append(
            IndexJob(
                reference=KnowledgeReference(
                    kind=_kind(row[0]), id=_text(row[1]), version=_integer(row[2])
                ),
                queued_at=_number(row[3]),
                route=row[4],
            )
        )
    return tuple(result)


def get_candidate(
    connection: sqlite3.Connection,
    reference: KnowledgeReference,
    *,
    include_inactive: bool = False,
    at: float | None = None,
) -> EvidenceSnapshot | MemoryRecord | None:
    """当前索引只采用最新有效版本。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        reference: 包含身份与确切版本的认识或原文引用。
        include_inactive: 是否同时返回已失效的历史记录，并保留失效原因。 默认 False。
        at: 核验记录有效性的 Unix 时间戳；None 使用当前时间。

    Returns:
        经权威版本核验的原文或认识记录。 未取得对应记录或素材时返回 None；读取故障仍抛出。

    Note:
        显式历史保留原快照和真实撤销标记。

        原文历史自身的 valid 不等于授权当前索引继续使用旧向量。历史查询不修改
        保存事实，默认安装/排名入口仍会再次核验最新版本和来源状态。
    """
    if reference.kind == "memory":
        current = get_memory(
            connection,
            reference.id,
            version=reference.version,
            include_inactive=include_inactive,
            at=at,
        )
        return current
    snapshot = get_evidence(
        connection, reference.id, version=reference.version, include_inactive=include_inactive
    )
    if snapshot is not None and not include_inactive:
        latest = _one(
            connection,
            "SELECT MAX(version) FROM knowledge_evidence WHERE evidence_id=?",
            (reference.id,),
        )
        if latest != (reference.version,):
            return None
    return snapshot


def semantic_rebuild_candidates(
    connection: sqlite3.Connection, model_id: str, *, limit: int = 32
) -> tuple[KnowledgeReference, ...]:
    """有限扫描当前空间缺少的版本。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        model_id: 标识实际向量空间及其版本的模型身份。
        limit: 本次读取或更新的最大条目数。 默认 32。

    Returns:
        按相关性排列的确切版本引用，使用前仍须复查权威资格。

    Raises:
        ValueError: 向量重建需要明确空间版本。

    Note:
        旧模型向量保留，不成为当前排名的依据。
    """
    _page_limit(limit)
    if not model_id.strip():
        raise ValueError("向量重建需要明确空间版本。")
    cursor = connection.execute(
        "SELECT 'evidence' AS kind,e.evidence_id AS id,e.version AS version "
        "FROM knowledge_evidence e "
        "LEFT JOIN knowledge_index i ON i.kind='evidence' AND i.id=e.evidence_id AND "
        "i.version=e.version WHERE e.valid=1 AND e.version=(SELECT MAX(version) FROM "
        "knowledge_evidence WHERE evidence_id=e.evidence_id) AND "
        "(i.model IS NULL OR i.model<>? OR i.vector_json IS NULL) AND NOT EXISTS "
        "(SELECT 1 FROM pending_knowledge_index p WHERE p.kind='evidence' AND p.id=e.evidence_id "
        "AND p.version=e.version AND p.route='semantic') UNION ALL "
        "SELECT 'memory',m.memory_id,m.version FROM memories m LEFT JOIN knowledge_index i "
        "ON i.kind='memory' AND i.id=m.memory_id AND i.version=m.version WHERE "
        "m.retired IS NULL AND m.version=(SELECT MAX(version) FROM memories WHERE "
        "memory_id=m.memory_id) AND (i.model IS NULL OR i.model<>? OR i.vector_json IS NULL) "
        "AND NOT EXISTS(SELECT 1 FROM pending_knowledge_index p WHERE p.kind='memory' AND "
        "p.id=m.memory_id AND p.version=m.version AND p.route='semantic') ORDER BY kind,id,version",
        (model_id, model_id),
    )
    result: list[KnowledgeReference] = []
    while rows := cursor.fetchmany(32):
        for row in cast(list[tuple[object, ...]], rows):
            reference = KnowledgeReference(
                kind=_kind(row[0]), id=_text(row[1]), version=_integer(row[2])
            )
            if get_candidate(connection, reference) is not None:
                result.append(reference)
                if len(result) == limit:
                    return tuple(result)
    return tuple(result)


def queue_semantic_rebuild(
    connection: sqlite3.Connection, model_id: str, references: tuple[KnowledgeReference, ...]
) -> int:
    """原子补齐有限派生任务，等待期间已经失效或被安装的候选不再重复排队。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        model_id: 标识实际向量空间及其版本的模型身份。
        references: 包含身份与确切版本的引用集合。

    Raises:
        ValueError: 向量重建需要明确空间版本和至多 32 个候选。
    """
    if not model_id.strip() or not 1 <= len(references) <= 32:
        raise ValueError("向量重建需要明确空间版本和至多 32 个候选。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        count = 0
        for reference in references:
            if get_candidate(connection, reference) is None:
                continue
            installed = _one(
                connection,
                "SELECT model,vector_json FROM knowledge_index WHERE kind=? AND id=? AND version=?",
                (reference.kind, reference.id, reference.version),
            )
            if installed is not None and installed[0] == model_id and installed[1] is not None:
                continue
            cursor = connection.execute(
                "INSERT OR IGNORE INTO pending_knowledge_index VALUES(?,?,?,?,'semantic')",
                (reference.kind, reference.id, reference.version, time.time()),
            )
            count += cursor.rowcount
        return count


def discard_stale_index(
    connection: sqlite3.Connection, reference: KnowledgeReference, *, route: str
) -> bool:
    """只确认当前仍失效的原版本任务。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        reference: 包含身份与确切版本的认识或原文引用。
        route: 派生索引路线名称，用于区分关键词与语义任务。

    Raises:
        ValueError: 未知索引路线。

    Note:
        不向 apply_index 填入合成向量。
    """
    if route not in ("keyword", "semantic"):
        raise ValueError("未知索引路线。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if get_candidate(connection, reference) is not None:
            return False
        result = connection.execute(
            "DELETE FROM pending_knowledge_index WHERE kind=? AND id=? AND version=? AND route=?",
            (reference.kind, reference.id, reference.version, route),
        )
        return result.rowcount > 0


def _install_index(
    connection: sqlite3.Connection,
    reference: KnowledgeReference,
    terms: str,
    vector: tuple[float, ...] | None,
    model: str | None,
) -> bool:
    """在已有短事务中安装派生值；路线独立，历史索引仍保留可审计。"""
    route = "keyword" if vector is None else "semantic"
    current = get_candidate(connection, reference)
    connection.execute(
        "DELETE FROM pending_knowledge_index WHERE kind=? AND id=? AND version=? AND route=?",
        (reference.kind, reference.id, reference.version, route),
    )
    if current is None:
        return False
    if vector is None:
        connection.execute(
            "INSERT INTO knowledge_index(kind,id,version,terms) VALUES(?,?,?,?) ON "
            "CONFLICT(kind,id,version) DO UPDATE SET terms=excluded.terms",
            (reference.kind, reference.id, reference.version, terms),
        )
        connection.execute(
            "DELETE FROM knowledge_search WHERE kind=? AND id=? AND version=?",
            (reference.kind, reference.id, reference.version),
        )
        connection.execute(
            "INSERT INTO knowledge_search VALUES(?,?,?,?)",
            (reference.kind, reference.id, reference.version, terms),
        )
    else:
        connection.execute(
            "INSERT INTO knowledge_index VALUES(?,?,?,?,?,?) ON CONFLICT(kind,id,version) "
            "DO UPDATE SET vector_json=excluded.vector_json,model=excluded.model",
            (
                reference.kind,
                reference.id,
                reference.version,
                terms,
                json.dumps(vector, allow_nan=False),
                model,
            ),
        )
    return True


def _unit(vector: tuple[float, ...]) -> tuple[float, ...]:
    """有限非零向量归一化，避免 NaN/溢出在排序中伪装成有效相关性。"""
    if not vector or any(isinstance(value, bool) or not math.isfinite(value) for value in vector):
        raise ValueError("向量必须非空且有限。")
    norm = math.hypot(*vector)
    if not norm or not math.isfinite(norm):
        raise ValueError("向量范数必须有限且非零。")
    return tuple(value / norm for value in vector)


def apply_index(
    connection: sqlite3.Connection,
    reference: KnowledgeReference,
    *,
    terms: str,
    vector: tuple[float, ...] | None = None,
    model: str | None = None,
) -> bool:
    """异步计算后只安装仍有效的同版本索引，不复活撤销或被替代认识。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        reference: 包含身份与确切版本的认识或原文引用。
        terms: 从同版本完整正文生成的关键词索引文本。
        vector: 对应明确模型空间的有限数值向量。
        model: 本次调用使用的显式模型名，不在领域层推断替代模型。

    Raises:
        ValueError: 向量必须绑定明确模型；无向量不能标记向量模型。
    """
    normalized = None
    if vector is not None:
        if not model:
            raise ValueError("向量必须绑定明确模型。")
        normalized = _unit(vector)
    elif model is not None:
        raise ValueError("无向量不能标记向量模型。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        return _install_index(connection, reference, terms, normalized, model)


def refresh_keyword_index(connection: sqlite3.Connection, *, limit: int = 100) -> int:
    """仅提交有限本地关键词任务，不消费语义计算，也不等待外部服务。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        limit: 本次读取或更新的最大条目数。 默认 100。
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        jobs = pending_index(connection, route="keyword", limit=limit)
        for job in jobs:
            record = get_candidate(connection, job.reference)
            text = (
                ""
                if record is None
                else record.items_json or record.text
                if isinstance(record, EvidenceSnapshot)
                else record.patch.content
            )
            _install_index(connection, job.reference, " ".join(_terms(text)), None, None)
        return len(jobs)


def semantic_search(
    connection: sqlite3.Connection, vector: tuple[float, ...], model: str, *, limit: int = 40
) -> tuple[KnowledgeReference, ...]:
    """对同模型派生向量做有限候选选择，读取前核验当前版本及来源。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        vector: 对应明确模型空间的有限数值向量。
        model: 本次调用使用的显式模型名，不在领域层推断替代模型。
        limit: 本次读取或更新的最大条目数。 默认 40。

    Returns:
        按相关性排列的确切版本引用，使用前仍须复查权威资格。

    Raises:
        StorageError: 权威记录缺失、版本不一致、载荷无效或提交条件不成立。
    """
    import heapq

    _page_limit(limit)
    query = _unit(vector)
    cursor = connection.execute(
        "SELECT kind,id,version,vector_json FROM knowledge_index WHERE model=? AND "
        "vector_json IS NOT NULL",
        (model,),
    )
    ranked: list[tuple[float, str, str, int, KnowledgeReference]] = []
    while True:
        rows: list[tuple[object, ...]] = cursor.fetchmany(32)
        if not rows:
            break
        for row in rows:
            reference = KnowledgeReference(
                kind=_kind(row[0]), id=_text(row[1]), version=_integer(row[2])
            )
            if get_candidate(connection, reference) is None:
                continue
            try:
                raw: object = json.loads(_text(row[3]))
            except json.JSONDecodeError as error:
                raise StorageError("派生向量不是有效JSON。", code="invalid_index") from error
            if not isinstance(raw, list):
                raise StorageError("派生向量形状损坏。", code="invalid_index")
            values: list[float] = []
            for value in cast(list[object], raw):
                values.append(_number(value))
            try:
                stored = _unit(tuple(values))
            except ValueError as error:
                raise StorageError("派生向量无有效方向。", code="invalid_index") from error
            if len(stored) != len(query):
                raise StorageError("同模型向量维度不一致。", code="invalid_index")
            score = sum(left * right for left, right in zip(query, stored, strict=True))
            heapq.heappush(
                ranked, (score, reference.id, reference.kind, reference.version, reference)
            )
            if len(ranked) > limit:
                heapq.heappop(ranked)
    return tuple(item[-1] for item in sorted(ranked, reverse=True))


def _terms(text: str) -> tuple[str, ...]:
    """沿用中文单字、二元组和规范化拉丁词，关键词不依赖 token 猜测。"""
    terms: list[str] = []
    for word in re.findall(r"[\u3400-\u9fff]+|[a-z0-9_]+", text.casefold()):
        if "\u3400" <= word[0] <= "\u9fff":
            terms.extend(word)
            terms.extend(word[index : index + 2] for index in range(len(word) - 1))
        else:
            terms.append(word)
    return tuple(dict.fromkeys(terms))


def _route_records(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Sequence[object],
    limit: int,
    include_inactive: bool,
    at: float | None,
) -> tuple[tuple[KnowledgeReference, EvidenceSnapshot | MemoryRecord], ...]:
    """游标小批读取，失效项在路线排名前剔除，不挤掉后面的有效候选。"""
    cursor = connection.execute(sql, parameters)
    result: list[tuple[KnowledgeReference, EvidenceSnapshot | MemoryRecord]] = []
    while len(result) < limit:
        rows: list[tuple[object, ...]] = cursor.fetchmany(32)
        if not rows:
            break
        for row in rows:
            reference = KnowledgeReference(
                kind=_kind(row[0]), id=_text(row[1]), version=_integer(row[2])
            )
            record = get_candidate(connection, reference, include_inactive=include_inactive, at=at)
            if record is not None:
                result.append((reference, record))
                if len(result) == limit:
                    break
    return tuple(result)


def search_knowledge(
    connection: sqlite3.Connection,
    query: str,
    *,
    entities: tuple[str, ...] = (),
    start: float | None = None,
    end: float | None = None,
    at: float | None = None,
    include_inactive: bool = False,
    limit: int = 40,
) -> tuple[KnowledgeCandidate, ...]:
    """保留 BM25 关键词、明确一跳关系、原文发生时间路线，并在排名前核验资格。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        query: 用于关键词或语义检索的意图文本。
        entities: 需要共同检索或关联的实体名称集合。 默认不附加此类条目。
        start: 按发生时间筛选的起点；None 表示不限制此端。
        end: 按发生时间筛选的终点；None 表示不限制此端。
        at: 核验记录有效性的 Unix 时间戳；None 使用当前时间。
        include_inactive: 是否同时返回已失效的历史记录，并保留失效原因。 默认 False。
        limit: 本次读取或更新的最大条目数。 默认 40。

    Returns:
        已通过来源及版本核验的候选集合，保留各路线排名。
    """
    _page_limit(limit)
    routes: dict[str, tuple[tuple[KnowledgeReference, EvidenceSnapshot | MemoryRecord], ...]] = {}
    terms = _terms(query)
    if terms:
        expression = " OR ".join('"' + term + '"' for term in terms)
        routes["keyword"] = _route_records(
            connection,
            "SELECT kind,id,CAST(version AS INTEGER) FROM knowledge_search WHERE "
            "knowledge_search MATCH ? ORDER BY bm25(knowledge_search)",
            (expression,),
            limit,
            include_inactive,
            at,
        )
    if entities:
        placeholders = ",".join("?" for _ in entities)
        sql = (
            "SELECT DISTINCT 'memory',m.memory_id,m.version FROM memories m WHERE "
            "EXISTS(SELECT 1 FROM json_each(json_extract(m.payload_json,'$.entities')) e "
            "WHERE e.value IN(" + placeholders + ")) "
            "OR EXISTS(SELECT 1 FROM memory_sources ms JOIN knowledge_slices s ON "
            "ms.slice_ref=s.slice_ref "
            "WHERE ms.memory_id=m.memory_id AND ms.version=m.version AND (s.evidence_id IN("
            + placeholders
            + ") OR s.slice_ref IN("
            + placeholders
            + "))) "
            "UNION SELECT DISTINCT 'evidence',s.evidence_id,s.version FROM "
            "knowledge_slices s JOIN memory_sources ms ON s.slice_ref=ms.slice_ref WHERE "
            "ms.memory_id IN(" + placeholders + ")"
        )
        routes["relation"] = _route_records(
            connection,
            sql,
            (*entities, *entities, *entities, *entities),
            limit,
            include_inactive,
            at,
        )
    if start is not None and end is not None:
        routes["time"] = _route_records(
            connection,
            "SELECT 'evidence',evidence_id,version FROM knowledge_evidence WHERE "
            "json_extract(payload_json,'$.occurred_at') BETWEEN ? AND ? ORDER BY "
            "json_extract(payload_json,'$.occurred_at') DESC",
            (start, end),
            limit,
            include_inactive,
            at,
        )
    ranking: dict[KnowledgeReference, list[tuple[str, int]]] = {}
    values: dict[KnowledgeReference, EvidenceSnapshot | MemoryRecord] = {}
    for route, candidates in routes.items():
        for rank, (reference, record) in enumerate(candidates, 1):
            ranking.setdefault(reference, []).append((route, rank))
            values[reference] = record
    ordered = sorted(
        ranking,
        key=lambda reference: (
            -sum(1 / (60 + rank) for _, rank in ranking[reference]),
            reference.kind,
            reference.id,
            reference.version,
        ),
    )
    return tuple(
        KnowledgeCandidate(
            reference=reference, record=values[reference], ranks=tuple(ranking[reference])
        )
        for reference in ordered[:limit]
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
        KnowledgeInputError: 来源、版本、时间范围或材料引用不满足当前操作要求。
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
        # 尚未读的材料可以明确暂缓，不能据此引用具体内容或写入认识。
        if any(_overlap(left, right) for left in reviewed for right in deferred):
            raise KnowledgeInputError("同一范围不能同时复核和暂缓。")
        if any(not _covered(fragment, (*reviewed, *deferred)) for fragment in required):
            raise KnowledgeInputError("本批每个分配范围必须复核或明确暂缓。")
        expected = {finding.id for finding in batch.findings}
        if {finding.id for finding in submission.findings} != expected or len(
            submission.findings
        ) != len(expected):
            raise KnowledgeInputError("本批程序异常必须逐项判断。")
        decisions: list[tuple[EvidenceSlice, str]] = []
        writes: list[tuple[object, ...]] = connection.execute(
            "SELECT memory_id,memory_version FROM reflection_writes WHERE batch_id=? AND "
            "batch_version=?",
            (batch_id, version),
        ).fetchall()
        written: list[KnowledgeReference] = []
        for review in submission.reviews:
            scopes = _presented(connection, batch_id, version, review.evidence)
            for scope in scopes:
                if any(
                    _overlap(scope, prior) and decision != review.memory_decision
                    for prior, decision in decisions
                ):
                    raise KnowledgeInputError("同一范围的知识处理取舍互相矛盾。")
                decisions.append((scope, review.memory_decision))
            if review.memory_decision == "written":
                actual: list[EvidenceSlice] = []
                for row in writes:
                    memory = _memory(connection, _text(row[0]), _integer(row[1]), time.time())
                    if memory is not None and memory.active:
                        actual.extend(_require_slices(connection, memory.patch.sources))
                        reference = KnowledgeReference(
                            kind="memory", id=memory.patch.id, version=memory.version
                        )
                        if reference not in written:
                            written.append(reference)
                if any(not _covered(scope, actual) for scope in scopes):
                    raise KnowledgeInputError("written 缺少本批实际有效修订及对应来源范围。")
        receipt = ReflectionReceipt(
            batch_id=batch_id,
            version=version,
            completed_at=time.time(),
            submission=submission,
            written=tuple(written),
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
        source_keys = dict.fromkeys(
            (fragment.evidence_id, fragment.version)
            for fragment in (*required, *reviewed, *deferred)
        )
        for identity, source_version in source_keys:
            snapshot = _evidence(connection, identity, source_version)
            if snapshot is None:
                raise StorageError("批次来源缺失。", code="invalid_record")
            rows: list[tuple[object, ...]] = connection.execute(
                "SELECT r.slice_ref,r.state,r.reason FROM knowledge_review_ranges r JOIN "
                "knowledge_slices s ON r.slice_ref=s.slice_ref JOIN reflection_batches b "
                "ON r.batch_id=b.batch_id AND r.batch_version=b.version WHERE "
                "s.evidence_id=? AND s.version=? ORDER BY "
                "json_extract(b.receipt_json,'$.completed_at')",
                (identity, source_version),
            ).fetchall()
            effective: dict[str, tuple[str, str | None]] = {}
            for row in rows:
                effective[_text(row[0])] = (_text(row[1]), _optional_text(row[2]))
            scopes = _require_slices(connection, tuple(effective))
            unit: RangeUnit = "characters" if snapshot.items_json is None else "items"
            total = (
                len(snapshot.text)
                if unit == "characters"
                else len(cast(list[object], json.loads(snapshot.items_json or "[]")))
            )
            full = EvidenceSlice(
                slice_ref=required[0].slice_ref,
                evidence_id=identity,
                version=source_version,
                start=0,
                end=total,
                unit=unit,
                media_ids=tuple(item.media_id for item in snapshot.media),
            )
            if _covered(full, scopes):
                deferred_ranges = [item for item in effective.values() if item[0] == "deferred"]
                state = "deferred" if deferred_ranges else "reviewed"
                reasons = (
                    "；".join(reason for _, reason in deferred_ranges if reason is not None) or None
                )
                connection.execute(
                    "UPDATE pending_knowledge SET state=?,reason=? WHERE evidence_id=? AND "
                    "version=?",
                    (state, reasons, identity, source_version),
                )
        return receipt


# ---------------------------------------------------------------------------
# 监管交付与前台计划
# ---------------------------------------------------------------------------


def _notice_active(connection: sqlite3.Connection, notice: SupervisionNotice) -> bool:
    """交付前复核来源和活动版本；业务撤销令意见失效，内部错误继续传播。"""
    if notice.expires_at is not None and notice.expires_at <= time.time():
        return False
    if notice.activity_id is not None:
        row = _one(
            connection, "SELECT version FROM activities WHERE activity_id=?", (notice.activity_id,)
        )
        if row != (notice.activity_version,):
            return False
    try:
        _require_slices(connection, notice.evidence)
    except KnowledgeConflict:
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
        KnowledgeConflict: 监管来源、期限或活动版本已失效；监管身份已有不同意见。
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        _active_batch(connection, batch_id, version)
        _presented(connection, batch_id, version, notice.evidence)
        if not _notice_active(connection, notice):
            raise KnowledgeConflict("监管来源、期限或活动版本已失效。")
        row = _one(
            connection,
            "SELECT payload_json,state,response,created_at FROM knowledge_supervision WHERE id=?",
            (notice.id,),
        )
        if row is not None:
            if _decode(row[0], SupervisionNotice) != notice:
                raise KnowledgeConflict("监管身份已有不同意见。")
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
        KnowledgeInputError: 监管回应需 accept/partial/reject 与明确理由。
        KnowledgeConflict: 批次、来源或认识版本已变化，不能采用本次提交。
    """
    if decision not in ("accept", "partial", "reject") or not reason.strip():
        raise KnowledgeInputError("监管回应需 accept/partial/reject 与明确理由。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _one(
            connection,
            "SELECT payload_json,state,response,created_at FROM knowledge_supervision WHERE id=?",
            (identity,),
        )
        if row is None:
            raise KnowledgeConflict("监管意见不存在。")
        notice = _decode(row[0], SupervisionNotice)
        if row[1] != "open":
            if (row[1], row[2]) != (decision, reason):
                raise KnowledgeConflict("监管意见已经回应。")
        else:
            if not _notice_active(connection, notice):
                raise KnowledgeConflict("监管意见已失效。")
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
            raise KnowledgeConflict("计划来源已失效。")
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
        KnowledgeInputError: 计划需要未来期限与未完成状态。
        KnowledgeConflict: 计划身份已有不同内容。
    """
    if plan.due <= time.time() or plan.state != "pending":
        raise KnowledgeInputError("计划需要未来期限与未完成状态。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_plan_evidence(connection, plan.evidence)
        row = _one(connection, "SELECT payload_json FROM knowledge_plans WHERE id=?", (plan.id,))
        if row is not None:
            existing = _decode(row[0], WakeupPlan)
            if existing != plan:
                raise KnowledgeConflict("计划身份已有不同内容。")
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
        KnowledgeInputError: 计划完成需要实际结果说明。
        KnowledgeConflict: 计划不存在；计划已有不同完成事实。
    """
    if not outcome.strip():
        raise KnowledgeInputError("计划完成需要实际结果说明。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_plan_evidence(connection, evidence)
        row = _one(connection, "SELECT payload_json FROM knowledge_plans WHERE id=?", (identity,))
        if row is None:
            raise KnowledgeConflict("计划不存在。")
        plan = _decode(row[0], WakeupPlan)
        if plan.state == "completed":
            if (plan.outcome, plan.result_evidence) != (outcome, evidence):
                raise KnowledgeConflict("计划已有不同完成事实。")
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
    """借用调用方启动事务核验表并撤旧批次资格，不独立提交认识恢复。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
    """
    for table in (
        "knowledge_evidence",
        "evidence_media",
        "knowledge_slices",
        "memories",
        "reflection_batches",
        "reflection_presented",
        "reflection_writes",
        "pending_knowledge",
        "pending_knowledge_index",
        "knowledge_index",
        "knowledge_cursors",
        "knowledge_supervision",
        "knowledge_plans",
        "memory_sources",
        "memory_conflicts",
        "knowledge_search",
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
