"""实际活动步骤经共享执行器/唯一 Store 的资格、回执与资源寿命验证。

本地 Provider 只对测试生成的数据产生可重放效果。暂停竞争和权威失败使用
真实短事务前后的可控等待，不替换执行器或绕过 Pydantic/Journal 派发。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from purrcept_engine import EngineConfig, EngineHost
from purrcept_engine.app.config import ExecutionSettings
from purrcept_engine.errors import StorageError
from purrcept_engine.execution.catalog import CapabilityDeclaration, Catalog, ObjectDeclaration
from purrcept_engine.execution.providers import CallContext, CallOutcome, FunctionCapability
from purrcept_engine.execution.service import ExecutionService
from purrcept_engine.plugins.base import (
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
    typed_factory,
)
from purrcept_engine.storage.activities import ActivityCommand, ActivityRecord, ActivityState
from purrcept_engine.storage.execution import ActionOutcome
from purrcept_engine.storage.store import Store
from purrcept_engine.subject.activities import (
    ActivityDefinition,
    ActivityError,
    ActivityPort,
    ActivityService,
    DriverAcceptance,
    RecoveryObservation,
)


class _Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    label: str
    block: bool = False
    uncertain: bool = False


@dataclass(slots=True)
class _Time:
    seconds: float = 1000.0

    def now(self) -> float:
        return self.seconds


class _Driver:
    def __init__(self, port: ActivityPort) -> None:
        self.port = port
        self.received = asyncio.Event()

    async def accept(self, command: ActivityCommand, port: ActivityPort) -> DriverAcceptance:
        assert command.activity_id == port.activity_id == self.port.activity_id
        self.received.set()
        return DriverAcceptance(True)

    async def reconcile(self, record: ActivityRecord) -> RecoveryObservation:
        assert record.activity_id == self.port.activity_id
        return RecoveryObservation(True, '{"actual":"local stopped"}')

    async def close(self) -> None:
        pass


@dataclass
class _Rig:
    store: Store
    execution: ExecutionService
    activities: ActivityService
    clock: _Time
    drivers: list[_Driver]
    release: asyncio.Event = field(default_factory=asyncio.Event)
    entered: asyncio.Condition = field(default_factory=asyncio.Condition)
    contexts: dict[str, CallContext] = field(default_factory=lambda: dict[str, CallContext]())
    budgets: dict[str, float] = field(default_factory=lambda: dict[str, float]())
    driver_created: asyncio.Event = field(default_factory=asyncio.Event)
    effects: list[str] = field(default_factory=lambda: list[str]())
    expected_failure: bool = False

    async def wait_started(self, label: str) -> None:
        async with asyncio.timeout(5), self.entered:
            await self.entered.wait_for(lambda: label in self.contexts)

    async def running(self) -> tuple[ActivityPort, ActivityRecord]:
        command = await self.activities.command(
            "start-local", "start", definition="local", goal="局部目标"
        )
        async with asyncio.timeout(5):
            await self.driver_created.wait()
            driver = self.drivers[0]
            await driver.received.wait()
        await self.confirm(command, "running")
        return driver.port, await driver.port.snapshot()

    async def confirm(self, command: ActivityCommand, state: ActivityState) -> None:
        await self.drivers[0].port.confirm(
            command.command_id,
            run_generation=command.run_generation,
            version=command.version,
            state=state,
        )


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[_Rig]:
    store = await Store.open(tmp_path / "state.sqlite3")
    clock = _Time()
    drivers: list[_Driver] = []

    def driver(port: ActivityPort) -> _Driver:
        current = _Driver(port)
        drivers.append(current)
        value.driver_created.set()
        return current

    declaration = CapabilityDeclaration(
        "local.call",
        "me",
        "call",
        "本地实际能力",
        _Parameters,
        5.0,
        "await me.call(label='example')",
    )

    async def invoke(parameters: _Parameters, context: CallContext) -> CallOutcome:
        async with value.entered:
            value.contexts[parameters.label] = context
            value.budgets[parameters.label] = context.deadline - asyncio.get_running_loop().time()
            value.entered.notify_all()
        if parameters.block:
            await value.release.wait()
        value.effects.append(parameters.label)
        if parameters.uncertain:
            return CallOutcome(
                "unknown", {"observed": parameters.label}, "NativeUnknown", "实际结果仍未知。"
            )
        return CallOutcome("confirmed", {"actual": parameters.label})

    execution = ExecutionService(
        Catalog((ObjectDeclaration("me", "me", "主体"),), (declaration,)),
        (FunctionCapability(declaration, invoke),),
        store,
        ExecutionSettings(max_workers=1, max_calls=2, max_provider_calls=1),
        scratch_root=tmp_path / "scratch",
    )
    activities = ActivityService(
        store,
        (ActivityDefinition("local", driver),),
        max_steps=1,
        initial_seconds=10.0,
        max_remaining_seconds=10.0,
        monotonic=clock.now,
        invoke=execution.invoke,
        wait_invocation_released=execution.wait_invocation_released,
    )
    value = _Rig(store, execution, activities, clock, drivers)
    await activities.start()
    try:
        yield value
    finally:
        value.release.set()
        outcomes = await asyncio.gather(
            activities.close(), execution.close(), return_exceptions=True
        )
        await store.close()
        if not value.expected_failure:
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome


async def test_activity_invocation_uses_same_schema_journal_and_causal_identity(rig: _Rig) -> None:
    port, current = await rig.running()
    action = await port.invoke(
        "me.call", {"label": "effect"}, run_generation=current.run_generation
    )
    assert action.state == "completed" and action.calls[0].state == "confirmed"
    assert rig.effects == ["effect"] and action.output == ""
    assert json.loads(action.source)["kind"] == "structured_call"
    assert rig.contexts["effect"].input_id == "activity:" + current.activity_id
    assert action.input_id == rig.contexts["effect"].input_id
    invalid = await port.invoke("me.call", {"label": 7}, run_generation=current.run_generation)
    assert (
        invalid.calls[0].state == "failed" and invalid.calls[0].error_type == "CapabilityInputError"
    )
    assert rig.effects == ["effect"]


async def test_unknown_observation_keeps_step_until_real_provider_release(rig: _Rig) -> None:
    port, current = await rig.running()
    action = await port.invoke(
        "me.call",
        {"label": "late", "block": True, "uncertain": True},
        run_generation=current.run_generation,
        timeout_seconds=0.2,
    )
    assert action.state == "timed_out" and action.calls[0].state == "unknown"
    with pytest.raises(ActivityError) as full:
        await port.begin_step(run_generation=current.run_generation)
    assert full.value.code == "activity_busy"
    pause = await rig.activities.command("pause-local", "pause", activity_id=port.activity_id)
    await rig.confirm(pause, "paused")
    with pytest.raises(ActivityError):
        await rig.activities.command(
            "extend-local",
            "extend",
            activity_id=port.activity_id,
            additional_seconds=1.0,
            reason="真实在途仍持有步骤",
        )
    activities_closing = asyncio.create_task(rig.activities.close())
    execution_closing = asyncio.create_task(rig.execution.close())
    await asyncio.sleep(0.01)
    assert not activities_closing.done() and not execution_closing.done()
    assert rig.activities.pending_resources > 0 and rig.execution.pending_resources > 0
    rig.release.set()
    await asyncio.gather(activities_closing, execution_closing)
    latest = await rig.execution.query(action.action_id)
    assert latest is not None and latest.calls[0].state == "unknown"
    assert [result.state for result in latest.calls[0].history] == ["unknown", "unknown"]
    assert rig.effects == ["late"] and rig.activities.pending_resources == 0


async def test_cancelled_observer_keeps_owned_step_and_call(rig: _Rig) -> None:
    port, current = await rig.running()
    observing = asyncio.create_task(
        port.invoke(
            "me.call", {"label": "cancelled", "block": True}, run_generation=current.run_generation
        )
    )
    await rig.wait_started("cancelled")
    observing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observing
    with pytest.raises(ActivityError) as full:
        await port.begin_step(run_generation=current.run_generation)
    assert full.value.code == "activity_busy" and not rig.effects
    closing = asyncio.create_task(rig.activities.close())
    await asyncio.sleep(0.01)
    assert not closing.done()
    rig.release.set()
    await closing
    action = await rig.execution.query(rig.contexts["cancelled"].action_id)
    assert action is not None and action.calls[0].state == "confirmed"
    assert rig.effects == ["cancelled"]


@pytest.mark.parametrize("change", ["pause", "end", "budget"])
async def test_queued_activity_call_rechecks_current_authority_before_provider(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    port, current = await rig.running()
    holder = asyncio.create_task(
        rig.execution.invoke("me.call", {"label": "holder", "block": True})
    )
    await rig.wait_started("holder")
    prepared = asyncio.Event()
    original = rig.store.begin_call

    async def begin_call(
        call_id: str,
        *,
        action_id: str,
        sequence: int,
        path: str,
        target_id: str,
        arguments_json: str,
    ) -> None:
        await original(
            call_id,
            action_id=action_id,
            sequence=sequence,
            path=path,
            target_id=target_id,
            arguments_json=arguments_json,
        )
        if json.loads(arguments_json)["label"] == "queued":
            prepared.set()

    monkeypatch.setattr(rig.store, "begin_call", begin_call)
    queued = asyncio.create_task(
        port.invoke("me.call", {"label": "queued"}, run_generation=current.run_generation)
    )
    async with asyncio.timeout(5):
        await prepared.wait()
    if change == "budget":
        rig.clock.seconds += 10.0
    else:
        await rig.activities.command(
            "control-local", "end" if change == "end" else "pause", activity_id=port.activity_id
        )
    rig.release.set()
    action = await queued
    await holder
    assert action.calls[0].state == "not_started" and "queued" not in rig.contexts
    assert rig.effects == ["holder"]
    if change == "budget":
        assert (await port.snapshot()).state == "budget_exhausted"


async def test_pause_during_real_started_journal_wait_prevents_actual_provider(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    port, current = await rig.running()
    persisted, proceed = asyncio.Event(), asyncio.Event()
    original = rig.store.mark_call_started

    async def mark_started(call_id: str) -> None:
        await original(call_id)
        persisted.set()
        await proceed.wait()

    monkeypatch.setattr(rig.store, "mark_call_started", mark_started)
    invocation = asyncio.create_task(
        port.invoke("me.call", {"label": "revoked"}, run_generation=current.run_generation)
    )
    async with asyncio.timeout(5):
        await persisted.wait()
    await rig.activities.command("pause-local", "pause", activity_id=port.activity_id)
    proceed.set()
    action = await invocation
    assert action.calls[0].started_at is not None and action.calls[0].state == "not_started"
    assert not rig.contexts and not rig.effects


async def test_resumed_generation_rejects_old_driver_generation(rig: _Rig) -> None:
    port, original = await rig.running()
    pause = await rig.activities.command("pause-local", "pause", activity_id=port.activity_id)
    await rig.confirm(pause, "paused")
    resume = await rig.activities.command("resume-local", "resume", activity_id=port.activity_id)
    await rig.confirm(resume, "running")
    current = await port.snapshot()
    assert current.run_generation == original.run_generation + 1
    with pytest.raises(ActivityError):
        await port.invoke("me.call", {"label": "old"}, run_generation=original.run_generation)
    assert not rig.effects
    action = await port.invoke("me.call", {"label": "new"}, run_generation=current.run_generation)
    assert action.calls[0].state == "confirmed" and rig.effects == ["new"]


async def test_async_qualification_timeout_is_not_started(rig: _Rig) -> None:
    waiting = asyncio.Event()

    async def before_start() -> float | None:
        await waiting.wait()
        return 1.0

    action = await rig.execution.invoke(
        "me.call", {"label": "never"}, before_start=before_start, timeout_seconds=0.02
    )
    assert action.state == "timed_out" and action.calls[0].state == "not_started"
    assert [result.state for result in action.calls[0].history] == ["not_started"]
    assert not rig.effects
    await rig.execution.wait_invocation_released(action.action_id)


async def test_async_gate_only_shrinks_real_provider_deadline(rig: _Rig) -> None:
    async def before_start() -> float:
        return 0.03

    started = asyncio.get_running_loop().time()
    action = await rig.execution.invoke(
        "me.call", {"label": "short", "block": True}, before_start=before_start
    )
    assert action.state == "timed_out" and action.calls[0].state == "unknown"
    assert 0.0 < rig.budgets["short"] <= 0.031
    assert asyncio.get_running_loop().time() - started < 1.0
    releasing = asyncio.create_task(rig.execution.wait_invocation_released(action.action_id))
    await asyncio.sleep(0)
    assert not releasing.done()
    rig.release.set()
    await releasing


async def test_authority_finish_failure_keeps_step_by_admitted_identity(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    port, current = await rig.running()
    original = rig.store.finish_action
    failure = StorageError("本次首版结束提交失败", code="fixture_authority")
    injected = False

    async def finish_action(
        action_id: str,
        *,
        state: ActionOutcome,
        result_json: str | None,
        output: str,
        error: str | None,
    ) -> None:
        nonlocal injected
        if state == "timed_out" and not injected:
            injected = True
            raise failure
        await original(action_id, state=state, result_json=result_json, output=output, error=error)

    monkeypatch.setattr(rig.store, "finish_action", finish_action)
    rig.expected_failure = True
    with pytest.raises(StorageError) as raised:
        await port.invoke(
            "me.call",
            {"label": "authority", "block": True},
            run_generation=current.run_generation,
            timeout_seconds=0.2,
        )
    assert raised.value is failure
    with pytest.raises(ActivityError) as full:
        await port.begin_step(run_generation=current.run_generation)
    assert full.value.code == "activity_busy"
    activities_closing = asyncio.create_task(rig.activities.close())
    await asyncio.sleep(0.01)
    assert not activities_closing.done() and not rig.effects
    rig.release.set()
    await activities_closing
    assert rig.effects == ["authority"]
    action = await rig.execution.query(rig.contexts["authority"].action_id)
    assert action is not None and action.calls[0].state == "confirmed"


async def test_native_gate_failure_is_not_provider_failed(rig: _Rig) -> None:
    failure = StorageError("资格真实权威故障", code="fixture_authority")

    async def before_start() -> float:
        raise failure

    rig.expected_failure = True
    with pytest.raises(StorageError) as raised:
        await rig.execution.invoke("me.call", {"label": "never"}, before_start=before_start)
    assert raised.value is failure and not rig.contexts and not rig.effects


async def test_gate_own_cancellation_propagates_without_stranded_observer(rig: _Rig) -> None:
    async def before_start() -> float:
        raise asyncio.CancelledError

    async with asyncio.timeout(1):
        with pytest.raises(asyncio.CancelledError):
            await rig.execution.invoke("me.call", {"label": "never"}, before_start=before_start)
    assert not rig.contexts and not rig.effects


async def test_unstarted_receipt_failure_waits_for_actual_qualification_cleanup(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleaning, released = asyncio.Event(), asyncio.Event()
    identity: list[str] = []
    failure = StorageError("未开始回执提交失败", code="fixture_authority")

    async def before_start() -> float | None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await released.wait()
            return None
        return None

    async def fail_receipt(
        call_id: str,
        *,
        state: str,
        result_json: str | None,
        error_type: str | None,
        error: str | None,
    ) -> None:
        del call_id, state, result_json, error_type, error
        raise failure

    monkeypatch.setattr(rig.store, "record_call_result", fail_receipt)
    rig.expected_failure = True
    invoking = asyncio.create_task(
        rig.execution.invoke(
            "me.call",
            {"label": "never"},
            before_start=before_start,
            on_admitted=identity.append,
            timeout_seconds=0.1,
        )
    )
    async with asyncio.timeout(5):
        await cleaning.wait()
    ownership = asyncio.create_task(rig.execution.wait_invocation_released(identity[0]))
    await asyncio.sleep(0.01)
    assert not invoking.done() and not ownership.done() and rig.execution.pending_resources > 0
    released.set()
    with pytest.raises(StorageError) as raised:
        await invoking
    await ownership
    assert raised.value is failure and not rig.contexts and not rig.effects


async def test_admission_callback_failure_revokes_before_provider(rig: _Rig) -> None:
    admitted: list[str] = []
    failure = RuntimeError("同步所有权交接失败")

    def on_admitted(identity: str) -> None:
        admitted.append(identity)
        raise failure

    with pytest.raises(RuntimeError) as raised:
        await rig.execution.invoke("me.call", {"label": "never"}, on_admitted=on_admitted)
    assert raised.value is failure and len(admitted) == 1
    await rig.execution.wait_invocation_released(admitted[0])
    saved = await rig.execution.query(admitted[0])
    assert saved is not None and saved.calls[0].state == "not_started"
    assert not rig.effects


async def test_no_real_port_cannot_pretend_to_execute(rig: _Rig) -> None:
    controls_only = ActivityService(rig.store, (), max_steps=1)
    await controls_only.start()
    try:
        with pytest.raises(ActivityError) as unavailable:
            await controls_only.invoke("actual", "me.call", {"label": "none"}, run_generation=1)
        assert unavailable.value.code == "activity_unavailable"
    finally:
        await controls_only.close()


class _PluginSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _HostPlugin:
    """真实 Host 装配的本地能力与驱动，没有模型/设备客户端或专用执行旁路。"""

    def __init__(self) -> None:
        self.drivers: list[_Driver] = []
        self.driver_created = asyncio.Event()
        self.provider_started = asyncio.Event()
        self.release = asyncio.Event()
        self.effects: list[str] = []
        self.closed = False

    def factory(self) -> PluginFactory:
        def declare(instance_id: str, settings: _PluginSettings, base_dir: Path) -> PluginBlueprint:
            del settings, base_dir
            declaration = CapabilityDeclaration(
                instance_id + ":call",
                instance_id,
                "call",
                "本地专业步骤",
                _Parameters,
                5.0,
                "await me.local.call(label='example')",
            )

            def driver(port: ActivityPort) -> _Driver:
                current = _Driver(port)
                self.drivers.append(current)
                self.driver_created.set()
                return current

            async def build(ports: PluginPorts) -> PluginRuntime:
                assert ports.snapshot()["phase"] == "starting"

                async def invoke(parameters: _Parameters, context: CallContext) -> CallOutcome:
                    assert context.input_id is not None and context.input_id.startswith("activity:")
                    self.provider_started.set()
                    await self.release.wait()
                    self.effects.append(parameters.label)
                    return CallOutcome("confirmed", {"actual": parameters.label})

                async def close() -> None:
                    assert self.effects == ["actual"]
                    self.closed = True

                return PluginRuntime((FunctionCapability(declaration, invoke),), close=close)

            return PluginBlueprint(
                (ObjectDeclaration(instance_id, "local", "本地专业目标", "subject"),),
                (declaration,),
                build,
                activities=(ActivityDefinition("local", driver),),
            )

        return typed_factory(_PluginSettings, declare)


async def test_actual_host_keeps_plugin_store_lock_and_activity_until_real_release(
    tmp_path: Path,
) -> None:
    plugin = _HostPlugin()
    empty_settings: dict[str, object] = {}
    config = EngineConfig.validate(
        {
            "host": {"data_dir": tmp_path, "close_seconds": 0.01},
            "console": False,
            "models": {
                "local": {
                    "kind": "generation",
                    "model": "openai/test",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "context_window": 10000,
                    "max_output_tokens": 1000,
                    "capacity_rule": "test-only",
                }
            },
            "subject": {"foreground": "local", "reflection": "local", "summary": "local"},
            "plugins": [{"instance_id": "local", "entry": "fixture", "config": empty_settings}],
        },
        base_dir=tmp_path,
    )
    host = EngineHost(config, plugin_factories={"fixture": plugin.factory()})
    await host.start()
    try:
        command = await host.control_activity("start", definition="local", goal="本地目标")
        async with asyncio.timeout(5):
            await plugin.driver_created.wait()
            driver = plugin.drivers[0]
            await driver.received.wait()
        await driver.port.confirm(
            command.command_id,
            run_generation=command.run_generation,
            version=command.version,
            state="running",
        )
        current = await driver.port.snapshot()
        invoking = asyncio.create_task(
            driver.port.invoke(
                "me.local.call",
                {"label": "actual"},
                run_generation=current.run_generation,
                timeout_seconds=0.2,
            )
        )
        async with asyncio.timeout(5):
            await plugin.provider_started.wait()
        action = await invoking
        assert action.calls[0].state == "unknown"
        snapshot = await host.close()
        assert snapshot.phase == "closing" and not plugin.closed
        assert any(item.startswith("activities:") for item in snapshot.pending_resources)
        assert any(item.startswith("execution:") for item in snapshot.pending_resources)
        assert (
            "storage" in snapshot.pending_resources
            and "data_directory_lock" in snapshot.pending_resources
        )
    finally:
        plugin.release.set()
        await host.close()
        await host.wait_closed(wait_seconds=5)
    assert host.query().phase == "closed" and plugin.closed and plugin.effects == ["actual"]
    with sqlite3.connect(tmp_path / "engine.sqlite3") as database:
        assert database.execute(
            "SELECT input_id FROM actions WHERE action_id=?", (action.action_id,)
        ).fetchone() == ("activity:" + current.activity_id,)
        assert database.execute(
            "SELECT state FROM calls WHERE action_id=?", (action.action_id,)
        ).fetchone() == ("confirmed",)
