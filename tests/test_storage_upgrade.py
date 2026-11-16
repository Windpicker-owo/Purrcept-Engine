"""验证冻结格式16的原子迁移，以及不再支持的旧格式拒绝边界。

DDL 样本独立于当前模式。升级保留模型正文及行为评审，删除旧认识与记忆整理责任，原业务事实、
媒体和结果保持可读。完整检查或后置解码失败必须回滚模式、版本和恢复事实。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from purrcept_engine.domain.evidence.models import (
    BehaviorConclusion,
    EvidenceSlice,
    EvidenceSnapshot,
    FinishSubmission,
    ObservationScope,
    ReflectionBatch,
    ReflectionReceipt,
    Review,
    SupervisionNotice,
)
from purrcept_engine.domain.model_requests.models import RequestEvidence, RequestScope
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite import archive
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.serialization import encode


def old_database(path: Path) -> tuple[RequestEvidence, str]:
    """用冻结DDL和明确原件生成隔离的格式16样本。"""
    statements = json.loads(
        (Path(__file__).parent / "fixtures/schema_v16.json").read_text(encoding="utf-8")
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
        connection.execute("PRAGMA user_version=16")
        connection.execute("INSERT INTO media VALUES(?,?)", (media, b"original-media"))
        archive.put(
            connection,
            "request",
            request.request_id,
            request.materials_json or "",
            encode(request),
            1.0,
        )
        connection.execute(
            "INSERT INTO model_requests VALUES(?,?,'succeeded',1,2)",
            (request.request_id, encode(request)),
        )
        connection.execute(
            "INSERT INTO knowledge_evidence VALUES('fact',1,?,1,1)", (snapshot.model_dump_json(),)
        )
        fragment = EvidenceSlice(
            slice_ref="old-source", evidence_id="fact", version=1, start=0, end=len(snapshot.text)
        )
        connection.execute(
            "INSERT INTO knowledge_slices VALUES('old-source','fact',1,?)",
            (fragment.model_dump_json(),),
        )
        patch = {"id": "old-memory", "content": "旧认识。", "sources": ["old-source"]}
        connection.execute(
            "INSERT INTO memories VALUES('old-memory',1,?,1,NULL)", (json.dumps(patch),)
        )
        connection.execute("INSERT INTO memory_sources VALUES('old-memory',1,'old-source')")
        connection.execute(
            "INSERT INTO knowledge_cursors VALUES('old-memory-cursor','old-memory',1,0,8000,"
            "'memory')"
        )
        conclusion = BehaviorConclusion(
            decision="no_intervention", conclusion="保留行为评审。", evidence=(fragment.slice_ref,)
        )
        receipt = ReflectionReceipt(
            batch_id="kept-batch",
            version=1,
            completed_at=1.0,
            submission=FinishSubmission(
                reviews=(
                    Review(
                        issue="已核对",
                        evidence=(fragment.slice_ref,),
                        conclusion="无需干预。",
                        reason="原文支持。",
                    ),
                ),
                deferred=(),
                findings=(),
                behavior=conclusion,
            ),
        )
        legacy = receipt.model_dump(mode="json")
        legacy["written"] = []
        legacy["submission"]["abandoned_writes"] = []
        legacy["submission"]["reviews"][0]["memory_decision"] = "no_change"
        batch = ReflectionBatch(
            batch_id="kept-batch",
            version=1,
            required_slice_refs=(fragment.slice_ref,),
            purpose="behavior",
            state="completed",
        )
        payload = batch.model_dump(mode="json")
        payload["receipt"] = legacy
        connection.execute(
            "INSERT INTO reflection_batches VALUES('kept-batch',1,?,'completed',?)",
            (json.dumps(payload), json.dumps(legacy)),
        )
        connection.execute(
            "INSERT INTO behavior_reviews VALUES('kept-batch',1,?,1)",
            (conclusion.model_dump_json(),),
        )
        notice = SupervisionNotice(
            id="kept-notice",
            issue="真实意见",
            suggestion="核对再行动。",
            evidence=(fragment.slice_ref,),
            scope=ObservationScope(adapter_id="test"),
            expected_change="核对实际事实",
        )
        connection.execute(
            "INSERT INTO knowledge_supervision VALUES('kept-notice',?,'open',NULL,1)",
            (notice.model_dump_json(),),
        )
        connection.execute("INSERT INTO pending_knowledge VALUES('fact',1,1,'pending',NULL)")
        for purpose in ("memory", "behavior"):
            connection.execute(
                "INSERT INTO analysis_work(purpose,evidence_id,version,state,queued_at) "
                "VALUES(?,'fact',1,'pending',1)",
                (purpose,),
            )
    return request, media


def dump(path: Path) -> tuple[str, ...]:
    """比较实际模式和记录，拒绝时不能留下新表或部分恢复写入。"""
    with sqlite3.connect(path) as connection:
        return tuple(connection.iterdump())


async def test_format16_preserves_business_facts_and_separates_analysis_responsibility(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine.sqlite3"
    request, media = old_database(path)
    store = await Store.open(path)
    try:
        actual = await store.get_request(request.request_id)
        assert actual is not None and actual.request == request
        assert await store.get_media(media) == b"original-media"
        fact = await store.evidence.get_evidence("fact")
        assert fact is not None and fact.text == "原始事实。"
        kept = await store.evidence.get_reflection("kept-batch", 1)
        assert kept.receipt is not None and kept.receipt.submission.behavior is not None
        assert kept.receipt.submission.behavior.conclusion == "保留行为评审。"
        assert {item.notice.id for item in await store.evidence.pending_supervision()} == {
            "kept-notice"
        }
        for purpose in ("behavior",):
            assert (await store.feedback.reflection_state(purpose=purpose)).oldest_queued_at == 1
    finally:
        await store.close()
    assert path.with_name(path.name + ".v16-backup").exists()
    reopened = await Store.open(path)
    await reopened.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (17,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not {"memories", "memory_sources", "memory_conflicts", "pending_knowledge"} & tables
        assert (
            connection.execute("SELECT 1 FROM knowledge_cursors WHERE kind='memory'").fetchone()
            is None
        )
        assert connection.execute("SELECT purpose FROM analysis_work").fetchall() == [("behavior",)]


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
        assert connection.execute("PRAGMA user_version").fetchone() == (16,)


@pytest.mark.parametrize("version", [12, 13, 14, 15, 999])
async def test_unsupported_format_is_preserved(tmp_path: Path, version: int) -> None:
    path = tmp_path / "engine.sqlite3"
    old_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version={version}")
    before = dump(path)
    with pytest.raises(StorageError) as failed:
        await Store.open(path)
    assert failed.value.code == "unsupported_format" and dump(path) == before
