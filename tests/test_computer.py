"""合成图像、离线驱动和真实SQLite验证电脑观察，不读取实际桌面。

单步场景冻结后台采样以精确重放状态；另一场景使用真实后台任务和阻塞驱动
验证断开及迟到资格。Host场景仅执行离线能力，正式截图不产生额外输入。
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from PIL import Image
from pydantic import ValidationError

from purrcept_engine import EngineConfig, EngineHost
from purrcept_engine.app.evidence import EvidenceAccess
from purrcept_engine.errors import StorageError
from purrcept_engine.execution.protocol import decode_object
from purrcept_engine.plugins.computer import driver as driver_module
from purrcept_engine.plugins.computer import plugin
from purrcept_engine.plugins.computer.driver import ProcessDriver
from purrcept_engine.plugins.computer.models import (
    ChangeDetector,
    ComputerError,
    ComputerFrame,
    ForegroundInfo,
    ObservationPolicy,
)
from purrcept_engine.plugins.computer.runtime import ComputerRuntime, load_policy
from purrcept_engine.storage.models import Admission, InputRecord, InputSource, InputSubmission
from purrcept_engine.storage.store import Store
from purrcept_engine.storage.subject import InputProcessing


def frame(color: int = 0, captured: float = 1791878400.0) -> ComputerFrame:
    with Image.new("RGB", (16, 9), (color, color, color)) as image, io.BytesIO() as encoded:
        image.save(encoded, format="JPEG", quality=85)
        data = encoded.getvalue()
    return ComputerFrame(
        data=data,
        captured_at=captured,
        foreground=ForegroundInfo(
            window_id="offline-window",
            title="离线测试窗口",
            process_id=1,
            executable="offline.exe",
            bounds=(-16, 0, 0, 9),
            monitor_id="offline-monitor",
            monitor_bounds=(-16, 0, 0, 9),
        ),
        target="foreground_monitor:offline-monitor",
        original_size=(16, 9),
        delivered_size=(16, 9),
    )


class Inputs:
    """只借正式输入与真实处置查询，不修改主体展示状态。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def submit(self, submission: InputSubmission) -> Admission:
        return await self.store.admit(submission)

    async def query_input(self, input_id: str) -> InputRecord | None:
        return await self.store.get_input(input_id)

    async def query_processing(self, input_id: str) -> InputProcessing | None:
        return await self.store.input_processing(input_id)


class OfflineDriver:
    """可替换合成帧与可打断阻塞，没有屏幕/Win32调用。"""

    def __init__(self) -> None:
        self.started = False
        self.closed = 0
        self.value = frame()
        self.captures: list[bool] = []
        self.entered = asyncio.Event()
        self.released = asyncio.Event()
        self.block = False
        self.failure: RuntimeError | None = None

    @property
    def alive(self) -> bool:
        return self.started and self.closed == 0

    async def start(self) -> None:
        self.started = True

    async def foreground(self) -> ForegroundInfo:
        return self.value.foreground

    async def capture(self, policy: ObservationPolicy, *, native: bool = False) -> ComputerFrame:
        del policy
        self.captures.append(native)
        self.entered.set()
        if self.block:
            await self.released.wait()
        if not self.alive:
            raise ComputerError("离线连接已结束。")
        if self.failure is not None:
            raise self.failure
        return self.value

    async def close(self) -> None:
        self.closed += 1
        self.released.set()


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    database = await Store.open(tmp_path / "computer.sqlite3")
    try:
        yield database
    finally:
        await database.close()


async def controlled(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ComputerRuntime, OfflineDriver, list[float]]:
    driver, clock = OfflineDriver(), [0.0]
    runtime = ComputerRuntime(
        "computer",
        lambda: driver,
        Inputs(store),
        EvidenceAccess(store.knowledge, 8000),
        ObservationPolicy(),
        tmp_path / "preferences.json",
        monotonic=lambda: clock[0],
    )

    async def no_background() -> None:
        """单步测试由测试所有者调用step，不允许真实计时产生第二次采样。"""

    monkeypatch.setattr(runtime, "_run", no_background)
    await runtime.connect()
    return runtime, driver, clock


