"""真实目录锁、输入接管及关闭等待的宿主边界验证，不启动模型或设备。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from purrcept_engine import (
    EngineConfig,
    EngineHost,
    HostStateError,
    InputSource,
    InputSubmission,
    InstanceLocked,
    StorageError,
)
from purrcept_engine.domain.context.capacity import CapacityDeclaration, CapacityEstimate
from purrcept_engine.infrastructure.locking import InstanceLock
from purrcept_engine.infrastructure.models.clients import OpenAIGenerationResource
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system.contracts import (
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
)
from purrcept_engine.plugin_system.panels import DevicePanel
from purrcept_engine.values import JsonObject


def _config(directory: Path, *, close_seconds: float = 30.0) -> EngineConfig:
    return EngineConfig.validate(
        {
            "host": {"data_dir": directory, "close_seconds": close_seconds},
            "models": {
                "test": {
                    "kind": "generation",
                    "model": "openai/test",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic-key",
                    "context_window": 10000,
                    "max_output_tokens": 1000,
                    "capacity_rule": "test-only",
                }
            },
            "subject": {"foreground": "test", "reflection": "test", "summary": "test"},
        }
    )


def _submission() -> InputSubmission:
    now = datetime.now(UTC)
    return InputSubmission(
        InputSource("console", "local", "local", "message", native_event_id="one"),
        "你好",
        now,
        now,
    )


@pytest.mark.parametrize(
    "supports,rule,media,registered,expected",
    [
        (True, "reference-text-only-v1", "unsupported", False, False),
        (True, "reference-text-only-v1", "test-audio", True, False),
        (True, "test-audio", "unsupported", True, False),
        (True, "test-audio", "test-audio", False, False),
        (False, "test-audio", "test-audio", True, False),
        (True, "test-audio", "test-audio", True, True),
        (True, "litellm-token-counter-v1", "gemini-3-inline-v1", False, True),
        (True, "litellm-token-counter-v1", "unknown", False, False),
    ],
)
async def test_native_audio_admission_requires_registered_media_capacity(
    tmp_path: Path, supports: bool, rule: str, media: str, registered: bool, expected: bool
) -> None:
    """真实宿主接线不能把端点的音频声明当作原声请求可派发的依据。"""
    observed: list[bool] = []

    async def build(ports: PluginPorts) -> PluginRuntime:
        observed.append(ports.native_audio_enabled)
        return PluginRuntime(())

    def prepare(instance: str, raw: object, base: Path) -> PluginBlueprint:
        del instance, raw, base
        return PluginBlueprint((), (), build)

    def measure(
        payload: Mapping[str, object], declaration: CapacityDeclaration
    ) -> CapacityEstimate:
        del payload, declaration
        return CapacityEstimate(1, "test-audio", "synthetic", "synthetic")

    raw = _config(tmp_path).model_dump()
    raw["models"]["test"].update(
        supports_contents=["text", "audio"] if supports else ["text"],
        capacity_rule=rule,
        media_rule=media,
    )
    raw["plugins"] = [{"instance_id": "probe", "entry": "probe"}]
    host = EngineHost(
        EngineConfig.validate(raw, base_dir=tmp_path),
        plugin_factories={"probe": PluginFactory(prepare)},
        capacity_rules={rule: measure} if registered else {},
    )
    await host.start()
    try:
        assert observed == [expected]
    finally:
        await host.close()


async def test_voice_panel_reads_latest_processing_without_replaying_input(tmp_path: Path) -> None:
    """收音缓存中的旧阶段不能覆盖同库处理事实，查询不能启动模型或设备。"""
    voice: JsonObject = {"history": []}

    async def build(ports: PluginPorts) -> PluginRuntime:
        del ports
        return PluginRuntime(
            (), snapshot=lambda: voice, panel=DevicePanel("voice_input", (), lambda: "1")
        )

    def prepare(instance: str, raw: object, base: Path) -> PluginBlueprint:
        del instance, raw, base
        return PluginBlueprint((), (), build)

    raw = _config(tmp_path).model_dump()
    raw["plugins"] = [{"instance_id": "voice", "entry": "probe"}]
    host = EngineHost(
        EngineConfig.validate(raw, base_dir=tmp_path),
        plugin_factories={"probe": PluginFactory(prepare)},
    )
    await host.start()
    try:
        admitted = await host.submit(_submission())
        voice["history"] = [{"input_id": admitted.input_id, "processing_stage": "processed"}]
        snapshot = await host.query_panel()
        devices = snapshot["devices"]
        assert isinstance(devices, list)
        selected = next(
            d for d in devices if isinstance(d, dict) and d.get("kind") == "voice_input"
        )
        assert isinstance(selected, dict)
        state = selected["state"]
        assert isinstance(state, dict)
        latest = state["latest_input"]
        assert isinstance(latest, dict) and latest["processing_stage"] == "attention"
        assert "latest_input" not in voice
        assert (await host.query_panel())["devices"] == devices
    finally:
        await host.close()


def test_os_lock_rejects_second_owner_and_ignores_stale_file(tmp_path: Path) -> None:
    first = InstanceLock(tmp_path)
    second = InstanceLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(InstanceLocked):
            second.acquire()
    finally:
        first.release()
    assert (tmp_path / "instance.lock").exists()
    second.acquire()
    second.release()


async def test_host_input_is_recoverable_and_duplicate_is_not_reaccepted(tmp_path: Path) -> None:
    host = EngineHost(_config(tmp_path))
    await asyncio.gather(host.start(), host.start())
    submission = _submission()
    first = await host.submit(submission)
    repeated = await host.submit(submission)
    assert repeated.duplicate and repeated.input_id == first.input_id
    record = await host.query_input(first.input_id)
    assert record is not None and record.text == submission.text and record.state == "pending"
    version = host.query().version
    assert host.query().version == version
    await asyncio.gather(host.close(), host.close())
    assert host.query().phase == "closed"
    with pytest.raises(HostStateError):
        await host.submit(submission)

    recovered = EngineHost(_config(tmp_path))
    await recovered.start()
    try:
        assert await recovered.query_input(first.input_id) == record
    finally:
        await recovered.close()


async def test_close_timeout_and_cancel_keep_resource_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = EngineHost(_config(tmp_path, close_seconds=0.01))
    await host.start()
    entered = asyncio.Event()
    release = asyncio.Event()
    original_close = Store.close
    invocations = 0

    async def delayed_close(store: Store) -> None:
        nonlocal invocations
        invocations += 1
        entered.set()
        await release.wait()
        await original_close(store)

    monkeypatch.setattr(Store, "close", delayed_close)
    snapshot = await host.close()
    assert snapshot.phase == "closing" and "storage" in snapshot.pending_resources
    await entered.wait()
    with pytest.raises(InstanceLocked):
        InstanceLock(tmp_path).acquire()
    with pytest.raises(HostStateError):
        await host.submit(_submission())

    observer = asyncio.create_task(host.wait_closed())
    await asyncio.sleep(0)
    observer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observer
    assert (await host.close()).phase == "closing"
    release.set()
    assert (await host.wait_closed(wait_seconds=1.0)).phase == "closed"
    assert invocations == 1
    unlocked = InstanceLock(tmp_path)
    unlocked.acquire()
    unlocked.release()


async def test_cancelling_start_waiter_does_not_abandon_open_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_open = Store.open
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_open(
        cls: type[Store],
        path: Path,
        *,
        max_inputs: int = 1024,
        max_input_bytes: int = 256 * 1024 * 1024,
    ) -> Store:
        del cls
        entered.set()
        await release.wait()
        return await original_open(path, max_inputs=max_inputs, max_input_bytes=max_input_bytes)

    monkeypatch.setattr(Store, "open", classmethod(delayed_open))
    host = EngineHost(_config(tmp_path))
    waiter = asyncio.create_task(host.start())
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    closing = asyncio.create_task(host.close())
    await asyncio.sleep(0)
    assert host.query().phase == "closing"
    release.set()
    assert (await closing).phase == "closed"
    assert host.query().pending_resources == ()


async def test_start_failure_does_not_hold_directory_lock(tmp_path: Path) -> None:
    (tmp_path / "engine.sqlite3").write_bytes(b"not a SQLite database")
    host = EngineHost(_config(tmp_path))
    with pytest.raises(StorageError):
        await host.start()
    assert host.query().phase == "failed"
    unlocked = InstanceLock(tmp_path)
    unlocked.acquire()
    unlocked.release()
    assert (await host.close()).phase == "closed"


async def test_model_assembly_failure_closes_storage_before_unlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_creation(*args: object, **kwargs: object) -> OpenAIGenerationResource:
        del args, kwargs
        raise ArithmeticError("synthetic assembly failure")

    monkeypatch.setattr(OpenAIGenerationResource, "create", fail_creation)
    host = EngineHost(_config(tmp_path))
    with pytest.raises(ArithmeticError, match="synthetic assembly failure"):
        await host.start()
    assert host.query().phase == "failed"
    assert host.query().pending_resources == ()
    reopened = await Store.open(tmp_path / "engine.sqlite3")
    await reopened.close()
    unlocked = InstanceLock(tmp_path)
    unlocked.acquire()
    unlocked.release()
    assert (await host.close()).phase == "closed"
