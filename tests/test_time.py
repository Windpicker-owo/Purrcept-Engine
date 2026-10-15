"""隔离SQLite、可控墙钟与真实Host/Worker验证内置时钟及明确休息。

到期使用宿主的真实接管函数，故障触发器只能在本次生成库关闭后安装。没有
真实模型、账号、设备或迁移；时间依赖不存在时保持明确缺口，不伪造时区数据。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError

from purrcept_engine import EngineConfig, EngineHost
from purrcept_engine.domain.execution.providers import CallContext, CapabilityInputError
from purrcept_engine.domain.inputs.models import (
    Admission,
    InputRecord,
    InputSource,
    InputSubmission,
)
from purrcept_engine.domain.subject.models import InputProcessing
from purrcept_engine.domain.time.models import Alarm, AlarmFiring, TimeError
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.infrastructure.sqlite.time import next_daily_occurrence, parse_alarm_at
from purrcept_engine.plugin_system.contracts import PluginPorts
from purrcept_engine.plugins.rest import factory as bed_factory
from purrcept_engine.plugins.time import ClockRuntime, ClockSettings
from purrcept_engine.plugins.time import factory as clock_factory
from purrcept_engine.values import decode_object

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    database = await Store.open(tmp_path / "time.sqlite3")
    try:
        yield database
    finally:
        await database.close()


def alarm(
    *, at: datetime = NOW + timedelta(minutes=5), repeat: str = "once", clock_id: str = "clock"
) -> Alarm:
    return Alarm.model_validate(
        {
            "id": "alarm_" + uuid4().hex,
            "clock_id": clock_id,
            "at": at,
            "timezone": "UTC",
            "message": "事实提醒",
            "repeat": repeat,
            "occurrence_id": str(uuid4()),
        }
    )


@pytest.mark.parametrize(
    "text,expected",
    (
        ("11:05", NOW.replace(hour=11, minute=5)),
        ("10:00", NOW + timedelta(days=1)),
        ("9:59:30", (NOW + timedelta(days=1)).replace(hour=9, minute=59, second=30)),
        ("2026-10-14T06:00:00", datetime(2026, 10, 14, 6, 0, tzinfo=UTC)),
        ("2026-10-14T06:00:00+08:00", datetime(2026, 10, 13, 22, 0, tzinfo=UTC)),
    ),
)
def test_public_alarm_time_parsing_preserves_next_occurrence_and_explicit_offset(
    text: str, expected: datetime
) -> None:
    assert parse_alarm_at(text, timezone="UTC", now=NOW) == expected


@pytest.mark.parametrize("text", ("", "tomorrow", "2026-10-12T09:00:00Z", "25:00"))
def test_invalid_times_are_explicitly_rejected(text: str) -> None:
    with pytest.raises(TimeError):
        parse_alarm_at(text, timezone="UTC", now=NOW)


def test_daily_skips_multiple_days_but_keeps_wall_clock_time() -> None:
    at = NOW.replace(hour=8)
    future = next_daily_occurrence(at, timezone="UTC", now=NOW + timedelta(days=6))
    assert future == at + timedelta(days=7)


@pytest.mark.parametrize(
    "text,expected",
    (
        ("18:01", "2026-10-13T18:01:00+08:00"),
        ("18:00", "2026-10-14T18:00:00+08:00"),
        ("2026-10-14T06:00:00", "2026-10-14T06:00:00+08:00"),
        ("2026-10-14T06:00:00Z", "2026-10-14T14:00:00+08:00"),
    ),
)
def test_shanghai_uses_installed_iana_timezone_without_machine_defaults(
    text: str, expected: str
) -> None:
    assert ClockSettings().timezone == "Asia/Shanghai"
    assert parse_alarm_at(text, timezone="Asia/Shanghai", now=NOW).isoformat() == expected


@pytest.mark.asyncio
async def test_default_clock_read_is_full_shanghai_iso_and_timezone(store: Store) -> None:
    clock = ClockRuntime("clock", ClockSettings(), store.time, now=lambda: NOW)
    try:
        result = await clock.read()
        assert result == {
            "iso": "2026-10-13T18:00:00+08:00",
            "timezone": "Asia/Shanghai",
            "alarms": [],
        }
        assert clock_factory.prepare("clock", {}, Path.cwd()).required_modules == ("tzdata",)
        await clock.set_alarm("2026-10-13T19:00:00+08:00", "喝水", "once", "set")
        material = await clock.prepare_request()
        assert material["iso"] == result["iso"] and material["timezone"] == result["timezone"]
        assert len(str(material).split("喝水")) == 2
        assert "clock" not in material and "status" not in material
        assert "喝水" in str(clock.snapshot()["alarms"])
    finally:
        await clock.close()


@pytest.mark.asyncio
async def test_once_admission_consumption_and_occurrence_identity_survive_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "once.sqlite3"
    database = await Store.open(path)
    scheduled = alarm()
    try:
        await database.time.set_alarm(scheduled, NOW)
    finally:
        await database.close()
    reopened = await Store.open(path)
    try:
        await reopened.time.recover_daily("clock", scheduled.at + timedelta(hours=2))
        assert (await reopened.time.alarms("clock"))[0] == scheduled
        firing = await reopened.time.fire_alarm(
            "clock", scheduled.id, scheduled.occurrence_id, scheduled.at + timedelta(hours=2)
        )
        assert firing is not None and not await reopened.time.alarms("clock")
        pending = await reopened.pending()
        assert len(pending) == 1 and pending[0].input_id == firing.input_id
        payload = decode_object(pending[0].text.encode())
        assert payload["at"] == scheduled.at.isoformat() and payload["late_seconds"] == 7200.0
        assert pending[0].source.native_event_id == scheduled.occurrence_id
        assert pending[0].source.sender_id is None and pending[0].context.minimum_wake
        assert await reopened.knowledge.get_evidence(firing.input_id) is not None
    finally:
        await reopened.close()
    again = await Store.open(path)
    try:
        duplicate = await again.time.fire_alarm(
            "clock", scheduled.id, scheduled.occurrence_id, scheduled.at + timedelta(days=2)
        )
        assert duplicate == firing and len(await again.pending()) == 1
    finally:
        await again.close()


@pytest.mark.asyncio
async def test_daily_recovery_is_next_only_and_running_fire_advances_atomically(
    store: Store,
) -> None:
    scheduled = alarm(repeat="daily")
    await store.time.set_alarm(scheduled, NOW)
    late = NOW + timedelta(days=7)
    await store.time.recover_daily("clock", late)
    recovered = (await store.time.alarms("clock"))[0]
    assert recovered.at > late and recovered.occurrence_id != scheduled.occurrence_id
    assert not await store.pending()
    assert await store.time.fire_alarm("clock", scheduled.id, scheduled.occurrence_id, late) is None
    firing = await store.time.fire_alarm(
        "clock", recovered.id, recovered.occurrence_id, recovered.at
    )
    assert firing is not None and firing.next_at == recovered.at + timedelta(days=1)
    assert len(await store.pending()) == 1
    assert (await store.time.alarms("clock"))[0].at == firing.next_at


@pytest.mark.asyncio
async def test_alarm_limit_cancellation_and_clock_scope_do_not_corrupt_other_definitions(
    store: Store,
) -> None:
    definitions = [alarm() for _ in range(32)]
    for definition in definitions:
        await store.time.set_alarm(definition, NOW)
    with pytest.raises(TimeError, match="32"):
        await store.time.set_alarm(alarm(), NOW)
    with pytest.raises(TimeError):
        await store.time.set_alarm(definitions[0].model_copy(update={"clock_id": "another"}), NOW)
    cancelled = await store.time.cancel_alarm("clock", definitions[0].id)
    assert cancelled.state == "cancelled" and len(await store.time.alarms("clock")) == 31
    await store.time.set_alarm(alarm(), NOW)
    with pytest.raises(TimeError, match="不存在"):
        await store.time.cancel_alarm("another", definitions[1].id)
    assert len(await store.time.alarms("clock")) == 32


@pytest.mark.asyncio
async def test_atomic_firing_failure_rolls_back_inputs_evidence_and_consumption(
    tmp_path: Path,
) -> None:
    path = tmp_path / "failure.sqlite3"
    database = await Store.open(path)
    scheduled = alarm()
    await database.time.set_alarm(scheduled, NOW)
    await database.close()
    with sqlite3.connect(path) as fault:
        fault.execute(
            "CREATE TRIGGER reject_firing BEFORE INSERT ON alarm_firings "
            "BEGIN SELECT RAISE(ABORT,'synthetic firing failure'); END"
        )
    failed = await Store.open(path)
    try:
        with pytest.raises(StorageError):
            await failed.time.fire_alarm(
                "clock", scheduled.id, scheduled.occurrence_id, scheduled.at
            )
        assert not await failed.pending()
        assert (await failed.time.alarms("clock"))[0] == scheduled
        with pytest.raises(StorageError):
            await failed.time.cancel_alarm("clock", scheduled.id)
    finally:
        await failed.close()
    with sqlite3.connect(path) as inspect:
        assert inspect.execute("SELECT count(*) FROM knowledge_evidence").fetchone() == (0,)
        assert inspect.execute("SELECT count(*) FROM alarm_firings").fetchone() == (0,)


@pytest.mark.asyncio
async def test_rest_updates_only_intended_duration_keeps_started_at_and_never_auto_wakes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rest.sqlite3"
    database = await Store.open(path)
    first = await database.time.rest("bed", 480, NOW, "first")
    assert first.state is not None and not first.updated
    update = await database.time.rest("bed", 30, NOW + timedelta(minutes=20), "update")
    assert update.updated and update.state is not None
    assert update.state.started_at == first.state.started_at
    assert update.state.expected_duration_minutes == 30
    assert update.to_json()["elapsed_minutes"] == 20
    duplicate = await database.time.rest("bed", 30, NOW + timedelta(days=1), "update")
    assert duplicate == update
    with pytest.raises(TimeError, match="不同参数"):
        await database.time.rest("bed", 60, NOW, "update")
    await database.close()
    reopened = await Store.open(path)
    try:
        current = await reopened.time.get_rest()
        assert current is not None and current == update.state
        assert current.to_json(NOW + timedelta(days=10))["resting"]
        left = await reopened.time.leave(NOW + timedelta(days=10), "leave")
        assert left.changed and left.previous == current and left.state is None
        assert left.to_json()["resting"] is False
        again = await reopened.time.leave(NOW + timedelta(days=11), "awake")
        assert again.to_json() == {"resting": False, "changed": False}
    finally:
        await reopened.close()
    with sqlite3.connect(path) as inspect:
        assert inspect.execute("SELECT count(*) FROM rest_changes").fetchone() == (3,)


class Inputs:
    """插件只借用宿主正式接管入口，不取得SQLite或会话。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def submit(self, submission: InputSubmission) -> Admission:
        return await self.store.admit(submission)

    async def query_input(self, input_id: str) -> InputRecord | None:
        return await self.store.get_input(input_id)

    async def query_processing(self, input_id: str) -> InputProcessing | None:
        """保持输入端口真实处理查询，时钟本身不会消费此状态。"""
        return await self.store.input_processing(input_id)


