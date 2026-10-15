"""通过真实 Host、Windows Worker 与唯一 Store 验证活动接管和收尾。

插件纯声明只登记本地驱动；没有真实模型、游戏或网络调用。活动命令由实际
me.activity 调用产生，可信 call_id 与活动命令同库存储；手工确认模拟专业驱动
取得真实事实后的回报，明确区别受理与完成。关闭样例保留宿主资源直到驱动反馈
实际落盘，再释放插件和目录锁。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from purrcept_engine import EngineConfig, EngineHost, HostStateError
from purrcept_engine.domain.activities.models import ActivityCommand, ActivityRecord, ActivityState
from purrcept_engine.plugin_system.contracts import (
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
    typed_factory,
)
from purrcept_engine.storage import activities as sql
from purrcept_engine.subject.activities import (
    ActivityDefinition,
    ActivityPort,
    DriverAcceptance,
    RecoveryObservation,
)
from purrcept_engine.values import decode_object


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _Driver:
    """本地驱动只受理请求；测试另行提交专业工作已经确认的事实。"""

    def __init__(self, port: ActivityPort, fixture: _Plugin) -> None:
        self.port = port
        self.fixture = fixture
        self.commands: dict[str, ActivityCommand] = {}
        self.changed = asyncio.Condition()
        self.work: asyncio.Task[None] | None = None

    async def accept(self, command: ActivityCommand, port: ActivityPort) -> DriverAcceptance:
        assert port.activity_id == self.port.activity_id
        async with self.changed:
            self.commands[command.command_id] = command
            self.changed.notify_all()
        return DriverAcceptance(True)

    async def wait_command(self, command_id: str) -> ActivityCommand:
        async with asyncio.timeout(5), self.changed:
            await self.changed.wait_for(lambda: command_id in self.commands)
            return self.commands[command_id]

    async def confirm(self, command_id: str, state: ActivityState) -> ActivityCommand:
        command = await self.wait_command(command_id)
        confirmation = await self.port.confirm(
            command_id,
            run_generation=command.run_generation,
            version=command.version,
            state=state,
        )
        if state == "running" and self.fixture.release_step is not None and self.work is None:
            self.work = asyncio.create_task(self._local_step())
        return confirmation

    async def _local_step(self) -> None:
        """真实活动端口取得步骤，外部工作完成后才保存工作事实和归还所有权。"""
        current = await self.port.snapshot()
        step = await self.port.begin_step(run_generation=current.run_generation)
        self.fixture.step_started.set()
        release = self.fixture.release_step
        assert release is not None
        await release.wait()
        current = await self.port.snapshot()
        await self.port.checkpoint(
            run_generation=current.run_generation,
            version=current.version,
            work_json='{"actual_step":"finished"}',
        )
        await step.finish()
        self.fixture.events.append("step_finished")

    async def reconcile(self, record: ActivityRecord) -> RecoveryObservation:
        assert record.activity_id == self.port.activity_id
        return RecoveryObservation(True, '{"actual":"stopped","source":"local-driver"}')

    async def close(self) -> None:
        self.fixture.events.append("driver_close_entered")
        self.fixture.close_entered.set()
        if self.fixture.release_close is not None:
            await self.fixture.release_close.wait()
        if self.work is not None:
            await self.work
        if self.fixture.report_on_close:
            current = await self.port.snapshot()
            await self.port.feedback(
                "last-local-feedback",
                "progress",
                '{"fact":"actual-driver-final-progress"}',
                run_generation=current.run_generation,
                version=current.version,
            )
        self.fixture.events.append("driver_closed")


class _Plugin:
    def __init__(self, *, report_on_close: bool = False, run_step: bool = False) -> None:
        self.events: list[str] = []
        self.drivers: list[_Driver] = []
        self.driver_created = asyncio.Event()
        self.close_entered = asyncio.Event()
        self.step_started = asyncio.Event()
        self.release_step: asyncio.Event | None = asyncio.Event() if run_step else None
        self.release_close: asyncio.Event | None = None
        self.report_on_close = report_on_close

    def factory(self) -> PluginFactory:
        def declare(instance_id: str, settings: _Settings, base_dir: Path) -> PluginBlueprint:
            del settings, base_dir
            assert instance_id == "fixture"
            self.events.append("declare")

            def create_driver(port: ActivityPort) -> _Driver:
                driver = _Driver(port, self)
                self.drivers.append(driver)
                self.events.append("driver_created")
                self.driver_created.set()
                return driver

            async def build(ports: PluginPorts) -> PluginRuntime:
                assert ports.snapshot()["phase"] == "starting"
                self.events.append("build")

                async def close() -> None:
                    assert "driver_closed" in self.events
                    self.events.append("plugin_closed")

                return PluginRuntime((), close=close)

            return PluginBlueprint(
                (),
                (),
                build,
                activities=(ActivityDefinition("local-task", create_driver),),
            )

        return typed_factory(_Settings, declare)


def _config(directory: Path, *, close_seconds: float = 30.0) -> EngineConfig:
    empty_settings: dict[str, object] = {}
    return EngineConfig.validate(
        {
            "host": {"data_dir": directory, "close_seconds": close_seconds},
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
                },
            },
            "subject": {"foreground": "local", "reflection": "local", "summary": "local"},
            "plugins": [
                {"instance_id": "fixture", "entry": "local-fixture", "config": empty_settings}
            ],
        },
        base_dir=directory,
    )


async def test_actual_worker_activity_command_is_durable_and_two_phase(tmp_path: Path) -> None:
    plugin = _Plugin()
    host = EngineHost(_config(tmp_path), plugin_factories={"local-fixture": plugin.factory()})
    await host.start()
    try:
        assert plugin.events == ["declare", "build"] and plugin.drivers == []
        action = await host.execute(
            "from world import me\n"
            "result = await me.activity(command='start', definition='local-task', "
            "goal='局部目标')\n"
            "return {'activity_id': result.command.activity_id, "
            "'command_id': result.command.command_id}"
        )
        assert action.state == "completed" and action.result_json is not None
        returned = decode_object(action.result_json.encode("utf-8"))
        activity_id, command_id = returned["activity_id"], returned["command_id"]
        assert isinstance(activity_id, str) and isinstance(command_id, str)
        assert command_id == action.calls[0].call_id
        assert action.calls[0].path == "me.activity" and action.calls[0].state == "confirmed"
        record = await host.query_activity(activity_id)
        assert record is not None and record.state == "starting"
        async with asyncio.timeout(5):
            await plugin.driver_created.wait()
        driver = plugin.drivers[0]
        confirmation = await driver.confirm(command_id, "running")
        assert confirmation.applied and confirmation.status == "confirmed"
        record = await host.query_activity(activity_id)
        assert record is not None and record.state == "running"

        pause = await host.control_activity("pause", activity_id=activity_id)
        record = await host.query_activity(activity_id)
        assert pause.command_id != command_id and record is not None
        assert record.state == "pause_requested"
        await driver.confirm(pause.command_id, "paused")
        paused = await host.query_activity(activity_id)
        assert paused is not None and paused.state == "paused"
        resume = await host.control_activity("resume", activity_id=activity_id)
        assert resume.run_generation == confirmation.run_generation + 1
        await driver.confirm(resume.command_id, "running")
        resumed = await host.query_activity(activity_id)
        assert resumed is not None and resumed.granted_seconds == paused.granted_seconds
        assert resumed.used_seconds >= paused.used_seconds
    finally:
        await host.close()
    assert plugin.events[-2:] == ["driver_closed", "plugin_closed"]
    with sqlite3.connect(tmp_path / "engine.sqlite3") as connection:
        saved_command = sql.get_activity_command(connection, command_id)
        saved_activity = sql.get_activity(connection, activity_id)
        assert saved_command is not None and saved_command.status == "confirmed"
        assert saved_activity is not None and saved_activity.state == "interrupted"
        assert saved_activity.needs_reconciliation


async def test_host_close_waits_for_driver_feedback_before_releasing_plugin_and_store(
    tmp_path: Path,
) -> None:
    plugin = _Plugin(report_on_close=True)
    plugin.release_close = asyncio.Event()
    host = EngineHost(
        _config(tmp_path, close_seconds=0.01), plugin_factories={"local-fixture": plugin.factory()}
    )
    await host.start()
    command = await host.control_activity("start", definition="local-task", goal="局部目标")
    async with asyncio.timeout(5):
        await plugin.driver_created.wait()
    await plugin.drivers[0].confirm(command.command_id, "running")
    try:
        closing = await host.close()
        assert closing.phase == "closing"
        async with asyncio.timeout(5):
            await plugin.close_entered.wait()
        assert "plugin_closed" not in plugin.events
        assert any(resource.startswith("activities") for resource in closing.pending_resources)
        assert (
            "storage" in closing.pending_resources
            and "data_directory_lock" in closing.pending_resources
        )
        assert await host.query_activity(command.activity_id) is not None
        with pytest.raises(HostStateError):
            await host.control_activity("pause", activity_id=command.activity_id)
    finally:
        plugin.release_close.set()
        await host.wait_closed(wait_seconds=5)
    assert host.query().phase == "closed"
    assert plugin.events.count("driver_closed") == 1
    assert plugin.events[-1] == "plugin_closed"
    with sqlite3.connect(tmp_path / "engine.sqlite3") as connection:
        record = sql.get_activity(connection, command.activity_id)
        feedback = sql.get_activity_feedback(connection, "last-local-feedback")
        assert record is not None and record.state == "interrupted"
        assert record.progress_json == '{"fact":"actual-driver-final-progress"}'
        assert feedback is not None and feedback.applied


async def test_host_keeps_step_and_authority_until_actual_driver_work_finishes(
    tmp_path: Path,
) -> None:
    """关闭只结束宿主等待，正在运行的真实本地专业步骤仍能提交工作后收尾。"""
    plugin = _Plugin(report_on_close=True, run_step=True)
    host = EngineHost(
        _config(tmp_path, close_seconds=0.01), plugin_factories={"local-fixture": plugin.factory()}
    )
    await host.start()
    command = await host.control_activity("start", definition="local-task", goal="局部目标")
    async with asyncio.timeout(5):
        await plugin.driver_created.wait()
    driver = plugin.drivers[0]
    await driver.confirm(command.command_id, "running")
    async with asyncio.timeout(5):
        await plugin.step_started.wait()
    try:
        closing = await host.close()
        assert closing.phase == "closing" and "plugin_closed" not in plugin.events
        assert await host.query_activity(command.activity_id) is not None
        with pytest.raises(HostStateError):
            await driver.port.begin_step(run_generation=command.run_generation)
    finally:
        release = plugin.release_step
        assert release is not None
        release.set()
        await host.wait_closed(wait_seconds=5)
    assert plugin.events.index("step_finished") < plugin.events.index("driver_closed")
    assert plugin.events.index("driver_closed") < plugin.events.index("plugin_closed")
    with sqlite3.connect(tmp_path / "engine.sqlite3") as connection:
        record = sql.get_activity(connection, command.activity_id)
        notice = sql.get_activity_feedback(connection, "last-local-feedback")
        assert record is not None and record.work_json == '{"actual_step":"finished"}'
        assert record.state == "interrupted" and notice is not None and notice.applied
