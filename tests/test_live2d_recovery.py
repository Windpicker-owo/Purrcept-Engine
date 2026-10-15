"""有限 VTS 恢复、取消所有权与恢复后的表达边界。

连接和视素分析均使用明确资源替身；只替换恢复等待，不访问设备。
次数、取消和真实释放分别断言，避免以离线快照代替收尾证明。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from purrcept_engine import EngineConfig, EngineHost, InstanceLocked
from purrcept_engine.errors import HostStateError
from purrcept_engine.infrastructure.locking import InstanceLock
from purrcept_engine.plugin_system.body import BodyIntent
from purrcept_engine.plugins.live2d import runtime as runtime_module
from purrcept_engine.plugins.live2d.analyzer import VisemeFrame
from purrcept_engine.plugins.live2d.models import (
    VISEMES,
    BodyError,
    BodyStyle,
    BodyUnavailable,
    ModelProfile,
)
from purrcept_engine.plugins.live2d.plugin import Live2DSettings, make_factory
from purrcept_engine.plugins.live2d.runtime import Live2DRuntime

_SLEEP = asyncio.sleep


class Driver:
    """逐项记录连接和关闭，支持真实等待中的取消与清理失败。"""

    def __init__(
        self,
        *,
        connection_failure: BodyError | None = None,
        blocked_connect: bool = False,
        blocked_close: bool = False,
        frame_yields: bool = False,
        close_failure: RuntimeError | None = None,
    ) -> None:
        self.connection_failure = connection_failure
        self.frame_failure: BodyError | None = None
        self.blocked_connect, self.blocked_close = blocked_connect, blocked_close
        self.frame_yields = frame_yields
        self.close_failure = close_failure
        self.connect_entered, self.connect_release = asyncio.Event(), asyncio.Event()
        self.connect_cancelled = asyncio.Event()
        self.send_entered, self.send_failed = asyncio.Event(), asyncio.Event()
        self.close_entered, self.close_release = asyncio.Event(), asyncio.Event()
        self.close_attempts = 0
        self.closed = False
        self.frames: list[dict[str, float]] = []

    async def connect(self) -> None:
        """可暂停在原生授权期间，取消保持原异常并留下观察事实。"""
        self.connect_entered.set()
        if self.blocked_connect:
            try:
                await self.connect_release.wait()
            except asyncio.CancelledError:
                self.connect_cancelled.set()
                raise
        if self.connection_failure is not None:
            raise self.connection_failure

    async def send(self, channels: dict[str, float]) -> None:
        """返回只表示本帧真实采用，失败不会写入已确认帧列表。"""
        self.send_entered.set()
        if self.frame_yields:
            await _SLEEP(0)
        if self.frame_failure is not None:
            self.send_failed.set()
            raise self.frame_failure
        self.frames.append(channels)

    async def close(self) -> None:
        """关闭确认独立于连接任务结束，允许检验未归还资源。"""
        self.close_attempts += 1
        self.close_entered.set()
        if self.blocked_close:
            await self.close_release.wait()
        if self.close_failure is not None:
            raise self.close_failure
        self.closed = True


class Factory:
    """给每次连接尝试独立资源，额外尝试立即失败。"""

    def __init__(self, *drivers: Driver) -> None:
        self.drivers = drivers
        self.calls = 0

    def __call__(self) -> Driver:
        """按批准次数分配下一资源，不能重用已关闭的连接。"""
        assert self.calls < len(self.drivers), "恢复超出本轮允许的连接尝试。"
        driver = self.drivers[self.calls]
        self.calls += 1
        return driver


class Backoff:
    """保存精确恢复等待；测试调度仍使用原始零等待。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, blocked: bool = False) -> None:
        self.delays: list[float] = []
        self.blocked = blocked
        self.entered, self.release, self.cancelled = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        monkeypatch.setattr(runtime_module.asyncio, "sleep", self.sleep)

    async def sleep(self, delay: float) -> None:
        """有界恢复等待可重放，取消不被替身吞掉。"""
        if delay > 0:
            self.delays.append(delay)
            self.entered.set()
            if self.blocked:
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise
        await _SLEEP(0)


async def settled(predicate: Callable[[], bool]) -> None:
    """仅推进有限事件循环轮次，等待已创建任务交付状态。"""
    for _ in range(100):
        if predicate():
            return
        await _SLEEP(0)
    raise AssertionError("恢复任务没有在有限调度内交付预期状态。")


