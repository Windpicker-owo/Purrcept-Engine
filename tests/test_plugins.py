"""纯插件声明、实际宿主装配和真实 Worker 调用，不连接外部服务。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from purrcept_engine import EngineConfig, EngineHost
from purrcept_engine.app.assembly import prepare_plugins
from purrcept_engine.app.config import PluginSettings
from purrcept_engine.errors import ConfigError
from purrcept_engine.execution.catalog import CapabilityDeclaration, ObjectDeclaration
from purrcept_engine.execution.providers import CallContext, CallOutcome, FunctionCapability
from purrcept_engine.plugins.base import (
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
    typed_factory,
)


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    prefix: str


class _Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    text: str


def _factory(
    events: list[str],
    *,
    release: asyncio.Event | None = None,
    entered: asyncio.Event | None = None,
) -> PluginFactory:
    def declare(instance_id: str, settings: _Settings, base_dir: Path) -> PluginBlueprint:
        del base_dir
        events.append("declare")
        declaration = CapabilityDeclaration(
            instance_id + ".echo",
            instance_id,
            "echo",
            "返回已确认的本地测试结果。",
            _Parameters,
            5.0,
            'await me.test.echo(text="hi")',
        )

        async def build(ports: PluginPorts) -> PluginRuntime:
            events.append("build")
            assert ports.snapshot()["phase"] == "starting"

            async def invoke(parameters: _Parameters, context: CallContext) -> CallOutcome:
                assert context.call_id
                events.append("invoke")
                if entered is not None:
                    entered.set()
                if release is not None:
                    await release.wait()
                events.append("effect")
                return CallOutcome("confirmed", {"text": settings.prefix + parameters.text})

            async def start() -> None:
                assert ports.snapshot()["phase"] == "running"
                events.append("start")

            async def stop_inputs() -> None:
                events.append("stop_inputs")

            async def close() -> None:
                events.append("close")

            return PluginRuntime(
                (FunctionCapability(declaration, invoke),), start, stop_inputs, close
            )

        return PluginBlueprint(
            (ObjectDeclaration(instance_id, "test", "本地能力", "subject"),),
            (declaration,),
            build,
        )

    return typed_factory(_Settings, declare)


def _config(tmp_path: Path, *, console: bool = False, close_seconds: float = 30) -> EngineConfig:
    return EngineConfig.validate(
        {
            "host": {"data_dir": tmp_path, "close_seconds": close_seconds},
            "console": console,
            "models": {
                "test": {
                    "kind": "generation",
                    "model": "openai/test",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "context_window": 10000,
                    "max_output_tokens": 1000,
                    "capacity_rule": "test-only",
                }
            },
            "subject": {"foreground": "test", "reflection": "test", "summary": "test"},
            "plugins": [{"instance_id": "local", "entry": "fixture", "config": {"prefix": "x:"}}],
        },
        base_dir=tmp_path,
    )


def test_pure_declaration_does_not_build_and_config_is_an_immutable_snapshot(
    tmp_path: Path,
) -> None:
    original: dict[str, object] = {"prefix": "synthetic-secret"}
    settings = PluginSettings.model_validate(
        {"instance_id": "test", "entry": "fixture", "config": original}
    )
    original["prefix"] = "changed"
    assert settings.configuration() == {"prefix": "synthetic-secret"}
    copy = settings.configuration()
    copy.clear()
    assert settings.configuration() == {"prefix": "synthetic-secret"}
    assert "synthetic-secret" not in repr(settings) + settings.model_dump_json()
    events: list[str] = []
    prepared = prepare_plugins(
        (settings,), base_dir=tmp_path, factories={"fixture": _factory(events)}
    )
    assert events == ["declare"]
    assert "me.test.echo" in prepared.catalog.bindings
    assert not list(tmp_path.iterdir())


def test_disabled_plugin_is_not_imported_and_bad_settings_are_sanitized(tmp_path: Path) -> None:
    disabled = PluginSettings(instance_id="unused", entry="does.not.exist:factory", enabled=False)
    assert not prepare_plugins((disabled,), base_dir=tmp_path, factories={}).catalog.bindings
    invalid = PluginSettings.model_validate(
        {"instance_id": "bad", "entry": "fixture", "config": {"prefix": 2, "secret": "hidden"}}
    )
    with pytest.raises(ConfigError) as caught:
        prepare_plugins((invalid,), base_dir=tmp_path, factories={"fixture": _factory([])})
    assert "prefix" in str(caught.value) and "hidden" not in str(caught.value)


async def test_actual_host_uses_frozen_catalog_and_real_worker(tmp_path: Path) -> None:
    events: list[str] = []
    host = EngineHost(_config(tmp_path), plugin_factories={"fixture": _factory(events)})
    assert events == ["declare"]
    await host.start()
    try:
        record = await host.execute('from world import me\nreturn await me.test.echo(text="hi")')
        assert record.state == "completed"
        assert json.loads(record.result_json or "null") == {"text": "x:hi"}
        assert record.calls[0].state == "confirmed"
        assert record.catalog_version == host.catalog.version
        assert await host.query_action(record.action_id) == record
    finally:
        await host.close()
    assert events == ["declare", "build", "start", "invoke", "effect", "stop_inputs", "close"]


async def test_plugin_resource_is_not_closed_while_a_started_call_borrows_it(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    release = asyncio.Event()
    entered = asyncio.Event()
    host = EngineHost(
        _config(tmp_path, close_seconds=0.02),
        plugin_factories={"fixture": _factory(events, release=release, entered=entered)},
    )
    await host.start()
    action = asyncio.create_task(
        host.execute('from world import me\nawait me.test.echo(text="hi")')
    )
    async with asyncio.timeout(5):
        await entered.wait()
    snapshot = await host.close()
    assert snapshot.phase == "closing"
    assert "stop_inputs" in events and "close" not in events
    release.set()
    await action
    assert (await host.wait_closed(wait_seconds=5)).phase == "closed"
    assert events.index("effect") < events.index("close")


async def test_console_output_only_occurs_after_an_actual_capability_call(tmp_path: Path) -> None:
    output: list[str] = []
    host = EngineHost(
        _config(tmp_path, console=True),
        plugin_factories={"fixture": _factory([])},
        console_output=output.append,
    )
    await host.start()
    try:
        assert output == []
        record = await host.execute('from world import me\nawait me.console.output(text="visible")')
        assert record.state == "completed"
        assert output == ["visible"]
        assert json.loads(record.calls[0].result_json or "null") == {
            "channel": "console",
            "text": "visible",
        }
    finally:
        await host.close()
