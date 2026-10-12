"""身体最新语义、自然运动与视觉确认，不拥有任何声音资源。

音频实际开始才登记同身份姿态；进度只更新该关联。连续帧采用平滑目标，
离散手势只在首次真实发送的身份开始，过期/结束/断开撤销联动。VTS 离线
只影响身体，恢复创建新连接和基线，不补演旧手势或旧语音。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from purrcept_engine.errors import HostStateError
from purrcept_engine.execution.protocol import JsonObject
from purrcept_engine.execution.providers import CallContext, CallOutcome

from .analyzer import VisemeAnalyzer, VisemeFrame
from .models import VISEMES, BodyError, BodyIntent, BodyStyle
from .vts import BodyDriver

_LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class _Intent:
    identity: str
    value: BodyIntent
    expires_at: float
    playback_id: str | None
    accepted_at: float
    adopted_at: float | None = None
    gesture_at: float | None = None
    gesture_allowed: bool = True


@dataclass(frozen=True, slots=True)
class _PCM:
    playback_id: str
    sample: int
    data: bytes


class Live2DRuntime:
    """app 构造的独立视觉所有者，同时提供 BodyPort 的轻量事实接缝。

    构造不连接；明确 connect 后启动一个最新帧循环，没有历史帧队列。
    apply 用宿主 CallContext 身份并收紧为配置期限。VTS 预期故障保存离线
    状态，程序异常原样由后台任务及 close 报告，不能假装外围暂不可用。
    """

    def __init__(
        self,
        instance_id: str,
        driver_factory: Callable[[], BodyDriver],
        *,
        duration_seconds: float = 4.0,
        frame_hz: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
        analyzer_factory: Callable[[], VisemeAnalyzer] | None = None,
        sample_rate: int = 32000,
        lip_sync_offset_ms: float = 0.0,
        style: BodyStyle | None = None,
    ) -> None:
        """冻结视觉工厂和宿主期限，不取得音频或数据库所有权。"""
        self.instance_id, self._factory = instance_id, driver_factory
        self._duration, self._frame_hz, self._clock = duration_seconds, frame_hz, monotonic
        self._analyzer_factory, self._rate = analyzer_factory, sample_rate
        self._offset_ms, self._style = lip_sync_offset_ms, style or BodyStyle()
        self._analyzer: VisemeAnalyzer | None = None
        self._analysis_task: asyncio.Task[None] | None = None
        self._analysis_wake = asyncio.Event()
        self._pcm: deque[_PCM] = deque(maxlen=100)
        self._visemes: deque[VisemeFrame] = deque(maxlen=512)
        self._prepared_playback: str | None = None
        self._dropped_pcm = 0
        self._last_gesture = -float("inf")
        self._driver: BodyDriver | None = None
        self._connected, self._stopping = False, False
        self._intent: _Intent | None = None
        self._playback: str | None = None
        self._playback_result: JsonObject | None = None
        self._energy, self._elapsed = 0.0, 0.0
        self._generation = 0
        self._error: JsonObject | None = None
        self._loop: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._connection_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._adopted = asyncio.Event()
        self._channels: dict[str, float] = {
            "valence": 0.0,
            "arousal": 0.0,
            "tension": 0.0,
            "gaze_x": 0.0,
            "gaze_y": 0.0,
            "head_x": 0.0,
            "head_y": 0.0,
            "head_z": 0.0,
            "eye_open": 1.0,
            "mouth_open": 0.0,
            "mouth_form": 0.0,
            "breath": 0.0,
            "body_x": 0.0,
            "body_y": 0.0,
            "body_z": 0.0,
            "eye_l": 1.0,
            "eye_r": 1.0,
            "brow_l": 0.0,
            "brow_r": 0.0,
            "smile": 0.0,
            **{"viseme_" + name: 0.0 for name in VISEMES},
        }

    async def connect(self) -> CallOutcome:
        """明确连接不恢复旧姿态，部分失败也必须收回已取得连接。"""
        async with self._connection_lock:
            if self._stopping:
                raise HostStateError("身体已经停止新连接。")
            if self._connected:
                return CallOutcome("confirmed", self.snapshot())
            if self._driver is not None:
                await self._driver.close()
                self._driver = None
            if self._analyzer is not None:
                await self._analyzer.close()
                self._analyzer = None
            if self._loop is not None:
                await self._loop
            if self._analysis_task is not None:
                await self._analysis_task
            driver = self._factory()
            self._driver = driver
            try:
                await driver.connect()
                if self._analyzer_factory is not None:
                    self._analyzer = self._analyzer_factory()
                    await self._analyzer.start()
                if self._stopping:
                    raise HostStateError("身体连接在启动期间已撤销。")
            except BaseException as failure:
                cleanup_failures: list[BaseException] = []
                try:
                    await driver.close()
                    self._driver = None
                except BaseException as cleanup:
                    cleanup_failures.append(cleanup)
                if self._analyzer is not None:
                    try:
                        await self._analyzer.close()
                        self._analyzer = None
                    except BaseException as cleanup:
                        cleanup_failures.append(cleanup)
                if cleanup_failures:
                    raise BaseExceptionGroup(
                        "身体连接与资源清理失败。", [failure, *cleanup_failures]
                    ) from None
                if isinstance(failure, BodyError):
                    self._error = {"error_type": type(failure).__name__, "message": str(failure)}
                    return CallOutcome(
                        "failed", self.snapshot(), type(failure).__name__, str(failure)
                    )
                raise
            self._connected, self._error = True, None
            self._generation += 1
            self._intent, self._playback = None, None
            self._energy = 0.0
            self._prepared_playback = None
            self._pcm.clear()
            self._visemes.clear()
            if self._analyzer is not None:
                self._analysis_task = asyncio.create_task(
                    self._analyze(), name="live2d-analysis:" + self.instance_id
                )
                self._analysis_task.add_done_callback(self._loop_done)
            self._loop = asyncio.create_task(self._run(), name="live2d:" + self.instance_id)
            self._loop.add_done_callback(self._loop_done)
            return CallOutcome("confirmed", self.snapshot())

    def _loop_done(self, task: asyncio.Task[None]) -> None:
        """程序故障记录原型并保留到关闭，不静默转换成视觉离线。"""
        if not task.cancelled() and (error := task.exception()) is not None:
            self._connected = False
            self._wake.set()
            self._analysis_wake.set()
            self._adopted.set()
            self._error = {"error_type": type(error).__name__, "message": str(error)}
            _LOG.error(
                "身体驱动任务失败。",
                exc_info=(type(error), error, error.__traceback__),
                extra={"plugin_id": self.instance_id},
            )

    def playback_started(
        self, playback_id: str, intent: BodyIntent | None, deadline: float
    ) -> JsonObject:
        """只接受当前在线实际播放；离线不留下未来可复活的表达。"""
        if not self._connected or self._stopping or deadline <= self._clock():
            return {
                "accepted": False,
                "adopted": False,
                "status": "offline" if not self._connected else "expired",
                "playback_id": playback_id,
            }
        self._playback, self._elapsed, self._energy = playback_id, 0.0, 0.0
        self._playback_result = {
            "accepted": True,
            "adopted": False,
            "adopted_at": None,
            "visual_frame_confirmed": False,
            "semantic_intent": intent is not None,
            "playback_id": playback_id,
        }
        if intent is not None:
            self._intent = _Intent(playback_id, intent, deadline, playback_id, self._clock())
        elif self._intent is not None and self._intent.playback_id is not None:
            self._intent = None
        self._adopted.clear()
        self._wake.set()
        return {**self._playback_result, "status": "accepted"}

    def playback_progress(self, playback_id: str, elapsed_seconds: float, energy: float) -> None:
        """同身份实际音频采样强度控制口型；旧进度不覆盖新播放。"""
        if self._connected and self._playback == playback_id:
            self._elapsed, self._energy = elapsed_seconds, energy

    def playback_pcm(
        self, playback_id: str, sample_rate: int, sample_offset: int, pcm: bytes
    ) -> None:
        """只接管最多2秒20ms视觉采样，慢分析丢视觉旧块不回压音频。

        PCM可以先于播放器时钟到达，只准备分析，不采用姿态或口型。
        当前关联和播放结束门槛另行核验，恢复不会保留旧准备材料。
        """
        if not self._connected or self._stopping or self._analyzer is None:
            return
        if sample_rate != self._rate or len(pcm) != self._rate // 50 * 2 or sample_offset < 0:
            self._error = {
                "error_type": "BodyPCMFormat",
                "message": "身体与实际调音PCM采样格式不符。",
            }
            return
        if self._prepared_playback != playback_id:
            self._prepared_playback = playback_id
            self._pcm.clear()
            self._visemes.clear()
        if len(self._pcm) == self._pcm.maxlen:
            self._dropped_pcm += 1
        self._pcm.append(_PCM(playback_id, sample_offset, pcm))
        self._analysis_wake.set()

    async def _analyze(self) -> None:
        """独立原生进程串行取最新有界块，迟到视素不能串到下一次声音。"""
        identity: str | None = None
        generation = self._generation
        while self._connected and not self._stopping and generation == self._generation:
            self._analysis_wake.clear()
            analyzer = self._analyzer
            if analyzer is None:
                return
            if not self._pcm:
                await self._analysis_wake.wait()
                continue
            candidate = self._pcm.popleft()
            try:
                if candidate.playback_id != identity:
                    await analyzer.reset()
                    identity = candidate.playback_id
                frame = await analyzer.analyze(candidate.data, candidate.sample)
            except BodyError as error:
                if generation != self._generation:
                    return
                self._error = {"error_type": type(error).__name__, "message": str(error)}
                self._connected = False
                self._intent, self._playback, self._prepared_playback = None, None, None
                self._pcm.clear()
                self._visemes.clear()
                self._wake.set()
                self._adopted.set()
                return
            if (
                self._connected
                and generation == self._generation
                and candidate.playback_id == self._prepared_playback
            ):
                self._visemes.append(frame)

    def playback_finished(self, playback_id: str) -> JsonObject:
        """结束撤当前联动，已有采用事实不随独立apply或离线被抹掉。"""
        intent = self._intent
        history = self._playback_result
        if history is not None and history["playback_id"] == playback_id:
            # 历史只保留同一项串行声音，结束交回后释放；它不是恢复队列。
            result: JsonObject = {**history, "status": "finished", "error": self._error}
            self._playback_result = None
        else:
            result = {
                "playback_id": playback_id,
                "accepted": False,
                "adopted": False,
                "status": "finished",
            }
        if self._playback == playback_id:
            self._playback, self._energy = None, 0.0
            if intent is not None and intent.playback_id == playback_id:
                self._intent = None
            self._wake.set()
        if self._prepared_playback == playback_id:
            self._prepared_playback = None
            self._pcm.clear()
            self._visemes.clear()
        return result

    async def apply(self, intent: BodyIntent, context: CallContext) -> CallOutcome:
        """独立姿态最多配置4秒，只有真实帧确认才返回 adopted=true。"""
        now = self._clock()
        expires = min(context.deadline, now + self._duration)
        if not self._connected or self._stopping or expires <= now:
            return CallOutcome(
                "failed",
                {
                    "accepted": False,
                    "adopted": False,
                    "status": "offline" if not self._connected else "expired",
                },
                "BodyUnavailable",
                "身体未在线或姿态期限已到。",
            )
        value = _Intent(context.call_id, intent, expires, None, now)
        self._intent = value
        self._adopted.clear()
        self._wake.set()
        while value is self._intent and self._connected and value.adopted_at is None:
            remaining = expires - self._clock()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._adopted.wait(), remaining)
            except TimeoutError:
                break
        adopted = value.adopted_at is not None
        result: JsonObject = {
            "intent_id": context.call_id,
            "accepted": True,
            "adopted": adopted,
            "expires_at": expires,
            "adopted_at": value.adopted_at,
            "status": "adopted" if adopted else "unconfirmed",
        }
        return CallOutcome("confirmed" if adopted else "unknown", result)

    def _frame(self, now: float) -> tuple[dict[str, float], _Intent | None]:
        """目标平滑与一次手势共享当前身份；有效期不随帧发送而延长。"""
        intent = self._intent
        if intent is not None and (
            intent.expires_at <= now
            or (intent.playback_id is not None and intent.playback_id != self._playback)
        ):
            self._intent = intent = None
        style = self._style
        target = (
            BodyIntent(valence=style.baseline_valence, arousal=style.baseline_arousal)
            if intent is None
            else intent.value
        )
        values = {
            "valence": target.valence,
            "arousal": target.arousal,
            "tension": target.tension,
            "gaze_x": target.gaze_x,
            "gaze_y": target.gaze_y,
        }
        for name, value in values.items():
            self._channels[name] += (value - self._channels[name]) * 0.2
        idle = style.motion_scale * style.idle_motion_scale
        speech = style.motion_scale * style.speech_motion_scale * self._energy
        self._channels["head_x"] = target.gaze_x * 0.2 + math.sin(now * 0.7) * 0.025 * idle
        self._channels["head_y"] = (
            target.gaze_y * 0.2
            + math.sin(now * 0.53) * 0.02 * idle
            + math.sin(now * 4) * speech * 0.03
        )
        self._channels["head_z"] = math.sin(now * 0.37) * 0.015 * idle
        self._channels["body_x"] = self._channels["head_x"] * 0.2
        self._channels["body_y"] = self._channels["head_y"] * 0.1
        self._channels["body_z"] = self._channels["head_z"] * 0.3
        self._channels["eye_open"] = 0.05 if now % style.blink_interval_seconds < 0.12 else 1.0
        self._channels["eye_l"] = self._channels["eye_r"] = self._channels["eye_open"]
        self._channels["brow_l"] = self._channels["brow_r"] = (
            target.tension * -0.3 + math.sin(now * 0.8) * 0.01 * style.micro_expression_scale
        )
        self._channels["smile"] = target.valence * 0.4
        self._channels["breath"] = math.sin(now * 1.7) * 0.1
        sample = round((self._elapsed + self._offset_ms / 1000) * self._rate)
        frame = next((item for item in reversed(self._visemes) if item.sample <= sample), None)
        if self._playback is None or frame is None or sample - frame.sample > self._rate // 25:
            weights = (0.0,) * len(VISEMES)
        else:
            weights = frame.weights
        for name, weight in zip(VISEMES, weights, strict=True):
            self._channels["viseme_" + name] = weight
        if self._analyzer_factory is not None:
            self._channels["mouth_open"] = max(weights[10:])
        else:
            self._channels["mouth_open"] = self._energy if self._playback is not None else 0.0
        self._channels["mouth_form"] = max(-1.0, min(1.0, target.valence * 0.2))
        if intent is not None and target.gesture != "none" and intent.gesture_allowed:
            if (
                intent.gesture_at is None
                and now - self._last_gesture < style.gesture_cooldown_seconds
            ):
                intent.gesture_allowed = False
                return dict(self._channels), intent
            gesture_at = now if intent.gesture_at is None else intent.gesture_at
            age = now - gesture_at
            if age <= 0.9:
                wave = math.sin(age / 0.9 * math.pi * 2) * (1 - age / 0.9) * 0.2
                channel = {"nod": "head_y", "shake": "head_x", "tilt": "head_z"}[target.gesture]
                self._channels[channel] += wave
        return dict(self._channels), intent

    async def step(self) -> None:
        """发送一项当下帧；await 后核对代次/身份，迟到确认不复活表达。"""
        driver, generation = self._driver, self._generation
        if driver is None or not self._connected:
            return
        channels, intent = self._frame(self._clock())
        playback = self._playback
        await driver.send(channels)
        history = self._playback_result
        if (
            generation == self._generation
            and playback is not None
            and playback == self._playback
            and history is not None
            and history["playback_id"] == playback
        ):
            history["visual_frame_confirmed"] = True
        if (
            generation == self._generation
            and self._connected
            and intent is self._intent
            and intent is not None
            and intent.expires_at > self._clock()
        ):
            if intent.adopted_at is None:
                intent.adopted_at = self._clock()
                intent.gesture_at = intent.adopted_at
                if intent.value.gesture != "none" and intent.gesture_allowed:
                    self._last_gesture = intent.adopted_at
                if history is not None and history["playback_id"] == intent.playback_id:
                    history["adopted"] = True
                    history["adopted_at"] = intent.adopted_at
            self._adopted.set()

    async def _run(self) -> None:
        """只发最新帧，已知外围失败离线并撤旧姿态；不恢复重播。"""
        while self._connected and not self._stopping:
            self._wake.clear()
            try:
                await self.step()
            except BodyError as error:
                self._connected = False
                self._intent, self._playback = None, None
                self._energy = 0.0
                self._prepared_playback = None
                self._pcm.clear()
                self._visemes.clear()
                self._analysis_wake.set()
                self._error = {"error_type": type(error).__name__, "message": str(error)}
                self._adopted.set()
                return
            try:
                await asyncio.wait_for(self._wake.wait(), 1 / self._frame_hz)
            except TimeoutError:
                pass

    def snapshot(self) -> JsonObject:
        """只读真实连接、姿态和采用确认，不声明物理模型效果已经验收。"""
        intent = self._intent
        return {
            "connected": self._connected,
            "generation": self._generation,
            "playback_id": self._playback,
            "intent_id": None if intent is None else intent.identity,
            "adopted": intent is not None and intent.adopted_at is not None,
            "expires_at": None if intent is None else intent.expires_at,
            "elapsed_seconds": self._elapsed,
            "error": self._error,
            "lip_sync_basis": "processed_pcm_ovr_at_player_clock"
            if self._analyzer_factory is not None
            else "processed_pcm_energy_at_player_clock",
            "native_visemes": self._analyzer is not None,
            "analysis_queued_frames": len(self._pcm),
            "analysis_dropped_frames": self._dropped_pcm,
        }

    async def disconnect(self) -> CallOutcome:
        """先撤连接代次与姿态，再回收视觉连接，声音不受影响。"""
        async with self._connection_lock:
            self._connected = False
            self._generation += 1
            self._intent, self._playback = None, None
            self._energy = 0.0
            self._wake.set()
            self._analysis_wake.set()
            self._adopted.set()
            if self._driver is not None:
                await self._driver.close()
                self._driver = None
            if self._loop is not None:
                await self._loop
            if self._analyzer is not None:
                await self._analyzer.close()
                self._analyzer = None
            if self._analysis_task is not None:
                await self._analysis_task
            return CallOutcome("confirmed", self.snapshot())

    async def stop_inputs(self) -> None:
        """关闭禁止新表达/连接；已开始视觉回执仍可收尾。"""
        self._stopping = True
        self._connected = False
        self._generation += 1
        self._intent, self._playback = None, None
        self._energy = 0.0
        self._wake.set()
        self._analysis_wake.set()
        self._prepared_playback = None
        self._pcm.clear()
        self._visemes.clear()
        self._adopted.set()

    async def close(self) -> None:
        """重复等待同一视觉收尾，不关闭任何音频或共享 Store。"""
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(), name="live2d-close:" + self.instance_id
            )
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        """后台内部故障仍继续释放连接，然后原样聚合报告。"""
        await self.stop_inputs()
        results: list[object] = []
        if self._driver is not None:
            try:
                await self._driver.close()
                self._driver = None
            except BaseException as error:
                results.append(error)
        if self._loop is not None:
            results.extend(await asyncio.gather(self._loop, return_exceptions=True))
        if self._analyzer is not None:
            try:
                await self._analyzer.close()
                self._analyzer = None
            except BaseException as error:
                results.append(error)
        if self._analysis_task is not None:
            results.extend(await asyncio.gather(self._analysis_task, return_exceptions=True))
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise BaseExceptionGroup("身体资源收尾失败。", failures)
