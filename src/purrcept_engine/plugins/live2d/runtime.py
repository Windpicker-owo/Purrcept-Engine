"""身体最新语义、自然运动与视觉确认，不拥有任何声音资源。

音频实际开始才登记同身份姿态；进度只更新该关联。MotionController 分别维护
有期限的情绪、注视与动作；SpeechPerformance 从 PCM 准备韵律候选，只有
实际播放进度和首帧确认之后才选择语音动作。本模块拥有发送确认和口型对齐，
MouthController 将媒体时刻上的口型目标变成有惯性的最终开合。
连续帧采用平滑目标，
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
from pathlib import Path

from purrcept_engine.errors import HostStateError
from purrcept_engine.plugin_system import BodyIntent, CallContext, CallOutcome, ExpressionAssessment
from purrcept_engine.plugins.live2d.analyzer import VisemeAnalyzer, VisemeFrame
from purrcept_engine.plugins.live2d.models import (
    VISEMES,
    BodyError,
    BodyStyle,
    BodyUnavailable,
    FocusLayout,
    GazeCalibration,
)
from purrcept_engine.plugins.live2d.motion import MotionController
from purrcept_engine.plugins.live2d.mouth import MouthController, viseme_mouth_form
from purrcept_engine.plugins.live2d.preferences import Preferences, load_preferences
from purrcept_engine.plugins.live2d.prosody import SpeechPerformance
from purrcept_engine.plugins.live2d.vts import BodyDriver
from purrcept_engine.values import JsonObject

_LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class _Intent:
    """保存最新请求的发送确认，不承担已经采用的字段与动作生命周期。"""

    identity: str
    value: BodyIntent
    expires_at: float
    playback_id: str | None
    accepted_at: float
    adopted_at: float | None = None


@dataclass(frozen=True, slots=True)
class _PCM:
    """保存当前播放身份的一段 PCM 与采样位置。"""

    playback_id: str
    sample: int
    data: bytes


class Live2DRuntime:
    """app 构造的独立视觉所有者，同时提供 BodyPort 的轻量事实接缝。

    构造不连接；start 登记唯一有限恢复任务，connect 可以等待同一任务。
    每轮最多五次建连，首帧实际确认后才完成本轮；后续失联开启新轮，
    没有历史帧队列。
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
        gaze_calibration: GazeCalibration | None = None,
        motion_seed: int | None = None,
        focus_layout: FocusLayout | None = None,
        preferences_path: Path | None = None,
        model_id: str = "unbound",
    ) -> None:
        """冻结视觉工厂和宿主期限，不取得音频或数据库所有权。

        Args:
            instance_id: 本次装配中唯一的插件或宿主实例身份。
            driver_factory: 为当前插件实例创建独立设备驱动的工厂。
            duration_seconds: 本次身体姿态允许采用的时长（秒）。 默认 4.0。
            frame_hz: 视觉帧发送频率（Hz）。 默认 30.0。
            monotonic: 本实例使用的单调时钟，不与持久化墙钟混用。
            analyzer_factory: 创建本实例音素分析资源的工厂；None 不启用分析。
            sample_rate: PCM 的明确采样率（Hz）。 默认 32000。
            lip_sync_offset_ms: 实际播放与口型对齐采用的毫秒偏移。 默认 0.0。
            style: 可选身体样式参数，缺省采用当前运行配置。
            gaze_calibration: 归一化头身到注视偏移的模型校准。
            motion_seed: 动作事件随机种子，固定值用于离线复现。
            focus_layout: 语境注视对象的模型校准坐标。
            preferences_path: 可选模型偏好文件；连接期间每秒检查，在工作线程有界读取。
            model_id: 偏好必须匹配的校准模型身份。
        """
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
        self._driver: BodyDriver | None = None
        self._connected, self._stopping = False, False
        self._recovery_enabled = False
        self._intent: _Intent | None = None
        self._playback: str | None = None
        self._playback_result: JsonObject | None = None
        self._energy, self._elapsed = 0.0, 0.0
        self._generation = 0
        self._error: JsonObject | None = None
        self._loop: asyncio.Task[None] | None = None
        self._recovery: asyncio.Task[CallOutcome] | None = None
        self._first_frame: asyncio.Future[None] | None = None
        self._attempt = 0
        self._last_failure: BodyError | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._resources_released = False
        self._cleaning_connection = False
        self._connection_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._adopted = asyncio.Event()
        self._motion = MotionController(
            self._style, gaze_calibration or GazeCalibration(), seed=motion_seed, focus=focus_layout
        )
        self._speech = SpeechPerformance(self._style)
        self._mouth = MouthController(self._style)
        self._preferences_path, self._model_id = preferences_path, model_id
        self._preferences_revision = ""
        self._preferences_error: str | None = None
        self._preferences_at = -math.inf
        self._preferences = Preferences(model_id=model_id)

    def configure(self, preferences: Preferences) -> None:
        """在同一循环采用完整偏好，姿态与口型保持运动连续。

        Args:
            preferences: 与构造时模型一致的已校验偏好。

        Raises:
            ValueError: 模型身份不符。

        Note:
            不写磁盘、不改变语音所有权；独立调节页用此入口预览草稿。
        """
        if preferences.model_id != self._model_id:
            raise ValueError("偏好模型身份不符。")
        self._preferences = preferences
        self._motion.configure(preferences)
        self._mouth.configure(preferences)

    async def _reload_preferences(self, now: float) -> None:
        """读取不占用帧循环；坏文件保留最后有效版本并在快照显式报告。

        由唯一发送循环串行调用，关闭时随该循环收尾；没有独立无限轮询任务。
        """
        if self._preferences_path is None or now < self._preferences_at:
            return
        self._preferences_at = now + 1.0
        try:
            value, revision = await asyncio.to_thread(
                load_preferences, self._preferences_path, self._model_id
            )
        except (ValueError, OSError):
            self._preferences_error = "偏好读取失败，保留上一有效版本；请检查模型身份和文件。"
            return
        if revision != self._preferences_revision:
            self.configure(value)
            self._preferences_revision = revision
        self._preferences_error = None

    @property
    def resources_released(self) -> bool:
        """证明连接/分析器实际关闭，且连接与后台任务已结束借用。

        Note:
            只在唯一 close 任务取得全部释放确认后为真。旧帧任务的内部异常
            仍由 close 报告；关闭操作失败保持假，不能以离线快照推断释放。
        """
        return self._resources_released

    async def connect(self) -> CallOutcome:
        """等待唯一恢复轮。

        Returns:
            实际确认、失败或未知结论，以及可 JSON 化的业务值和原生媒体引用。

        Raises:
            HostStateError: 身体已经停止新连接。

        Note:
            取消观察者不取消已登记的连接所有者。

            宿主通过 stop_inputs 撤销所有者。耗尽或协议拒绝后不会自行重开；
            显式 connect 可以新开一轮，但程序异常必须原样报告。
        """
        if self._stopping:
            raise HostStateError("身体已经停止新连接。")
        self._recovery_enabled = True
        task = self._recovery
        if task is not None and not task.done():
            return await asyncio.shield(task)
        if task is not None and task.done() and not task.cancelled():
            await task
        if self._connected:
            return CallOutcome("confirmed", self.receipt())
        return await asyncio.shield(self._begin_recovery())

    async def start(self) -> None:
        """登记外围连接后归还插件启动，不让原生授权阻塞宿主退出。

        Raises:
            HostStateError: 身体已经停止新连接。
        """
        if self._stopping:
            raise HostStateError("身体已经停止新连接。")
        task = self._recovery
        if task is not None and task.done() and not task.cancelled():
            await task
        self._recovery_enabled = True
        self._begin_recovery()

    def _begin_recovery(self) -> asyncio.Task[CallOutcome]:
        """所有入口复用当前轮，帧任务不能并行创建第二个连接所有者。"""
        if self._recovery is None or self._recovery.done():
            self._attempt = 0
            self._recovery = asyncio.create_task(
                self._recover(), name="live2d-recovery:" + self.instance_id
            )
            self._recovery.add_done_callback(self._recovery_done)
        return self._recovery

    def _recovery_done(self, task: asyncio.Task[CallOutcome]) -> None:
        """恢复中的程序异常走同一故障记录，仍保留到 close 原样报告。"""
        if not task.cancelled() and (error := task.exception()) is not None:
            self._record_task_error(error)

    async def _recover(self) -> CallOutcome:
        """只重试明确的传输失联，失败资源实际归还之后才能等待下一次。"""
        delays = (0.5, 1.0, 2.0, 4.0)
        outcome: CallOutcome | None = None
        for attempt in range(1, 6):
            if self._stopping or not self._recovery_enabled:
                raise asyncio.CancelledError
            if attempt > 1:
                await asyncio.sleep(delays[attempt - 2])
            if self._stopping:
                raise HostStateError("身体连接在启动期间已撤销。")
            self._attempt = attempt
            outcome = await self._connect_once()
            if self._stopping or not self._recovery_enabled:
                raise asyncio.CancelledError
            if self._connected:
                # 认证并不证明帧路径可用；真实首帧完成才结束本轮，不能
                # 依赖事件循环让出几次或短暂建连不断刷新恢复额度。
                first_frame = self._first_frame
                if first_frame is None:
                    raise RuntimeError("身体连接没有登记首帧确认。")
                try:
                    await first_frame
                except BodyError:
                    # 帧/分析循环已经保存原失败；下面按其类型结束或继续本轮。
                    pass
                if self._connected:
                    return CallOutcome("confirmed", self.receipt())
            failure = self._last_failure
            if failure is None:
                return outcome
            outcome = CallOutcome("failed", self.receipt(), type(failure).__name__, str(failure))
            if not isinstance(failure, BodyUnavailable):
                return outcome
        if outcome is None:
            raise RuntimeError("身体恢复轮没有执行连接尝试。")
        return outcome

    async def _connect_once(self) -> CallOutcome:
        """一轮中的一次资源借用；失败先清理，恢复不会跨旧任务代次。"""
        async with self._connection_lock:
            if self._stopping:
                raise HostStateError("身体已经停止新连接。")
            if self._connected:
                return CallOutcome("confirmed", self.receipt())
            self._cleaning_connection = True
            try:
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
            finally:
                self._cleaning_connection = False
            # 旧资源close可以跨越停止/断开命令；归还不是取得新连接的许可。
            if self._stopping or not self._recovery_enabled:
                raise asyncio.CancelledError
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
                self._cleaning_connection = True
                try:
                    try:
                        await driver.close()
                        self._driver = None
                    except BaseException as cleanup:
                        cleanup_failures.append(cleanup)
                        if getattr(driver, "resources_released", None) is True:
                            self._driver = None
                    if self._analyzer is not None:
                        try:
                            await self._analyzer.close()
                            self._analyzer = None
                        except BaseException as cleanup:
                            cleanup_failures.append(cleanup)
                finally:
                    self._cleaning_connection = False
                if cleanup_failures:
                    raise BaseExceptionGroup(
                        "身体连接与资源清理失败。", [failure, *cleanup_failures]
                    ) from None
                if isinstance(failure, BodyError):
                    self._last_failure = failure
                    self._error = {"error_type": type(failure).__name__, "message": str(failure)}
                    return CallOutcome(
                        "failed", self.receipt(), type(failure).__name__, str(failure)
                    )
                raise
            self._connected, self._error = True, None
            self._last_failure = None
            self._generation += 1
            self._intent, self._playback = None, None
            self._energy, self._elapsed = 0.0, 0.0
            self._prepared_playback = None
            self._pcm.clear()
            self._visemes.clear()
            # 新连接不能继承旧表达的目标、动作或速度。
            self._motion.reset(self._clock())
            self._speech.reset()
            self._mouth.reset(self._clock())
            self._first_frame = asyncio.get_running_loop().create_future()
            if self._analyzer is not None:
                self._analysis_task = asyncio.create_task(
                    self._analyze(), name="live2d-analysis:" + self.instance_id
                )
                self._analysis_task.add_done_callback(self._loop_done)
            self._loop = asyncio.create_task(self._run(), name="live2d:" + self.instance_id)
            self._loop.add_done_callback(self._loop_done)
            return CallOutcome("confirmed", self.receipt())

    def _loop_done(self, task: asyncio.Task[None]) -> None:
        """程序故障记录原型并保留到关闭，不静默转换成视觉离线。"""
        if not task.cancelled() and (error := task.exception()) is not None:
            self._record_task_error(error)

    def _record_task_error(self, error: BaseException) -> None:
        """程序故障只停止视觉，不伪装成可自动重试的外围失联。"""
        self._connected = False
        self._speech.reset()
        self._wake.set()
        self._analysis_wake.set()
        self._adopted.set()
        self._error = {"error_type": type(error).__name__, "message": str(error)}
        if self._first_frame is not None and not self._first_frame.done():
            self._first_frame.set_exception(error)
        _LOG.error(
            "身体驱动任务失败。",
            exc_info=(type(error), error, error.__traceback__),
            extra={"plugin_id": self.instance_id},
        )

    def observe_expression(
        self, assessment: ExpressionAssessment, *, deadline: float, urgent: bool
    ) -> None:
        """接收协调者已经采用的短期语境，离线或关闭时直接丢弃。

        Args:
            assessment: 已校验的情绪与注视选择，各自带独立置信度。
            deadline: 宿主单调时钟中的绝对失效时间，恢复连接不能续期。
            urgent: 可以打断语境停留，不覆盖显式动作或改变力度。

        Raises:
            ValueError: 宿主交付非有限期限。

        Note:
            同步更新本地帧目标，不等待 VTS 确认，也不影响注意提交或声音播放。
        """
        if not math.isfinite(deadline):
            raise ValueError("语境判断期限必须有限。")
        now = self._clock()
        if not self._connected or self._stopping or deadline <= now:
            return
        self._motion.observe(assessment, now=now, deadline=deadline, urgent=urgent)
        self._wake.set()

    def playback_started(
        self, playback_id: str, intent: BodyIntent | None, deadline: float
    ) -> JsonObject:
        """只接受当前在线实际播放。

        Args:
            playback_id: 本次实际播放的稳定身份，用于拒绝迟到口型或停止确认。
            intent: 有明确期限的身体语义姿态。
            deadline: 所属事件循环的绝对单调截止时间，不是 Unix 时间戳。

        Returns:
            业务对象，按实际结果提供 accepted、adopted、status、playback_id 字段。

        Note:
            离线不留下未来可复活的表达。
        """
        if not self._connected or self._stopping or deadline <= self._clock():
            return {
                "accepted": False,
                "adopted": False,
                "status": "offline" if not self._connected else "expired",
                "playback_id": playback_id,
            }
        if self._playback is not None and self._playback != playback_id:
            self._motion.revoke(self._playback, self._clock())
            self._speech.finish(self._playback)
            self._mouth.release()
        self._playback, self._elapsed, self._energy = playback_id, 0.0, 0.0
        self._speech.begin(playback_id, intent, deadline)
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
            self._motion.submit(intent, now=self._clock(), expires=deadline, playback=playback_id)
        elif self._intent is not None and self._intent.playback_id is not None:
            self._intent = None
        self._adopted.clear()
        self._wake.set()
        return {**self._playback_result, "status": "accepted"}

    def playback_progress(self, playback_id: str, elapsed_seconds: float, energy: float) -> None:
        """同身份实际媒体进度驱动口型与韵律；重复或倒退进度不重放动作。

        Args:
            playback_id: 本次实际播放的稳定身份，用于拒绝迟到口型或停止确认。
            elapsed_seconds: 实际播放器报告的已播放秒数。
            energy: 实际 PCM 片段的音量能量，用于驱动视觉反馈。

        Note:
            旧进度不覆盖新播放。
        """
        if self._connected and self._playback == playback_id:
            if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0 or not 0 <= energy <= 1:
                self._error = {
                    "error_type": "BodyPlaybackClock",
                    "message": "身体收到非有限或越界的播放进度/能量。",
                }
                return
            if elapsed_seconds < self._elapsed:
                return
            self._elapsed, self._energy = elapsed_seconds, energy

    def playback_pcm(
        self, playback_id: str, sample_rate: int, sample_offset: int, pcm: bytes
    ) -> None:
        """接管20ms视觉采样；本地韵律只保存候选，OVR队列最多保留2秒。

        Args:
            playback_id: 本次实际播放的稳定身份，用于拒绝迟到口型或停止确认。
            sample_rate: PCM 的明确采样率（Hz）。
            sample_offset: 当前 PCM 片段在本次播放中的起始采样位置。
            pcm: 已解码的 PCM 字节或按播放顺序产生的字节流。

        Note:
            PCM可以先于播放器时钟到达，只准备分析，不采用姿态或口型。
            当前关联和播放结束门槛另行核验，恢复不会保留旧准备材料。
        """
        if not self._connected or self._stopping:
            return
        if sample_rate != self._rate or len(pcm) != self._rate // 50 * 2 or sample_offset < 0:
            self._error = {
                "error_type": "BodyPCMFormat",
                "message": "身体与实际调音PCM采样格式不符。",
            }
            return
        if not self._speech.feed(playback_id, sample_rate, sample_offset, pcm):
            return
        if self._analyzer is None:
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
                self._last_failure = error
                if self._first_frame is not None and not self._first_frame.done():
                    self._first_frame.set_exception(error)
                self._connected = False
                self._speech.reset()
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
        """结束撤当前联动，已有采用事实不随独立apply或离线被抹掉。

        Args:
            playback_id: 本次实际播放的稳定身份，用于拒绝迟到口型或停止确认。

        Returns:
            业务对象，按实际结果提供 playback_id、accepted、adopted、status 字段。
        """
        intent = self._intent
        history = self._playback_result
        if history is not None and history["playback_id"] == playback_id:
            # 历史只保留同一项串行声音，结束交回后释放；它不是恢复队列。
            result: JsonObject = {
                **history,
                "status": "finished",
                "error": history.get("error", self._error),
            }
            self._playback_result = None
        else:
            result = {
                "playback_id": playback_id,
                "accepted": False,
                "adopted": False,
                "status": "finished",
            }
        self._motion.revoke(playback_id, self._clock())
        self._speech.finish(playback_id)
        if self._playback == playback_id:
            self._playback, self._energy = None, 0.0
            self._mouth.release()
            if intent is not None and intent.playback_id == playback_id:
                self._intent = None
            self._wake.set()
        if self._prepared_playback == playback_id:
            self._prepared_playback = None
            self._pcm.clear()
            self._visemes.clear()
        return result

    async def apply(self, intent: BodyIntent, context: CallContext) -> CallOutcome:
        """在配置期限内采用局部身体更新，真实帧确认才返回 adopted=true。

        Args:
            intent: 只更新非空字段；其他字段与已开始的动作保留原有期限。
                delivery 需要 speak 的实际播放时间轴，本入口明确返回失败。
            context: 宿主或 Core 注入的可信调用上下文，不由模型自行填写。

        Returns:
            实际确认、失败或未知结论，以及可 JSON 化的业务值和原生媒体引用。
        """
        if intent.delivery is not None:
            return CallOutcome(
                "failed",
                {"accepted": False, "adopted": False, "status": "speech_required"},
                "BodySpeechContextRequired",
                "delivery 需要实际播放时间轴，请通过 speak(body=...) 提交。",
            )
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
        self._motion.submit(intent, now=now, expires=expires, playback=None)
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
        """动作状态独立推进；口型最后按实际播放器进度取值，最新意图仅供确认。"""
        intent = self._intent
        if intent is not None and (
            intent.expires_at <= now
            or (intent.playback_id is not None and intent.playback_id != self._playback)
        ):
            self._intent = intent = None
        playback = self._playback
        if playback is not None:
            self._motion.advance_speech(playback, self._elapsed)
            history = self._playback_result
            if history is not None and history.get("visual_frame_confirmed") is True:
                cue = self._speech.sample(self._elapsed, now)
                if cue is not None:
                    self._motion.speech_cue(
                        cue,
                        playback=playback,
                        elapsed=self._elapsed,
                        now=now,
                        expires=self._speech.deadline,
                    )
        channels = self._motion.sample(now)
        sample = round((self._elapsed + self._offset_ms / 1000) * self._rate)
        frame = next((item for item in reversed(self._visemes) if item.sample <= sample), None)
        if self._playback is None or frame is None or sample - frame.sample > self._rate // 25:
            weights = (0.0,) * len(VISEMES)
        else:
            weights = frame.weights
        if self._analyzer_factory is not None:
            opening = max(weights[10:])
        else:
            opening = self._energy if self._playback is not None else 0.0
        channels["mouth_open"], weights = self._mouth.sample(
            now, opening, weights, active=self._playback is not None
        )
        # Q 等双通道模型也利用实际视素改变横向嘴型；完整视素绑定仍原样交付。
        # 先平滑权重再投影，不能把音素边界直接变成面部跳变。
        channels["mouth_form"] = max(
            -1.0,
            min(
                1.0,
                channels["mouth_form"]
                + viseme_mouth_form(weights)
                * self._preferences.mouth_viseme_form_scale
                * self._preferences.mouth_form_gain,
            ),
        )
        for name, weight in zip(VISEMES, weights, strict=True):
            channels["viseme_" + name] = weight
        return channels, intent

    async def step(self) -> None:
        """发送一项当下帧。

        Note:
            await 后核对代次/身份，迟到确认不复活表达。
        """
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
                self._motion.confirm(
                    intent.value,
                    now=intent.adopted_at,
                    expires=intent.expires_at,
                    playback=intent.playback_id,
                )
                if history is not None and history["playback_id"] == intent.playback_id:
                    history["adopted"] = True
                    history["adopted_at"] = intent.adopted_at
            self._adopted.set()

    async def _run(self) -> None:
        """只发最新帧；失联撤旧表达，传输故障交唯一有限恢复轮。"""
        while self._connected and not self._stopping:
            self._wake.clear()
            frame_started = self._clock()
            try:
                await self._reload_preferences(frame_started)
                await self.step()
            except BodyError as error:
                self._connected = False
                self._speech.reset()
                self._intent, self._playback = None, None
                self._energy = 0.0
                self._prepared_playback = None
                self._pcm.clear()
                self._visemes.clear()
                self._analysis_wake.set()
                self._error = {"error_type": type(error).__name__, "message": str(error)}
                self._last_failure = error
                if self._first_frame is not None and not self._first_frame.done():
                    self._first_frame.set_exception(error)
                if self._playback_result is not None:
                    self._playback_result["error"] = self._error
                self._adopted.set()
                if (
                    isinstance(error, BodyUnavailable)
                    and self._recovery_enabled
                    and not self._stopping
                ):
                    self._begin_recovery()
                return
            if self._first_frame is not None and not self._first_frame.done():
                self._first_frame.set_result(None)
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    max(0.001, 1 / self._frame_hz - (self._clock() - frame_started)),
                )
            except TimeoutError:
                pass

    def snapshot(self) -> JsonObject:
        """只读真实连接、姿态和采用确认，不声明物理模型效果已经验收。

        Returns:
            业务对象，按实际结果提供 connected、generation、playback_id、intent_id、adopted、
            expires_at、elapsed_seconds、error、recovering、connection_attempt 等 字段。
        """
        intent = self._intent
        return {
            "connected": self._connected,
            "preferences_revision": self._preferences_revision,
            "preferences_error": self._preferences_error,
            "generation": self._generation,
            "playback_id": self._playback,
            "intent_id": None if intent is None else intent.identity,
            "adopted": intent is not None and intent.adopted_at is not None,
            "expires_at": None if intent is None else intent.expires_at,
            "elapsed_seconds": self._elapsed,
            "error": self._error,
            "recovering": self._recovery is not None and not self._recovery.done(),
            "connection_attempt": self._attempt,
            "lip_sync_basis": "processed_pcm_ovr_at_player_clock"
            if self._analyzer_factory is not None
            else "processed_pcm_energy_at_player_clock",
            "native_visemes": self._analyzer is not None,
            "analysis_queued_frames": len(self._pcm),
            "analysis_dropped_frames": self._dropped_pcm,
            "speech_pending_events": self._speech.pending_events,
            "speech_dropped_events": self._speech.dropped_events,
        }

    def receipt(self) -> JsonObject:
        """返回面向主体的精简连接回执，诊断计数与内部代次留在 snapshot。"""
        intent = self._intent
        result: JsonObject = {
            "connected": self._connected,
            "adopted": intent is not None and intent.adopted_at is not None,
        }
        if self._playback is not None:
            result["playback_id"] = self._playback
        if self._error is not None:
            result["error"] = self._error
        if self._recovery is not None and not self._recovery.done():
            result["recovering"] = True
        return result

    async def prepare_request(self) -> JsonObject:
        """保留身体可用性和实际采用状态，音素分析队列与重连计数留给诊断。"""
        state = self.snapshot()
        result: JsonObject = {
            key: state[key]
            for key in (
                "connected",
                "playback_id",
                "adopted",
                "elapsed_seconds",
                "error",
                "recovering",
            )
        }
        result["remaining_seconds_at_assembly"] = (
            None if self._intent is None else max(0, self._intent.expires_at - self._clock())
        )
        return result

    async def disconnect(self) -> CallOutcome:
        """先撤连接代次与姿态，再回收视觉连接，声音不受影响。

        Returns:
            实际确认、失败或未知结论，以及可 JSON 化的业务值和原生媒体引用。

        Raises:
            BaseExceptionGroup: 身体恢复撤销失败。
        """
        self._recovery_enabled = False
        recovery = self._recovery
        if recovery is not None and not recovery.done():
            if not recovery.cancelling() and not self._cleaning_connection:
                recovery.cancel()
            results = await asyncio.gather(recovery, return_exceptions=True)
            failures = [
                result
                for result in results
                if isinstance(result, BaseException)
                and not isinstance(result, asyncio.CancelledError)
            ]
            if failures:
                raise BaseExceptionGroup("身体恢复撤销失败。", failures)
        async with self._connection_lock:
            self._connected = False
            self._speech.reset()
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
            return CallOutcome("confirmed", self.receipt())

    async def stop_inputs(self) -> None:
        """关闭禁止新表达/连接。

        Note:
            已开始视觉回执仍可收尾。
        """
        if (
            not self._stopping
            and self._recovery is not None
            and not self._recovery.done()
            and not self._recovery.cancelling()
            and not self._cleaning_connection
        ):
            self._recovery.cancel()
        self._stopping = True
        # 资格立即撤销，但资源正在close时不再次取消真实清理。恢复所有者
        # 归还资源后先检查资格，不能再睡眠或创建下一连接。
        self._recovery_enabled = False
        self._connected = False
        self._speech.reset()
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
        """等启动归还资源后取得真实关闭明证，历史任务故障仍原样报告。"""
        await self.stop_inputs()
        results: list[object] = []
        if self._recovery is not None:
            results.extend(await asyncio.gather(self._recovery, return_exceptions=True))
            # 所有者取消是明确的关闭命令；其清理或原程序错误仍须报告。
            results = [
                result for result in results if not isinstance(result, asyncio.CancelledError)
            ]
        # connect/disconnect持有此锁借用真实资源；关闭不能在启动尚未结束
        # 时提前收回连接，或把暂未登记分析器误当成全部资源已经释放。
        async with self._connection_lock:
            if self._driver is not None:
                try:
                    await self._driver.close()
                    self._driver = None
                except BaseException as error:
                    results.append(error)
                    if getattr(self._driver, "resources_released", None) is True:
                        self._driver = None
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
            # 实际close成功或驱动的强释放明证才清引用；原错保留在results，
            # 不能让宿主把仍存活的视觉连接当作已归还。
            self._resources_released = (
                self._driver is None
                and self._analyzer is None
                and (self._loop is None or self._loop.done())
                and (self._analysis_task is None or self._analysis_task.done())
                and (self._recovery is None or self._recovery.done())
            )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise BaseExceptionGroup("身体资源收尾失败。", failures)
