"""真实目录锁、输入接管及关闭等待的宿主边界验证，不启动模型或设备。"""

from __future__ import annotations

import asyncio
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


@pytest.mark.parametrize("supports", [True, False])
async def test_native_audio_admission_follows_declared_transport_support(
    tmp_path: Path, supports: bool
) -> None:
    """统一估算无需额外登记，原声准入由前台音频能力声明决定。"""
    observed: list[bool] = []

    async def build(ports: PluginPorts) -> PluginRuntime:
        observed.append(ports.native_audio_enabled)
        return PluginRuntime(())

    def prepare(instance: str, raw: object, base: Path) -> PluginBlueprint:
        del instance, raw, base
        return PluginBlueprint((), (), build)

    raw = _config(tmp_path).model_dump()
    raw["models"]["test"].update(
        supports_contents=["text", "audio"] if supports else ["text"],
    )
    raw["plugins"] = [{"instance_id": "probe", "entry": "probe"}]
    host = EngineHost(
        EngineConfig.validate(raw, base_dir=tmp_path),
        plugin_factories={"probe": PluginFactory(prepare)},
    )
    await host.start()
    try:
        assert observed == [supports]
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


async def test_coordinator_crash_stops_dispatch_but_keeps_history_queryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实协调任务崩溃后生命周期仍独立，面板不能从空闲推导正常待命。"""
    from purrcept_engine.domain.subject.coordinator import StimulusCoordinator

    trigger = asyncio.Event()
    fault = LookupError("synthetic-private-fault")

    async def fail(coordinator: StimulusCoordinator) -> None:
        del coordinator
        await trigger.wait()
        raise fault

    monkeypatch.setattr(StimulusCoordinator, "run", fail)
    host = EngineHost(_config(tmp_path))
    await host.start()
    try:
        assert host.query_health()["can_process"] is True
        trigger.set()
        async with asyncio.timeout(3):
            while True:
                if host.query_health()["can_process"] is False:
                    break
                await asyncio.sleep(0)
        assert host.query().phase == "running"
        panel = await host.query_panel()
        assert panel["authority_available"] is True
        assert panel["recent_actions"] == []
        assert panel["health"] == host.query_health()
        with pytest.raises(LookupError) as rejected:
            await host.submit(_submission())
        assert rejected.value is fault
    finally:
        with pytest.raises(BaseExceptionGroup):
            await host.close()
    assert host.query().phase == "failed" and host.query().pending_resources == ()
    unlocked = InstanceLock(tmp_path)
    unlocked.acquire()
    unlocked.release()


async def test_starting_plugin_can_admit_input_but_public_operations_wait_until_ready(
    tmp_path: Path,
) -> None:
    """插件启动可先存输入，UI 与模型不能趁生命周期 running 提前派发行动。"""
    from purrcept_core.models import Message, ModelEventSink, ModelRequest, ModelResponse

    entered, release, called = asyncio.Event(), asyncio.Event(), asyncio.Event()
    admitted: list[str] = []

    class Backend:
        """只报告实际模型派发，测试不接触网络。"""

        async def generate(
            self,
            request: ModelRequest,
            *,
            model: str,
            emit: ModelEventSink | None = None,
        ) -> ModelResponse:
            """记录真实 Core 请求已跨过派发门槛，再返回内部文本。"""
            del request, model, emit
            called.set()
            return ModelResponse(Message.assistant("合成内部结果。"))

    async def build(ports: PluginPorts) -> PluginRuntime:
        async def start() -> None:
            """模拟主动输入插件在连接尚未完成时交付第一条输入。"""
            admission = await ports.inputs.submit(_submission())
            admitted.append(admission.input_id)
            entered.set()
            await release.wait()

        return PluginRuntime((), start=start)

    def prepare(instance: str, raw: object, base: Path) -> PluginBlueprint:
        del instance, raw, base
        return PluginBlueprint((), (), build)

    raw = _config(tmp_path).model_dump()
    raw["models"]["test"]["context_window"] = 100000
    raw["plugins"] = [{"instance_id": "probe", "entry": "probe"}]
    host = EngineHost(
        EngineConfig.validate(raw, base_dir=tmp_path),
        plugin_factories={"probe": PluginFactory(prepare)},
        generation_backends={"test": Backend()},
    )
    starting = asyncio.create_task(host.start())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        health = host.query_health()
        assert health["phase"] == "running" and health["can_process"] is False
        assert health["startup_stage"] == "plugin_start:probe"
        assert await host.query_input(admitted[0]) is not None
        assert not called.is_set()
        with pytest.raises(HostStateError, match="启动"):
            await host.execute("pass")
        with pytest.raises(HostStateError):
            await host.query_panel()
        release.set()
        await starting
        await asyncio.wait_for(called.wait(), 5)
        assert host.query_health()["can_process"] is True
    finally:
        release.set()
        await starting
        await host.close()


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
