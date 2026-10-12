"""用临时 SQLite 核验诊断时间边界、有限批次和权威事实保留。

每个场景只有一条测试拥有的连接。诊断正文独立淘汰；真实 SQL 表中的请求、
完整参数、响应、媒体和业务事实快照不得随诊断过期改变。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from purrcept_engine.errors import StorageError
from purrcept_engine.storage import model_requests
from purrcept_engine.storage.retention import (
    diagnostic_events_digest,
    expire_diagnostics,
)


def _open_database(path: Path) -> sqlite3.Connection:
    """创建只有此测试持有的新库，真实外键关系和诊断索引均启用。"""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("CREATE TABLE media (media_id TEXT PRIMARY KEY,content BLOB NOT NULL)")
    for statement in model_requests.SCHEMA:
        connection.execute(statement)
    connection.execute("CREATE TABLE authority (kind TEXT PRIMARY KEY,evidence TEXT NOT NULL)")
    connection.executemany(
        "INSERT INTO authority VALUES (?,?)",
        ((name, f"original-{name}") for name in ("input", "receipt", "knowledge", "activity")),
    )
    connection.execute("INSERT INTO media VALUES ('media',?)", (b"original-owned-media",))
    connection.execute(
        "INSERT INTO model_requests VALUES ('request',?,'succeeded',0,10)",
        ('{"materials_json":"original-model-input","scope":{}}',),
    )
    connection.commit()
    return connection


def _diagnostics(
    connection: sqlite3.Connection,
    attempt_id: str,
    created_at: float,
    *,
    events: str = '[{"type":"TextDelta","text":"fragment"}]',
    complete: bool = True,
    omitted: int = 0,
) -> str:
    """放入独立完整参数/响应事实，诊断覆盖与原正文摘要另表保存。"""
    row: tuple[object, ...] = connection.execute("SELECT count(*) FROM model_attempts").fetchone()
    count = row[0]
    assert isinstance(count, int)
    sequence = count + 1
    connection.execute(
        "INSERT INTO model_attempts VALUES (?, 'request',?, ?, ?, ?, ?)",
        (
            attempt_id,
            sequence,
            f"original-start-{attempt_id}",
            f"original-full-arguments-and-response-{attempt_id}",
            created_at - 1,
            created_at,
        ),
    )
    connection.execute("INSERT INTO model_attempt_media VALUES (?, 'media')", (attempt_id,))
    digest = diagnostic_events_digest(events)
    connection.execute(
        "INSERT INTO model_attempt_diagnostics VALUES (?, ?, ?, ?, ?, ?, NULL)",
        (attempt_id, events, digest, int(complete), omitted, created_at),
    )
    connection.commit()
    return digest


def _authority_snapshot(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    """冻结所有与清理无关的真实 SQL 行，避免仅检查记录数量遗漏正文变化。"""
    tables = ("authority", "media", "model_requests", "model_attempts", "model_attempt_media")
    rows: list[tuple[object, ...]] = []
    for table in tables:
        values: list[tuple[object, ...]] = connection.execute(f"SELECT * FROM {table}").fetchall()
        rows.extend((table, *value) for value in values)
    return tuple(rows)


def test_retention_preserves_authority_original_coverage_and_digest(tmp_path: Path) -> None:
    connection = _open_database(tmp_path / "retention.sqlite3")
    try:
        digest = _diagnostics(connection, "old", 10.0, complete=False, omitted=3)
        _diagnostics(connection, "boundary", 20.0)
        _diagnostics(connection, "recent", 30.0)
        before = _authority_snapshot(connection)

        result = expire_diagnostics(connection, before=20.0, expired_at=40.0)

        assert result.expired_attempts == 1
        assert result.removed_events == 1
        assert result.removed_bytes > 0
        assert not result.has_more
        assert _authority_snapshot(connection) == before
        assert connection.execute(
            "SELECT events_json,original_events_digest,diagnostics_complete,"
            "omitted_events,expired_at FROM model_attempt_diagnostics WHERE attempt_id='old'"
        ).fetchone() == ("[]", digest, 0, 3, 40.0)
        retained: list[tuple[object, ...]] = connection.execute(
            "SELECT attempt_id,expired_at FROM model_attempt_diagnostics "
            "WHERE attempt_id!='old' ORDER BY attempt_id"
        ).fetchall()
        assert retained == [("boundary", None), ("recent", None)]
        assert expire_diagnostics(connection, before=20.0, expired_at=50.0).expired_attempts == 0
    finally:
        connection.close()


def test_retention_is_batched_and_empty_diagnostics_expire_explicitly(tmp_path: Path) -> None:
    path = tmp_path / "batch.sqlite3"
    connection = _open_database(path)
    try:
        for index in range(4):
            _diagnostics(connection, f"attempt-{index}", float(index), events="[]")
        first = expire_diagnostics(connection, before=10.0, expired_at=20.0, limit=2)
        assert (first.expired_attempts, first.removed_events, first.removed_bytes) == (2, 0, 0)
        assert first.has_more
    finally:
        connection.close()

    # 重开同一临时库核验清理标记是真实提交，而不是连接内的临时投影。
    connection = sqlite3.connect(path)
    try:
        second = expire_diagnostics(connection, before=10.0, expired_at=30.0, limit=2)
        assert second.expired_attempts == 2
        assert not second.has_more
        assert connection.execute("SELECT count(*) FROM model_attempts").fetchone() == (4,)
        assert connection.execute(
            "SELECT expired_at FROM model_attempt_diagnostics ORDER BY attempt_id"
        ).fetchall() == [(20.0,), (20.0,), (30.0,), (30.0,)]
    finally:
        connection.close()


def test_bad_diagnostics_roll_back_the_whole_batch(tmp_path: Path) -> None:
    connection = _open_database(tmp_path / "bad.sqlite3")
    try:
        _diagnostics(connection, "first", 1.0)
        _diagnostics(connection, "bad", 2.0)
        connection.execute(
            "UPDATE model_attempt_diagnostics SET events_json='{}' WHERE attempt_id='bad'"
        )
        connection.commit()
        with pytest.raises(StorageError, match="记录损坏"):
            expire_diagnostics(connection, before=10.0, expired_at=20.0)
        assert connection.execute(
            "SELECT count(*) FROM model_attempt_diagnostics WHERE expired_at IS NOT NULL"
        ).fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize("events", ["{}", "null", "[NaN]", '[{"a":1,"a":2}]', '["\\ud800"]'])
def test_digest_rejects_invalid_diagnostic_material(events: str) -> None:
    with pytest.raises(StorageError):
        diagnostic_events_digest(events)


def test_digest_represents_original_text_bytes() -> None:
    original = '[ {"text": "中文"} ]'
    assert (
        diagnostic_events_digest(original) == hashlib.sha256(original.encode("utf-8")).hexdigest()
    )
    assert diagnostic_events_digest(original) != diagnostic_events_digest('[{"text":"中文"}]')


@pytest.mark.parametrize("limit", [0, -1, True])
def test_cleanup_rejects_unbounded_or_invalid_batch(tmp_path: Path, limit: int) -> None:
    connection = _open_database(tmp_path / "invalid.sqlite3")
    try:
        with pytest.raises(ValueError, match="批量"):
            expire_diagnostics(connection, before=10.0, expired_at=20.0, limit=limit)
    finally:
        connection.close()