async def test_initial_unavailability_exhausts_exactly_five_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backoff = Backoff(monkeypatch)
    drivers = tuple(Driver(connection_failure=BodyUnavailable("连接不可用")) for _ in range(5))
    factory = Factory(*drivers)
    body = Live2DRuntime("body", factory)

    outcome = await body.connect()

    assert outcome.state == "failed" and outcome.error_type == "BodyUnavailable"
    assert factory.calls == 5
    assert backoff.delays == [0.5, 1.0, 2.0, 4.0]
    assert all(driver.closed and driver.close_attempts == 1 for driver in drivers)
    assert body.snapshot()["connection_attempt"] == 5
    assert body.snapshot()["connected"] is False
    assert body.snapshot()["recovering"] is False
    await _SLEEP(0)
    assert factory.calls == 5
    await body.close()
    assert body.resources_released is True


@pytest.mark.parametrize("success_attempt", [1, 3, 5])
async def test_recovery_stops_at_first_success(
    monkeypatch: pytest.MonkeyPatch, success_attempt: int
) -> None:
    backoff = Backoff(monkeypatch)
    failures = tuple(
        Driver(connection_failure=BodyUnavailable("暂时失联")) for _ in range(success_attempt - 1)
    )
    online = Driver()
    factory = Factory(*failures, online)
    body = Live2DRuntime("body", factory)

    outcome = await body.connect()

    assert outcome.state == "confirmed"
    assert factory.calls == success_attempt
    assert backoff.delays == [0.5, 1.0, 2.0, 4.0][: success_attempt - 1]
    assert all(driver.closed for driver in failures)
    assert online.frames and not online.closed
    assert body.snapshot()["connected"] is True
    assert body.snapshot()["generation"] == 1
    await body.close()
    assert online.closed and body.resources_released


@pytest.mark.parametrize("frame_yields", [False, True])
async def test_first_frame_disconnect_does_not_refresh_recovery_budget(
    monkeypatch: pytest.MonkeyPatch, frame_yields: bool
) -> None:
    backoff = Backoff(monkeypatch)
    drivers = tuple(Driver(frame_yields=frame_yields) for _ in range(5))
    for driver in drivers:
        driver.frame_failure = BodyUnavailable("首帧立即断开")
    factory = Factory(*drivers)
    body = Live2DRuntime("body", factory)

    outcome = await body.connect()

    assert outcome.state == "failed"
    assert factory.calls == 5
    assert backoff.delays == [0.5, 1.0, 2.0, 4.0]
    assert all(driver.send_failed.is_set() for driver in drivers)
    assert body.snapshot()["connected"] is False
    assert body.snapshot()["recovering"] is False
    await body.close()
    assert all(driver.closed for driver in drivers)
    assert body.resources_released is True


async def test_later_disconnect_opens_one_new_finite_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backoff = Backoff(monkeypatch)
    first, restored = Driver(), Driver()
    failures = tuple(Driver(connection_failure=BodyUnavailable("恢复仍失联")) for _ in range(4))
    factory = Factory(first, *failures, restored)
    body = Live2DRuntime("body", factory)
    assert (await body.connect()).state == "confirmed"
    assert factory.calls == 1 and backoff.delays == []

    first.frame_failure = BodyUnavailable("在线连接随后失联")
    body.playback_started("voice", BodyIntent(), 1e12)
    await asyncio.wait_for(first.send_failed.wait(), 1)
    await asyncio.wait_for(restored.send_entered.wait(), 1)
    await settled(lambda: body.snapshot()["recovering"] is False)

    assert factory.calls == 6
    assert backoff.delays == [0.5, 1.0, 2.0, 4.0]
    assert first.closed and all(driver.closed for driver in failures)
    assert body.snapshot()["connection_attempt"] == 5
    assert body.snapshot()["generation"] == 2
    assert body.snapshot()["connected"] is True
    assert body.snapshot()["playback_id"] is None
    await body.close()
    assert restored.closed and body.resources_released


