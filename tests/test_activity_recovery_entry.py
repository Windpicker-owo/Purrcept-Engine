"""通过两次真实宿主启动和 Worker 验证主脑活动核对与后续决定。

本地驱动只记录显式控制和只读核对，不连接设备或模型。重启后的首次模型控制
必须先交付核对材料，后续新调用才恢复或结束；普通页面控制不能代替主脑判断。
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing as close_resource
from datetime import UTC, datetime
from pathlib import Path

import pytest
from purrcept_core.models import (
    Message,
    MessageRole,
    ModelEventSink,
    ModelRequest,
    ModelResponse,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from pydantic import BaseModel, ConfigDict

from purrcept_engine import EngineConfig, EngineHost, InputSource, InputSubmission
from purrcept_engine.execution.protocol import JsonObject, decode_object
from purrcept_engine.plugins.base import (
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
    typed_factory,
)
from purrcept_engine.storage.activities import (
    ActivityCommand,
    ActivityFeedback,
    ActivityRecord,
    ActivityState,
    get_activity_feedback,
)
from purrcept_engine.storage.store import Store
from purrcept_engine.subject.activities import (
    ActivityDefinition,
    ActivityError,
    ActivityPort,
    ActivityRecovery,
    ActivityService,
    DriverAcceptance,
    RecoveryObservation,
)


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _Driver:
    def __init__(self, port: ActivityPort, fixture: _Fixture) -> None:
        self.port = port
        self.fixture = fixture
        fixture.drivers.append(self)

    async def accept(self, command: ActivityCommand, port: ActivityPort) -> DriverAcceptance:
        self.fixture.commands.append(command)
        if command.command == "end" and self.fixture.end_release is not None:
            await self.fixture.end_release.wait()
        states: dict[str, ActivityState] = {"start": "running", "resume": "running", "end": "ended"}
        await port.confirm(
            command.command_id,
            run_generation=command.run_generation,
            version=command.version,
            state=states[command.command],
        )
        self.fixture.confirmed.set()
        return DriverAcceptance(True)

    async def reconcile(self, record: ActivityRecord) -> RecoveryObservation:
        assert record.activity_id == self.port.activity_id
        self.fixture.observations.append(record)
        self.fixture.observation_started.set()
        if self.fixture.observation_release is not None:
            await self.fixture.observation_release.wait()
        return RecoveryObservation(
            self.fixture.stopped, '{"actual":"local driver has no running task","work":"saved"}'
        )

    async def close(self) -> None:
        self.fixture.closed += 1


class _Fixture:
    def __init__(self, *, stopped: bool | None = True) -> None:
        self.stopped = stopped
        self.drivers: list[_Driver] = []
        self.commands: list[ActivityCommand] = []
        self.observations: list[ActivityRecord] = []
        self.closed = 0
        self.confirmed = asyncio.Event()
        self.observation_started = asyncio.Event()
        self.observation_release: asyncio.Event | None = None
        self.end_release: asyncio.Event | None = None
        self.stop_entered = asyncio.Event()
        self.stop_release: asyncio.Event | None = None

    def factory(self) -> PluginFactory:
        def declare(instance_id: str, settings: _Settings, base_dir: Path) -> PluginBlueprint:
            del instance_id, settings, base_dir

            def driver(port: ActivityPort) -> _Driver:
                value = _Driver(port, self)
                return value

            async def build(ports: PluginPorts) -> PluginRuntime:
                del ports

                async def stop_inputs() -> None:
                    self.stop_entered.set()
                    if self.stop_release is not None:
                        await self.stop_release.wait()

                return PluginRuntime((), stop_inputs=stop_inputs)

            return PluginBlueprint(
                (), (), build, activities=(ActivityDefinition("local-task", driver),)
            )

        return typed_factory(_Settings, declare)


def _config(directory: Path) -> EngineConfig:
    settings: dict[str, object] = {}
    return EngineConfig.validate(
        {
            "host": {"data_dir": directory},
            "console": False,
            "models": {
                "local": {
                    "kind": "generation",
                    "model": "openai/test",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "context_window": 100000,
                    "max_output_tokens": 1000,
                    "capacity_rule": "reference-text-only-v1",
                }
            },
            "subject": {"foreground": "local", "reflection": "local", "summary": "local"},
            "plugins": [{"instance_id": "fixture", "entry": "local", "config": settings}],
        },
        base_dir=directory,
    )


async def _run(host: EngineHost, command: str, activity_id: str) -> JsonObject:
    action = await host.execute(
        "from world import me\n"
        f"result = await me.activity(command={command!r}, activity_id={activity_id!r})\n"
        "return result"
    )
    assert action.state == "completed", action
    assert action.result_json is not None
    return decode_object(action.result_json.encode("utf-8"))


async def _create(directory: Path) -> tuple[str, ActivityRecord]:
    first = _Fixture()
    host = EngineHost(_config(directory), plugin_factories={"local": first.factory()})
    await host.start()
    try:
        command = await host.control_activity("start", definition="local-task", goal="继续已存工作")
        async with asyncio.timeout(5):
            await first.confirmed.wait()
        current = await host.query_activity(command.activity_id)
        assert current is not None and current.state == "running"
        await first.drivers[0].port.checkpoint(
            run_generation=current.run_generation,
            version=current.version,
            work_json='{"checkpoint":"saved","side_effects":1}',
        )
    finally:
        await host.close()
    return command.activity_id, current


def _action(request: ModelRequest, identity: str) -> JsonObject:
    """取实际 Core 请求中的完整行动，不从存储旁路伪造模型已看到的材料。"""
    for message in request.messages:
        for block in message.content:
            if isinstance(block, ToolResultBlock) and block.tool_call_id == identity:
                content = block.content[0]
                assert isinstance(content, TextBlock)
                return decode_object(content.text.encode("utf-8"))
    raise AssertionError("实际模型请求缺少完整活动行动结果。")


def _material(request: ModelRequest, identity: str) -> JsonObject:
    result = _action(request, identity)["result_json"]
    assert isinstance(result, str)
    return decode_object(result.encode("utf-8"))


def _call(identity: str, command: str, activity_id: str) -> ToolCallBlock:
    return ToolCallBlock(
        identity,
        "play_action",
        arguments={
            "action_code": "from world import me\n"
            f"return await me.activity(command={command!r}, activity_id={activity_id!r})"
        },
    )


class _Backend:
    """离线响应借真实主体工具循环和请求呈现链，不直接调用活动内部核对。"""

    def __init__(self, activity_id: str, decision: str) -> None:
        self.activity_id = activity_id
        self.decision = decision
        self.requests: list[ModelRequest] = []
        self.host: EngineHost | None = None
        self.material: JsonObject | None = None
        self.decision_result: JsonObject | None = None

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            command, identity = "resume", "observe"
        elif len(self.requests) == 2:
            self.material = _material(request, "observe")
            assert self.material["command"] is None and self.material["decision_required"] is True
            assert self.host is not None
            with pytest.raises(ActivityError, match="核对"):
                await self.host.control_activity("resume", activity_id=self.activity_id)
            command, identity = self.decision, "decide"
        elif len(self.requests) == 3:
            self.decision_result = _material(request, "decide")
            return ModelResponse(
                Message.assistant("核对材料与明确选择已交回，确认仍以实际驱动为准。")
            )
        else:
            raise AssertionError("活动样例出现了未安排的模型请求。")
        return ModelResponse(
            Message(
                MessageRole.ASSISTANT,
                (_call(identity, command, self.activity_id),),
            )
        )


async def _submit(host: EngineHost) -> None:
    now = datetime.now(UTC)
    admission = await host.submit(
        InputSubmission(
            InputSource("local", "account", "dialog", "text", "decision"), "核对活动", now, now
        )
    )
    async with asyncio.timeout(10):
        while True:
            record = await host.query_input(admission.input_id)
            assert record is not None
            if record.state == "processed":
                result = await host.query_processing(admission.input_id)
                assert result is not None and result.stage == "processed"
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("decision", ["resume", "end"])
async def test_model_recovery_delivers_actual_evidence_before_new_decision(
    tmp_path: Path, decision: str
) -> None:
    activity_id, original = await _create(tmp_path)
    fixture = _Fixture()
    backend = _Backend(activity_id, decision)
    host = EngineHost(
        _config(tmp_path),
        plugin_factories={"local": fixture.factory()},
        generation_backends={"local": backend},
    )
    backend.host = host
    await host.start()
    try:
        assert fixture.drivers == [] and fixture.observations == []
        recovered = await host.query_activity(activity_id)
        assert recovered is not None and recovered.needs_reconciliation
        with pytest.raises(ActivityError, match="核对"):
            await host.control_activity("resume", activity_id=activity_id)
        assert fixture.drivers == []

        await _submit(host)
        material = backend.material
        assert material is not None
        assert material["command"] is None and material["decision_required"] is True
        assert material["reconciliation"] == {
            "stopped": True,
            "evidence_json": '{"actual":"local driver has no running task","work":"saved"}',
        }
        decision_result = backend.decision_result
        assert decision_result is not None
        assert decision_result["command"] is not None
        async with asyncio.timeout(5):
            await fixture.confirmed.wait()
        final = await host.query_activity(activity_id)
        assert final is not None and final.state == ("running" if decision == "resume" else "ended")
        assert [command.command for command in fixture.commands] == [decision]
        assert len(fixture.observations) == 1
        assert final.work_json == '{"checkpoint":"saved","side_effects":1}'
        assert final.granted_seconds == original.granted_seconds
        assert final.used_seconds >= original.used_seconds
        assert final.run_generation == recovered.run_generation + (decision == "resume")
    finally:
        await host.close()


async def _restarted_service(
    directory: Path,
) -> tuple[ActivityService, Store, _Fixture, str]:
    """持久恢复样例与模型入口样例使用相同 Store 和驱动，不借内存记录重启。"""
    initial = _Fixture()
    store = await Store.open(directory / "activities.sqlite3")
    service = ActivityService(
        store,
        (ActivityDefinition("local-task", lambda port: _Driver(port, initial)),),
        max_steps=4,
        initial_seconds=10,
        max_remaining_seconds=20,
    )
    await service.start()
    command = await service.command("start", "start", definition="local-task", goal="保留工作")
    async with asyncio.timeout(5):
        await initial.confirmed.wait()
    await service.close()
    await store.close()

    resumed = _Fixture()
    store = await Store.open(directory / "activities.sqlite3")
    service = ActivityService(
        store,
        (ActivityDefinition("local-task", lambda port: _Driver(port, resumed)),),
        max_steps=4,
        initial_seconds=10,
        max_remaining_seconds=20,
    )
    await service.start()
    return service, store, resumed, command.activity_id


async def test_same_action_cannot_consume_recovery_and_replays_preserve_material(
    tmp_path: Path,
) -> None:
    service, store, fixture, activity_id = await _restarted_service(tmp_path)
    try:
        first = await service.subject_command(
            "first:1", "resume", action_id="first", activity_id=activity_id
        )
        assert isinstance(first, ActivityRecovery)
        repeated = await service.subject_command(
            "first:1", "resume", action_id="first", activity_id=activity_id
        )
        assert repeated == first and len(fixture.observations) == 1
        same_script = await service.subject_command(
            "first:2",
            "end",
            action_id="first",
            activity_id=activity_id,
            presented_call_ids=frozenset({"first:1"}),
        )
        assert isinstance(same_script, ActivityRecovery) and fixture.commands == []
        assert (await service.activity(activity_id)).needs_reconciliation
        with pytest.raises(ActivityError, match="核对"):
            await service.command("page-resume", "resume", activity_id=activity_id)

        unseen_action = await service.subject_command(
            "unseen:1", "end", action_id="unseen", activity_id=activity_id
        )
        assert isinstance(unseen_action, ActivityRecovery) and fixture.commands == []

        decision = await service.subject_command(
            "decision:1",
            "end",
            action_id="decision",
            activity_id=activity_id,
            presented_call_ids=frozenset({"unseen:1"}),
        )
        assert isinstance(decision, ActivityCommand)
        async with asyncio.timeout(5):
            await fixture.confirmed.wait()
        assert (await service.activity(activity_id)).state == "ended"
        driver_count = len(fixture.drivers)
        with pytest.raises(ActivityError, match="已结束"):
            await service.subject_command(
                "invalid:1", "resume", action_id="invalid", activity_id=activity_id
            )
        assert len(fixture.drivers) == driver_count
        saved_decision = await service.subject_command(
            "decision:1", "end", action_id="decision", activity_id=activity_id
        )
        assert saved_decision == await service.command_record(decision.command_id)
        saved_read = await service.subject_command(
            "first:1", "resume", action_id="first", activity_id=activity_id
        )
        assert saved_read == first and [item.command for item in fixture.commands] == ["end"]
    finally:
        await service.close()
        await store.close()


async def test_state_change_invalidates_readonly_recovery_before_decision(tmp_path: Path) -> None:
    service, store, fixture, activity_id = await _restarted_service(tmp_path)
    try:
        first = await service.subject_command(
            "read:1", "resume", action_id="read", activity_id=activity_id
        )
        assert isinstance(first, ActivityRecovery)
        await service.command(
            "extend",
            "extend",
            activity_id=activity_id,
            additional_seconds=1,
            reason="追加不采用旧核对",
        )
        again = await service.subject_command(
            "decision:1",
            "resume",
            action_id="decision",
            activity_id=activity_id,
            presented_call_ids=frozenset({"read:1"}),
        )
        assert isinstance(again, ActivityRecovery)
        assert len(fixture.observations) == 2 and fixture.commands == []
    finally:
        await service.close()
        await store.close()


async def test_cancelled_observer_and_close_keep_driver_until_actual_reconciliation(
    tmp_path: Path,
) -> None:
    service, store, fixture, activity_id = await _restarted_service(tmp_path)
    fixture.observation_release = asyncio.Event()
    observed = asyncio.create_task(
        service.subject_command("read:1", "resume", action_id="read", activity_id=activity_id)
    )
    closing: asyncio.Task[None] | None = None
    try:
        async with asyncio.timeout(5):
            await fixture.observation_started.wait()
        observed.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observed
        assert service.pending_resources == 2
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        assert not closing.done() and fixture.closed == 0
        fixture.observation_release.set()
        await closing
        assert fixture.closed == 1 and service.pending_resources == 0
        material = await store.get_activity_feedback("reconcile-read:1")
        assert material is not None and material.kind == "reconciled"
        current = await service.activity(activity_id)
        assert current.needs_reconciliation and fixture.commands == []
    finally:
        fixture.observation_release.set()
        if closing is None:
            await service.close()
        else:
            await asyncio.gather(closing, return_exceptions=True)
        await store.close()


@pytest.mark.parametrize("stopped", [False, None])
async def test_unknown_recovery_is_visible_and_never_dispatches(
    tmp_path: Path, stopped: bool | None
) -> None:
    activity_id, _ = await _create(tmp_path)
    fixture = _Fixture(stopped=stopped)
    backend = _Backend(activity_id, "resume")
    host = EngineHost(
        _config(tmp_path),
        plugin_factories={"local": fixture.factory()},
        generation_backends={"local": backend},
    )
    backend.host = host
    await host.start()
    try:
        await _submit(host)
        material = backend.material
        assert material is not None
        assert material["decision_required"] is True and material["command"] is None
        reconciliation = material["reconciliation"]
        assert isinstance(reconciliation, dict) and reconciliation["stopped"] is stopped
        record = await host.query_activity(activity_id)
        assert record is not None and record.needs_reconciliation
        assert backend.decision_result is not None and backend.decision_result["command"] is None
        assert fixture.commands == []
    finally:
        await host.close()


@pytest.mark.parametrize("stopped", [False, None])
async def test_unknown_material_allows_explicit_end_but_requires_actual_confirmation(
    tmp_path: Path, stopped: bool | None
) -> None:
    activity_id, _ = await _create(tmp_path)
    fixture = _Fixture(stopped=stopped)
    fixture.end_release = asyncio.Event()
    backend = _Backend(activity_id, "end")
    host = EngineHost(
        _config(tmp_path),
        plugin_factories={"local": fixture.factory()},
        generation_backends={"local": backend},
    )
    backend.host = host
    await host.start()
    try:
        await _submit(host)
        assert backend.material is not None and backend.decision_result is not None
        assert backend.decision_result["command"] is not None
        current = await host.query_activity(activity_id)
        assert current is not None and current.state == "end_requested"
        assert not fixture.confirmed.is_set()
        assert [command.command for command in fixture.commands] == ["end"]
        fixture.end_release.set()
        async with asyncio.timeout(5):
            await fixture.confirmed.wait()
        ended = await host.query_activity(activity_id)
        assert ended is not None and ended.state == "ended"
    finally:
        fixture.end_release.set()
        await host.close()


async def test_direct_embedded_actions_do_not_authorize_a_model_recovery_decision(
    tmp_path: Path,
) -> None:
    activity_id, _ = await _create(tmp_path)
    fixture = _Fixture()
    host = EngineHost(_config(tmp_path), plugin_factories={"local": fixture.factory()})
    await host.start()
    try:
        first = await _run(host, "resume", activity_id)
        second = await _run(host, "resume", activity_id)
        assert first["command"] is None and second["command"] is None
        assert first["decision_required"] is True and second["decision_required"] is True
        current = await host.query_activity(activity_id)
        assert current is not None and current.needs_reconciliation
        assert fixture.commands == []
    finally:
        await host.close()


async def test_one_model_response_cannot_authorize_its_second_action_before_presentation(
    tmp_path: Path,
) -> None:
    """不同 Worker 身份也须等待下一实际请求，不能用同响应中的第二工具绕过。"""
    activity_id, _ = await _create(tmp_path)
    fixture = _Fixture()

    class BatchBackend:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def generate(
            self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
        ) -> ModelResponse:
            self.requests.append(request)
            if len(self.requests) == 1:
                return ModelResponse(
                    Message(
                        MessageRole.ASSISTANT,
                        (
                            _call("first", "resume", activity_id),
                            _call("premature", "resume", activity_id),
                        ),
                    )
                )
            if len(self.requests) == 2:
                first, second = _action(request, "first"), _action(request, "premature")
                assert first["action_id"] != second["action_id"]
                for action in (first, second):
                    result = action["result_json"]
                    if isinstance(result, str):
                        material = decode_object(result.encode("utf-8"))
                        assert material["command"] is None and material["decision_required"] is True
                assert fixture.commands == []
                return ModelResponse(
                    Message(
                        MessageRole.ASSISTANT, (_call("after-presentation", "resume", activity_id),)
                    )
                )
            assert len(self.requests) == 3
            assert _material(request, "after-presentation")["command"] is not None
            return ModelResponse(Message.assistant("核对材料实际呈现后才恢复。"))

    backend = BatchBackend()
    host = EngineHost(
        _config(tmp_path),
        plugin_factories={"local": fixture.factory()},
        generation_backends={"local": backend},
    )
    await host.start()
    try:
        await _submit(host)
        assert len(backend.requests) == 3
        assert [command.command for command in fixture.commands] == ["resume"]
        current = await host.query_activity(activity_id)
        assert current is not None and current.state == "running"
    finally:
        await host.close()


async def test_host_closing_keeps_started_observation_before_activity_close_starts(
    tmp_path: Path,
) -> None:
    """宿主已撤销新派发但插件尚在停止接入时，已开始的核对仍保存真实材料。"""
    activity_id, _ = await _create(tmp_path)
    fixture = _Fixture()
    fixture.observation_release = asyncio.Event()
    fixture.stop_release = asyncio.Event()
    host = EngineHost(_config(tmp_path), plugin_factories={"local": fixture.factory()})
    await host.start()
    observed = asyncio.create_task(
        host.execute(
            "from world import me\n"
            f"return await me.activity(command='resume', activity_id={activity_id!r})"
        )
    )
    closing: asyncio.Task[object] | None = None
    try:
        async with asyncio.timeout(5):
            await fixture.observation_started.wait()
        closing = asyncio.create_task(host.close())
        async with asyncio.timeout(5):
            await fixture.stop_entered.wait()
        assert host.query().phase == "closing" and fixture.closed == 0
        fixture.observation_release.set()
        action = await observed
        assert len(action.calls) == 1 and action.calls[0].state == "unknown"

        # 退出可以结束 Worker/Provider 的观察；活动所有者仍须保存真实返回。
        def persisted_feedback() -> ActivityFeedback | None:
            database = tmp_path / "engine.sqlite3"
            with close_resource(
                sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            ) as connection:
                return get_activity_feedback(connection, "reconcile-" + action.calls[0].call_id)

        async with asyncio.timeout(5):
            while True:
                feedback = await asyncio.to_thread(persisted_feedback)
                if feedback is not None:
                    break
                await asyncio.sleep(0.01)
        assert feedback.kind == "reconciled"
        assert fixture.commands == []
        fixture.stop_release.set()
        await closing
        assert host.query().phase == "closed" and fixture.closed == 1
    finally:
        fixture.observation_release.set()
        fixture.stop_release.set()
        await asyncio.gather(observed, return_exceptions=True)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        else:
            await host.close()