@pytest.mark.asyncio
async def test_five_real_capability_contracts_keep_params_and_boundaries(
    store: Store, tmp_path: Path
) -> None:
    ports = PluginPorts(Inputs(store), lambda: {}, time=store.time)
    clock = await clock_factory.prepare("clock", {"timezone": "UTC"}, tmp_path).build(ports)
    bed = await bed_factory.prepare("bed", {}, tmp_path).build(ports)
    implementations = {
        item.declaration.name: item for item in (*clock.capabilities, *bed.capabilities)
    }
    context = CallContext("call", "action", asyncio.get_running_loop().time() + 5)
    try:
        read = await implementations["read"].invoke({}, context)
        assert isinstance(read.value, dict) and read.value["timezone"] == "UTC"
        assert isinstance(read.value["iso"], str) and read.value["alarms"] == []
        for invalid in (True, 0, 20161):
            with pytest.raises(CapabilityInputError):
                await implementations["rest"].invoke(
                    {"expected_duration_minutes": invalid}, context
                )
        assert not await store.time.get_rest()
        resting = await implementations["rest"].invoke({"expected_duration_minutes": 480}, context)
        assert isinstance(resting.value, dict) and resting.value["resting"]
        left = await implementations["leave"].invoke(
            {}, CallContext("leave", "action", context.deadline)
        )
        assert isinstance(left.value, dict) and left.value["changed"]
        message = " " + "x" * 500 + " "
        created = await implementations["set_alarm"].invoke(
            {"at": "23:59", "message": message}, context
        )
        assert isinstance(created.value, dict) and created.value["message"] == "x" * 500
        assert bed.prepare_request is not None
        assert (await bed.prepare_request())["rest"] == {"resting": False}
        with pytest.raises(ValidationError):
            clock_factory.prepare("clock", {"timezone": "UTC", "owner_id": "person"}, tmp_path)
    finally:
        assert clock.close is not None
        await clock.close()