@pytest.mark.parametrize("failure_phase", ["connect", "frame"])
async def test_terminal_body_error_does_not_retry(
    monkeypatch: pytest.MonkeyPatch, failure_phase: str
) -> None:
    backoff = Backoff(monkeypatch)
    rejection = BodyError("认证拒绝或协议无效")
    driver = Driver(connection_failure=rejection if failure_phase == "connect" else None)
    factory = Factory(driver)
    body = Live2DRuntime("body", factory)

    outcome = await body.connect()
    if failure_phase == "connect":
        assert outcome.state == "failed" and outcome.error_type == "BodyError"
    else:
        assert outcome.state == "confirmed"
        driver.frame_failure = rejection
        body.playback_started("voice", BodyIntent(), 1e12)
        await asyncio.wait_for(driver.send_failed.wait(), 1)
    await settled(lambda: body.snapshot()["recovering"] is False)

    assert factory.calls == 1 and backoff.delays == []
    assert body.snapshot()["connected"] is False
    assert body.snapshot()["error"] == {
        "error_type": "BodyError",
        "message": "认证拒绝或协议无效",
    }
    await body.close()
    assert driver.closed and body.resources_released


async def test_cancelled_connect_observer_does_not_cancel_shared_owner() -> None:
    driver = Driver(blocked_connect=True)
    factory = Factory(driver)
    body = Live2DRuntime("body", factory)
    first = asyncio.create_task(body.connect())
    second = asyncio.create_task(body.connect())
    await asyncio.wait_for(driver.connect_entered.wait(), 1)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not driver.connect_cancelled.is_set()
    assert body.snapshot()["recovering"] is True
    assert factory.calls == 1 and not second.done()

    driver.connect_release.set()
    assert (await second).state == "confirmed"
    assert factory.calls == 1 and not driver.connect_cancelled.is_set()
    await body.close()
    assert driver.close_attempts == 1 and body.resources_released


async def test_stop_inputs_cancels_backoff_without_another_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backoff = Backoff(monkeypatch, blocked=True)
    driver = Driver(connection_failure=BodyUnavailable("初次失联"))
    factory = Factory(driver)
    body = Live2DRuntime("body", factory)
    await body.start()
    await asyncio.wait_for(backoff.entered.wait(), 1)

    await body.stop_inputs()
    await asyncio.wait_for(backoff.cancelled.wait(), 1)
    await body.close()

    assert factory.calls == 1 and backoff.delays == [0.5]
    assert driver.closed and driver.close_attempts == 1
    assert body.resources_released is True
    assert body.snapshot()["connected"] is False
    with pytest.raises(HostStateError):
        await body.start()
    with pytest.raises(HostStateError):
        await body.connect()


async def test_start_returns_before_connect_and_close_waits_for_actual_cleanup() -> None:
    driver = Driver(blocked_connect=True, blocked_close=True)
    factory = Factory(driver)
    body = Live2DRuntime("body", factory)

    await body.start()
    await asyncio.wait_for(driver.connect_entered.wait(), 1)
    assert body.snapshot()["connected"] is False
    closing = asyncio.create_task(body.close())
    await asyncio.wait_for(driver.connect_cancelled.wait(), 1)
    await asyncio.wait_for(driver.close_entered.wait(), 1)

    assert not closing.done() and not body.resources_released
    assert not driver.closed and driver.close_attempts == 1
    await body.stop_inputs()
    await _SLEEP(0)
    assert not closing.done()
    driver.close_release.set()
    await closing
    assert factory.calls == driver.close_attempts == 1
    assert driver.closed and body.resources_released


