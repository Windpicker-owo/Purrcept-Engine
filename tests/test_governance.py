"""用真实临时库验证保留策略、预算、独立复核及无行动轮次的跨层契约。

所有材料为合成数据；不读取运行配置、真实数据库或设备。迁移基线来自修复前
冻结的格式15 DDL，不能由新模式反向生成。时间推进只替换本次被测存储模块。
"""

from __future__ import annotations

import base64
import io
import json
import sqlite3
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from purrcept_core.models import (
    ConversationCheckpoint,
    ConversationState,
    Message,
    ModelResponse,
    ReminderState,
)

from purrcept_engine.domain.context.checkpoints import encode_checkpoint
from purrcept_engine.domain.context.usage import UsageAnchor
from purrcept_engine.domain.inputs.models import InputContext, InputSource, InputSubmission
from purrcept_engine.domain.knowledge.models import (
    BehaviorConclusion,
    EvidenceSnapshot,
    FinishSubmission,
    KnowledgeInputError,
    ObservationAssessment,
    ObservationScope,
    Review,
    SupervisionNotice,
)
from purrcept_engine.domain.model_requests.models import (
    AttemptEvidence,
    AttemptStart,
    BackgroundBudgetExceeded,
    RequestEvidence,
    RequestScope,
)
from purrcept_engine.domain.subject.human_feedback import HumanFeedback
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite import analysis_work, archive, projections
from purrcept_engine.infrastructure.sqlite.maintenance_cli import maintain
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.serialization import encode


async def test_retry_schedule_and_only_related_evidence_can_reopen_exhausted_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """三次失败持久退避，第三次后不空转；无关新事实不能借机刷新预算。"""
    clock = [1_800_000_000.0]
    monkeypatch.setattr(analysis_work.time, "time", lambda: clock[0])
    store = await Store.open(tmp_path / "retry.sqlite3")
    try:
        await store.knowledge.put_evidence(
            EvidenceSnapshot(
                evidence_id="original",
                version=1,
                kind="test",
                text="明确原始证据。",
                source_json='{"action_id":"actual-action"}',
                occurred_at=clock[0],
            )
        )
        page = await store.knowledge.read_evidence("original")
        assert page.slice is not None
        for attempt, wait in enumerate((60, 300, None), start=1):
            state = await store.feedback.reflection_state()
            batch_id = f"retry:{attempt}"
            assert await store.feedback.reserve_reflection(
                batch_id=batch_id,
                epoch=state.epoch,
                pages=(page,),
                successful_turns=0,
            )
            batch = await store.knowledge.begin_reflection(batch_id, (page.slice.slice_ref,))
            await store.knowledge.abandon_reflection(batch_id, batch.version, "ModelTimeoutError")
            state = await store.feedback.reflection_state()
            assert state.oldest_queued_at is None
            if wait is not None:
                assert state.next_attempt_at == clock[0] + wait
                clock[0] += wait
            else:
                assert state.waiting_evidence_count == 1 and state.next_attempt_at is None
        await store.knowledge.put_evidence(
            EvidenceSnapshot(
                evidence_id="unrelated",
                version=1,
                kind="test",
                text="另一个事件。",
                source_json='{"action_id":"other-action"}',
                occurred_at=clock[0],
            )
        )
        state = await store.feedback.reflection_state()
        sources = await store.feedback.reflection_sources(epoch=state.epoch)
        assert {source.evidence_id for source in sources} == {"unrelated"}
        await store.knowledge.put_evidence(
            EvidenceSnapshot(
                evidence_id="follow-up",
                version=1,
                kind="test",
                text="同次行动的新增回执。",
                source_json='{"action_id":"actual-action"}',
                occurred_at=clock[0],
            )
        )
        state = await store.feedback.reflection_state()
        sources = await store.feedback.reflection_sources(epoch=state.epoch)
        assert {source.evidence_id for source in sources} == {"original", "unrelated", "follow-up"}
        assert state.waiting_evidence_count == 0
    finally:
        await store.close()


