"""用真实临时库核验人工审查的因果材料、版本冻结和双消费者可靠交接。

不访问模型或设备；同一批的多条刺激、迟到回执、背压和重开均沿正式 Store
提交器执行，不能用 HTTP 成功冒充评价持久化或模型已经采用。
"""

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from purrcept_engine.domain.controls.models import ControlError
from purrcept_engine.domain.inputs.models import (
    InputContext,
    InputSource,
    InputSubmission,
    MediaAsset,
)
from purrcept_engine.domain.subject.human_feedback import HumanFeedback
from purrcept_engine.infrastructure.sqlite import feedback as feedback_sql
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.values import JsonObject, decode_object


async def admit(store: Store, identity: str, *, media: tuple[MediaAsset, ...] = ()) -> str:
    """接管具有不同来源身份的真实刺激，保留完整正文和媒体。"""
    now = datetime.now(UTC)
    result = await store.admit(
        InputSubmission(
            InputSource("test", "account", "room", "message", identity),
            "完整刺激：" + identity,
            now,
            now,
            media=media,
            context=InputContext(minimum_wake=True),
        )
    )
    return result.input_id


async def action(store: Store, identity: str = "action", *, input_id: str | None = None) -> None:
    """准备未知回执和已结束行动，以便验证后续澄清不覆盖旧评价。"""
    await store.begin_action(
        identity, catalog_version="catalog", source="await me.send()", input_id=input_id
    )
    await store.begin_call(
        "call-" + identity,
        action_id=identity,
        sequence=0,
        path="me.send",
        target_id="test",
        arguments_json='{"text":"完整发送正文"}',
    )
    await store.mark_call_started("call-" + identity)
    await store.record_call_result(
        "call-" + identity,
        state="unknown",
        result_json=None,
        error_type="Unconfirmed",
        error="尚未确认",
    )
    await store.finish_action(identity, state="completed", result_json=None, output="", error=None)


def revision(detail: JsonObject | None) -> str:
    """缺少材料必须使测试失败，不能补造一个可提交的审查版本。"""
    assert detail is not None and isinstance(detail["revision"], str)
    return detail["revision"]


async def test_review_contains_whole_turn_and_original_media_but_not_later_input(tmp_path: Path):
    """材料完整还原合批现场，持久评价只引用不可变刺激以免递归复制。"""
    store = await Store.open(tmp_path / "state.sqlite3")
    try:
        first = await admit(store, "first", media=(MediaAsset(b"original-image", "image/png"),))
        second = await admit(store, "second")
        turn = await store.begin_turn("turn", "foreground", 0, 1, (first, second))
        await action(store, input_id=first)
        await admit(store, "later")
        await store.finish_turn(turn, "cancelled", error_json='{"error_type":"CancelledError"}')
        detail = await store.human_feedback.detail("action")
        assert detail is not None and detail["turn_id"] == "turn"
        stimuli = detail["stimuli"]
        assert isinstance(stimuli, list)
        assert [s["input_id"] for s in stimuli if isinstance(s, dict)] == [first, second]
        original = await store.get_input(first)
        assert original is not None
        media_id = original.media[0].media_id
        assert await store.human_feedback.media("action", media_id) == (
            b"original-image",
            "image/png",
        )
        assert await store.human_feedback.media("missing", media_id) is None
        assert await store.human_feedback.media("action", "unrelated") is None
        assert await store.human_feedback.detail("missing") is None
        await store.human_feedback.submit(
            "whole-turn", "action", revision(detail), HumanFeedback(rating="bad")
        )
        evidence = await store.knowledge.get_evidence("human-feedback:whole-turn", version=1)
        assert evidence is not None
        frozen = decode_object(evidence.text.encode())["reviewed_context"]
        assert isinstance(frozen, dict)
        assert frozen["stimuli"] == [
            {"input_id": first, "evidence_ref": {"id": first, "version": 1}},
            {"input_id": second, "evidence_ref": {"id": second, "version": 1}},
        ]
        assert "完整刺激：first" not in evidence.text
        assert (await store.get_input(first)) == original
    finally:
        await store.close()


async def test_review_freezes_receipt_and_retransmission_survives_later_confirmation(
    tmp_path: Path,
):
    """迟到确认只改变当前审查版本，不能改写已保存评价的未知回执。"""
    store = await Store.open(tmp_path / "state.sqlite3")
    try:
        await action(store)
        old = revision(await store.human_feedback.detail("action"))
        value = HumanFeedback(rating="bad", comment="不要在对方忙碌时连续打扰，等待回应再说。")
        await store.human_feedback.submit("review-one", "action", old, value)
        await store.record_call_result(
            "call-action",
            state="confirmed",
            result_json='{"sent":true}',
            error_type=None,
            error=None,
        )
        await store.human_feedback.submit("review-one", "action", old, value)
        detail = await store.human_feedback.detail("action")
        assert revision(detail) != old
        assert detail is not None and isinstance(detail["feedback"], list)
        assert len(detail["feedback"]) == 1
        snapshot = await store.knowledge.get_evidence("human-feedback:review-one", version=1)
        assert snapshot is not None
        frozen = decode_object(snapshot.text.encode())["reviewed_context"]
        assert isinstance(frozen, dict) and isinstance(frozen["action"], dict)
        assert '"state":"unknown"' in snapshot.text
        assert '"sent":true' not in snapshot.text
        with pytest.raises(ControlError) as stale:
            await store.human_feedback.submit("stale", "action", old, value)
        assert stale.value.code == "stale_revision"
        with pytest.raises(ControlError) as conflict:
            await store.human_feedback.submit(
                "review-one", "action", old, HumanFeedback(rating="good")
            )
        assert conflict.value.code == "control_conflict"
        assert await store.knowledge.get_evidence("human-feedback:stale", version=1) is None
    finally:
        await store.close()


