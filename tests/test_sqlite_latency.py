"""用临时库和可控线程核验存储热路径的查询范围与排队诊断。

不读取真实运行库，不通过墙钟耗时断言性能。查询计划和指令额度约束小时预算、
原文因果反查与反思续页的扫描范围；线程门闩区分执行和排队，并核验日志脱敏。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from purrcept_engine.domain.evidence.models import (
    BehaviorConclusion,
    EvidenceSlice,
    EvidenceSnapshot,
    FinishSubmission,
    ReflectionBatch,
    ReflectionReceipt,
    Review,
)
from purrcept_engine.domain.model_requests.models import AttemptStart, RequestEvidence, RequestScope
from purrcept_engine.infrastructure.sqlite import analysis_work, evidence, feedback, queue, schema
from purrcept_engine.infrastructure.sqlite.schema import initialize
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.serialization import encode


def test_hourly_budget_uses_attempt_time_index_and_counts_retries(tmp_path: Path) -> None:
    """历史请求增长不扩大额度查询范围；边界、前台和真实重试分别处理。"""
    now = 10_000.0
    with sqlite3.connect(tmp_path / "engine.sqlite3") as connection:
        initialize(connection)
        for number in range(2000):
            identity = f"old-{number}"
            request = RequestEvidence(
                identity,
                RequestScope("behavior_review", "turn"),
                "generation",
                "synthetic",
                None,
                "compiler",
                2,
                10,
                20,
            )
            connection.execute(
                "INSERT INTO model_requests VALUES(?,?,'succeeded',?,?)",
                (identity, encode(request), 1, 2),
            )
            attempt = AttemptStart(identity, identity + ":1", 1, 20)
            connection.execute(
                "INSERT INTO model_attempts VALUES(?,?,1,?,NULL,1,2)",
                (attempt.attempt_id, identity, encode(attempt)),
            )
        for identity, owner, stamp, attempts in (
            ("boundary", "behavior_review", now - 3600, 1),
            ("memory", "behavior_review", now - 60, 2),
            ("behavior", "behavior_review", now - 30, 1),
            ("foreground", "foreground", now - 10, 1),
        ):
            request = RequestEvidence(
                identity,
                RequestScope(owner, "turn"),
                "generation",
                "synthetic",
                None,
                "compiler",
                attempts,
                10,
                20,
            )
            connection.execute(
                "INSERT INTO model_requests VALUES(?,?,'succeeded',?,?)",
                (identity, encode(request), stamp, stamp),
            )
            for sequence in range(1, attempts + 1):
                attempt = AttemptStart(identity, f"{identity}:{sequence}", sequence, 20)
                connection.execute(
                    "INSERT INTO model_attempts VALUES(?,?,?,?,NULL,?,?)",
                    (attempt.attempt_id, identity, sequence, encode(attempt), stamp, stamp),
                )
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        assert analysis_work.model_budget(connection, now) == (29, now - 60 + 3600)
        connection.set_trace_callback(None)
        plan = connection.execute("EXPLAIN QUERY PLAN " + statements[0]).fetchall()
        assert any("model_attempts_recent" in row[3] and "created_at>?" in row[3] for row in plan)
        assert not any("model_requests_scope" in row[3] for row in plan)


async def test_storage_diagnostics_separate_queue_wait_from_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """真实线程先执行长操作，再处理排队请求；日志只含代码身份和计时。"""
    clock = [0.0]
    monkeypatch.setattr(queue, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    entered, release = threading.Event(), threading.Event()
    executor = queue.SqliteExecutor(tmp_path / "engine.sqlite3", 8, 1024)
    await executor.start()
    caplog.set_level(logging.WARNING, logger="purrcept_engine.infrastructure.sqlite.queue")

    def blocked(connection: sqlite3.Connection) -> str:
        entered.set()
        if not release.wait(5):
            raise TimeoutError("synthetic gate")
        return "secret-result"

    first = asyncio.create_task(executor.submit(blocked))
    second: asyncio.Task[int] | None = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        second = asyncio.create_task(executor.submit(lambda connection: 1, control=True))
        await asyncio.sleep(0)
        clock[0] = 3.0
        release.set()
        assert await first == "secret-result"
        assert await second == 1
        records = [r for r in caplog.records if r.name.endswith("sqlite.queue")]
        assert len(records) == 2
        assert records[0].__dict__["execution_ms"] == 3000
        assert records[0].__dict__["queue_wait_ms"] == 0
        assert records[1].__dict__["execution_ms"] == 0
        assert records[1].__dict__["queue_wait_ms"] == 3000
        assert records[1].__dict__["queue"] == "control"
        assert records[0].__dict__["operation"] == (
            "test_sqlite_latency."
            "test_storage_diagnostics_separate_queue_wait_from_execution.<locals>.blocked"
        )
        assert "secret-result" not in caplog.text
    finally:
        release.set()
        await first
        if second is not None:
            await second
        await executor.close()


async def test_current_work_locates_originals_without_scanning_historical_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """完整工作投影面对大量历史仍按因果索引定位，再核验当前真实回执。"""
    measuring = threading.Event()
    instructions = 0
    statements: list[str] = []

    def progress() -> int:
        nonlocal instructions
        if measuring.is_set():
            instructions += 100
        return int(instructions > 40_000)

    def prepare(connection: sqlite3.Connection) -> None:
        """在数据库所有者线程建立合成历史；工作投影不得恢复伪造的历史正文。"""
        initialize(connection)
        for number in range(2000):
            snapshot = EvidenceSnapshot(
                evidence_id=f"historical-{number}",
                version=1,
                kind="action_result" if number % 2 else "call_receipt",
                text="unrelated-body" * 100,
                source_json=json.dumps(
                    {
                        "action_id": f"old-{number}",
                        "call_id": f"old-{number}:1",
                        "receipt_revision": 1,
                    }
                ),
                occurred_at=1.0,
            )
            connection.execute(
                "INSERT INTO knowledge_evidence VALUES(?,?,?,1,1)",
                (snapshot.evidence_id, 1, snapshot.model_dump_json()),
            )
        connection.execute(
            "INSERT INTO subject_sessions VALUES('foreground',7,'not-a-checkpoint',NULL)"
        )
        connection.commit()
        connection.set_progress_handler(progress, 100)
        connection.set_trace_callback(
            lambda sql: statements.append(sql) if measuring.is_set() else None
        )

    monkeypatch.setattr(queue, "initialize", prepare)
    path = tmp_path / "engine.sqlite3"
    store = await Store.open(path)
    try:
        await store.begin_action("current", catalog_version="test", source="return None")
        await store.begin_call(
            "current-call",
            action_id="current",
            sequence=0,
            path="me.test.read",
            target_id="test",
            arguments_json="{}",
        )
        await store.mark_call_started("current-call")
        await store.record_call_result(
            "current-call",
            state="unknown",
            result_json=None,
            error_type="TimeoutError",
            error="not confirmed",
        )
        await store.feedback.bridge(now=10.0)
        measuring.set()
        try:
            result = await store.subject_work()
        finally:
            measuring.clear()
        assert result["foreground_session_version"] == 7
        calls = result["calls"]
        assert isinstance(calls, list) and len(calls) == 1
        assert isinstance(calls[0], dict)
        assert calls[0]["original_ref"] == {"id": "call:current-call:1", "version": 1}
        assert not any("checkpoint_json" in sql or "FROM media" in sql for sql in statements)
        lookups = [
            sql
            for sql in statements
            if sql.startswith(
                (
                    "SELECT evidence_id,version FROM knowledge_evidence WHERE valid=1 AND",
                    "SELECT e.evidence_id,e.version FROM knowledge_evidence e ",
                )
            )
        ]
        assert len(lookups) == 2
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            for sql in lookups:
                plan = connection.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
                assert any("knowledge_evidence_cause" in row[3] for row in plan)
    finally:
        measuring.clear()
        await store.close()


def test_source_indexes_install_atomically_on_existing_format17(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧库补派生索引失败时回滚；重开不重写来源或改变数据库格式。"""
    names = ("knowledge_evidence_cause", "knowledge_slices_source", "knowledge_cursors_source")
    with sqlite3.connect(tmp_path / "engine.sqlite3") as connection:
        initialize(connection)
        original = EvidenceSnapshot(
            evidence_id="retained",
            version=1,
            kind="input",
            text="保留原业务证据。",
            source_json="{}",
            occurred_at=1.0,
        )
        evidence.put_evidence(connection, original, needs_reflection=False)
        for name in names:
            connection.execute(f"DROP INDEX {name}")
        connection.commit()

        def rejected(connection: sqlite3.Connection) -> None:
            raise RuntimeError("synthetic recovery rejection")

        with monkeypatch.context() as patch:
            patch.setattr(schema.archive, "initialize", rejected)
            with pytest.raises(RuntimeError, match="recovery rejection"):
                initialize(connection)
        assert not set(names) & {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        initialize(connection)
        assert set(names) <= {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert connection.execute("PRAGMA user_version").fetchone() == (17,)
        assert evidence.get_evidence(connection, "retained") == original


def test_reflection_prefix_and_finish_do_not_scan_unrelated_range_history(tmp_path: Path) -> None:
    """历史范围较多时仍按来源定位，完成首段后续读真实连续前缀。"""
    with sqlite3.connect(tmp_path / "engine.sqlite3") as connection:
        initialize(connection)
        historical = EvidenceSnapshot(
            evidence_id="historical",
            version=1,
            kind="input",
            text="x" * 1200,
            source_json="{}",
            occurred_at=1.0,
        )
        evidence.put_evidence(connection, historical, needs_reflection=False)
        fragments = tuple(
            EvidenceSlice(
                slice_ref=f"old-{n}", evidence_id="historical", version=1, start=n, end=n + 1
            )
            for n in range(1200)
        )
        connection.executemany(
            "INSERT INTO knowledge_slices VALUES(?,?,?,?)",
            ((f.slice_ref, f.evidence_id, f.version, f.model_dump_json()) for f in fragments),
        )
        references = tuple(fragment.slice_ref for fragment in fragments)
        submission = FinishSubmission(
            behavior=BehaviorConclusion(
                decision="no_intervention",
                conclusion="对照已交付原文完成复核。",
                evidence=tuple(
                    ref
                    for review in (
                        (
                            Review(
                                issue="历史复核",
                                evidence=references,
                                conclusion="已复核历史范围。",
                                reason="无新增认识。",
                            ),
                        )
                    )
                    for ref in review.evidence
                ),
            ),
            reviews=(
                Review(
                    issue="历史复核",
                    evidence=references,
                    conclusion="已复核历史范围。",
                    reason="无新增认识。",
                ),
            ),
            deferred=(),
            findings=(),
        )
        receipt = ReflectionReceipt(
            batch_id="historical-batch",
            version=1,
            completed_at=1.0,
            submission=submission,
            coverage=references,
        )
        batch = ReflectionBatch(
            batch_id="historical-batch",
            version=1,
            required_slice_refs=references,
            state="completed",
            receipt=receipt,
        )
        connection.execute(
            "INSERT INTO reflection_batches VALUES(?,1,?,'completed',?)",
            (batch.batch_id, batch.model_dump_json(), receipt.model_dump_json()),
        )
        connection.executemany(
            "INSERT INTO knowledge_review_ranges VALUES(?,?,1,'reviewed','done')",
            ((ref, batch.batch_id) for ref in references),
        )
        connection.commit()
        evidence.put_evidence(
            connection,
            historical.model_copy(
                update={"evidence_id": "current", "text": "1234567890abcdefghij"}
            ),
        )
        page = evidence.read_evidence(connection, "current", max_chars=10)
        assert page.slice is not None and page.next_cursor is not None
        current = evidence.begin_reflection(connection, "current-batch", (page.slice.slice_ref,))
        evidence.mark_presented(connection, current.batch_id, 1, (page.slice.slice_ref,))
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        evidence.finish_reflection(
            connection,
            current.batch_id,
            1,
            FinishSubmission(
                behavior=BehaviorConclusion(
                    decision="no_intervention",
                    conclusion="对照已交付原文完成复核。",
                    evidence=tuple(
                        ref
                        for review in (
                            (
                                Review(
                                    issue="本次复核",
                                    evidence=(page.slice.slice_ref,),
                                    conclusion="首段完成。",
                                    reason="无需写入。",
                                ),
                            )
                        )
                        for ref in review.evidence
                    ),
                ),
                reviews=(
                    Review(
                        issue="本次复核",
                        evidence=(page.slice.slice_ref,),
                        conclusion="首段完成。",
                        reason="无需写入。",
                    ),
                ),
                deferred=(),
                findings=(),
            ),
        )
        sources = feedback.reflection_sources(connection, epoch=10_000)
        connection.set_trace_callback(None)
        assert len(sources) == 1 and sources[0].cursor == page.next_cursor
        lookups = [
            sql
            for sql in statements
            if sql.startswith(
                (
                    "SELECT r.slice_ref,r.state FROM knowledge_review_ranges",
                    "SELECT r.slice_ref,r.state,r.reason FROM knowledge_review_ranges",
                )
            )
        ]
        assert len(lookups) == 1
        for sql in lookups:
            plan = connection.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
            assert any("knowledge_slices_source" in row[3] for row in plan)
            assert not any(row[3] in ("SCAN r", "SCAN s") for row in plan)