async def _request(
    store: Store, identity: str = "request", scope: str = "foreground"
) -> tuple[RequestEvidence, AttemptEvidence]:
    """写入一项有完整模型材料、响应、参数事件和已提交终态的合成调用。"""
    request = RequestEvidence(
        identity,
        RequestScope(scope, identity),
        "generation",
        "test",
        '{"payload":{"messages":[{"role":"user","content":"' + "x" * 2048 + '"}]}}',
        "compiler",
        1,
        10,
        10,
    )
    await store.begin_request(request)
    start = AttemptStart(identity, identity + ":attempt", 1, time.monotonic() + 10)
    await store.begin_attempt(start)
    result = AttemptEvidence(
        start,
        "succeeded",
        encode(ModelResponse(Message.assistant("ok"))),
        None,
        "[]",
        "[]",
        True,
        0,
    )
    await store.finish_attempt(result)
    await store.finish_request(identity, "succeeded")
    return request, result


async def test_expired_bodies_keep_digest_identity_usage_and_business_records(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine.sqlite3"
    store = await Store.open(path)
    try:
        request, result = await _request(store)
        cleared = await store.expire_model_bodies(
            before=time.time() + 1, now=time.time() + 2, max_bytes=1
        )
        assert cleared["expired_bodies"] == 3
        record = await store.get_request(request.request_id)
        assert record is not None and record.request.materials_json is None
        assert record.materials_status is not None and not record.materials_status.available
        assert record.materials_status.reason == "retention_expired"
        assert record.materials_status.summary is not None
        assert "1 项消息" in record.materials_status.summary
        attempt = record.attempts[0].result
        assert (
            attempt is not None
            and attempt.response_json is None
            and attempt.argument_events_json is None
        )
        assert attempt.state == "succeeded" and attempt.body_status is not None
        await store.begin_request(request)
        await store.finish_attempt(result)
        with pytest.raises(StorageError):
            await store.begin_request(replace(request, materials_json='{"payload":{}}'))
    finally:
        await store.close()
    summary = maintain(tmp_path, "db-stats")
    assert summary["quick_check"] == "ok" and summary["reusable_bytes"] is not None
    maintain(tmp_path, "db-vacuum")


async def test_usage_anchor_survives_without_pinning_diagnostic_bodies(tmp_path: Path) -> None:
    """容量锚点只依赖永久指纹，引用成功请求不能永久保留完整诊断副本。"""
    store = await Store.open(tmp_path / "engine.sqlite3")
    try:
        request, _ = await _request(store)
        anchor = UsageAnchor(request.request_id, "compiler", "a" * 64, "messages", ("b" * 64,), 42)
        await store.put_usage_anchor("foreground", anchor)

        cleared = await store.expire_model_bodies(
            before=time.time() + 1, now=time.time() + 2, max_bytes=1
        )
        assert cleared["expired_bodies"] == 3
        assert await store.get_usage_anchor("foreground") == anchor
        record = await store.get_request(request.request_id)
        assert record is not None and record.request.materials_json is None
    finally:
        await store.close()


async def test_running_request_survives_capacity_cleanup(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "engine.sqlite3")
    try:
        request = RequestEvidence(
            "running",
            RequestScope("foreground", "turn"),
            "generation",
            "test",
            '{"payload":{}}',
            "compiler",
            1,
            10,
            10,
        )
        await store.begin_request(request)
        result = await store.expire_model_bodies(
            before=time.time() + 1, now=time.time() + 2, max_bytes=1
        )
        assert result["expired_bodies"] == 0 and result["over_capacity"] is True
        actual = await store.get_request("running")
        assert actual is not None and actual.request.materials_json == request.materials_json
    finally:
        await store.close()


async def test_hourly_attempt_budget_survives_restart_and_does_not_fault_store(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine.sqlite3"
    store = await Store.open(path)
    for number in range(32):
        await _request(store, str(number), "memory_consolidation")
    await store.close()
    reopened = await Store.open(path)
    try:
        request = RequestEvidence(
            "overflow",
            RequestScope("behavior_review", "turn"),
            "generation",
            "test",
            '{"payload":{}}',
            "compiler",
            1,
            10,
            10,
        )
        await reopened.begin_request(request)
        with pytest.raises(BackgroundBudgetExceeded):
            await reopened.begin_attempt(
                AttemptStart("overflow", "overflow:attempt", 1, time.monotonic() + 10)
            )
        assert reopened.authority_available
        assert (await reopened.feedback.reflection_state()).budget_remaining == 0
    finally:
        await reopened.close()


async def test_behavior_completion_does_not_consume_memory_responsibility(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "engine.sqlite3")
    try:
        await store.knowledge.put_evidence(
            EvidenceSnapshot(
                evidence_id="fact",
                version=1,
                kind="test",
                source_json="{}",
                text="普通互动无需纠偏。",
                occurred_at=time.time(),
            )
        )
        page = await store.knowledge.read_evidence("fact")
        assert page.slice is not None
        reference = page.slice.slice_ref
        state = await store.feedback.reflection_state(purpose="behavior")
        assert await store.feedback.reserve_reflection(
            batch_id="review",
            epoch=state.epoch,
            pages=(page,),
            successful_turns=0,
            purpose="behavior",
        )
        batch = await store.knowledge.begin_reflection("review", (reference,), purpose="behavior")
        await store.knowledge.mark_presented(batch.batch_id, batch.version, (reference,))
        with pytest.raises(KnowledgeInputError, match="独立来源"):
            await store.knowledge.supervise(
                batch.batch_id,
                batch.version,
                SupervisionNotice(
                    id="bad-generalization",
                    issue="一次表现",
                    suggestion="永久改变",
                    evidence=(reference,),
                    generalized=True,
                    scope=ObservationScope(adapter_id="test"),
                    expected_change="后续表现",
                ),
            )
        conclusion = BehaviorConclusion(
            decision="no_intervention", conclusion="无需干预。", evidence=(reference,)
        )
        await store.knowledge.finish_reflection(
            batch.batch_id,
            batch.version,
            FinishSubmission(
                reviews=(
                    Review(
                        issue="检查",
                        evidence=(reference,),
                        conclusion="无需干预。",
                        memory_decision="no_change",
                        reason="没有问题。",
                    ),
                ),
                deferred=(),
                findings=(),
                behavior=conclusion,
            ),
        )
        assert (await store.feedback.reflection_state(purpose="behavior")).oldest_queued_at is None
        assert (
            await store.feedback.reflection_state(purpose="memory")
        ).oldest_queued_at is not None
        assert len(await store.knowledge.pending_reflection()) == 1
    finally:
        await store.close()


async def test_format15_migration_splits_bodies_and_preserves_exact_request(tmp_path: Path) -> None:
    path = tmp_path / "engine.sqlite3"
    statements = json.loads(
        (Path(__file__).parent / "fixtures/schema_v15.json").read_text(encoding="utf-8")
    )
    from purrcept_engine.infrastructure.sqlite import model_requests

    with sqlite3.connect(path) as connection:
        for statement in statements:
            connection.execute(statement)
        connection.execute("PRAGMA application_id=1347768901")
        connection.execute("PRAGMA user_version=15")
        request = RequestEvidence(
            "old",
            RequestScope("foreground", "old-turn"),
            "generation",
            "test",
            '{"payload":{}}',
            "old-compiler",
            1,
            10,
            10,
        )
        connection.execute(
            "INSERT INTO model_requests VALUES(?,?,'succeeded',?,?)", ("old", encode(request), 1, 2)
        )
    store = await Store.open(path)
    try:
        actual = await store.get_request("old")
        assert actual is not None and actual.request == request
        await store.begin_request(request)
    finally:
        await store.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (16,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            model_requests.decode_request(
                connection.execute("SELECT evidence_json FROM model_requests").fetchone()[0]
            ).materials_json
            is None
        )


async def test_panel_projection_does_not_restore_checkpoint_or_read_media(tmp_path: Path) -> None:
    path = tmp_path / "engine.sqlite3"
    store = await Store.open(path)
    await store.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO subject_sessions VALUES('foreground',7,'not-a-checkpoint',NULL)"
        )
        connection.execute("INSERT INTO media VALUES('unreferenced',?)", (b"x" * 1024 * 1024,))
        connection.commit()
        trace: list[str] = []
        connection.set_trace_callback(trace.append)
        result = projections.runtime(connection)
        assert result["sessions"] is not None
        assert not any("checkpoint_json" in sql or "FROM media" in sql for sql in trace)
        assert archive.collect_media(connection) == 1


async def test_normal_reopen_skips_full_database_audits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """格式16启动只核验结构与恢复资格；完整审计由维护命令独立执行。"""
    path = tmp_path / "reopen.sqlite3"
    store = await Store.open(path)
    await store.close()
    original = sqlite3.connect
    trace: list[str] = []

    def traced(database: Path) -> sqlite3.Connection:
        connection = original(database)
        connection.set_trace_callback(trace.append)
        return connection

    monkeypatch.setattr("purrcept_engine.infrastructure.sqlite.queue.sqlite3.connect", traced)
    reopened = await Store.open(path)
    await reopened.close()
    assert trace
    assert not any(
        "quick_check" in statement or "foreign_key_check" in statement for statement in trace
    )


async def test_no_action_turn_can_receive_reliable_human_feedback(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "engine.sqlite3")
    try:
        turn = await store.begin_turn("omission", "foreground", 0, 1, ())
        await store.finish_turn(turn, "failed", error_json='{"reason":"synthetic"}')
        detail = await store.human_feedback.detail("turn:omission")
        assert detail is not None and detail["action"] is None and detail["actions"] == []
        revision = detail["revision"]
        assert isinstance(revision, str)
        await store.human_feedback.submit(
            "review-omission",
            "turn:omission",
            revision,
            HumanFeedback(rating="bad", comment="应当回应却没有回应。"),
        )
        updated = await store.human_feedback.detail("turn:omission")
        assert updated is not None
        history = updated["feedback"]
        assert isinstance(history, list) and len(history) == 1
    finally:
        await store.close()


async def test_suggestion_dedup_and_observation_end_do_not_claim_adoption_is_improvement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """接受建议后仍为样本不足；真实相关轮次耗尽机会，过期与复发结论单独表达。"""
    clock = [time.time()]
    monkeypatch.setattr(analysis_work.time, "time", lambda: clock[0])
    store = await Store.open(tmp_path / "observation.sqlite3")
    try:
        await store.knowledge.put_evidence(
            EvidenceSnapshot(
                evidence_id="fact",
                version=1,
                kind="test",
                text="一次有实际原文的问题。",
                source_json="{}",
                occurred_at=clock[0],
            )
        )
        page = await store.knowledge.read_evidence("fact")
        assert page.slice is not None
        reference = page.slice.slice_ref
        batch = await store.knowledge.begin_reflection("review", (reference,), purpose="behavior")
        await store.knowledge.mark_presented(batch.batch_id, batch.version, (reference,))
        notice = SupervisionNotice(
            id="notice",
            issue="局部问题",
            suggestion="下次先核对。",
            evidence=(reference,),
            scope=ObservationScope(adapter_id="synthetic"),
            expected_change="先核对实际结果。",
        )
        first = await store.knowledge.supervise(batch.batch_id, batch.version, notice)
        duplicate = await store.knowledge.supervise(
            batch.batch_id,
            batch.version,
            notice.model_copy(update={"id": "duplicate"}),
        )
        assert duplicate.duplicate and duplicate.notice.id == first.notice.id
        await store.knowledge.respond_supervision("notice", "accept", "接受局部建议。")
        initial = (await store.runtime_projection())["supervision_observations"]
        assert isinstance(initial, list) and isinstance(initial[0], dict)
        assert (
            initial[0]["adoption"] == "accept" and initial[0]["outcome"] == "insufficient_samples"
        )
        checkpoint = encode_checkpoint(
            ConversationCheckpoint(
                ConversationState(
                    (Message.user("合成刺激"), Message.assistant("未行动")),
                    turn_index=1,
                    turn_boundaries=(2,),
                ),
                ReminderState(),
            )
        )
        for index in range(21):
            clock[0] += 1
            now = datetime.now(UTC)
            admission = await store.admit(
                InputSubmission(
                    InputSource(
                        "synthetic", "local", "local", "message", native_event_id=str(index)
                    ),
                    "合成相关机会",
                    now,
                    now,
                    context=InputContext(minimum_wake=True),
                )
            )
            previous = await store.get_session_state("foreground")
            turn = await store.begin_turn(
                f"observation:{index}",
                "foreground",
                previous.version,
                index + 1,
                (admission.input_id,),
            )
            await store.finish_turn(turn, "succeeded", checkpoint_json=checkpoint)
            if index < 4:
                observed = await store.knowledge.read_evidence(
                    f"behavior-observation:notice:{turn.turn_id}"
                )
                assert observed.slice is not None
                observed_ref = observed.slice.slice_ref
                review = await store.knowledge.begin_reflection(
                    f"review:{index}", (observed_ref,), purpose="behavior"
                )
                await store.knowledge.mark_presented(
                    review.batch_id, review.version, (observed_ref,)
                )
                await store.knowledge.finish_reflection(
                    review.batch_id,
                    review.version,
                    FinishSubmission(
                        reviews=(
                            Review(
                                issue="后续观察",
                                evidence=(observed_ref,),
                                conclusion="核对实际机会。",
                                memory_decision="no_change",
                                reason="只记录行为结论。",
                            ),
                        ),
                        deferred=(),
                        findings=(),
                        behavior=BehaviorConclusion(
                            decision="no_intervention",
                            conclusion="登记有据观察。",
                            evidence=(observed_ref,),
                            observations=(
                                ObservationAssessment(
                                    notice_id="notice",
                                    turn_id=turn.turn_id,
                                    outcome="improved" if index < 3 else "recurred",
                                    evidence=(observed_ref,),
                                ),
                            ),
                        ),
                    ),
                )
                current = (await store.runtime_projection())["supervision_observations"]
                assert isinstance(current, list) and isinstance(current[0], dict)
                if index == 2:
                    assert current[0]["outcome"] == "improved"
                elif index == 3:
                    assert current[0]["outcome"] == "recurred"
        observations = (await store.runtime_projection())["supervision_observations"]
        assert isinstance(observations, list) and isinstance(observations[0], dict)
        assert observations[0]["opportunities"] == 20
        assert observations[0]["observation_state"] == "opportunities_exhausted"
        assert observations[0]["outcome"] == "recurred"
        pending = await store.feedback.reflection_state(purpose="behavior")
        assert pending.oldest_queued_at is not None
        # 新的有效期限与旧建议独立；不把先前已结束的观察窗口重新延长。
        newer = await store.knowledge.supervise(
            batch.batch_id,
            batch.version,
            notice.model_copy(update={"id": "new-window"}),
        )
        assert not newer.duplicate
        clock[0] += 7 * 86400 + 1
        observations = (await store.runtime_projection())["supervision_observations"]
        assert isinstance(observations, list) and isinstance(observations[0], dict)
        assert observations[0]["observation_state"] == "expired"
        assert observations[0]["outcome"] == "insufficient_samples"
    finally:
        await store.close()


async def test_continuous_human_feedback_keeps_an_ordinary_source_seat(tmp_path: Path) -> None:
    """持续新评价仍优先进入下一批，同时每批推进一份普通来源。"""
    store = await Store.open(tmp_path / "priority.sqlite3")
    try:
        for index in range(5):
            await store.knowledge.put_evidence(
                EvidenceSnapshot(
                    evidence_id=f"ordinary:{index}",
                    version=1,
                    kind="test",
                    text="普通待复核事实。",
                    source_json="{}",
                    occurred_at=time.time(),
                )
            )
        turn = await store.begin_turn("review-target", "foreground", 0, 1, ())
        await store.finish_turn(turn, "failed", error_json='{"reason":"synthetic"}')
        detail = await store.human_feedback.detail("turn:review-target")
        assert detail is not None and isinstance(detail["revision"], str)
        for cycle in range(3):
            for index in range(35):
                await store.human_feedback.submit(
                    f"grade-{cycle}-{index}",
                    "turn:review-target",
                    detail["revision"],
                    HumanFeedback(rating="bad", comment="合成评价，请核对遗漏回应。"),
                )
            state = await store.feedback.reflection_state()
            sources = await store.feedback.reflection_sources(epoch=state.epoch)
            assert len(sources) == 32 and sources[0].evidence_id.startswith("human-feedback:")
            assert sum(source.evidence_id.startswith("ordinary:") for source in sources) == 1
            pages = tuple(
                [await store.knowledge.read_evidence(source.evidence_id) for source in sources]
            )
            references = tuple(page.slice.slice_ref for page in pages if page.slice is not None)
            batch_id = f"priority:{cycle}"
            assert await store.feedback.reserve_reflection(
                batch_id=batch_id,
                epoch=state.epoch,
                pages=pages,
                successful_turns=0,
            )
            batch = await store.knowledge.begin_reflection(batch_id, references)
            await store.knowledge.mark_presented(batch_id, batch.version, references)
            await store.knowledge.finish_reflection(
                batch_id,
                batch.version,
                FinishSubmission(
                    reviews=(
                        Review(
                            issue="实际复核",
                            evidence=references,
                            conclusion="保留原始意见和事实。",
                            memory_decision="no_change",
                            reason="没有长期泛化依据。",
                        ),
                    ),
                    deferred=(),
                    findings=(),
                ),
            )
        pending = await store.knowledge.pending_reflection()
        assert sum(source.evidence_id.startswith("ordinary:") for source in pending) == 2
    finally:
        await store.close()


async def test_diagnostic_media_is_counted_before_deduplication_and_collected_after_expiry(
    tmp_path: Path,
) -> None:
    """媒体指针不能绕过正文容量；没有业务引用的诊断媒体随原文淘汰回收。"""
    with Image.new("RGB", (256, 256), (12, 34, 56)) as image, io.BytesIO() as output:
        image.save(output, format="PNG", compress_level=0)
        image_data = output.getvalue()
    materials = encode(
        {
            "payload": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64,"
                                    + base64.b64encode(image_data).decode()
                                },
                            }
                        ],
                    }
                ]
            }
        }
    )
    request = RequestEvidence(
        "media-budget",
        RequestScope("foreground", "synthetic"),
        "generation",
        "test",
        materials,
        "compiler",
        1,
        10,
        10,
    )
    store = await Store.open(tmp_path / "media-budget.sqlite3")
    try:
        await store.begin_request(request)
        await store.finish_request(request.request_id, "cancelled")
        result = await store.get_request(request.request_id)
        assert result is not None and result.materials_status is not None
        assert result.materials_status.original_bytes == len(materials.encode("utf-8"))
        await store.expire_model_bodies(before=time.time() - 1, now=time.time(), max_bytes=2000)
        expired = await store.get_request(request.request_id)
        assert expired is not None and expired.request.materials_json is None
        assert await store.collect_unused_media() == 1
    finally:
        await store.close()


async def test_media_gc_byte_budget_makes_progress_without_a_large_delete_transaction(
    tmp_path: Path,
) -> None:
    """超大单项可以独占一批，但同批不能继续删除其他大 BLOB。"""
    import hashlib
    from contextlib import closing

    path = tmp_path / "gc.sqlite3"
    store = await Store.open(path)
    await store.close()
    with closing(sqlite3.connect(path)) as connection, connection:
        for index in range(3):
            content = bytes((index,)) * 2048
            connection.execute(
                "INSERT INTO media VALUES(?,?)", (hashlib.sha256(content).hexdigest(), content)
            )
        connection.commit()
        assert [archive.collect_media(connection, byte_budget=1024) for _ in range(4)] == [
            1,
            1,
            1,
            0,
        ]
