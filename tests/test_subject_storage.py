"""以隔离权威库验证筛选交接、失败恢复和会话安装的真实事务。"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from purrcept_core.models import ConversationCheckpoint, ConversationState, Message, ReminderState

from purrcept_engine.domain.context.checkpoints import decode_checkpoint, encode_checkpoint
from purrcept_engine.domain.inputs.models import InputContext, InputSource, InputSubmission
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite.store import Store


def submission(native_id: str, *, wake: bool = False, text: str = "原始材料") -> InputSubmission:
    """独立来源身份保持可重放，接收时间变化不改变发生事实。"""
    now = datetime(2026, 10, 13, tzinfo=UTC)
    return InputSubmission(
        InputSource("test", "account", "room", "message", native_id),
        text,
        now,
        now,
        context=InputContext(minimum_wake=wake),
    )


@pytest.mark.asyncio
async def test_wake_snapshot_never_takes_unsubmitted_or_later_inputs(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    try:
        cached = await store.admit(submission("cached"))
        high = await store.admit(submission("high", wake=True))
        priority = await store.input_processing(high.input_id)
        assert priority is not None
        await store.submit_inputs((priority,), minimum_wake=True)
        records = await store.take_foreground_inputs()
        assert tuple(record.input_id for record in records) == (high.input_id,)
        assert not await store.take_foreground_inputs()
        turn = await store.begin_turn("turn", "foreground", 0, 1, (high.input_id,))
        later = await store.admit(submission("later"))
        await store.finish_turn(turn, "cancelled", error_json='{"error_type":"CancelledError"}')
        assert (await store.failed_turns("foreground"))[0].input_ids == (high.input_id,)
        assert {record.input_id for record in await store.pending()} == {
            cached.input_id,
            later.input_id,
        }
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_complete_turn_installs_checkpoint_and_input_state_together(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "engine.sqlite3")
    admitted = await store.admit(submission("one", wake=True))
    turn = await store.begin_turn("turn", "foreground", 0, 1, (admitted.input_id,))
    checkpoint = ConversationCheckpoint(
        ConversationState(
            (Message.user("原始材料"), Message.assistant("内部结果")),
            turn_index=1,
            turn_boundaries=(2,),
        ),
        ReminderState(),
    )
    committed = await store.finish_turn(
        turn, "succeeded", checkpoint_json=encode_checkpoint(checkpoint)
    )
    assert committed.version == 1 and committed.active_turn is None
    assert committed.checkpoint_json is not None
    assert decode_checkpoint(committed.checkpoint_json) == checkpoint
    assert await store.pending() == ()
    item = await store.input_processing(admitted.input_id)
    assert item is not None and item.stage == "processed"
    original = await store.get_input(admitted.input_id)
    assert original is not None and original.text == "原始材料"
    with pytest.raises(StorageError, match="版本"):
        await store.finish_turn(turn, "failed", error_json='{"error_type":"late"}')
    assert (await store.get_session("foreground")) == committed
    await store.close()


@pytest.mark.asyncio
async def test_restart_retains_failed_experience_without_replaying_original(tmp_path: Path) -> None:
    path = tmp_path / "engine.sqlite3"
    store = await Store.open(path)
    admitted = await store.admit(submission("one", wake=True))
    await store.begin_turn("crashed", "foreground", 0, 1, (admitted.input_id,))
    await store.close()

    store = await Store.open(path)
    failures = await store.failed_turns("foreground")
    assert len(failures) == 1 and failures[0].state == "interrupted"
    assert failures[0].input_ids == (admitted.input_id,)
    assert await store.staged_inputs("foreground") == ()
    assert (await store.get_session("foreground")).checkpoint_json is None
    duplicate = await store.admit(submission("one", wake=True))
    assert duplicate.duplicate and duplicate.input_id == admitted.input_id
    assert await store.pending() == ()
    await store.close()


@pytest.mark.asyncio
async def test_source_context_is_deduplicated_as_fact_and_text_cannot_wake(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "engine.sqlite3", max_inputs=1)
    item = submission("one", text="停止，现在马上打断")
    admitted = await store.admit(item)
    processing = await store.input_processing(admitted.input_id)
    assert processing is not None and processing.stage == "attention"
    with pytest.raises(StorageError) as conflict:
        await store.admit(submission("one", wake=True, text=item.text))
    assert conflict.value.code == "source_conflict"
    assert (await store.get_input(admitted.input_id)) is not None
    await store.close()
