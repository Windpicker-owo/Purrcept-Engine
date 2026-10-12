"""真实宿主/执行器验证面板控制去重、取消、版本和查询无副作用。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from purrcept_engine import EngineConfig, EngineHost
from purrcept_engine.app.panel import ControlRequest
from purrcept_engine.execution.catalog import CapabilityDeclaration, ObjectDeclaration
from purrcept_engine.execution.providers import CallContext, CallOutcome, FunctionCapability
from purrcept_engine.plugins.base import PluginBlueprint, PluginPorts, PluginRuntime, typed_factory
from purrcept_engine.plugins.panels import DevicePanel, PanelControl, PreviewFrame
from purrcept_engine.storage.controls import ControlError
from purrcept_engine.storage.store import Store


class _Empty(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _host(tmp_path: Path, *, release: asyncio.Event | None = None):
    """测试能力只产生本地计数，保持真正的 Host/Journal/Provider 生命周期。"""
    calls: list[str] = []
    revision = ["1"]
    entered = asyncio.Event()

    def declare(instance_id: str, settings: _Empty, base_dir: Path) -> PluginBlueprint:
        del settings, base_dir
        declaration = CapabilityDeclaration(
            "local.control",
            "local",
            "control",
            "执行本地控制。",
            _Empty,
            10.0,
            "await {path}()",
            control=True,
        )

        async def build(ports: PluginPorts) -> PluginRuntime:
            del ports

            async def invoke(parameters: _Empty, context: CallContext) -> CallOutcome:
                del parameters
                calls.append(context.call_id)
                entered.set()
                if release is not None:
                    await release.wait()
                return CallOutcome("confirmed", {"actual": "done"})

            return PluginRuntime(
                (FunctionCapability(declaration, invoke),),
                snapshot=lambda: {"revision": revision[0]},
                panel=DevicePanel(
                    "computer",
                    (PanelControl("connect", "连接电脑", declaration.capability_id),),
                    lambda: revision[0],
                    lambda: PreviewFrame(b"cached", "image/jpeg", 1.0),
                ),
            )

        return PluginBlueprint(
            (ObjectDeclaration("local", "test", "本地测试", "subject"),), (declaration,), build
        )

    config = EngineConfig.validate(
        {
            "host": {"data_dir": tmp_path},
            "console": False,
            "models": {
                "model": {
                    "kind": "generation",
                    "model": "openai/test",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "context_window": 100_000,
                    "max_output_tokens": 1000,
                    "capacity_rule": "reference-text-only-v1",
                }
            },
            "subject": {"foreground": "model", "reflection": "model", "summary": "model"},
            "plugins": [{"instance_id": "local", "entry": "local"}],
        },
        base_dir=tmp_path,
    )
    return (
        EngineHost(config, plugin_factories={"local": typed_factory(_Empty, declare)}),
        calls,
        revision,
        entered,
    )


def _request(host: EngineHost, identity: str, *, revision: str = "1", operation: str = "connect"):
    return ControlRequest(
        request_id=identity,
        instance_id=host.query().instance_id,
        target="local",
        operation=operation,
        expected_revision=revision,
    )


async def test_snapshot_preview_and_repeated_query_do_not_open_devices(tmp_path: Path) -> None:
    host, calls, _, _ = _host(tmp_path)
    await host.start()
    try:
        first = await host.query_panel()
        second = await host.query_panel()
        assert first["instance_id"] == second["instance_id"]
        assert first["pending_inputs"] == [] and first["recent_actions"] == []
        assert host.query_preview("local") == PreviewFrame(b"cached", "image/jpeg", 1.0)
        assert calls == []
    finally:
        await host.close()


async def test_disconnect_of_observer_does_not_repeat_or_cancel_control(tmp_path: Path) -> None:
    release = asyncio.Event()
    host, calls, _, entered = _host(tmp_path, release=release)
    await host.start()
    observer = asyncio.create_task(host.submit_control(_request(host, "once")))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        observer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observer
        duplicate = asyncio.create_task(host.submit_control(_request(host, "once")))
        release.set()
        result = await asyncio.wait_for(duplicate, 5)
        assert result.state == "finished" and result.action_id is not None
        assert await host.submit_control(_request(host, "once")) == result
        actual = await host.query_control("once")
        assert actual is not None and actual["action"] is not None
        assert len(calls) == 1
        with pytest.raises(ControlError, match="不同请求"):
            await host.submit_control(_request(host, "once", revision="2"))
    finally:
        release.set()
        await host.close()


async def test_stale_revision_and_arbitrary_action_are_rejected_without_effect(
    tmp_path: Path,
) -> None:
    host, calls, revision, _ = _host(tmp_path)
    await host.start()
    try:
        revision[0] = "2"
        result = await host.submit_control(_request(host, "stale"))
        assert result.state == "rejected" and result.error_code == "stale_revision"
        result = await host.submit_control(
            _request(host, "python", revision="2", operation="execute")
        )
        assert result.state == "rejected" and result.error_code == "control_target"
        assert calls == []
        assert (await host.query_panel())["authority_available"] is True
        with pytest.raises(ControlError, match="过期"):
            await host.submit_control(
                ControlRequest(
                    request_id="old",
                    instance_id="old-instance",
                    target="local",
                    operation="connect",
                    expected_revision="2",
                )
            )
    finally:
        await host.close()


async def test_reopen_preserves_control_identity_without_replaying(tmp_path: Path) -> None:
    path = tmp_path / "engine.sqlite3"
    store = await Store.open(path)
    try:
        record, fresh = await store.controls.admit("one", "instance", {"operation": "connect"})
        assert fresh and record.state == "admitted"
        with pytest.raises(ControlError, match="不同请求"):
            await store.controls.admit("one", "instance", {"operation": "disconnect"})
        assert store.authority_available
    finally:
        await store.close()
    reopened = await Store.open(path)
    try:
        retained = await reopened.controls.get("one")
        assert retained is not None and retained.state == "interrupted"
        same, fresh = await reopened.controls.admit("one", "instance", {"operation": "connect"})
        assert not fresh and same == retained
        assert await reopened.pending() == ()
    finally:
        await reopened.close()
