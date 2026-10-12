"""用真实隔离 SQLite 与受控本地驱动验证活动控制和累计，不模拟真实游戏。

驱动只记录明确受理，测试另外提交真实确认；没有确认不把 starting 写成 running。
可控单调时钟覆盖暂停、追加、崩溃末段与离线时段，避免靠长时间 sleep 测预算。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from purrcept_engine.errors import HostStateError, StorageError
from purrcept_engine.storage import activities as sql
from purrcept_engine.storage.activities import ActivityCommand, ActivityFeedback, ActivityRecord
from purrcept_engine.subject.activities import (
    ActivityDefinition,
    ActivityError,
    ActivityPort,
    ActivityService,
    DriverAcceptance,
    RecoveryObservation,
)


@dataclass(slots=True)
class _Time:
    elapsed: float = 100.0
    utc: float = 1000.0

    def monotonic(self) -> float:
        return self.elapsed

    def wall(self) -> float:
        return self.utc

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds
        self.utc += seconds


class _SqliteStore:
    """测试连接明确由测试拥有；生产接口交给 Host 的唯一 Store 线程实现。"""

    def __init__(self, path: Path, clock: _Time) -> None:
        fresh = not path.exists()
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA foreign_keys=ON")
        if fresh:
            with self.connection:
                for statement in sql.SCHEMA:
                    self.connection.execute(statement)
        self.clock = clock
        self.gate: asyncio.Event | None = None
        self.commit_entered = asyncio.Event()
        self.fail_commit = False
        self.accepted_events: dict[str, asyncio.Event] = {}

    def accepted_event(self, command_id: str) -> asyncio.Event:
        return self.accepted_events.setdefault(command_id, asyncio.Event())

    async def activity_records(self) -> tuple[ActivityRecord, ...]:
        return sql.activity_records(self.connection)

    async def get_activity(self, activity_id: str) -> ActivityRecord | None:
        return sql.get_activity(self.connection, activity_id)

    async def get_activity_command(self, command_id: str) -> ActivityCommand | None:
        return sql.get_activity_command(self.connection, command_id)

    async def get_activity_feedback(self, feedback_id: str) -> ActivityFeedback | None:
        return sql.get_activity_feedback(self.connection, feedback_id)

    async def recover_activities(self) -> tuple[ActivityRecord, ...]:
        return sql.recover_activities(self.connection, now=self.clock.wall())

    async def commit_activity(
        self,
        record: ActivityRecord,
        *,
        expected_version: int | None,
        command: ActivityCommand | None = None,
        feedback: ActivityFeedback | None = None,
    ) -> None:
        self.commit_entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_commit:
            raise StorageError("测试权威提交失败。")
        sql.commit_activity(
            self.connection,
            record,
            expected_version=expected_version,
            command=command,
            feedback=feedback,
        )
        if command is not None and command.status != "admitted":
            self.accepted_event(command.command_id).set()


class _Driver:
    def __init__(self, port: ActivityPort) -> None:
        self.port = port
        self.commands: list[ActivityCommand] = []
        self.entered = asyncio.Event()
        self.accept_gate: asyncio.Event | None = None
        self.recovery = RecoveryObservation(True, '{"actual":"stopped"}')
        self.closed = 0
        self.confirm_before_accept = False
        self.confirmed_in_accept = asyncio.Event()

    async def accept(self, command: ActivityCommand, port: ActivityPort) -> DriverAcceptance:
        assert port.activity_id == self.port.activity_id
        self.commands.append(command)
        self.entered.set()
        if self.confirm_before_accept:
            await self.confirm(command, "running")
            self.confirmed_in_accept.set()
        if self.accept_gate is not None and command.command in ("start", "resume", "adjust"):
            await self.accept_gate.wait()
        return DriverAcceptance(True)

    async def reconcile(self, record: ActivityRecord) -> RecoveryObservation:
        assert record.activity_id == self.port.activity_id
        return self.recovery

    async def close(self) -> None:
        self.closed += 1
        if self.accept_gate is not None:
            self.accept_gate.set()

    async def confirm(self, command: ActivityCommand, state: sql.ActivityState) -> ActivityCommand:
        return await self.port.confirm(
            command.command_id,
            run_generation=command.run_generation,
            version=command.version,
            state=state,
        )


class _Fixture:
    def __init__(
        self,
        path: Path,
        *,
        initial: float = 3600.0,
        maximum: float = 3600.0,
        max_active: int = 8,
        max_steps: int = 4 * 64,
        single_instance: bool = False,
        can_start: Callable[[], None] | None = None,
    ) -> None:
        self.clock = _Time()
        self.store = _SqliteStore(path, self.clock)
        self.drivers: list[_Driver] = []
        self.accept_gate: asyncio.Event | None = None
        self.confirm_before_accept = False
        self.driver_created = asyncio.Event()
        self.definition = ActivityDefinition("fixture", self.factory, single_instance)
        self.service = ActivityService(
            self.store,
            (self.definition,),
            initial_seconds=initial,
            max_remaining_seconds=maximum,
            max_active=max_active,
            max_steps=max_steps,
            can_start=can_start,
            monotonic=self.clock.monotonic,
            wall_time=self.clock.wall,
        )

    def factory(self, port: ActivityPort) -> _Driver:
        driver = _Driver(port)
        driver.accept_gate = self.accept_gate
        driver.confirm_before_accept = self.confirm_before_accept
        self.drivers.append(driver)
        self.driver_created.set()
        return driver

    async def start_running(self, command_id: str = "start") -> tuple[ActivityCommand, _Driver]:
        command = await self.service.command(
            command_id, "start", definition="fixture", goal="局部目标"
        )
        await self.accepted(command)
        driver = next(
            driver for driver in self.drivers if driver.port.activity_id == command.activity_id
        )
        await driver.confirm(command, "running")
        return command, driver

    async def accepted(self, command: ActivityCommand) -> None:
        async with asyncio.timeout(2):
            await self.store.accepted_event(command.command_id).wait()

    async def close(self) -> None:
        try:
            await self.service.close()
        finally:
            self.store.connection.close()


async def test_two_phase_start_and_stable_command_identity(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path / "activities.sqlite3")
    await fixture.service.start()
    try:
        command = await fixture.service.command(
            "one", "start", definition="fixture", goal="局部目标"
        )
        assert command.status == "admitted"
        assert command.activity_id.startswith("activity-")
        await fixture.accepted(command)
        assert (await fixture.service.command_record("one")).status == "accepted"
        assert (await fixture.service.activity(command.activity_id)).state == "starting"
        same = await fixture.service.command("one", "start", definition="fixture", goal="局部目标")
        assert same.activity_id == command.activity_id
        assert len(fixture.drivers) == 1 and len(fixture.drivers[0].commands) == 1
        with pytest.raises(ActivityError, match="不同参数"):
            await fixture.service.command("one", "start", definition="fixture", goal="另一目标")
        await fixture.drivers[0].confirm(command, "running")
        assert (await fixture.service.wait_command("one")).status == "confirmed"
        assert (await fixture.service.activity(command.activity_id)).state == "running"
    finally:
        await fixture.close()


async def test_cancelled_admission_wait_keeps_owned_command(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path / "activities.sqlite3")
    await fixture.service.start()
    fixture.store.gate = asyncio.Event()
    try:
        waiter = asyncio.create_task(
            fixture.service.command("one", "start", definition="fixture", goal="目标")
        )
        await fixture.store.commit_entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        fixture.store.gate.set()
        same = await fixture.service.command("one", "start", definition="fixture", goal="目标")
        await fixture.accepted(same)
        assert len(fixture.drivers) == 1
        assert (await fixture.service.command_record("one")).status == "accepted"
    finally:
        fixture.store.gate.set()
        await fixture.close()


async def test_pause_revokes_steps_before_real_confirmation_and_preserves_elapsed(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path / "activities.sqlite3", initial=100, maximum=100)
    await fixture.service.start()
    try:
        command, driver = await fixture.start_running()
        fixture.clock.advance(12)
        step = await fixture.service.begin_step(command.activity_id, command.run_generation)
        pause = await fixture.service.command("pause", "pause", activity_id=command.activity_id)
        assert (await fixture.service.activity(command.activity_id)).state == "pause_requested"
        with pytest.raises(ActivityError, match="资格"):
            await fixture.service.begin_step(command.activity_id, command.run_generation)
        fixture.clock.advance(3)
        await fixture.accepted(pause)
        await driver.confirm(pause, "paused")
        record = await fixture.service.activity(command.activity_id)
        assert record.used_seconds == 15 and record.remaining_seconds == 85
        with pytest.raises(ActivityError, match="在途"):
            await fixture.service.command(
                "extend",
                "extend",
                activity_id=command.activity_id,
                additional_seconds=10,
                reason="明确核对进展",
            )
        await step.finish()
        fixture.clock.advance(500)
        extension = await fixture.service.command(
            "extend",
            "extend",
            activity_id=command.activity_id,
            additional_seconds=10,
            reason="明确核对进展",
        )
        assert extension.status == "confirmed"
        assert (await fixture.service.activity(command.activity_id)).remaining_seconds == 95
        duplicate = await fixture.service.command(
            "extend",
            "extend",
            activity_id=command.activity_id,
            additional_seconds=10,
            reason="明确核对进展",
        )
        assert duplicate == extension
        resume = await fixture.service.command("resume", "resume", activity_id=command.activity_id)
        assert resume.run_generation == command.run_generation + 1
        await fixture.accepted(resume)
        await driver.confirm(resume, "running")
        fixture.clock.advance(4)
        await fixture.service.checkpoint()
        assert (await fixture.service.activity(command.activity_id)).used_seconds == 19
    finally:
        await fixture.close()


async def test_late_pause_does_not_overwrite_completion_or_old_resume_revive_end(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path / "activities.sqlite3")
    await fixture.service.start()
    try:
        command, driver = await fixture.start_running()
        pause = await fixture.service.command("pause", "pause", activity_id=command.activity_id)
        await fixture.accepted(pause)
        current = await driver.port.snapshot()
        await driver.port.feedback(
            "done",
            "completed",
            '{"result":"完成"}',
            run_generation=current.run_generation,
            version=current.version,
        )
        confirmation = await driver.confirm(pause, "paused")
        assert confirmation.applied is False
        assert (await driver.port.snapshot()).state == "completed"

        second, other = await fixture.start_running("second")
        pause2 = await fixture.service.command("pause2", "pause", activity_id=second.activity_id)
        await fixture.accepted(pause2)
        await other.confirm(pause2, "paused")
        resume = await fixture.service.command("resume", "resume", activity_id=second.activity_id)
        await fixture.accepted(resume)
        end = await fixture.service.command("end", "end", activity_id=second.activity_id)
        await fixture.accepted(end)
        await other.confirm(end, "ended")
        late = await other.confirm(resume, "running")
        assert late.applied is False
        assert (await other.port.snapshot()).state == "ended"
    finally:
        await fixture.close()


async def test_budget_exhaustion_and_extend_do_not_restart_driver(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path / "activities.sqlite3", initial=10, maximum=10)
    await fixture.service.start()
    try:
        command, driver = await fixture.start_running()
        fixture.clock.advance(11)
        with pytest.raises(ActivityError, match="耗尽"):
            await fixture.service.begin_step(command.activity_id, command.run_generation)
        current = await driver.port.snapshot()
        assert current.state == "budget_exhausted" and current.used_seconds == 11
        budget = await fixture.service.command_record(current.last_command_id)
        await fixture.accepted(budget)
        await driver.confirm(budget, "paused")
        before = len(driver.commands)
        await fixture.service.command(
            "extend",
            "extend",
            activity_id=command.activity_id,
            additional_seconds=10,
            reason="基于实际进展继续",
        )
        after = await driver.port.snapshot()
        assert after.state == "budget_exhausted" and after.used_seconds == 11
        assert after.remaining_seconds == 9
        assert len(driver.commands) == before
        with pytest.raises(ActivityError, match="上限"):
            await fixture.service.command(
                "too-much",
                "extend",
                activity_id=command.activity_id,
                additional_seconds=2,
                reason="超额",
            )
        notices = sql.pending_activity_feedback(fixture.store.connection, "foreground")
        assert len(notices) == 1 and notices[0].kind == "needs_decision"
    finally:
        await fixture.close()


async def test_restart_preserves_checkpoint_gap_commands_and_offline_balance(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path / "activities.sqlite3", initial=100, maximum=100)
    await fixture.service.start()
    try:
        command, driver = await fixture.start_running()
        fixture.clock.advance(7)
        await fixture.service.checkpoint()
        saved = await driver.port.snapshot()
        pause = await fixture.service.command(
            "unconfirmed-pause", "pause", activity_id=command.activity_id
        )
        await fixture.accepted(pause)
        # 复制最后成功持久化事实模拟崩溃，不让正常 close 的结算进入样本。
        copied_path = tmp_path / "restart.sqlite3"
        copy = sqlite3.connect(copied_path)
        fixture.store.connection.backup(copy)
        copy.close()
        fixture.clock.advance(500)
        restarted_store = _SqliteStore(copied_path, fixture.clock)
        rebuilt: list[_Driver] = []

        def factory(port: ActivityPort) -> _Driver:
            rebuilt.append(_Driver(port))
            return rebuilt[-1]

        restarted = ActivityService(
            restarted_store,
            (ActivityDefinition("fixture", factory),),
            max_steps=4 * 64,
            initial_seconds=100,
            max_remaining_seconds=100,
            monotonic=fixture.clock.monotonic,
            wall_time=fixture.clock.wall,
        )
        await restarted.start()
        try:
            recovered = await restarted.activity(command.activity_id)
            assert recovered.state == "interrupted" and recovered.needs_reconciliation
            assert recovered.used_seconds == saved.used_seconds == 7
            assert recovered.checkpoint_at == saved.checkpoint_at
            assert recovered.timing_incomplete and len(rebuilt) == 0
            assert (await restarted.command_record(pause.command_id)).status == "accepted"
            await restarted.command(
                "extend-after-crash",
                "extend",
                activity_id=command.activity_id,
                additional_seconds=5,
                reason="追加不解除核对",
            )
            with pytest.raises(ActivityError, match="核对"):
                await restarted.command(
                    "resume-before-check", "resume", activity_id=command.activity_id
                )
            assert len(rebuilt) == 0
            await restarted.reconcile(command.activity_id)
            assert len(rebuilt) == 1 and rebuilt[0].commands == []
            resume = await restarted.command(
                "resume-after-check", "resume", activity_id=command.activity_id
            )
            async with asyncio.timeout(2):
                await restarted_store.accepted_event(resume.command_id).wait()
            await rebuilt[0].confirm(resume, "running")
            assert (await restarted.activity(command.activity_id)).remaining_seconds == 98
        finally:
            await restarted.close()
            restarted_store.connection.close()
    finally:
        await fixture.close()


@pytest.mark.parametrize("single_instance", [False, True])
async def test_active_capacity_is_checked_before_factory(
    tmp_path: Path, single_instance: bool
) -> None:
    fixture = _Fixture(
        tmp_path / "activities.sqlite3", max_active=2, single_instance=single_instance
    )
    await fixture.service.start()
    try:
        await fixture.start_running("one")
        if not single_instance:
            await fixture.start_running("two")
        with pytest.raises(ActivityError, match="额度已满"):
            await fixture.service.command("excess", "start", definition="fixture", goal="目标")
        assert len(fixture.drivers) == (1 if single_instance else 2)
    finally:
        await fixture.close()


async def test_critical_feedback_has_two_independent_consumers_and_duplicate_is_stable(
    tmp_path: Path,
) -> None:
    fixture = _Fixture(tmp_path / "activities.sqlite3")
    await fixture.service.start()
    try:
        command, driver = await fixture.start_running()
        current = await driver.port.snapshot()
        notice = await driver.port.feedback(
            "critical",
            "needs_decision",
            '{"raw":"核对"}',
            run_generation=current.run_generation,
            version=current.version,
        )
        same = await driver.port.feedback(
            "critical",
            "needs_decision",
            '{"raw":"核对"}',
            run_generation=current.run_generation,
            version=current.version,
        )
        assert same == notice
        assert sql.pending_activity_feedback(fixture.store.connection, "foreground") == (notice,)
        sql.acknowledge_activity_feedback(fixture.store.connection, "critical", "foreground")
        assert sql.pending_activity_feedback(fixture.store.connection, "foreground") == ()
        assert sql.pending_activity_feedback(fixture.store.connection, "reflection") == (notice,)
        sql.acknowledge_activity_feedback(fixture.store.connection, "critical", "reflection")
        assert sql.get_activity_feedback(fixture.store.connection, "critical") == notice
        with pytest.raises(ActivityError, match="不同材料"):
            await driver.port.feedback(
                "critical",
                "needs_decision",
                '{"raw":"改写"}',
                run_generation=current.run_generation,
                version=current.version,
            )
        assert (await fixture.service.activity(command.activity_id)).state == "awaiting_decision"
    finally:
        await fixture.close()


async def test_close_during_step_commit_cannot_grant_a_new_step(tmp_path: Path) -> None:
    fixture = _Fixture(tmp_path / "activities.sqlite3")
    await fixture.service.start()
    command, _ = await fixture.start_running()
    fixture.store.commit_entered.clear()
    fixture.store.gate = asyncio.Event()
    step_task = asyncio.create_task(
        fixture.service.begin_step(command.activity_id, command.run_generation)
    )
    await fixture.store.commit_entered.wait()
    closing = asyncio.create_task(fixture.service.close())
    await asyncio.sleep(0)
    fixture.store.gate.set()
    try:
        with pytest.raises(HostStateError, match="关闭"):
            await step_task
        await closing
    finally:
        fixture.store.connection.close()


def test_storage_cas_rolls_back_conflicting_command_and_preserves_original(tmp_path: Path) -> None:
    store = _SqliteStore(tmp_path / "activities.sqlite3", _Time())
    try:
        record = ActivityRecord(
            "a",
            "fixture",
            "目标",
            "starting",
            1,
            1,
            3600,
            0,
            None,
            False,
            False,
            False,
            "c",
            "{}",
            None,
            1000,
            1000,
        )
        command = ActivityCommand(
            "c",
            "a",
            "start",
            "fixture",
            "目标",
            None,
            None,
            "explicit",
            "starting",
            1,
            1,
            "admitted",
            1000,
        )
        sql.commit_activity(store.connection, record, expected_version=None, command=command)
        with pytest.raises(StorageError, match="身份"):
            sql.commit_activity(
                store.connection,
                replace(record, version=2, goal="不可落盘"),
                expected_version=1,
                command=replace(command, goal="冲突"),
            )
        assert sql.get_activity(store.connection, "a") == record
        assert sql.get_activity_command(store.connection, "c") == command
        with pytest.raises(StorageError, match="版本"):
            sql.commit_activity(store.connection, replace(record, version=3), expected_version=2)
    finally:
        store.connection.close()


async def test_clock_checkpoint_does_not_invalidate_same_control_feedback(tmp_path: Path) -> None:
    """真实 SQLite 累计推进版本后，旧观察仍属同控制代次，当前余额不能回退。"""
    fixture = _Fixture(tmp_path / "activities.sqlite3", initial=100, maximum=100)
    await fixture.service.start()
    try:
        _, driver = await fixture.start_running()
        observed = await driver.port.snapshot()
        fixture.clock.advance(6)
        await fixture.service.checkpoint()
        latest = await driver.port.snapshot()
        assert latest.version > observed.version
        assert latest.control_version == observed.control_version
        progress = await driver.port.feedback(
            "same-control",
            "progress",
            '{"work":"当前进展"}',
            run_generation=observed.run_generation,
            version=observed.version,
        )
        current = await driver.port.snapshot()
        assert progress.applied is True and current.progress_json == '{"work":"当前进展"}'
        assert current.used_seconds == 6 and current.remaining_seconds == 94

        pause = await fixture.service.command(
            "new-control", "pause", activity_id=current.activity_id
        )
        await fixture.accepted(pause)
        stale = await driver.port.feedback(
            "previous-control",
            "completed",
            '{"result":"旧控制结果"}',
            run_generation=observed.run_generation,
            version=observed.version,
        )
        current = await driver.port.snapshot()
        assert stale.applied is False and current.state == "pause_requested"
        assert current.control_version == pause.version
        assert current.used_seconds == 6 and current.remaining_seconds == 94
        assert sql.pending_activity_feedback(fixture.store.connection, "foreground") == (stale,)
        await driver.confirm(pause, "paused")
    finally:
        await fixture.close()


async def test_confirmation_precedes_acceptance_without_late_overwrite(tmp_path: Path) -> None:
    """驱动在 accept 返回前真实确认，迟到受理不能把 confirmed 改回 accepted。"""
    fixture = _Fixture(tmp_path / "activities.sqlite3")
    fixture.accept_gate = asyncio.Event()
    fixture.confirm_before_accept = True
    await fixture.service.start()
    try:
        command = await fixture.service.command("early", "start", definition="fixture", goal="目标")
        async with asyncio.timeout(2):
            await fixture.driver_created.wait()
            driver = fixture.drivers[0]
            await driver.confirmed_in_accept.wait()
        confirmed = await fixture.service.command_record(command.command_id)
        assert confirmed.status == "confirmed" and confirmed.actual_state == "running"
        fixture.accept_gate.set()
        await fixture.service.close()
        assert await fixture.service.command_record(command.command_id) == confirmed
        assert driver.closed == 1 and fixture.service.pending_resources == 0
    finally:
        await fixture.close()


async def test_pause_control_bypasses_blocked_normal_accept(tmp_path: Path) -> None:
    """普通控制等待不会占暂停入口；旧启动确认仍不能越过较新的暂停。"""
    fixture = _Fixture(tmp_path / "activities.sqlite3")
    fixture.accept_gate = asyncio.Event()
    await fixture.service.start()
    try:
        start = await fixture.service.command("blocked", "start", definition="fixture", goal="目标")
        async with asyncio.timeout(2):
            await fixture.driver_created.wait()
            driver = fixture.drivers[0]
            await driver.entered.wait()
        pause = await fixture.service.command("priority", "pause", activity_id=start.activity_id)
        await fixture.accepted(pause)
        assert driver.commands[-1].command == "pause" and not fixture.accept_gate.is_set()
        await driver.confirm(pause, "paused")
        late = await driver.confirm(start, "running")
        assert late.applied is False and (await driver.port.snapshot()).state == "paused"
    finally:
        await fixture.close()


async def test_native_authority_failure_is_preserved_and_drivers_still_close(
    tmp_path: Path,
) -> None:
    """权威失败原型继续传播；失败记录不切内存，其他资源仍有收尾所有者。"""
    fixture = _Fixture(tmp_path / "activities.sqlite3")
    await fixture.service.start()
    command, driver = await fixture.start_running()
    fixture.store.fail_commit = True
    try:
        with pytest.raises(StorageError) as initial:
            await fixture.service.checkpoint()
        with pytest.raises(StorageError) as repeated:
            await fixture.service.begin_step(command.activity_id, command.run_generation)
        assert repeated.value is initial.value
        assert (await fixture.service.activity(command.activity_id)).used_seconds == 0
        with pytest.raises(ExceptionGroup) as closing:
            await fixture.service.close()
        assert initial.value in closing.value.exceptions
        assert driver.closed == 1
    finally:
        fixture.store.connection.close()


async def test_health_gate_changed_during_commit_prevents_step_but_allows_feedback(
    tmp_path: Path,
) -> None:
    """持久提交等待后仍核同步门槛；真实收尾反馈不借用新派发资格。"""
    failure: HostStateError | None = None

    def can_start() -> None:
        if failure is not None:
            raise failure

    fixture = _Fixture(tmp_path / "activities.sqlite3", can_start=can_start)
    await fixture.service.start()
    command, driver = await fixture.start_running()
    fixture.store.commit_entered.clear()
    fixture.store.gate = asyncio.Event()
    waiting = asyncio.create_task(
        fixture.service.begin_step(command.activity_id, command.run_generation)
    )
    await fixture.store.commit_entered.wait()
    failure = HostStateError("测试宿主已关闭新入口。")
    fixture.store.gate.set()
    try:
        with pytest.raises(HostStateError) as denied:
            await waiting
        assert denied.value is failure
        observed = await driver.port.snapshot()
        notice = await driver.port.feedback(
            "closing-result",
            "progress",
            '{"fact":"真实收尾"}',
            run_generation=observed.run_generation,
            version=observed.version,
        )
        assert notice.applied is True
        await driver.port.checkpoint(
            run_generation=observed.run_generation,
            version=(await driver.port.snapshot()).version,
            work_json='{"checkpoint":"实际已完成"}',
        )
    finally:
        await fixture.close()


async def test_step_capacity_covers_database_wait_and_actual_running_steps(tmp_path: Path) -> None:
    """未完成提交也占已有步骤界，满额不登记无限等待，真实归还后才释放。"""
    fixture = _Fixture(tmp_path / "activities.sqlite3", max_steps=1)
    await fixture.service.start()
    command, driver = await fixture.start_running()
    fixture.store.commit_entered.clear()
    fixture.store.gate = asyncio.Event()
    waiting = asyncio.create_task(driver.port.begin_step(run_generation=command.run_generation))
    await fixture.store.commit_entered.wait()
    step = None
    try:
        with pytest.raises(ActivityError) as before_commit:
            await driver.port.begin_step(run_generation=command.run_generation)
        assert before_commit.value.code == "activity_busy"
        fixture.store.gate.set()
        step = await waiting
        with pytest.raises(ActivityError) as running:
            await driver.port.begin_step(run_generation=command.run_generation)
        assert running.value.code == "activity_busy"
        current = await driver.port.snapshot()
        notice = await driver.port.feedback(
            "while-full",
            "progress",
            '{"actual":"still-running"}',
            run_generation=current.run_generation,
            version=current.version,
        )
        assert notice.applied
        await step.finish()
        replacement = await driver.port.begin_step(run_generation=command.run_generation)
        await replacement.finish()
    finally:
        fixture.store.gate.set()
        if step is not None:
            await step.finish()
        await fixture.close()