@pytest.mark.parametrize(
    "payload",
    (
        {"target": "monitor"},
        {"changed_blocks": True},
        {"threshold": float("nan")},
        {"min_interval": 2.0, "max_interval": 0.25},
        {"analysis_interval": 9.0},
    ),
)
def test_observation_policy_rejects_invalid_real_boundaries(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ObservationPolicy.model_validate(payload)


def test_change_detector_keeps_cumulative_basis_until_actual_admission() -> None:
    detector, policy = ChangeDetector(), ObservationPolicy()
    changed, initial = detector.sample(frame(0), policy)
    assert changed
    detector.committed(initial)
    assert not detector.sample(frame(5), policy)[0]
    assert detector.sample(frame(10), policy)[0]
    assert detector.submitted == initial


@pytest.mark.asyncio
async def test_explicit_connect_does_not_capture_and_snapshot_does_not_read_devices(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, driver, _ = await controlled(store, tmp_path, monkeypatch)
    try:
        assert driver.started and driver.captures == []
        first = runtime.snapshot()
        assert first["connected"] and not first["observing"] and runtime.preview() is None
        assert (await runtime.connect())["connection_id"] == first["connection_id"]
        assert (await runtime.foreground())["window_id"] == "offline-window"
        assert driver.captures == [] and "result" not in runtime.snapshot()
    finally:
        await runtime.close()
    assert driver.closed == 1


@pytest.mark.asyncio
async def test_screenshot_has_full_blob_authority_and_does_not_create_pending_input(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, driver, _ = await controlled(store, tmp_path, monkeypatch)
    try:
        result = await runtime.screenshot(None, None)
        assert driver.captures == [True] and len(result.media) == 1
        assert await store.get_media(result.media[0].media_id) == driver.value.data
        assert not await store.pending()
        assert isinstance(result.value, dict)
        reference = result.value["evidence_ref"]
        assert isinstance(reference, str)
        saved = await store.knowledge.get_evidence(reference)
        assert saved is not None and saved.media == result.media
        assert decode_object(saved.source_json.encode())["foreground"] == (
            driver.value.foreground.model_dump(mode="json")
        )
        material = await runtime.prepare_request()
        assert material["connected"] is True and material["captured_at"] == driver.value.captured_at
        assert material["last_window_title"] == "离线测试窗口"
        assert "offline.exe" not in str(material) and "offline-window" not in str(material)
        assert "policy" not in material and "generation" not in material
        assert driver.captures == [True]  # 组装上下文不额外采集桌面。
        with pytest.raises(ValidationError):
            await runtime.screenshot("monitor", None)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_one_admitted_frame_and_replaceable_candidate_wait_for_real_processed_stage(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, driver, clock = await controlled(store, tmp_path, monkeypatch)
    try:
        await runtime.observe("start")
        await runtime.step()
        pending = await store.pending()
        assert len(pending) == 1
        original = pending[0]
        driver.value, clock[0] = frame(50), 20.0
        await runtime.step()
        driver.value = frame(200)
        await runtime.step()
        assert len(await store.pending()) == 1 and runtime.snapshot()["candidate"]
        assert runtime.snapshot()["input_id"] == original.input_id
        processing = await store.input_processing(original.input_id)
        assert processing is not None and processing.stage == "foreground"
        assert original.context.minimum_wake and not original.context.interrupt_generation
        await store.complete_input(original.input_id)
        await runtime.step()
        assert runtime.snapshot()["input_id"] is None
        clock[0] = 31.0
        await runtime.step()
        newer = await store.pending()
        assert len(newer) == 1 and newer[0].input_id != original.input_id
        assert await store.get_media(newer[0].media[0].media_id) == driver.value.data
        assert await store.get_media(original.media[0].media_id) != driver.value.data
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_pending_waits_and_real_failed_turn_pauses_without_new_input(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, driver, clock = await controlled(store, tmp_path, monkeypatch)
    try:
        await runtime.observe("start")
        await runtime.step()
        original = (await store.pending())[0]
        processing = await store.input_processing(original.input_id)
        assert processing is not None and processing.stage == "foreground"
        clock[0], driver.value = 50.0, frame(200)
        await runtime.step()
        assert (
            len(await store.pending()) == 1 and runtime.snapshot()["input_id"] == original.input_id
        )
        turn = await store.begin_turn("failed", "foreground", 0, 1, (original.input_id,))
        await store.finish_turn(turn, "failed", error_json='{"error_type":"OfflineCapacity"}')
        await runtime.step()
        assert not runtime.snapshot()["observing"]
        assert runtime.snapshot()["input_id"] == original.input_id
        assert runtime.snapshot()["error"] is not None
        count = len(driver.captures)
        await runtime.step()
        assert len(driver.captures) == count
        assert await store.get_input(original.input_id) is not None
        old_generation = runtime.snapshot()["generation"]
        await runtime.observe("start")
        assert runtime.snapshot()["generation"] != old_generation
        await runtime.step()
        assert runtime.snapshot()["input_id"] != original.input_id
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_background_disconnect_ends_blocked_capture_and_rejects_late_preview(
    store: Store, tmp_path: Path
) -> None:
    driver = OfflineDriver()
    driver.block = True
    runtime = ComputerRuntime(
        "computer",
        lambda: driver,
        Inputs(store),
        EvidenceAccess(store.knowledge, 8000),
        ObservationPolicy(),
        tmp_path / "preferences.json",
    )
    try:
        await runtime.connect()
        await runtime.observe("start")
        await asyncio.wait_for(driver.entered.wait(), 1)
        result = await asyncio.wait_for(runtime.disconnect(), 1)
        assert not result["connected"] and runtime.preview() is None
        assert not await store.pending() and driver.closed == 1
        with pytest.raises(ComputerError):
            await runtime.foreground()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_observation_preferences_survive_reopen_but_do_not_restore_connection(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _, _ = await controlled(store, tmp_path, monkeypatch)
    await runtime.observe("start")
    await runtime.close()
    restored = await asyncio.to_thread(load_policy, tmp_path / "preferences.json")
    assert restored.enabled
    driver = OfflineDriver()
    unopened = ComputerRuntime(
        "computer",
        lambda: driver,
        Inputs(store),
        EvidenceAccess(store.knowledge, 8000),
        restored,
        tmp_path / "preferences.json",
    )
    try:
        assert not unopened.snapshot()["connected"] and not unopened.snapshot()["observing"]
        assert not driver.started
    finally:
        await unopened.close()


@pytest.mark.asyncio
async def test_owned_process_ready_and_close_never_read_actual_desktop() -> None:
    driver = ProcessDriver()
    await driver.start()
    assert driver.alive
    await driver.close()
    assert not driver.alive
    await driver.close()


@pytest.mark.asyncio
async def test_cancelled_process_start_observer_does_not_leave_late_process() -> None:
    driver = ProcessDriver()
    observer = asyncio.create_task(driver.start())
    await asyncio.sleep(0)
    observer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observer
    await driver.close()
    assert not driver.alive


@pytest.mark.asyncio
async def test_pipe_allocation_failure_reclaims_pair_before_job_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_pipe = os.pipe
    allocated: list[int] = []

    def limited() -> tuple[int, int]:
        if allocated:
            raise OSError("离线管道额度不足")
        pair = real_pipe()
        allocated.extend(pair)
        return pair

    monkeypatch.setattr(driver_module.os, "pipe", limited)
    driver = ProcessDriver()
    try:
        with pytest.raises(OSError, match="额度"):
            await driver.start()
        for descriptor in allocated:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_real_job_pipe_transfers_synthetic_frame_without_desktop_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = frame(200)
    payload = sample.model_dump(mode="python", exclude={"data"})
    encoded = {
        **payload,
        "data": base64.b64encode(sample.data).decode("ascii"),
        "foreground": sample.foreground.model_dump(mode="json"),
    }
    worker = (
        "import io,json,msvcrt,os,struct,sys\n"
        "assert 'PURRCEPT_TEST_SECRET' not in os.environ\n"
        "incoming=os.fdopen(msvcrt.open_osfhandle(int(sys.argv[1]),os.O_RDONLY|os.O_BINARY),'rb')\n"
        "outgoing=os.fdopen(msvcrt.open_osfhandle(int(sys.argv[2]),os.O_WRONLY|os.O_BINARY),'wb')\n"
        f"sample={encoded!r}\n"
        "while header:=incoming.read(4):\n"
        " command=json.loads(incoming.read(struct.unpack('!I',header)[0]))['command']\n"
        " result={'ready':True} if command=='ready' else (sample['foreground'] "
        "if command=='foreground' else sample)\n"
        " payload=json.dumps({'result':result},allow_nan=False).encode()\n"
        " outgoing.write(struct.pack('!I',len(payload))+payload);outgoing.flush()\n"
    )
    await asyncio.to_thread((tmp_path / "worker.py").write_text, worker, encoding="utf-8")
    monkeypatch.setattr(driver_module, "__file__", str(tmp_path / "driver.py"))
    monkeypatch.setenv("PURRCEPT_TEST_SECRET", "synthetic-host-only")
    driver = ProcessDriver()
    try:
        await driver.start()
        assert await driver.foreground() == sample.foreground
        actual = await driver.capture(ObservationPolicy(), native=True)
        assert actual == sample
    finally:
        await driver.close()
    assert not driver.alive


@pytest.mark.asyncio
async def test_cancelled_admission_and_close_observers_keep_actual_input_and_media(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, driver, _ = await controlled(store, tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = Inputs.submit

    async def held(inputs: Inputs, submission: InputSubmission) -> Admission:
        entered.set()
        await release.wait()
        return await original(inputs, submission)

    monkeypatch.setattr(Inputs, "submit", held)
    await runtime.observe("start")
    step = asyncio.create_task(runtime.step())
    await entered.wait()
    step.cancel()
    with pytest.raises(asyncio.CancelledError):
        await step
    closer = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer
    release.set()
    await runtime.close()
    pending = await store.pending()
    assert len(pending) == 1
    assert await store.get_media(pending[0].media[0].media_id) == driver.value.data
    assert driver.closed == 1


@pytest.mark.asyncio
async def test_late_failed_processing_cannot_pause_new_observation_generation(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _, _ = await controlled(store, tmp_path, monkeypatch)
    try:
        await runtime.observe("start")
        await runtime.step()
        input_id = (await store.pending())[0].input_id
        stage = await store.input_processing(input_id)
        assert stage is not None and stage.stage == "foreground"
        turn = await store.begin_turn("failure", "foreground", 0, 1, (input_id,))
        await store.finish_turn(turn, "failed", error_json='{"error_type":"OfflineFailure"}')
        entered, release = asyncio.Event(), asyncio.Event()
        original = Inputs.query_processing

        async def held(inputs: Inputs, value: str) -> InputProcessing | None:
            old = await original(inputs, value)
            entered.set()
            await release.wait()
            return old

        monkeypatch.setattr(Inputs, "query_processing", held)
        pending_step = asyncio.create_task(runtime.step())
        await entered.wait()
        await runtime.observe("start")
        release.set()
        await pending_step
        assert runtime.snapshot()["observing"] and runtime.snapshot()["error"] is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_real_sampler_capture_failure_pauses_and_close_still_releases_driver(
    store: Store, tmp_path: Path
) -> None:
    driver = OfflineDriver()
    driver.failure = RuntimeError("离线采集故障")
    runtime = ComputerRuntime(
        "computer",
        lambda: driver,
        Inputs(store),
        EvidenceAccess(store.knowledge, 8000),
        ObservationPolicy(),
        tmp_path / "preferences.json",
    )
    await runtime.connect()
    await runtime.observe("start")
    await asyncio.wait_for(driver.entered.wait(), 1)
    assert runtime.preview() is None and not await store.pending()
    assert runtime.snapshot()["error"] is not None
    with pytest.raises(BaseExceptionGroup) as close:
        await runtime.close()
    assert isinstance(close.value.exceptions[0], RuntimeError) and driver.closed == 1


@pytest.mark.asyncio
async def test_real_sampler_capacity_failure_pauses_until_explicit_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 10, 13, tzinfo=UTC)
    database = await Store.open(tmp_path / "capacity.sqlite3", max_inputs=1)
    existing = await database.admit(
        InputSubmission(InputSource("test", "local", "local", "test"), "占位", now, now)
    )
    driver = OfflineDriver()
    runtime = ComputerRuntime(
        "computer",
        lambda: driver,
        Inputs(database),
        EvidenceAccess(database.knowledge, 8000),
        ObservationPolicy(),
        tmp_path / "preferences.json",
    )
    rejected, received = asyncio.Event(), asyncio.Event()
    original_submit = Inputs.submit

    async def observed(inputs: Inputs, submission: InputSubmission) -> Admission:
        try:
            result = await original_submit(inputs, submission)
        except StorageError:
            rejected.set()
            raise
        received.set()
        return result

    monkeypatch.setattr(Inputs, "submit", observed)
    try:
        await runtime.connect()
        await runtime.observe("start")
        await asyncio.wait_for(rejected.wait(), 1)
        assert len(await database.pending()) == 1
        assert runtime.snapshot()["candidate"] and runtime.snapshot()["error"] is not None
        await database.complete_input(existing.input_id)
        await runtime.observe("start")
        await asyncio.wait_for(received.wait(), 1)
        assert len(await database.pending()) == 1 and runtime.snapshot()["observing"]
        assert runtime.snapshot()["error"] is None
    finally:
        try:
            await runtime.close()
        except BaseExceptionGroup as failure:
            assert all(isinstance(item, StorageError) for item in failure.exceptions)
        await database.close()


@pytest.mark.asyncio
async def test_actual_host_worker_persists_native_screenshot_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = OfflineDriver()
    monkeypatch.setattr(plugin, "factory", plugin.make_factory(lambda: driver))
    payload: dict[str, object] = {
        "host": {"data_dir": tmp_path / "data"},
        "console": False,
        "models": {
            "test": {
                "kind": "generation",
                "model": "openai/offline",
                "api_base": "http://127.0.0.1:9999/v1",
                "api_key": "synthetic",
                "context_window": 100000,
                "max_output_tokens": 1000,
                "capacity_rule": "reference-text-only-v1",
            }
        },
        "subject": {"foreground": "test", "reflection": "test", "summary": "test"},
        "plugins": [
            {
                "instance_id": "computer",
                "entry": "purrcept_engine.plugins.computer.plugin:factory",
                "config": {},
            }
        ],
    }
    host = EngineHost(EngineConfig.validate(payload, base_dir=tmp_path))
    await host.start()
    try:
        action = await host.execute(
            "from world import me\nawait me.computer.connect()\n"
            "await me.computer.foreground()\nreturn await me.computer.screenshot()"
        )
        assert action.state == "completed" and len(action.calls) == 3
        assert len(action.calls[-1].media) == 1
        assert driver.captures == [True]
        assert {path for path in host.catalog.bindings if path.startswith("me.computer.")} == {
            "me.computer.status",
            "me.computer.connect",
            "me.computer.disconnect",
            "me.computer.foreground",
            "me.computer.screenshot",
            "me.computer.observe",
        }
    finally:
        await host.close()