async def test_stop_during_failed_connect_cleanup_waits_without_cancel_or_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预期连接失败开始清理后，停止只撤资格，不能把真实 close 取消成故障。"""
    backoff = Backoff(monkeypatch)
    driver = Driver(connection_failure=BodyUnavailable("连接失败"), blocked_close=True)
    factory = Factory(driver)
    body = Live2DRuntime("body", factory)
    await body.start()
    await asyncio.wait_for(driver.close_entered.wait(), 1)

    await body.stop_inputs()
    closing = asyncio.create_task(body.close())
    await _SLEEP(0)
    await _SLEEP(0)
    assert not closing.done() and not driver.closed
    assert driver.close_attempts == 1 and factory.calls == 1
    assert backoff.delays == [] and not body.resources_released

    driver.close_release.set()
    await closing
    assert driver.closed and driver.close_attempts == 1
    assert factory.calls == 1 and backoff.delays == []
    assert body.resources_released is True
    assert body.snapshot()["connected"] is False
    assert body.snapshot()["recovering"] is False


@pytest.mark.parametrize("withdrawal", ["stop", "disconnect"])
async def test_withdrawal_during_old_driver_cleanup_prevents_new_factory(
    monkeypatch: pytest.MonkeyPatch, withdrawal: str
) -> None:
    """旧在线资源归还后必须重新核验恢复资格，不能创建已被撤销的下一连接。"""
    backoff = Backoff(monkeypatch)
    driver = Driver(blocked_close=True)
    factory = Factory(driver)
    body = Live2DRuntime("body", factory)
    assert (await body.connect()).state == "confirmed"
    driver.frame_failure = BodyUnavailable("旧在线连接失联")
    body.playback_started("voice", BodyIntent(), 1e12)
    await asyncio.wait_for(driver.send_failed.wait(), 1)
    await asyncio.wait_for(driver.close_entered.wait(), 1)

    disconnecting = None
    if withdrawal == "stop":
        await body.stop_inputs()
    else:
        disconnecting = asyncio.create_task(body.disconnect())
        await _SLEEP(0)
        assert not disconnecting.done()
    assert factory.calls == 1 and driver.close_attempts == 1
    assert not driver.closed and not body.resources_released
    assert backoff.delays == []

    driver.close_release.set()
    if withdrawal == "disconnect":
        assert disconnecting is not None
        assert (await disconnecting).state == "confirmed"
    await body.close()

    assert driver.closed and driver.close_attempts == 1
    assert factory.calls == 1 and backoff.delays == []
    assert body.snapshot()["connected"] is False
    assert body.snapshot()["recovering"] is False
    assert body.resources_released is True


@pytest.mark.parametrize("release_evidence", [False, True])
async def test_old_io_error_reports_without_discarding_explicit_release_evidence(
    release_evidence: bool,
) -> None:
    """历史 I/O 错误必须保留，唯有独立明证允许宿主继续释放其依赖。"""
    failure = BodyError("已观察的令牌 I/O 故障")

    class ReleasedDriver(Driver):
        @property
        def resources_released(self) -> bool:
            """替身只有实际 close 结束后才提供明确释放事实。"""
            return release_evidence and self.closed

        async def close(self) -> None:
            """先归还连接，再原样报告历史 I/O 故障。"""
            await super().close()
            raise failure

    driver = ReleasedDriver()
    body = Live2DRuntime("body", lambda: driver)
    assert (await body.connect()).state == "confirmed"

    with pytest.raises(BaseExceptionGroup) as first:
        await body.close()
    assert first.value.exceptions == (failure,)
    assert driver.closed and driver.close_attempts == 1
    assert body.resources_released is release_evidence
    with pytest.raises(BaseExceptionGroup) as second:
        await body.close()
    assert second.value is first.value
    assert driver.close_attempts == 1
    assert body.resources_released is release_evidence


async def test_actual_host_can_close_during_plugin_native_authorization(tmp_path: Path) -> None:
    """真实装配必须接入非阻塞 start，SQLite 和 OS 锁等视觉清理确认后释放。"""
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        '{"model_id":"fixture","bindings":'
        '[{"parameter":"PurrceptSmile","sources":{"valence":1.0}}]}',
        encoding="utf-8",
    )
    driver = Driver(blocked_connect=True, blocked_close=True)

    def factory(settings: Live2DSettings, path: Path, profile: ModelProfile) -> Driver:
        assert settings.live2d_id == "live2d"
        assert path == tmp_path / ".purrcept" / "live2d" / "token"
        assert profile.model_id == "fixture"
        return driver

    data_dir = tmp_path / "data"
    config = EngineConfig.validate(
        {
            "host": {"data_dir": data_dir},
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
            "plugins": [
                {
                    "instance_id": "body",
                    "entry": "fixture",
                    "config": {"profile_path": "profile.json"},
                }
            ],
        },
        base_dir=tmp_path,
    )
    host = EngineHost(config, plugin_factories={"fixture": make_factory(factory)})
    other = InstanceLock(data_dir)
    try:
        assert (await asyncio.wait_for(host.start(), 2)).phase == "running"
        await asyncio.wait_for(driver.connect_entered.wait(), 1)
        closing = asyncio.create_task(host.close())
        await asyncio.wait_for(driver.connect_cancelled.wait(), 1)
        await asyncio.wait_for(driver.close_entered.wait(), 1)

        assert host.query().phase == "closing"
        assert not closing.done() and not driver.closed
        with pytest.raises(InstanceLocked):
            other.acquire()
        driver.close_release.set()
        await closing
        assert host.query().phase == "closed"
        assert driver.closed and driver.close_attempts == 1
        other.acquire()
        other.release()
    finally:
        driver.close_release.set()
        await host.close()


async def test_failed_old_driver_cleanup_prevents_new_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backoff = Backoff(monkeypatch)
    failure = RuntimeError("旧连接未确认关闭")
    driver = Driver(close_failure=failure)
    factory = Factory(driver)
    body = Live2DRuntime("body", factory)
    assert (await body.connect()).state == "confirmed"
    driver.frame_failure = BodyUnavailable("失联需要恢复")
    body.playback_started("voice", BodyIntent(), 1e12)
    await asyncio.wait_for(driver.send_failed.wait(), 1)
    await asyncio.wait_for(driver.close_entered.wait(), 1)

    with pytest.raises(RuntimeError) as original:
        await body.connect()
    assert original.value is failure
    assert factory.calls == 1 and backoff.delays == []
    assert not driver.closed and not body.resources_released
    with pytest.raises(BaseExceptionGroup) as closing:
        await body.close()
    assert closing.value.subgroup(RuntimeError) is not None
    assert factory.calls == 1 and not body.resources_released


class Analyzer:
    """立即交付旧语音视素，用于核验恢复不会复用分析材料。"""

    def __init__(self) -> None:
        self.produced = asyncio.Event()
        self.closed = False

    async def start(self) -> None:
        """建立本轮独立分析所有者。"""
        return

    async def reset(self) -> None:
        """替身没有跨播放缓存。"""
        return

    async def analyze(self, pcm: bytes, sample: int) -> VisemeFrame:
        """交付明确非零口型，不能用原本全零的材料证明已清理。"""
        assert pcm == bytes(1280)
        self.produced.set()
        return VisemeFrame(sample, (0.0,) * 10 + (1.0,) + (0.0,) * 4)

    async def close(self) -> None:
        """确认本轮分析资源实际归还。"""
        self.closed = True


async def test_recovery_clears_smoothing_and_pcm_but_preserves_old_adoption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Backoff(monkeypatch)
    first, restored = Driver(), Driver()
    old_analyzer, new_analyzer = Analyzer(), Analyzer()
    analyzers = iter((old_analyzer, new_analyzer))
    factory = Factory(first, restored)
    style = BodyStyle(baseline_valence=0.15, baseline_arousal=0.25, motion_scale=0.0)
    body = Live2DRuntime(
        "body",
        factory,
        monotonic=lambda: 10.0,
        analyzer_factory=lambda: next(analyzers),
        style=style,
    )
    assert (await body.connect()).state == "confirmed"
    body.playback_started(
        "old", BodyIntent(valence=1.0, arousal=1.0, tension=1.0, gaze_x=0.8, gesture="shake"), 20
    )
    body.playback_progress("old", 0.0, 1.0)
    body.playback_pcm("old", 32000, 0, bytes(1280))
    await asyncio.wait_for(old_analyzer.produced.wait(), 1)
    await body.step()
    assert first.frames[-1]["mouth_open"] == 1.0
    assert first.frames[-1]["valence"] > style.baseline_valence
    assert body.snapshot()["adopted"] is True

    first.frame_failure = BodyUnavailable("表达过程中失联")
    await asyncio.wait_for(first.send_failed.wait(), 1)
    await asyncio.wait_for(restored.send_entered.wait(), 1)
    await settled(lambda: body.snapshot()["recovering"] is False)

    assert first.closed and old_analyzer.closed
    assert body.snapshot()["playback_id"] is None
    assert body.snapshot()["intent_id"] is None
    assert body.snapshot()["analysis_queued_frames"] == 0
    for frame in restored.frames:
        assert frame["valence"] == style.baseline_valence
        assert frame["arousal"] == style.baseline_arousal
        assert frame["tension"] == frame["gaze_x"] == frame["gaze_y"] == 0
        assert frame["mouth_open"] == 0
        assert all(frame["viseme_" + name] == 0 for name in VISEMES)
    result = body.playback_finished("old")
    assert result["accepted"] is True
    assert result["adopted"] is True
    assert result["visual_frame_confirmed"] is True
    assert result["error"] == {"error_type": "BodyUnavailable", "message": "表达过程中失联"}
    await body.close()
    assert restored.closed and new_analyzer.closed and body.resources_released
