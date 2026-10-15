"""真实临时SQLite验证摘要资格、CAS、媒体归档和安装事务回滚。

摘要响应通过同连接的正式请求账本保存，测试不以孤立假结果绕过权威提交。
原始历史、媒体和来源仅使用本次生成的副本，无用户数据或真实模型调用。
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from purrcept_core.models import (
    ConversationCheckpoint,
    ConversationState,
    ImageBlock,
    ImageBytes,
    Message,
    MessageRole,
    ModelContinuation,
    ModelResponse,
    ReminderState,
    SystemReminder,
    TextBlock,
)

from purrcept_engine.context.capacity import GenerationCapacity, assess_capacity
from purrcept_engine.context.checkpoints import (
    encode_checkpoint,
    intern_checkpoint,
    restore_checkpoint,
)
from purrcept_engine.context.maintenance import (
    SummaryError,
    SummaryProposal,
    SummarySource,
    build_summary_proposal,
    candidate_checkpoint,
    freeze_summary_source,
)
from purrcept_engine.domain.knowledge.models import EvidenceSnapshot
from purrcept_engine.domain.model_requests.models import (
    AttemptEvidence,
    AttemptStart,
    RequestEvidence,
    RequestScope,
)
from purrcept_engine.serialization import encode
from purrcept_engine.storage import knowledge, maintenance, model_requests, subject


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    database = sqlite3.connect(tmp_path / "maintenance.sqlite3")
    database.execute("PRAGMA foreign_keys=ON")
    database.execute("CREATE TABLE media(media_id TEXT PRIMARY KEY,content BLOB NOT NULL)")
    database.execute("CREATE TABLE inputs(input_id TEXT PRIMARY KEY)")
    for statement in (
        *model_requests.SCHEMA,
        *subject.SCHEMA,
        *knowledge.SCHEMA,
        *maintenance.SCHEMA,
    ):
        database.execute(statement)
    try:
        yield database
    finally:
        database.close()


def checkpoint() -> ConversationCheckpoint:
    """六个完整轮次含旧/近期不同媒体，提醒和continuation必须完整保留。"""
    messages: list[Message] = []
    boundaries: list[int] = []
    for index in range(6):
        content = (TextBlock("source:" + str(index) + "x" * 500),)
        if index in (0, 5):
            messages.append(Message.user("media"))
            messages.append(
                Message(
                    MessageRole.USER,
                    (*content, ImageBlock(ImageBytes(f"image-{index}".encode(), "image/png"))),
                    metadata={"evidence": ("input-source",)},
                )
            )
        else:
            messages.append(Message.user(content[0].text, metadata={"evidence": ("input-source",)}))
        messages.append(Message.assistant("complete response"))
        boundaries.append(len(messages))
    return ConversationCheckpoint(
        ConversationState(
            tuple(messages),
            turn_index=6,
            turn_boundaries=tuple(boundaries),
            continuation=ModelContinuation("provider", {"cursor": "opaque"}),
        ),
        ReminderState((SystemReminder((TextBlock("unchanged-tail"),)),)),
    )


def saved_source(connection: sqlite3.Connection, *, version: int = 3) -> SummarySource:
    original = checkpoint()
    knowledge.put_evidence(
        connection,
        EvidenceSnapshot(
            evidence_id="input-source",
            version=1,
            kind="input",
            text="long-term original",
            source_json="{}",
            occurred_at=1.0,
        ),
    )
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        stored, media = intern_checkpoint(connection, encode_checkpoint(original))
        connection.execute(
            "INSERT INTO subject_sessions VALUES('foreground',?,?,NULL)", (version, stored)
        )
        connection.executemany(
            "INSERT INTO session_media VALUES('foreground',?)", ((identity,) for identity in media)
        )
    frozen = freeze_summary_source(original, owner_id="foreground", source_version=version)
    assert frozen is not None
    return frozen


def assessment_payload(value: ConversationCheckpoint) -> dict[str, object]:
    """计量两侧都包含固定前缀和实时尾部，此测试不声称是真实媒体token计量。"""
    return {
        "model": "same-model",
        "max_tokens": 100,
        "messages": [
            {"role": "system", "content": "fixed " + "z" * 1200},
            *(
                {"role": message.role.value, "content": message.text}
                for message in value.history.messages
            ),
            {"role": "user", "content": "same-dynamic-tail"},
        ],
    }


def proposal(
    connection: sqlite3.Connection,
    source: SummarySource,
    *,
    request_id: str = "summary-request",
    state: str = "succeeded",
    owner: str = "foreground",
) -> SummaryProposal:
    summary = maintenance.summary_message(source, "实际摘要正文")
    candidate = candidate_checkpoint(source, summary)
    large = GenerationCapacity(100_000, 100, "reference-text-only-v1")
    candidate_size = assess_capacity(assessment_payload(candidate), large).estimate.input_units
    limits = GenerationCapacity(
        int(candidate_size / 0.6 / 0.9) + 100, 100, "reference-text-only-v1"
    )
    before = assess_capacity(assessment_payload(source.checkpoint), limits)
    after = assess_capacity(assessment_payload(candidate), limits)
    reference = "summary:" + source.coverage_id
    evidence = RequestEvidence(
        request_id,
        RequestScope(owner, reference, material_refs=(reference,)),
        "generation",
        "model",
        '{"payload":{"messages":[]}}',
        "test-compiler",
        1,
        120.0,
        180.0,
    )
    model_requests.begin_request(connection, evidence)
    start = AttemptStart(request_id, request_id + ":attempt", 1, time.monotonic() + 120)
    model_requests.begin_attempt(connection, start)
    model_requests.finish_attempt(
        connection,
        AttemptEvidence(
            start,
            "succeeded",
            encode(ModelResponse(Message.assistant('{"summary":"实际摘要正文"}'))),
            None,
            "[]",
            "[]",
            True,
            0,
        ),
    )
    model_requests.finish_request(
        connection, request_id, "succeeded" if state == "succeeded" else "failed"
    )
    return build_summary_proposal(
        source, summary, summary_request_id=request_id, before=before, after=after
    )


def test_atomic_install_keeps_complete_archive_and_source_and_candidate_media(
    connection: sqlite3.Connection,
) -> None:
    source = saved_source(connection)
    assert maintenance.reserve_summary(connection, source) == "reserved"
    candidate = proposal(connection, source)
    assert candidate.after.within_budget and not candidate.after.target_met
    assert maintenance.install_summary(connection, candidate) == 4
    current = subject.session(connection, "foreground")
    assert current.version == 4 and current.checkpoint_json is not None
    row = connection.execute(
        "SELECT source_checkpoint_json,candidate_checkpoint_json,summary_request_id "
        "FROM summary_attempts"
    ).fetchone()
    assert row is not None
    assert restore_checkpoint(connection, row[0]) == source.checkpoint
    assert restore_checkpoint(connection, row[1]) == candidate.candidate
    assert row[2] == candidate.summary_request_id
    assert candidate.candidate.history.turn_index == 2
    assert candidate.candidate.history.continuation == source.checkpoint.history.continuation
    assert candidate.candidate.reminders == source.checkpoint.reminders
    assert connection.execute("SELECT count(*) FROM summary_source_media").fetchone() == (2,)
    assert connection.execute("SELECT count(*) FROM summary_candidate_media").fetchone() == (1,)
    old_media = hashlib.sha256(b"image-0").hexdigest()
    assert connection.execute(
        "SELECT 1 FROM summary_source_media WHERE media_id=?", (old_media,)
    ).fetchone()
    with pytest.raises(sqlite3.IntegrityError), connection:
        connection.execute("DELETE FROM media WHERE media_id=?", (old_media,))


@pytest.mark.parametrize("change", ("stale", "busy"))
def test_reserve_and_install_version_and_idle_are_real_database_gates(
    connection: sqlite3.Connection, change: str
) -> None:
    source = saved_source(connection)
    with connection:
        if change == "stale":
            connection.execute("UPDATE subject_sessions SET version=4")
        else:
            connection.execute("UPDATE subject_sessions SET active_turn='active'")
    assert maintenance.reserve_summary(connection, source) == change
    assert connection.execute("SELECT count(*) FROM summary_attempts").fetchone() == (0,)
    with connection:
        connection.execute("UPDATE subject_sessions SET version=3,active_turn=NULL")
    assert maintenance.reserve_summary(connection, source) == "reserved"
    candidate = proposal(connection, source)
    with connection:
        if change == "stale":
            connection.execute("UPDATE subject_sessions SET version=4")
        else:
            connection.execute("UPDATE subject_sessions SET active_turn='active'")
    assert maintenance.install_summary(connection, candidate) is None
    assert connection.execute(
        "SELECT candidate_checkpoint_json FROM summary_attempts"
    ).fetchone() == (None,)
    assert subject.session(connection, "foreground").checkpoint_json == encode_checkpoint(
        source.checkpoint
    )


def test_failed_same_coverage_never_retries_after_version_and_tail_change(
    connection: sqlite3.Connection,
) -> None:
    source = saved_source(connection)
    assert maintenance.reserve_summary(connection, source) == "reserved"
    changed = replace(
        source.checkpoint,
        reminders=ReminderState((SystemReminder((TextBlock("different-tail"),)),)),
    )
    with connection:
        stored, _ = intern_checkpoint(connection, encode_checkpoint(changed))
        connection.execute("UPDATE subject_sessions SET version=9,checkpoint_json=?", (stored,))
    frozen = freeze_summary_source(changed, owner_id="foreground", source_version=9)
    assert frozen is not None and frozen.coverage_id == source.coverage_id
    maintenance.initialize(connection)
    assert maintenance.reserve_summary(connection, frozen) == "already_attempted"


@pytest.mark.parametrize(
    "field", ("checkpoint", "protection", "text", "request_owner", "request_state", "capacity")
)
def test_forged_or_failed_candidates_never_replace_committed_history(
    connection: sqlite3.Connection, field: str
) -> None:
    source = saved_source(connection)
    assert maintenance.reserve_summary(connection, source) == "reserved"
    candidate = proposal(
        connection,
        source,
        owner="reflection" if field == "request_owner" else "foreground",
        state="failed" if field == "request_state" else "succeeded",
    )
    if field == "checkpoint":
        candidate = replace(
            candidate, candidate=replace(candidate.candidate, reminders=ReminderState())
        )
    elif field == "protection":
        candidate = replace(candidate, source=replace(source, protected_turns=1))
    elif field == "text":
        fake = maintenance.summary_message(source, "不是实际响应正文")
        candidate = replace(candidate, summary=fake, candidate=candidate_checkpoint(source, fake))
    elif field == "capacity":
        candidate = replace(candidate, after=candidate.before)
    with pytest.raises(SummaryError):
        maintenance.install_summary(connection, candidate)
    assert subject.session(connection, "foreground").version == 3
    assert subject.session(connection, "foreground").checkpoint_json == encode_checkpoint(
        source.checkpoint
    )
    assert connection.execute(
        "SELECT candidate_checkpoint_json FROM summary_attempts"
    ).fetchone() == (None,)


def test_media_relation_failure_rolls_back_archive_candidate_session_and_version(
    connection: sqlite3.Connection,
) -> None:
    source = saved_source(connection)
    assert maintenance.reserve_summary(connection, source) == "reserved"
    candidate = proposal(connection, source)
    connection.execute(
        "CREATE TRIGGER reject_candidate_media BEFORE INSERT ON summary_candidate_media "
        "BEGIN SELECT RAISE(ABORT,'synthetic media relationship failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic media"):
        maintenance.install_summary(connection, candidate)
    assert subject.session(connection, "foreground").version == 3
    assert subject.session(connection, "foreground").checkpoint_json == encode_checkpoint(
        source.checkpoint
    )
    assert connection.execute(
        "SELECT candidate_checkpoint_json,summary_request_id FROM summary_attempts"
    ).fetchone() == (None, None)
    assert connection.execute("SELECT count(*) FROM session_media").fetchone() == (2,)
    assert connection.execute("SELECT count(*) FROM summary_source_media").fetchone() == (2,)


def test_revoked_exact_source_version_cannot_be_requalified_by_new_version(
    connection: sqlite3.Connection,
) -> None:
    source = saved_source(connection)
    assert maintenance.reserve_summary(connection, source) == "reserved"
    candidate = proposal(connection, source)
    knowledge.revoke_evidence(connection, "input-source", 1)
    knowledge.put_evidence(
        connection,
        EvidenceSnapshot(
            evidence_id="input-source",
            version=2,
            kind="input",
            text="newer fact",
            source_json="{}",
            occurred_at=2.0,
        ),
    )
    with pytest.raises(SummaryError, match="撤销"):
        maintenance.install_summary(connection, candidate)
    assert subject.session(connection, "foreground").version == 3


def test_revocation_before_reservation_cannot_borrow_newer_fact(
    connection: sqlite3.Connection,
) -> None:
    source = saved_source(connection)
    knowledge.revoke_evidence(connection, "input-source", 1)
    knowledge.put_evidence(
        connection,
        EvidenceSnapshot(
            evidence_id="input-source",
            version=2,
            kind="input",
            text="newer fact",
            source_json="{}",
            occurred_at=2.0,
        ),
    )
    with pytest.raises(SummaryError, match="失效"):
        maintenance.reserve_summary(connection, source)
    assert connection.execute("SELECT count(*) FROM summary_attempts").fetchone() == (0,)
    assert subject.session(connection, "foreground").version == 3


def test_failed_source_media_archive_relationship_does_not_consume_coverage(
    connection: sqlite3.Connection,
) -> None:
    source = saved_source(connection)
    connection.execute(
        "CREATE TRIGGER reject_source_media BEFORE INSERT ON summary_source_media "
        "BEGIN SELECT RAISE(ABORT,'synthetic source relationship failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic source"):
        maintenance.reserve_summary(connection, source)
    assert connection.execute("SELECT count(*) FROM summary_attempts").fetchone() == (0,)
    assert connection.execute("SELECT count(*) FROM summary_source_media").fetchone() == (0,)
    assert subject.session(connection, "foreground").checkpoint_json == encode_checkpoint(
        source.checkpoint
    )
