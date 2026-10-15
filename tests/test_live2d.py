"""无VTS/声音设备的身体期限、采用确认和恢复不补演回归。

驱动替身只记录真实send响应，替身时钟控制身份失效。音频接口保持
轻量本地操作，网络离线不把已接受意图变成未来要播放的队列。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from purrcept_engine.execution.providers import CallContext
from purrcept_engine.plugin_system.body import BodyIntent
from purrcept_engine.plugins.live2d import analyzer as analyzer_module
from purrcept_engine.plugins.live2d.analyzer import AnalyzerProcess, VisemeFrame
from purrcept_engine.plugins.live2d.models import BodyError, ModelProfile
from purrcept_engine.plugins.live2d.runtime import Live2DRuntime


class Clock:
    """单调时间可重放，不用墙钟sleep推断姿态已失效。"""

    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        return self.now


class Driver:
    """离线视觉协议替身，send返回才是采用确认。"""

    def __init__(self) -> None:
        self.frames: list[dict[str, float]] = []
        self.closed = 0
        self.failure: BodyError | None = None
        self.block = False
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.failed = asyncio.Event()

    async def connect(self) -> None:
        return

    async def send(self, channels: dict[str, float]) -> None:
        self.entered.set()
        if self.block:
            await self.release.wait()
        if self.failure is not None:
            self.failed.set()
            raise self.failure
        self.frames.append(channels)

    async def close(self) -> None:
        self.closed += 1
        self.release.set()


@pytest.mark.parametrize(
    "field,value",
    [
        ("valence", 1.1),
        ("gaze_x", -1.1),
        ("arousal", -0.1),
        ("tension", 1.1),
        ("gesture", "jump"),
        ("playback_id", "model-owned"),
    ],
)
def test_semantic_body_parameters_reject_unknown_identity_and_invalid_ranges(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        BodyIntent.model_validate({field: value})
    assert BodyIntent().model_dump() == {
        "valence": 0.0,
        "arousal": 0.0,
        "tension": 0.0,
        "gaze_x": 0.0,
        "gaze_y": 0.0,
        "gesture": "none",
    }


async def test_playback_adopts_only_actual_start_and_gesture_does_not_restart() -> None:
    clock, driver = Clock(), Driver()
    body = Live2DRuntime("body", lambda: driver, monotonic=clock)
    assert body.playback_started("offline", BodyIntent(gesture="nod"), 20)["accepted"] is False
    await body.connect()
    accepted = body.playback_started("playing", BodyIntent(gesture="nod"), 20)
    assert accepted["adopted"] is False
    await body.step()
    assert body.snapshot()["adopted"] is True
    material = await body.prepare_request()
    assert material["connected"] is True and material["adopted"] is True
    assert material["playback_id"] == "playing"
    assert "remaining_seconds_at_assembly" in material
    assert "expires_at" not in material and "analysis_queued_frames" not in material
    assert "generation" not in material
    clock.now += 0.3
    await body.step()
    gesture = driver.frames[-1]["head_y"]
    clock.now += 1.0
    await body.step()
    neutral = driver.frames[-1]["head_y"]
    assert abs(gesture) > abs(neutral)
    result = body.playback_finished("playing")
    assert result["adopted"] is True
    body.playback_progress("playing", 1.0, 1.0)
    await body.step()
    assert driver.frames[-1]["mouth_open"] == 0
    await body.close()


async def test_body_offline_does_not_keep_old_gesture_for_reconnect() -> None:
    clock = Clock()
    first, second = Driver(), Driver()
    drivers = iter((first, second))
    body = Live2DRuntime("body", lambda: next(drivers), monotonic=clock)
    await body.connect()
    body.playback_started("old", BodyIntent(valence=0.8, gesture="shake"), 20)
    first.failure = BodyError("VTS离线")
    await first.failed.wait()
    assert body.snapshot()["connected"] is False
    assert body.playback_started("offline", BodyIntent(gesture="nod"), 20)["accepted"] is False
    await body.connect()
    assert body.snapshot()["playback_id"] is None
    assert body.snapshot()["intent_id"] is None
    body.playback_finished("old")
    body.playback_progress("old", 2.0, 1.0)
    await body.step()
    assert second.frames[-1]["mouth_open"] == 0
    await body.close()


async def test_independent_apply_is_host_identified_and_expires_after_four_seconds() -> None:
    clock, driver = Clock(), Driver()
    body = Live2DRuntime("body", lambda: driver, monotonic=clock)
    await body.connect()
    result = await body.apply(BodyIntent(valence=0.4), CallContext("host-call", "action", 100))
    assert isinstance(result.value, dict)
    assert result.value["intent_id"] == "host-call"
    assert result.value["expires_at"] == 14.0
    assert result.value["adopted"] is True
    clock.now = 14.01
    await body.step()
    assert body.snapshot()["intent_id"] is None
    await body.close()


async def test_late_frame_confirmation_cannot_revive_finished_playback() -> None:
    clock, driver = Clock(), Driver()
    body = Live2DRuntime("body", lambda: driver, monotonic=clock)
    await body.connect()
    driver.entered.clear()
    driver.block = True
    body.playback_started("old", BodyIntent(gesture="tilt"), 20)
    await driver.entered.wait()
    body.playback_finished("old")
    driver.release.set()
    await asyncio.sleep(0)
    assert body.snapshot()["intent_id"] is None
    assert body.snapshot()["adopted"] is False
    await body.close()


async def test_connect_observer_waits_for_pending_first_visual_frame() -> None:
    """已认证尚未确认首帧时，后来观察者仍等待同一恢复所有者。"""
    driver = Driver()
    driver.block = True
    body = Live2DRuntime("body", lambda: driver)
    await body.start()
    await driver.entered.wait()
    assert body.snapshot()["connected"] is True
    observer = asyncio.create_task(body.connect())
    await asyncio.sleep(0)
    assert not observer.done()
    driver.release.set()
    assert (await observer).state == "confirmed"
    await body.close()


async def test_actual_body_adoption_survives_later_apply_and_device_failure() -> None:
    clock, driver = Clock(), Driver()
    body = Live2DRuntime("body", lambda: driver, monotonic=clock)
    await body.connect()
    body.playback_started("voice", BodyIntent(valence=0.5), 20)
    await body.step()
    await body.apply(BodyIntent(gaze_x=0.3), CallContext("standalone", "action", 20))
    driver.failure = BodyError("视觉传输已断开")
    await driver.failed.wait()
    result = body.playback_finished("voice")
    assert result["accepted"] is True
    assert result["adopted"] is True
    assert result["visual_frame_confirmed"] is True
    assert result["error"] is not None
    await body.close()


def test_profile_unknown_channel_and_duplicate_input_are_not_silent_zeroes() -> None:
    with pytest.raises(ValidationError):
        ModelProfile.model_validate(
            {
                "model_id": "model",
                "bindings": ({"parameter": "PurrceptHead", "sources": {"wrong": 1.0}},),
            }
        )


class Analyzer:
    """独立原生资源替身，阻塞分析不得背压声音或积压无限视觉帧。"""

    def __init__(self) -> None:
        self.entered, self.release, self.produced = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        self.closed = 0

    async def start(self) -> None:
        return

    async def reset(self) -> None:
        return

    async def analyze(self, pcm: bytes, sample: int) -> VisemeFrame:
        del pcm
        self.entered.set()
        await self.release.wait()
        self.produced.set()
        return VisemeFrame(sample, (0.0,) * 10 + (1.0,) + (0.0,) * 4)

    async def close(self) -> None:
        self.closed += 1
        self.release.set()


@pytest.mark.parametrize("failed_task", ["frames", "analysis"])
async def test_old_body_task_error_still_reports_after_actual_resources_released(
    failed_task: str,
) -> None:
    """两个后台任务的原错误都不能冒充连接或本机分析器关闭失败。"""

    class FailedDriver(Driver):
        async def send(self, channels: dict[str, float]) -> None:
            if failed_task == "frames":
                self.entered.set()
                raise RuntimeError("原身体帧故障")
            await super().send(channels)

    class FailedAnalyzer(Analyzer):
        async def analyze(self, pcm: bytes, sample: int) -> VisemeFrame:
            self.entered.set()
            raise RuntimeError("原身体分析故障")

    driver, analyzer = FailedDriver(), FailedAnalyzer()
    body = Live2DRuntime("body", lambda: driver, analyzer_factory=lambda: analyzer)
    assert body.resources_released is False
    if failed_task == "frames":
        with pytest.raises(RuntimeError, match="原身体帧故障"):
            await body.connect()
    else:
        await body.connect()
    if failed_task == "frames":
        await driver.entered.wait()
    else:
        body.playback_pcm("voice", 32000, 0, bytes(1280))
        await analyzer.entered.wait()
    with pytest.raises(BaseExceptionGroup) as first:
        await body.close()
    assert first.value.subgroup(RuntimeError) is not None
    assert body.resources_released is True
    assert driver.closed == analyzer.closed == 1
    with pytest.raises(BaseExceptionGroup) as second:
        await body.close()
    assert second.value is first.value
    assert driver.closed == analyzer.closed == 1


@pytest.mark.parametrize("failing_resource", ["driver", "analyzer"])
async def test_actual_body_close_failure_keeps_release_evidence_false(
    failing_resource: str,
) -> None:
    """任务完成与离线快照均不能替代两份独立资源的实际关闭确认。"""

    class CloseDriver(Driver):
        async def close(self) -> None:
            await super().close()
            if failing_resource == "driver":
                raise RuntimeError("视觉连接实际关闭失败")

    class CloseAnalyzer(Analyzer):
        async def close(self) -> None:
            await super().close()
            if failing_resource == "analyzer":
                raise RuntimeError("分析器实际关闭失败")

    driver, analyzer = CloseDriver(), CloseAnalyzer()
    body = Live2DRuntime("body", lambda: driver, analyzer_factory=lambda: analyzer)
    await body.connect()
    with pytest.raises(BaseExceptionGroup) as first:
        await body.close()
    assert body.resources_released is False
    assert driver.closed == analyzer.closed == 1
    with pytest.raises(BaseExceptionGroup) as second:
        await body.close()
    assert second.value is first.value
    assert driver.closed == analyzer.closed == 1


async def test_body_close_waits_for_resource_borrowed_by_connect() -> None:
    """关闭撤销已开始连接，但仍等待真实资源关闭才取得明证。"""

    class StartingDriver(Driver):
        async def connect(self) -> None:
            self.entered.set()
            await asyncio.Event().wait()

        async def close(self) -> None:
            await self.release.wait()
            await super().close()

    driver = StartingDriver()
    body = Live2DRuntime("body", lambda: driver)
    connecting = asyncio.create_task(body.connect())
    await driver.entered.wait()
    closing = asyncio.create_task(body.close())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not closing.done()
    assert body.resources_released is False
    assert driver.closed == 0
    driver.release.set()
    with pytest.raises(asyncio.CancelledError):
        await connecting
    await closing
    assert body.resources_released is True
    assert driver.closed == 1


@pytest.mark.parametrize("waiting_resource", ["driver", "analyzer"])
async def test_body_release_evidence_waits_for_actual_resource_close(
    waiting_resource: str,
) -> None:
    """后台任务停止之后，资源close的真实确认仍是独立门槛。"""
    entered, release = asyncio.Event(), asyncio.Event()

    class ClosingDriver(Driver):
        async def close(self) -> None:
            if waiting_resource == "driver":
                entered.set()
                await release.wait()
            await super().close()

    class ClosingAnalyzer(Analyzer):
        async def close(self) -> None:
            if waiting_resource == "analyzer":
                entered.set()
                await release.wait()
            await super().close()

    driver, analyzer = ClosingDriver(), ClosingAnalyzer()
    body = Live2DRuntime("body", lambda: driver, analyzer_factory=lambda: analyzer)
    await body.connect()
    closing = asyncio.create_task(body.close())
    await entered.wait()
    assert body.resources_released is False
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert body.resources_released is False
    release.set()
    await body.close()
    assert body.resources_released is True
    assert driver.closed == analyzer.closed == 1


async def test_ovr_prepared_pcm_does_not_adopt_until_actual_playback_clock() -> None:
    clock, driver, analyzer = Clock(), Driver(), Analyzer()
    body = Live2DRuntime("body", lambda: driver, monotonic=clock, analyzer_factory=lambda: analyzer)
    await body.connect()
    body.playback_pcm("voice", 32000, 0, bytes(1280))
    await analyzer.entered.wait()
    analyzer.release.set()
    await analyzer.produced.wait()
    await body.step()
    assert driver.frames[-1]["viseme_aa"] == 0
    body.playback_started("voice", None, 20)
    body.playback_progress("voice", 0.005, 0.2)
    await body.step()
    assert driver.frames[-1]["viseme_aa"] == 1
    body.playback_finished("voice")
    await body.step()
    assert driver.frames[-1]["viseme_aa"] == 0
    await body.close()
    assert analyzer.closed == 1


async def test_slow_ovr_is_bounded_and_finished_playback_discards_late_visemes() -> None:
    clock, driver, analyzer = Clock(), Driver(), Analyzer()
    body = Live2DRuntime("body", lambda: driver, monotonic=clock, analyzer_factory=lambda: analyzer)
    await body.connect()
    body.playback_pcm("voice", 32000, 0, bytes(1280))
    await analyzer.entered.wait()
    body.playback_started("voice", BodyIntent(), 20)
    for index in range(150):
        body.playback_pcm("voice", 32000, (index + 1) * 640, bytes(1280))
    assert body.snapshot()["analysis_queued_frames"] == 100
    assert body.snapshot()["analysis_dropped_frames"] == 50
    body.playback_finished("voice")
    analyzer.release.set()
    await analyzer.produced.wait()
    await body.step()
    assert body.snapshot()["analysis_queued_frames"] == 0
    assert driver.frames[-1]["mouth_open"] == 0
    await body.close()


async def test_native_analyzer_actual_local_process_ipc_and_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []
    worker = (
        "import json,sys\nprint(json.dumps({'ready':True}),flush=True)\n"
        "for raw in sys.stdin.buffer:\n"
        " request=json.loads(raw)\n"
        " result={'reset':True} if request['operation']=='reset' else "
        "{'sample':max(0,request['sample']-64),'weights':[0.0]*10+[1.0]+[0.0]*4}\n"
        " print(json.dumps(result),flush=True)\n"
    )

    async def launch(*arguments: str, **options: object) -> asyncio.subprocess.Process:
        del options
        assert "purrcept_engine.plugins.live2d.analyzer_worker" in arguments
        process = await create(
            sys.executable,
            "-c",
            worker,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(analyzer_module.asyncio, "create_subprocess_exec", launch)
    resource = AnalyzerProcess(Path("not-a-real-dll"), 32000)
    await resource.start()
    await resource.reset()
    frame = await resource.analyze(bytes(1280), 640)
    assert frame.sample == 576 and frame.weights[10] == 1
    await resource.close()
    await resource.close()
    assert processes[0].returncode == 0
