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

from purrcept_engine.domain.evidence.models import (
    EvidenceConflict,
    EvidenceInputError,
    EvidencePage,
    EvidenceSnapshot,
    ObservationScope,
    SupervisionNotice,
    WakeupPlan,
)
from purrcept_engine.domain.inputs.models import MediaReference
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite import evidence as ledger


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    database = sqlite3.connect(tmp_path / "evidence.sqlite3")
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


def test_same_source_facts_are_idempotent_and_conflicts_do_not_overwrite(
    connection: sqlite3.Connection,
) -> None:
    source = _snapshot()
    assert ledger.put_evidence(connection, source) == ledger.put_evidence(connection, source)
    assert len(ledger.pending_review(connection)) == 1
    with pytest.raises(EvidenceConflict):
        ledger.put_evidence(connection, _snapshot(text="冲突正文"))
    ledger.revoke_evidence(connection, source.evidence_id, source.version)
    assert not ledger.put_evidence(connection, source).valid
    assert ledger.get_evidence(connection, source.evidence_id) is None
    assert ledger.get_evidence(connection, source.evidence_id, include_inactive=True) is not None


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
    assert len(ledger.pending_review(connection)) == 1


def test_cursor_resolves_saved_snapshot_and_can_change_page_width(
    connection: sqlite3.Connection,
) -> None:
    first = _page(connection, text="abcdefghijkl", max_chars=4)
    assert first.next_cursor is not None
    ledger.put_evidence(connection, _snapshot(text="changed new version", version=2))
    second = ledger.read_evidence_cursor(connection, first.next_cursor, max_chars=5)
    assert second.text == "efghi" and second.snapshot is not None and second.snapshot.version == 1
    assert ledger.read_evidence_cursor(connection, first.next_cursor, max_chars=5) == second
    with pytest.raises(EvidenceInputError):
        ledger.read_evidence_cursor(connection, "cursor:unknown")


@pytest.mark.parametrize("limit", (-1, 0, True))
def test_negative_sql_limit_cannot_make_durable_mailboxes_unbounded(
    connection: sqlite3.Connection, limit: int
) -> None:
    with pytest.raises(EvidenceInputError):
        ledger.pending_review(connection, limit=limit)
    with pytest.raises(EvidenceInputError):
        ledger.pending_plans(connection, limit=limit)
    with pytest.raises(EvidenceInputError):
        ledger.pending_supervision(connection, limit=limit)


def test_plan_accepts_original_source_without_reflection_slice(
    connection: sqlite3.Connection,
) -> None:
    ledger.put_evidence(connection, _snapshot())
    plan = WakeupPlan(id="later", due=time.time() + 60, reason="检查实际结果", evidence="source")
    assert ledger.plan_wakeup(connection, plan) == plan
    assert ledger.resolve_plan(connection, "later", "已有真实结果", "source").state == "completed"
    assert not ledger.pending_plans(connection)
    ledger.revoke_evidence(connection, "source", 1)
    with pytest.raises(EvidenceConflict):
        ledger.plan_wakeup(connection, plan.model_copy(update={"id": "invalid"}))


def test_cursor_replays_original_version_after_new_snapshot(connection: sqlite3.Connection) -> None:
    first = _page(connection, text="abcdef", max_chars=3)
    assert first.slice is not None and first.next_cursor is not None
    ledger.put_evidence(connection, _snapshot(text="new-version", version=2))
    second = ledger.read_evidence(connection, "source", cursor=first.next_cursor, max_chars=3)
    assert second.text == "def" and second.snapshot is not None and second.snapshot.version == 1
    assert second == ledger.read_evidence(
        connection, "source", cursor=first.next_cursor, max_chars=3
    )
    with pytest.raises(EvidenceInputError):
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
    with pytest.raises(EvidenceInputError, match="单个结构化条目"):
        ledger.read_evidence(connection, "source", max_chars=2)


def test_supervision_and_wakeup_are_explicit_independent_facts(
    connection: sqlite3.Connection,
) -> None:
    page = _page(connection)
    batch_id, version, reference = _batch(connection, page)
    notice = SupervisionNotice(
        scope=ObservationScope(adapter_id="test"),
        expected_change="依据实际事实调整",
        id="notice",
        issue="核对",
        evidence=(reference,),
        suggestion="重新思考。",
        level="urgent",
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
    with pytest.raises(EvidenceConflict, match="尚未实际呈现"):
        ledger.supervise(
            connection,
            batch_id,
            version,
            SupervisionNotice(
                scope=ObservationScope(adapter_id="test"),
                expected_change="依据实际事实调整",
                id="unread",
                issue="核对",
                suggestion="先读原文",
                evidence=(reference,),
            ),
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