async def test_full_input_queue_retains_both_consumers_across_restart(tmp_path: Path):
    """主脑背压与重启不丢人工意见，也不能提前确认另一反思消费者。"""
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path, max_inputs=1)
    try:
        await action(store)
        occupied = await admit(store, "occupied")
        await store.human_feedback.submit(
            "review",
            "action",
            revision(await store.human_feedback.detail("action")),
            HumanFeedback(rating="bad", comment="先确认需求，再行动。"),
        )
        state = await store.feedback.reflection_state()
        sources = await store.feedback.reflection_sources(epoch=state.epoch)
        assert state.priority
        assert any(s.evidence_id == "human-feedback:review" and s.priority for s in sources)
        assert (await store.feedback.bridge(now=time.time())).blocked
    finally:
        await store.close()
    reopened = await Store.open(path, max_inputs=1)
    try:
        detail = await reopened.human_feedback.detail("action")
        assert detail is not None and isinstance(detail["feedback"], list)
        assert len(detail["feedback"]) == 1
        await reopened.complete_input(occupied)
        result = await reopened.feedback.bridge(now=time.time())
        assert not result.blocked and len(result.foreground_ids) == 1
        record = await reopened.get_input(result.foreground_ids[0])
        assert record is not None and record.context.minimum_wake
        assert decode_object(record.text.encode())["comment"] == "先确认需求，再行动。"
        assert not (await reopened.feedback.bridge(now=time.time())).foreground_ids
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "SELECT state FROM pending_knowledge WHERE evidence_id='human-feedback:review'"
            ).fetchone() == ("pending",)
            assert (
                connection.execute(
                    "SELECT 1 FROM pending_knowledge WHERE evidence_id=?", (record.input_id,)
                ).fetchone()
                is None
            )
    finally:
        await reopened.close()


async def test_outbox_failure_rolls_back_evidence_and_reflection_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """保存后置拒绝必须同时撤销原文、反思来源及前台投递。"""
    store = await Store.open(tmp_path / "state.sqlite3")
    original = feedback_sql.record_human_feedback

    def fail(
        connection: sqlite3.Connection, snapshot: feedback_sql.knowledge_models.EvidenceSnapshot
    ):
        """在两个责任均写入后拒绝，证明外层事务不会留下半份评价。"""
        original(connection, snapshot)
        raise ControlError("synthetic transaction rejection")

    try:
        await action(store)
        monkeypatch.setattr(feedback_sql, "record_human_feedback", fail)
        with pytest.raises(ControlError):
            await store.human_feedback.submit(
                "rollback",
                "action",
                revision(await store.human_feedback.detail("action")),
                HumanFeedback(rating="good"),
            )
        assert await store.knowledge.get_evidence("human-feedback:rollback", version=1) is None
        with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
            assert (
                connection.execute(
                    "SELECT 1 FROM feedback_sources WHERE evidence_id='human-feedback:rollback'"
                ).fetchone()
                is None
            )
            assert (
                connection.execute(
                    "SELECT 1 FROM feedback_deliveries WHERE kind='human_feedback'"
                ).fetchone()
                is None
            )
        assert store.authority_available
    finally:
        await store.close()


async def test_running_and_missing_actions_cannot_receive_feedback(tmp_path: Path):
    """尚未结束与不存在的行动都不能成为有效人工评价。"""
    store = await Store.open(tmp_path / "state.sqlite3")
    try:
        await store.begin_action("running", catalog_version="catalog", source="return None")
        with pytest.raises(ControlError) as running:
            await store.human_feedback.submit(
                "running-review",
                "running",
                revision(await store.human_feedback.detail("running")),
                HumanFeedback(rating="bad"),
            )
        assert running.value.code == "review_running"
        with pytest.raises(ControlError) as missing:
            await store.human_feedback.submit(
                "missing", "missing", "version", HumanFeedback(rating="good")
            )
        assert missing.value.code == "review_missing"
    finally:
        await store.close()


async def test_history_cursor_keeps_tied_timestamps_and_new_records_separate(tmp_path: Path):
    """同一时刻的行动不漏页，新插入记录不重复进入已经向前读取的历史。"""
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path)
    try:
        for index in range(23):
            await store.begin_action(
                f"a{index:02}", catalog_version="catalog", source="return None"
            )
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE actions SET created_at=10.0")
        first = await store.human_feedback.page()
        cursor = first["next_cursor"]
        assert isinstance(first["items"], list) and len(first["items"]) == 20
        assert isinstance(cursor, str)
        await store.begin_action("new", catalog_version="catalog", source="return None")
        second = await store.human_feedback.page(cursor)
        assert isinstance(second["items"], list) and len(second["items"]) == 3
        assert second["next_cursor"] is None
        with pytest.raises(ControlError):
            await store.human_feedback.page("absent")
    finally:
        await store.close()
