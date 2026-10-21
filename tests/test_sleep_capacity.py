"""用小额度真实 SQLite 重现睡眠积压堵住高优先输入，不连接真实账号或模型。

接管、休息和旧库重开均使用生产事务；原文、媒体及证据必须继续可读取，
不能靠删除历史、扩大额度或把高优先待办一并结案来恢复唤醒。
"""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from purrcept_engine.domain.inputs.models import (
    InputContext,
    InputSource,
    InputSubmission,
    MediaAsset,
)
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite.store import Store, input_logical_bytes


def _input(identity: str, *, priority: bool = False) -> InputSubmission:
    now = datetime.now(UTC)
    return InputSubmission(
        InputSource("test", "account", "group", "message", identity),
        "睡眠背景消息：" + identity,
        now,
        now,
        context=InputContext(minimum_wake=priority),
    )


@pytest.mark.parametrize("quota", ["count", "bytes"])
async def test_full_sleep_backlog_does_not_reject_direct_wake(tmp_path: Path, quota: str) -> None:
    """上床前已经占满的普通输入，也须能由正常事务释放接管额度。"""
    ordinary = replace(_input("old-background"), text="x" * 2048)
    priority = _input("direct", priority=True)
    limit = max(input_logical_bytes(ordinary), input_logical_bytes(priority))
    store = await Store.open(
        tmp_path / "sleep.sqlite3",
        max_inputs=1 if quota == "count" else 4,
        max_input_bytes=limit if quota == "bytes" else 4096,
    )
    try:
        old = await store.admit(ordinary)
        await store.time.rest("bed", 60, datetime.now(UTC), "rest")
        wake = await store.admit(priority)
        state = await store.input_processing(wake.input_id)
        assert state is not None and state.stage == "priority"
        assert await store.submit_inputs((state,), minimum_wake=True)
        taken = await store.take_foreground_inputs()
        assert wake.input_id in {record.input_id for record in taken}
        saved = await store.get_input(old.input_id)
        assert saved is not None and saved.text == ordinary.text
        replay = await store.admit(replace(ordinary, received_at=datetime.now(UTC)))
        assert replay.duplicate and replay.input_id == old.input_id
    finally:
        await store.close()


async def test_sleep_flood_is_recorded_with_media_and_evidence_without_pending_quota(
    tmp_path: Path,
) -> None:
    """累计逻辑字节远超额度，仍不把睡眠历史伪装成待处理工作或模型已看内容。"""
    store = await Store.open(tmp_path / "sleep.sqlite3", max_inputs=2, max_input_bytes=4096)
    try:
        await store.time.rest("bed", 60, datetime.now(UTC), "rest")
        original = replace(_input("media"), media=(MediaAsset(b"same" * 500, "image/png"),))
        identities: list[str] = []
        for index in range(20):
            admitted = await store.admit(
                replace(
                    original, source=replace(original.source, native_event_id=f"background-{index}")
                )
            )
            identities.append(admitted.input_id)
            processing = await store.input_processing(admitted.input_id)
            assert processing is not None and processing.stage == "processed"
            assert processing.reason == "subject.sleeping.recorded"
        assert await store.pending() == ()
        for identity in (identities[0], identities[-1]):
            record = await store.get_input(identity)
            assert (
                record is not None and record.text == original.text and record.state == "processed"
            )
            assert await store.get_media(record.media[0].media_id) == original.media[0].content
            evidence = await store.knowledge.get_evidence(identity)
            assert evidence is not None and evidence.text == original.text
        assert len(await store.knowledge.pending_reflection(limit=32)) == 20
        direct = await store.admit(_input("wake", priority=True))
        assert [record.input_id for record in await store.pending()] == [direct.input_id]
        oversized = replace(
            original,
            source=replace(original.source, native_event_id="too-large"),
            media=(MediaAsset(b"x" * 4097, "image/png"),),
        )
        with pytest.raises(StorageError) as error:
            await store.admit(oversized)
        assert error.value.code == "input_capacity"
    finally:
        await store.close()


@pytest.mark.parametrize("recovery", ["admission", "coordinator", "take"])
async def test_old_submitted_sleep_backlog_recovers_after_reopen(
    tmp_path: Path,
    recovery: str,
) -> None:
    """模拟旧版本满额度的 submitted 行，无迁移或人工清库也能恢复接管。"""
    path = tmp_path / "old-sleep.sqlite3"
    store = await Store.open(path, max_inputs=1)
    original = _input("old-version")
    old = await store.admit(original)
    expected = await store.input_processing(old.input_id)
    assert expected is not None and await store.submit_inputs((expected,), minimum_wake=False)
    expected = await store.input_processing(old.input_id)
    assert expected is not None and await store.decide_inputs((expected,), "observe", "old-observe")
    await store.time.rest("bed", 60, datetime.now(UTC), "rest")
    await store.close()
    reopened = await Store.open(path, max_inputs=1)
    try:
        if recovery == "coordinator":
            assert await reopened.apply_sleep_policy("owner") is not None
        elif recovery == "take":
            assert await reopened.take_foreground_inputs() == ()
        priority = await reopened.admit(_input("direct", priority=True))
        saved = await reopened.get_input(old.input_id)
        processing = await reopened.input_processing(old.input_id)
        assert saved is not None and saved.text == original.text
        assert processing is not None and processing.reason == "subject.sleeping.recorded"
        assert processing.stage == "processed"
        assert [record.input_id for record in await reopened.pending()] == [priority.input_id]
    finally:
        await reopened.close()


async def test_sleep_does_not_dispose_claimed_or_trusted_priority_work(tmp_path: Path) -> None:
    """普通历史可以记录，但受保护工作占满时仍有界拒收，不伪造全局容量豁免。"""
    store = await Store.open(tmp_path / "sleep.sqlite3", max_inputs=2)
    try:
        claimed = await store.admit(_input("claimed", priority=True))
        state = await store.input_processing(claimed.input_id)
        assert state is not None and await store.submit_inputs((state,), minimum_wake=True)
        assert len(await store.take_foreground_inputs()) == 1
        priority = await store.admit(_input("pending-priority", priority=True))
        await store.time.rest("bed", 60, datetime.now(UTC), "rest")
        await store.apply_sleep_policy("owner")
        ordinary = await store.admit(_input("sleep-history"))
        for identity, expected in (
            (claimed.input_id, "claimed"),
            (priority.input_id, "priority"),
            (ordinary.input_id, "processed"),
        ):
            state = await store.input_processing(identity)
            assert state is not None and state.stage == expected
        with pytest.raises(StorageError) as error:
            await store.admit(_input("too-many-priority", priority=True))
        assert error.value.code == "input_capacity"
    finally:
        await store.close()
