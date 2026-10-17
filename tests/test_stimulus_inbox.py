"""验证提交与唤醒分离，以及唤醒快照和迟到判断的真实事务边界。"""

from pathlib import Path

from test_subject_storage import submission

from purrcept_engine.infrastructure.sqlite.store import Store


async def test_observe_survives_restart_and_next_wake_drains_all_submitted(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite3"
    store = await Store.open(path)
    first = await store.admit(submission("observed"))
    state = await store.input_processing(first.input_id)
    assert state is not None
    assert await store.submit_inputs((state,), minimum_wake=False)
    submitted = await store.input_processing(first.input_id)
    assert submitted is not None
    assert await store.decide_inputs((submitted,), "observe", "无需现在思考")
    assert not await store.take_foreground_inputs()
    await store.close()

    store = await Store.open(path)
    try:
        assert (await store.staged_inputs("submitted"))[0].input_id == first.input_id
        high = await store.admit(submission("direct", wake=True))
        priority = await store.input_processing(high.input_id)
        assert priority is not None
        assert await store.submit_inputs((priority,), minimum_wake=True)
        records = await store.take_foreground_inputs()
        assert tuple(item.input_id for item in records) == (first.input_id, high.input_id)
        assert not await store.take_foreground_inputs()
        later = await store.admit(submission("later"))
        assert not await store.decide_inputs((submitted,), "urgent", "迟到")
        assert (await store.staged_inputs("attention"))[0].input_id == later.input_id
    finally:
        await store.close()


async def test_unstarted_wake_recovers_all_claimed_inputs(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    store = await Store.open(path)
    one = await store.admit(submission("pending"))
    state = await store.input_processing(one.input_id)
    assert state is not None
    await store.submit_inputs((state,), minimum_wake=False)
    high = await store.admit(submission("high", wake=True))
    priority = await store.input_processing(high.input_id)
    assert priority is not None
    await store.submit_inputs((priority,), minimum_wake=True)
    assert len(await store.take_foreground_inputs()) == 2
    await store.close()
    store = await Store.open(path)
    try:
        assert len(await store.take_foreground_inputs()) == 2
    finally:
        await store.close()


async def test_unfinished_assessment_recovers_without_waking_observed_batches(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.sqlite3"
    store = await Store.open(path)
    try:
        observed = await store.admit(submission("observed"))
        expected = await store.input_processing(observed.input_id)
        assert expected is not None
        await store.submit_inputs((expected,), minimum_wake=False)
        submitted = await store.input_processing(observed.input_id)
        assert submitted is not None
        await store.decide_inputs((submitted,), "observe", "继续积累")
        pending = await store.admit(submission("pending"))
        expected = await store.input_processing(pending.input_id)
        assert expected is not None
        await store.submit_inputs((expected,), minimum_wake=False)
    finally:
        await store.close()
    store = await Store.open(path)
    try:
        assert tuple(item.input_id for item in await store.staged_inputs("submitted")) == (
            observed.input_id,
        )
        assert tuple(item.input_id for item in await store.staged_inputs("attention")) == (
            pending.input_id,
        )
        assert not await store.staged_inputs("assessing")
        assert not await store.take_foreground_inputs()
    finally:
        await store.close()