@pytest.mark.asyncio
async def test_close_observation_cancel_keeps_current_admission_and_background_cleanup(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduled = alarm()
    port = store.time
    await port.set_alarm(scheduled, NOW)
    entered, release = asyncio.Event(), asyncio.Event()
    original = port.fire_alarm

    async def held(
        clock_id: str, alarm_id: str, occurrence_id: str, now: datetime
    ) -> AlarmFiring | None:
        entered.set()
        await release.wait()
        return await original(clock_id, alarm_id, occurrence_id, now)

    monkeypatch.setattr(port, "fire_alarm", held)
    clock = ClockRuntime("clock", ClockSettings(timezone="UTC"), port, now=lambda: scheduled.at)
    await clock.start()
    await entered.wait()
    closer = asyncio.create_task(clock.close())
    await asyncio.sleep(0)
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer
    assert not await store.pending()
    release.set()
    await clock.close()
    assert len(await store.pending()) == 1 and not await store.time.alarms("clock")


@pytest.mark.asyncio
async def test_capacity_rejection_keeps_alarm_then_real_admission_after_space_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = await Store.open(tmp_path / "capacity.sqlite3", max_inputs=1)
    scheduled = alarm()
    existing = await database.admit(
        InputSubmission(InputSource("test", "local", "local", "test"), "占位原文", NOW, NOW)
    )
    port = database.time
    await port.set_alarm(scheduled, NOW)
    attempted = asyncio.Event()
    original = port.fire_alarm
    attempts = 0

    async def observed(
        clock_id: str, alarm_id: str, occurrence_id: str, now: datetime
    ) -> AlarmFiring | None:
        nonlocal attempts
        attempts += 1
        try:
            return await original(clock_id, alarm_id, occurrence_id, now)
        finally:
            attempted.set()

    monkeypatch.setattr(port, "fire_alarm", observed)
    clock = ClockRuntime("clock", ClockSettings(timezone="UTC"), port, now=lambda: scheduled.at)
    try:
        await clock.start()
        await asyncio.wait_for(attempted.wait(), 1)
        await clock.stop_inputs()
        assert attempts == 1
        assert clock.snapshot()["failure"] == {
            "error_type": "StorageError",
            "code": "input_capacity",
            "state": "waiting_capacity",
        }
        assert await port.alarms("clock") == (scheduled,)
        assert len(await database.pending()) == 1
        await database.complete_input(existing.input_id)
        firing = await port.fire_alarm("clock", scheduled.id, scheduled.occurrence_id, scheduled.at)
        assert firing is not None and not await port.alarms("clock")
        assert (await database.pending())[0].input_id == firing.input_id
    finally:
        await clock.close()
        await database.close()


@pytest.mark.parametrize("table", ("clock_alarms", "subject_rest"))
@pytest.mark.asyncio
async def test_corrupt_persisted_index_is_reported_without_empty_state_replacement(
    tmp_path: Path, table: str
) -> None:
    path = tmp_path / "corrupt.sqlite3"
    database = await Store.open(path)
    await database.time.set_alarm(alarm(), NOW)
    await database.time.rest("bed", 30, NOW, "rest")
    await database.close()
    with sqlite3.connect(path) as corrupt:
        if table == "clock_alarms":
            corrupt.execute("UPDATE clock_alarms SET at_utc=0")
        else:
            corrupt.execute("UPDATE subject_rest SET version=100")
    reopened = await Store.open(path)
    try:
        with pytest.raises(StorageError, match="不一致"):
            if table == "clock_alarms":
                await reopened.time.alarms("clock")
            else:
                await reopened.time.get_rest()
    finally:
        await reopened.close()


def host_config(tmp_path: Path) -> EngineConfig:
    payload: dict[str, object] = {
        "host": {"data_dir": tmp_path},
        "console": False,
        "models": {
            "test": {
                "kind": "generation",
                "model": "openai/offline",
                "api_base": "http://127.0.0.1:9999/v1",
                "api_key": "synthetic",
                "context_window": 100_000,
                "max_output_tokens": 1000,
                "capacity_rule": "reference-text-only-v1",
            }
        },
        "subject": {"foreground": "test", "reflection": "test", "summary": "test"},
        "plugins": [
            {"instance_id": "clock", "entry": "clock", "config": {"timezone": "UTC"}},
            {"instance_id": "bed", "entry": "bed", "config": {}},
        ],
    }
    return EngineConfig.validate(payload, base_dir=tmp_path)


@pytest.mark.asyncio
async def test_actual_host_worker_paths_use_builtin_time_and_rest(tmp_path: Path) -> None:
    host = EngineHost(host_config(tmp_path / "host"))
    await host.start()
    try:
        action = await host.execute(
            "from world import me\nclock = await me.clock.read()\n"
            "alarm = await me.clock.set_alarm(at='23:59', message='隔离提醒')\n"
            "cancelled = await me.clock.cancel_alarm(alarm_id=alarm['id'])\n"
            "rest = await me.bed.rest(expected_duration_minutes=30)\nleave = await me.bed.leave()\n"
            "return {'clock':clock,'alarm':alarm,'cancelled':cancelled,'rest':rest,'leave':leave}"
        )
        assert action.state == "completed" and action.result_json is not None
        assert len(action.calls) == 5 and all(call.state == "confirmed" for call in action.calls)
        value = decode_object(action.result_json.encode())
        assert isinstance(value["clock"], dict) and value["clock"]["timezone"] == "UTC"
        assert isinstance(value["cancelled"], dict) and value["cancelled"]["cancelled"]
        assert isinstance(value["rest"], dict) and value["rest"]["resting"]
        assert isinstance(value["leave"], dict) and value["leave"]["resting"] is False
    finally:
        await host.close()
