"""真实临时 SQLite 的认识版本、来源覆盖、索引资格与原子复核。

只连接本次生成的数据库。SQL 函数在同一连接同步执行；异步 Store 装配由
宿主集成测试验证，本文件不打开第二份 Engine 权威连接或访问用户数据。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from purrcept_core.models import (
    ImageBlock,
    ImageBytes,
    Message,
    MessageRole,
    TextBlock,
    ToolResultBlock,
)
from pydantic import ValidationError

from purrcept_engine.errors import StorageError
from purrcept_engine.storage import knowledge as ledger
from purrcept_engine.storage.knowledge import (
    Deferred,
    EvidencePage,
    EvidenceSnapshot,
    FinishSubmission,
    KnowledgeConflict,
    KnowledgeInputError,
    KnowledgeReference,
    MemoryConflictTarget,
    MemoryPatch,
    SupervisionNotice,
    WakeupPlan,
)
from purrcept_engine.storage.models import MediaReference


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    database = sqlite3.connect(tmp_path / "knowledge.sqlite3")
    database.execute("PRAGMA foreign_keys=ON")
    database.execute("CREATE TABLE media (media_id TEXT PRIMARY KEY,content BLOB NOT NULL)")
    for statement in ledger.SCHEMA:
        database.execute(statement)
    try:
        yield database
    finally:
        database.close()


def _snapshot(
    identity: str = "source",
    text: str = "我喜欢茶，也不喝咖啡。",
    *,
    version: int = 1,
    occurred_at: float = 10.0,
) -> EvidenceSnapshot:
    return EvidenceSnapshot(
        evidence_id=identity,
        version=version,
        kind="input",
        text=text,
        source_json='{"adapter":"console","conversation":"local"}',
        occurred_at=occurred_at,
    )


def _page(
    connection: sqlite3.Connection,
    identity: str = "source",
    text: str = "我喜欢茶，也不喝咖啡。",
    *,
    max_chars: int = 8000,
) -> EvidencePage:
    ledger.put_evidence(connection, _snapshot(identity, text))
    return ledger.read_evidence(connection, identity, max_chars=max_chars)


def _batch(
    connection: sqlite3.Connection,
    page: EvidencePage,
    identity: str = "batch",
    *,
    presented: bool = True,
) -> tuple[str, int, str]:
    assert page.slice is not None
    batch = ledger.begin_reflection(connection, identity, (page.slice.slice_ref,))
    if presented:
        ledger.mark_presented(connection, batch.batch_id, batch.version, (page.slice.slice_ref,))
    return batch.batch_id, batch.version, page.slice.slice_ref


def _finish(reference: str, decision: str = "no_change") -> FinishSubmission:
    return FinishSubmission.model_validate(
        {
            "reviews": [
                {
                    "issue": "偏好",
                    "evidence": [reference],
                    "conclusion": "保留否定和对象。",
                    "memory_decision": decision,
                    "reason": "本批真实处理。",
                }
            ],
            "deferred": [],
            "findings": [],
        }
    )


def test_same_source_facts_are_idempotent_and_conflicts_do_not_overwrite(
    connection: sqlite3.Connection,
) -> None:
    source = _snapshot()
    assert ledger.put_evidence(connection, source) == ledger.put_evidence(connection, source)
    assert len(ledger.pending_reflection(connection)) == 1
    with pytest.raises(KnowledgeConflict):
        ledger.put_evidence(connection, _snapshot(text="冲突正文"))
    ledger.revoke_evidence(connection, source.evidence_id, source.version)
    assert not ledger.put_evidence(connection, source).valid
    assert ledger.get_evidence(connection, source.evidence_id) is None
    assert ledger.get_evidence(connection, source.evidence_id, include_inactive=True) is not None


def test_multiple_issues_on_same_range_keep_one_coverage_relationship(
    connection: sqlite3.Connection,
) -> None:
    page = _page(connection)
    identity, version, reference = _batch(connection, page)
    first = _finish(reference).reviews[0]
    second = first.model_copy(update={"issue": "另一项对象关系"})
    receipt = ledger.finish_reflection(
        connection,
        identity,
        version,
        FinishSubmission(reviews=(first, second), deferred=(), findings=()),
    )
    assert receipt.submission.reviews == (first, second)
    assert receipt.coverage == (reference,)
    assert connection.execute("SELECT count(*) FROM knowledge_review_ranges").fetchone() == (1,)
    assert not ledger.pending_reflection(connection)


def test_abandon_revokes_generation_but_retains_committed_revisions_and_pending(
    connection: sqlite3.Connection,
) -> None:
    page = _page(connection)
    identity, version, reference = _batch(connection, page)
    memory = ledger.revise_memory(
        connection,
        identity,
        version,
        MemoryPatch(
            id="memory", content="失败前已保存的认识", sources=(reference,), base_version=0
        ),
    )
    failed = ledger.abandon_reflection(connection, identity, version, "后续生成失败")
    assert failed.state == "interrupted" and failed.interrupted_reason == "后续生成失败"
    assert ledger.abandon_reflection(connection, identity, version, "别的原因") == failed
    assert ledger.get_memory(connection, "memory") == memory
    assert ledger.pending_reflection(connection)
    with pytest.raises(KnowledgeConflict):
        ledger.revise_memory(
            connection, identity, version, memory.patch.model_copy(update={"base_version": 1})
        )
    next_batch = ledger.begin_reflection(connection, identity, (reference,))
    assert next_batch.version == 2 and next_batch.interrupted_reason is None
    ledger.mark_presented(connection, identity, 2, (reference,))
    completed = ledger.finish_reflection(connection, identity, 2, _finish(reference))
    after = ledger.abandon_reflection(connection, identity, 2, "不能撤销完成")
    assert after.state == "completed" and after.receipt == completed


def test_unrelated_revoked_presented_source_does_not_invalidate_current_revision(
    connection: sqlite3.Connection,
) -> None:
    current = _page(connection, "current")
    unrelated = _page(connection, "unrelated")
    assert unrelated.slice is not None
    identity, version, reference = _batch(connection, current)
    ledger.mark_presented(connection, identity, version, (unrelated.slice.slice_ref,))
    ledger.revoke_evidence(connection, "unrelated", 1)
    record = ledger.revise_memory(
        connection,
        identity,
        version,
        MemoryPatch(
            id="current-memory", content="当前材料的认识", sources=(reference,), base_version=0
        ),
    )
    assert record.active
    assert tuple(item.evidence_id for item in ledger.pending_reflection(connection)) == ("current",)


def test_same_transaction_media_and_pending_are_authoritative(
    connection: sqlite3.Connection,
) -> None:
    content = b"original-media"
    identity = hashlib.sha256(content).hexdigest()
    snapshot = _snapshot().model_copy(
        update={"media": (MediaReference(identity, "image/png", len(content)),)}
    )
    with pytest.raises(StorageError):
        ledger.put_evidence(connection, snapshot)
    assert ledger.get_evidence(connection, "source") is None
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO media VALUES(?,?)", (identity, content))
        ledger.put_evidence_in_transaction(connection, snapshot)
    assert ledger.get_evidence(connection, "source") == snapshot
    assert connection.execute("SELECT media_id FROM evidence_media").fetchone() == (identity,)
    with pytest.raises(sqlite3.IntegrityError), connection:
        connection.execute("DELETE FROM media WHERE media_id=?", (identity,))
    with pytest.raises(RuntimeError):
        ledger.put_evidence_in_transaction(connection, _snapshot("outside"))
    broken = _snapshot("bad-length").model_copy(
        update={"media": (MediaReference(identity, "image/png", len(content) + 1),)}
    )
    with pytest.raises(StorageError):
        ledger.put_evidence(connection, broken)
    assert len(ledger.pending_reflection(connection)) == 1


def test_cursor_resolves_saved_snapshot_and_can_change_page_width(
    connection: sqlite3.Connection,
) -> None:
    first = _page(connection, text="abcdefghijkl", max_chars=4)
    assert first.next_cursor is not None
    ledger.put_evidence(connection, _snapshot(text="changed new version", version=2))
    second = ledger.read_evidence_cursor(connection, first.next_cursor, max_chars=5)
    assert second.text == "efghi" and second.snapshot is not None and second.snapshot.version == 1
    assert ledger.read_evidence_cursor(connection, first.next_cursor, max_chars=5) == second
    with pytest.raises(KnowledgeInputError):
        ledger.read_evidence_cursor(connection, "cursor:unknown")


@pytest.mark.parametrize("limit", (-1, 0, True))
def test_negative_sql_limit_cannot_make_durable_mailboxes_unbounded(
    connection: sqlite3.Connection, limit: int
) -> None:
    with pytest.raises(KnowledgeInputError):
        ledger.pending_reflection(connection, limit=limit)
    with pytest.raises(KnowledgeInputError):
        ledger.pending_index(connection, limit=limit)
    with pytest.raises(KnowledgeInputError):
        ledger.pending_plans(connection, limit=limit)
    with pytest.raises(KnowledgeInputError):
        ledger.pending_supervision(connection, limit=limit)
    with pytest.raises(KnowledgeInputError):
        ledger.semantic_search(connection, (1.0,), "model", limit=limit)
    with pytest.raises(KnowledgeInputError):
        ledger.search_knowledge(connection, "", limit=limit)


def test_plan_accepts_original_source_without_reflection_slice(
    connection: sqlite3.Connection,
) -> None:
    ledger.put_evidence(connection, _snapshot())
    plan = WakeupPlan(id="later", due=time.time() + 60, reason="检查实际结果", evidence="source")
    assert ledger.plan_wakeup(connection, plan) == plan
    assert ledger.resolve_plan(connection, "later", "已有真实结果", "source").state == "completed"
    assert not ledger.pending_plans(connection)
    ledger.revoke_evidence(connection, "source", 1)
    with pytest.raises(KnowledgeConflict):
        ledger.plan_wakeup(connection, plan.model_copy(update={"id": "invalid"}))


def test_created_or_summary_page_does_not_authorize_revision(
    connection: sqlite3.Connection,
) -> None:
    page = _page(connection)
    batch_id, version, reference = _batch(connection, page, presented=False)
    assert ledger.read_evidence(connection, "source", level="summary").slice is None
    with pytest.raises(KnowledgeConflict, match="尚未实际呈现"):
        ledger.revise_memory(
            connection,
            batch_id,
            version,
            MemoryPatch(id="preference", content="偏好茶。", sources=(reference,)),
        )
    assert ledger.get_memory(connection, "preference") is None
    ledger.mark_presented(connection, batch_id, version, (reference,))
    assert (
        ledger.revise_memory(
            connection,
            batch_id,
            version,
            MemoryPatch(id="preference", content="偏好茶。", sources=(reference,)),
        ).version
        == 1
    )


def test_versions_replacement_and_unresolved_conflicts_remain_auditable(
    connection: sqlite3.Connection,
) -> None:
    page = _page(connection)
    batch_id, version, reference = _batch(connection, page)
    first = ledger.revise_memory(
        connection,
        batch_id,
        version,
        MemoryPatch(id="a", content="喜欢茶。", sources=(reference,), entities=("console-person",)),
    )
    with pytest.raises(KnowledgeConflict, match="base_version"):
        ledger.revise_memory(connection, batch_id, version, first.patch)
    second = ledger.revise_memory(
        connection,
        batch_id,
        version,
        MemoryPatch(
            id="b",
            content="可能不喜欢茶，未决。",
            sources=(reference,),
            conflicts=(MemoryConflictTarget(kind="memory", id="a", version=1),),
        ),
    )
    a = ledger.get_memory(connection, "a")
    assert a is not None and a.active and a.conflicts[0].id == "b"
    assert second.active
    ledger.revise_memory(
        connection,
        batch_id,
        version,
        MemoryPatch(id="c", content="明确替代a。", sources=(reference,), supersedes="a"),
    )
    assert ledger.get_memory(connection, "a") is None
    historical = ledger.get_memory(connection, "a", include_inactive=True)
    assert historical is not None and historical.inactive_reason == "superseded"
    assert ledger.get_memory(connection, "b") is not None


def test_expiry_and_revoked_sources_cannot_be_revived_by_late_indexes(
    connection: sqlite3.Connection,
) -> None:
    page = _page(connection)
    batch_id, version, reference = _batch(connection, page)
    ledger.revise_memory(
        connection,
        batch_id,
        version,
        MemoryPatch(
            id="expired", content="短期认识。", sources=(reference,), valid_until=time.time() - 1
        ),
    )
    assert ledger.get_memory(connection, "expired") is None
    expired = ledger.get_memory(connection, "expired", include_inactive=True)
    assert expired is not None and expired.inactive_reason == "expired"
    old = KnowledgeReference(kind="memory", id="current", version=1)
    ledger.revise_memory(
        connection,
        batch_id,
        version,
        MemoryPatch(id="current", content="旧认识。", sources=(reference,)),
    )
    ledger.revise_memory(
        connection,
        batch_id,
        version,
        MemoryPatch(id="current", content="新认识。", sources=(reference,), base_version=1),
    )
    assert not ledger.apply_index(connection, old, terms="旧", vector=(1.0, 0.0), model="embedding")
    current = KnowledgeReference(kind="memory", id="current", version=2)
    assert ledger.apply_index(connection, current, terms="新", vector=(1.0, 0.0), model="embedding")
    ledger.revoke_evidence(connection, "source", 1)
    assert ledger.semantic_search(connection, (1.0, 0.0), "embedding") == ()
    assert not ledger.apply_index(
        connection, current, terms="新", vector=(1.0, 0.0), model="embedding"
    )
    record = ledger.get_memory(connection, "current", include_inactive=True)
    assert record is not None and record.inactive_reason == "source_revoked"


def test_keyword_relation_time_and_history_queries_check_authority(
    connection: sqlite3.Connection,
) -> None:
    page = _page(connection)
    batch_id, version, reference = _batch(connection, page)
    ledger.revise_memory(
        connection,
        batch_id,
        version,
        MemoryPatch(
            id="preference",
            content="偏好喝茶而非咖啡。",
            sources=(reference,),
            entities=("person",),
        ),
    )
    ledger.refresh_keyword_index(connection)
    assert ledger.pending_index(connection, route="keyword") == ()
    assert ledger.pending_index(connection, route="semantic")
    keyword = ledger.search_knowledge(connection, "茶")
    assert {item.reference.id for item in keyword} == {"source", "preference"}
    assert {
        item.reference.id for item in ledger.search_knowledge(connection, "", entities=("person",))
    } == {"preference"}
    assert [
        item.reference.id for item in ledger.search_knowledge(connection, "", start=0, end=20)
    ] == ["source"]
    ledger.revoke_evidence(connection, "source", 1)
    assert ledger.search_knowledge(connection, "茶") == ()
    historical = ledger.search_knowledge(connection, "茶", include_inactive=True)
    assert {item.reference.id for item in historical} == {"source", "preference"}


def test_cursor_replays_original_version_after_new_snapshot(connection: sqlite3.Connection) -> None:
    first = _page(connection, text="abcdef", max_chars=3)
    assert first.slice is not None and first.next_cursor is not None
    ledger.put_evidence(connection, _snapshot(text="new-version", version=2))
    second = ledger.read_evidence(connection, "source", cursor=first.next_cursor, max_chars=3)
    assert second.text == "def" and second.snapshot is not None and second.snapshot.version == 1
    assert second == ledger.read_evidence(
        connection, "source", cursor=first.next_cursor, max_chars=3
    )
    with pytest.raises(KnowledgeInputError):
        ledger.read_evidence(connection, "another", cursor=first.next_cursor, max_chars=3)


def test_structured_page_never_splits_a_json_entry(connection: sqlite3.Connection) -> None:
    source = _snapshot(text="").model_copy(update={"items_json": '[{"text":"一"},{"text":"二"}]'})
    ledger.put_evidence(connection, source)
    first = ledger.read_evidence(connection, "source", max_chars=16)
    assert first.slice is not None and first.slice.unit == "items"
    assert json.loads(first.text) == [{"text": "一"}]
    assert first.next_cursor is not None
    assert json.loads(
        ledger.read_evidence(connection, "source", cursor=first.next_cursor, max_chars=16).text
    ) == [{"text": "二"}]
    with pytest.raises(KnowledgeInputError, match="单个结构化条目"):
        ledger.read_evidence(connection, "source", max_chars=2)


def test_local_review_does_not_confirm_unread_remainder(connection: sqlite3.Connection) -> None:
    first = _page(connection, text="abcdef", max_chars=3)
    batch_id, version, reference = _batch(connection, first)
    ledger.finish_reflection(connection, batch_id, version, _finish(reference))
    pending = ledger.pending_reflection(connection)
    assert len(pending) == 1 and pending[0].reviewed_slice_refs == (reference,)
    assert first.next_cursor is not None
    second = ledger.read_evidence(connection, "source", cursor=first.next_cursor, max_chars=3)
    batch_id, version, reference = _batch(connection, second, "second-batch")
    ledger.finish_reflection(connection, batch_id, version, _finish(reference))
    assert ledger.pending_reflection(connection) == ()


def test_finish_validates_written_coverage_and_keeps_prior_revision_on_failure(
    connection: sqlite3.Connection,
) -> None:
    page = _page(connection)
    batch_id, version, reference = _batch(connection, page)
    with pytest.raises(KnowledgeInputError, match="written"):
        ledger.finish_reflection(connection, batch_id, version, _finish(reference, "written"))
    ledger.revise_memory(
        connection,
        batch_id,
        version,
        MemoryPatch(id="preference", content="真实写入。", sources=(reference,)),
    )
    with pytest.raises(KnowledgeInputError, match="每个分配范围"):
        ledger.finish_reflection(
            connection, batch_id, version, FinishSubmission(reviews=(), deferred=(), findings=())
        )
    assert ledger.get_memory(connection, "preference") is not None
    receipt = ledger.finish_reflection(connection, batch_id, version, _finish(reference, "written"))
    assert receipt.written[0].id == "preference"
    assert (
        ledger.finish_reflection(
            connection, batch_id, version, FinishSubmission(reviews=(), deferred=(), findings=())
        )
        == receipt
    )
    with pytest.raises(KnowledgeConflict):
        ledger.revise_memory(
            connection,
            batch_id,
            version,
            MemoryPatch(id="later", content="禁止。", sources=(reference,)),
        )
    ledger.mark_presented(connection, batch_id, version, ())


def test_overlapping_review_and_defer_are_rejected(connection: sqlite3.Connection) -> None:
    page = _page(connection)
    batch_id, version, reference = _batch(connection, page)
    submitted = _finish(reference).model_copy(
        update={"deferred": (Deferred(evidence=(reference,), reason="尚未复核。"),)}
    )
    with pytest.raises(KnowledgeInputError, match="同时复核和暂缓"):
        ledger.finish_reflection(connection, batch_id, version, submitted)
    assert ledger.get_reflection(connection, batch_id, version).state == "running"


def test_real_sql_failure_rolls_back_batch_and_mailbox_but_not_prior_memory(
    connection: sqlite3.Connection,
) -> None:
    page = _page(connection)
    batch_id, version, reference = _batch(connection, page)
    ledger.revise_memory(
        connection,
        batch_id,
        version,
        MemoryPatch(id="kept", content="已提交。", sources=(reference,)),
    )
    connection.execute(
        "CREATE TRIGGER reject_mail BEFORE UPDATE ON pending_knowledge BEGIN SELECT "
        "RAISE(ABORT,'synthetic finish failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic finish failure"):
        ledger.finish_reflection(connection, batch_id, version, _finish(reference, "written"))
    assert ledger.get_reflection(connection, batch_id, version).state == "running"
    assert ledger.pending_reflection(connection)[0].reviewed_slice_refs == ()
    assert ledger.get_memory(connection, "kept") is not None


def test_restart_retires_only_batch_generation(connection: sqlite3.Connection) -> None:
    page = _page(connection)
    batch_id, version, reference = _batch(connection, page)
    ledger.revise_memory(
        connection,
        batch_id,
        version,
        MemoryPatch(id="kept", content="真实写入。", sources=(reference,)),
    )
    with connection:
        ledger.initialize(connection)
    replacement = ledger.begin_reflection(connection, batch_id, (reference,))
    assert replacement.version == version + 1
    with pytest.raises(KnowledgeConflict):
        ledger.mark_presented(connection, batch_id, version, (reference,))
    assert ledger.get_memory(connection, "kept") is not None


def test_supervision_and_wakeup_are_explicit_independent_facts(
    connection: sqlite3.Connection,
) -> None:
    page = _page(connection)
    batch_id, version, reference = _batch(connection, page)
    notice = SupervisionNotice(
        id="notice", issue="核对", evidence=(reference,), suggestion="重新思考。", level="urgent"
    )
    first = ledger.supervise(connection, batch_id, version, notice)
    duplicate = ledger.supervise(connection, batch_id, version, notice)
    assert first.active and not first.duplicate and duplicate.duplicate
    response = ledger.respond_supervision(connection, notice.id, "partial", "证据支持部分判断。")
    assert response.state == "partial" and ledger.pending_supervision(connection) == ()
    plan = WakeupPlan(id="plan", due=time.time() + 20, reason="稍后复查。", evidence=reference)
    assert ledger.plan_wakeup(connection, plan) == plan
    assert ledger.pending_plans(connection, due_before=time.time()) == ()
    assert ledger.resolve_plan(connection, plan.id, "已核对。", reference).state == "completed"


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "x", "content": "内容", "sources": ["ref"], "base_version": True},
        {"id": "x", "content": "内容", "sources": ["ref"], "valid_from": float("nan")},
        {"id": "x", "content": "内容", "sources": ["ref"], "kind": "identity"},
    ],
)
def test_revision_boundary_rejects_bool_nonfinite_and_fixed_identity(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        MemoryPatch.model_validate(payload)


def test_persistent_delivery_restores_only_complete_original_material(
    connection: sqlite3.Connection,
) -> None:
    """精确登记可跨批恢复，创建登记本身不赋予模型已读资格。"""
    page = _page(connection)
    batch_id, version, reference = _batch(connection, page, presented=False)
    content = (TextBlock("真实原文"), ImageBlock(ImageBytes(b"real-pixels", "image/png")))
    initial = Message(MessageRole.USER, content, metadata={"reflection_batch": batch_id})
    first = ledger.record_delivery(
        connection, batch_id, version, tool_call_id=None, material=initial, slice_refs=(reference,)
    )
    tool = ledger.record_delivery(
        connection,
        batch_id,
        version,
        tool_call_id="actual-read",
        material=content,
        slice_refs=(reference,),
    )
    assert (
        ledger.record_delivery(
            connection,
            batch_id,
            version,
            tool_call_id=None,
            material=initial,
            slice_refs=(reference,),
        )
        == first
    )
    with pytest.raises(KnowledgeConflict, match="尚未实际呈现"):
        ledger.revise_memory(
            connection,
            batch_id,
            version,
            MemoryPatch(id="unread", content="提前写入", sources=(reference,)),
        )
    ledger.abandon_reflection(connection, batch_id, version, "原批次已经结束。")
    actual = Message(
        MessageRole.TOOL,
        (ToolResultBlock("actual-read", content=content),),
        name="read_evidence",
    )
    assert ledger.deliveries_for_history(connection, (initial, actual)) == (first, tool)
    forged = (
        Message.user("slice_ref=" + reference),
        Message(MessageRole.USER, content[:-1], metadata=initial.metadata),
        Message(MessageRole.TOOL, actual.content, name="recall"),
        Message(
            MessageRole.TOOL, (ToolResultBlock("other", content=content),), name="read_evidence"
        ),
        Message(
            MessageRole.TOOL,
            (ToolResultBlock("actual-read", content=content, is_error=True),),
            name="read_evidence",
        ),
        Message(
            MessageRole.TOOL,
            (ToolResultBlock("actual-read", content=content[:-1]),),
            name="read_evidence",
        ),
    )
    assert ledger.deliveries_for_history(connection, forged) == ()
    assert connection.execute("SELECT count(*) FROM media").fetchone() == (1,)
    assert connection.execute("SELECT count(*) FROM reflection_delivery_media").fetchone() == (2,)
    with connection:
        ledger.initialize(connection)
    assert ledger.deliveries_for_history(connection, (initial, actual)) == (first, tool)
    saved = connection.execute(
        "SELECT payload_json FROM reflection_deliveries WHERE delivery_id=?", (first.delivery_id,)
    ).fetchone()
    assert saved is not None and isinstance(saved[0], str)
    connection.execute(
        "UPDATE reflection_deliveries SET payload_json=? WHERE delivery_id=?",
        (
            saved[0].replace('"type":"Message"', '"type":"Other","type":"Message"', 1),
            first.delivery_id,
        ),
    )
    connection.commit()
    with pytest.raises(StorageError, match="重复字段"):
        ledger.deliveries_for_history(connection, (initial,))
    connection.execute(
        "UPDATE reflection_deliveries SET payload_json=? WHERE delivery_id=?",
        (saved[0], first.delivery_id),
    )
    connection.execute("UPDATE media SET content=?", (b"corrupt",))
    connection.commit()
    with pytest.raises(StorageError, match="摘要不符"):
        ledger.deliveries_for_history(connection, (initial,))
