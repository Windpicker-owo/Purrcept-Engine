"""验证冻结格式15的原子迁移，以及不再支持的旧格式拒绝边界。

DDL 样本独立于当前模式。升级拆分模型正文并建立独立分析责任，原业务事实、
媒体和结果保持可读。完整检查或后置解码失败必须回滚模式、版本和恢复事实。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from purrcept_engine.domain.knowledge.models import EvidenceSnapshot
from purrcept_engine.domain.model_requests.models import RequestEvidence, RequestScope
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.serialization import encode


def old_database(path: Path) -> tuple[RequestEvidence, str]:
    """用冻结DDL和明确原件生成隔离的格式15样本。"""
    statements = json.loads(
        (Path(__file__).parent / "fixtures/schema_v15.json").read_text(encoding="utf-8")
    )
    request = RequestEvidence(
        "original",
        RequestScope("foreground", "turn"),
        "generation",
        "test",
        '{"payload":{}}',
        "original-compiler",
        1,
        10,
        10,
    )
    media = hashlib.sha256(b"original-media").hexdigest()
    snapshot = EvidenceSnapshot(
        evidence_id="fact",
        version=1,
        kind="test",
        text="原始事实。",
        source_json="{}",
        occurred_at=1,
    )
    with sqlite3.connect(path) as connection:
        for statement in statements:
            connection.execute(statement)
        connection.execute("PRAGMA application_id=0x50555245")
        connection.execute("PRAGMA user_version=15")
        connection.execute("INSERT INTO media VALUES(?,?)", (media, b"original-media"))
        connection.execute(
            "INSERT INTO model_requests VALUES(?,?,'succeeded',1,2)",
            (request.request_id, encode(request)),
        )
        connection.execute(
            "INSERT INTO knowledge_evidence VALUES('fact',1,?,1,1)", (snapshot.model_dump_json(),)
        )
        connection.execute("INSERT INTO pending_knowledge VALUES('fact',1,1,'pending',NULL)")
    return request, media


def dump(path: Path) -> tuple[str, ...]:
    """比较实际模式和记录，拒绝时不能留下新表或部分恢复写入。"""
    with sqlite3.connect(path) as connection:
        return tuple(connection.iterdump())


async def test_format15_preserves_business_facts_and_separates_analysis_responsibility(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine.sqlite3"
    request, media = old_database(path)
    store = await Store.open(path)
    try:
        actual = await store.get_request(request.request_id)
        assert actual is not None and actual.request == request
        assert await store.get_media(media) == b"original-media"
        fact = await store.knowledge.get_evidence("fact")
        assert fact is not None and fact.text == "原始事实。"
        for purpose in ("memory", "behavior"):
            assert (await store.feedback.reflection_state(purpose=purpose)).oldest_queued_at == 1
    finally:
        await store.close()
    reopened = await Store.open(path)
    await reopened.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (16,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "failure", ["missing_reference", "invalid_payload", "invalid_request_kind"]
)
async def test_failed_migration_rolls_back_schema_version_and_recovery(
    tmp_path: Path, failure: str
) -> None:
    path = tmp_path / "engine.sqlite3"
    old_database(path)
    with sqlite3.connect(path) as connection:
        if failure == "missing_reference":
            connection.execute("INSERT INTO model_request_media VALUES('original','missing')")
        elif failure == "invalid_request_kind":
            payload = json.loads(
                connection.execute("SELECT evidence_json FROM model_requests").fetchone()[0]
            )
            payload["kind"] = "invalid"
            connection.execute("UPDATE model_requests SET evidence_json=?", (json.dumps(payload),))
        else:
            connection.execute("UPDATE knowledge_evidence SET payload_json='{}'")
    before = dump(path)
    with pytest.raises(StorageError):
        await Store.open(path)
    assert dump(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (15,)


@pytest.mark.parametrize("version", [12, 13, 14, 999])
async def test_unsupported_format_is_preserved(tmp_path: Path, version: int) -> None:
    path = tmp_path / "engine.sqlite3"
    old_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version={version}")
    before = dump(path)
    with pytest.raises(StorageError) as failed:
        await Store.open(path)
    assert failed.value.code == "unsupported_format" and dump(path) == before
