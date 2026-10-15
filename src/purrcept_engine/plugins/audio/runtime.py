"""现场语音队列、播放资格与真实音频事实的唯一所有者。

接管即保留一个有界位置；轮到该项才开始 TTS 和播放。新生成代次不
改变资格；interrupt 同步撤销当前及当时已有候播，再有限等待实际收尾。
不可撤销的同步合成由原任务持有，迟到文件记为已合成未播放。身体在
实际播放器开始后借用事实，其连接和失败均不拥有声音。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from purrcept_engine.errors import HostStateError
from purrcept_engine.plugin_system import (
    AudioError,
    AudioPort,
    BodyPort,
    CallContext,
    CallOutcome,
    LateResultPort,
    make_fact,
)
from purrcept_engine.plugins.audio.dsp import check_program, decode_pcm, process_audio, read_profile
from purrcept_engine.plugins.audio.models import (
    AudioSettings,
    PlaybackResult,
    RecordParameters,
    SpeechAudio,
    SpeechParameters,
)
from purrcept_engine.plugins.audio.player import AudioPlayer
from purrcept_engine.plugins.audio.tts import SpeechSource
from purrcept_engine.values import JsonObject, JsonValue

_LOG = logging.getLogger(__name__)

type _OwnedTask = asyncio.Task[None] | asyncio.Task[CallOutcome]


@dataclass(slots=True)
class _Speech:
    """保存一次已接管声音工作的任务、阶段与真实播放身份。"""

    context: CallContext
    playback_id: str
    parameters: SpeechParameters
    result: asyncio.Future[CallOutcome]
    stage: str = "queued"
    revoked: str | None = None
    synthesis_started: bool = False
    synthesized: bool = False
    started: bool = False
    byte_size: int = 0
    elapsed_seconds: float = 0.0
    late: bool = False
    body: JsonObject | None = None
    admission: asyncio.Task[None] | None = None
    expiration: asyncio.TimerHandle | None = None
    cancellation: asyncio.Task[None] | None = None
    player_stop: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _Stop:
    context: CallContext
    affected: tuple[_Speech, ...]
    player_stop: asyncio.Task[None] | None
    record_late: bool
    revision: int
    pending_reported: bool = False


@dataclass(slots=True)
class _Recording:
    """导出等待者是否已离开，不拥有或取消真实合成/文件资源。"""

    late: bool = False


class AudioRuntime:
    """app 构造的声音资源所有者，实现 AudioOutputPort。

    所有状态在宿主事件循环修改。accepted 后取消等待不撤销声音，行动
    deadline、明确停音和 close 才撤资格。只有一个 runner 串行合成/播放，
    候播最多配置数；没有旧刺激 consumed 票据，也没有身体共享设备。
    """

    def __init__(
        self,
        instance_id: str,
        settings: AudioSettings,
        source: SpeechSource,
        player: AudioPlayer,
        ledger: AudioPort,
        output_dir: Path,
        profile_path: Path | None,
        *,
        late_results: LateResultPort | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        decode: bool = True,
    ) -> None:
        """登记冻结资源与有限端口，构造不访问 TTS/设备/数据库。

        Args:
            instance_id: 本次装配中唯一的插件或宿主实例身份。
            settings: 当前组件已校验的配置或纯声明。
            source: 当前所有者借用的语音合成源。
            player: 当前所有者借用的实际播放器。
            ledger: 当前业务专用账本，不提供任意数据库操作。
            output_dir: 归此资源所有者管理的输出文件目录。
            profile_path: 声线预设文件路径；None 使用当前配置的预设。
            late_results: 将实际迟到确认追加到原调用的有限端口。
            monotonic: 本实例使用的单调时钟，不与持久化墙钟混用。
            decode: 是否在素材交付前进行明确的 PCM 解码。 默认 True。
        """
        self.instance_id, self.settings = instance_id, settings
        self._source, self._player, self._ledger = source, player, ledger
        self._output_dir, self._profile_path, self._clock = output_dir, profile_path, monotonic
        self._late_results, self._decode = late_results, decode
        self._body: BodyPort | None = None
        self._body_bound = False
        self._queue: deque[_Speech] = deque()
        self._active: _Speech | None = None
        self._runner: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._resources_released = False
        self._stopping, self._ready = False, False
        self._last: JsonObject | None = None
        self._last_control: JsonObject | None = None
        self._failure: BaseException | None = None
        self._revision = 0
        self._records: set[asyncio.Task[CallOutcome]] = set()
        self._cleanup: set[asyncio.Task[None]] = set()
        self._controls: set[asyncio.Task[CallOutcome]] = set()
        self._admissions: set[asyncio.Task[None]] = set()

    @property
    def resources_released(self) -> bool:
        """明确证明任务借用结束且播放器、合成连接均已实际关闭。

        Note:
            宿主仅在 close 报错时读取此明证，区分已释放后的旧任务故障与
            真正关闭失败。开始关闭、空候播队列或任务数为零均不能使它为真。
            重复 close 等待同一所有者任务，不重试或覆盖首次关闭的结果。
        """
        return self._resources_released

    def bind_body(self, body: BodyPort | None) -> None:
        """app 在全部 build 后一次明确接线。

        Args:
            body: 可选身体端口；缺省时声音资源仍独立工作。

        Raises:
            RuntimeError: 音频身体端口必须在启动前且只绑定一次。

        Note:
            无身体用 None 表达。
        """
        if self._body_bound or self._ready:
            raise RuntimeError("音频身体端口必须在启动前且只绑定一次。")
        self._body, self._body_bound = body, True

    async def start(self) -> None:
        """完成装配后允许接管，不预热付费服务或启动播放器。

        Raises:
            RuntimeError: 音频启动前尚未完成身体端口接线。
            HostStateError: 音频已经停止新工作。
        """
        if not self._body_bound:
            raise RuntimeError("音频启动前尚未完成身体端口接线。")
        if self._stopping:
            raise HostStateError("音频已经停止新工作。")
        self._ready = True

    async def _fact(self, identity: str, stage: str, payload: JsonObject) -> None:
        """实际阶段先入同库控制队列；权威失败不得继续播放或伪报成功。"""
        await self._ledger.append(
            make_fact(self.instance_id, identity, stage, time.time(), payload)
        )

    def _payload(self, speech: _Speech, playback: PlaybackResult) -> JsonObject:
        """保留完全未开始、已合成未播放及部分播放的区别。"""
        return {
            "playback_id": speech.playback_id,
            "call_id": speech.context.call_id,
            "action_id": speech.context.action_id,
            "input_id": speech.context.input_id,
            "accepted": True,
            "synthesis_started": speech.synthesis_started,
            "synthesized": speech.synthesized,
            "byte_size": speech.byte_size,
            "stop_reason": speech.revoked,
            "body": speech.body,
            **playback.json(),
        }

    async def speak(self, parameters: SpeechParameters, context: CallContext) -> CallOutcome:
        """接管本次合成与现场播放，返回真实声音阶段回执。

        Args:
            parameters: 已通过能力参数模型校验的调用参数。
            context: 宿主或 Core 注入的可信调用上下文，不由模型自行填写。

        Returns:
            实际确认、失败或未知结论，以及可 JSON 化的业务值和原生媒体引用。

        Raises:
            HostStateError: 音频不再接管新说话。

        Note:
            接管顺序由循环上的入队顺序确定，排队计入同一绝对期限。

            执行声明必须使用 parallel，让本模块唯一队列看见并限制每项候播；
            外层目标锁不能再隐藏一个不受 interrupt 约束的等待序列。
        """
        if not self._ready or self._stopping:
            raise HostStateError("音频不再接管新说话。")
        if self._failure is not None:
            raise self._failure
        if context.deadline <= self._clock():
            return CallOutcome(
                "confirmed",
                {
                    "accepted": False,
                    "status": "not_started",
                    "started": False,
                    "completed": False,
                    "reason": "调用期限已到。",
                },
            )
        if self._active is not None and len(self._queue) >= self.settings.max_pending:
            return CallOutcome(
                "failed",
                {"accepted": False, "status": "not_started", "started": False, "completed": False},
                "AudioQueueFull",
                "现场语音候播队列已满。",
            )
        speech = _Speech(
            context, uuid4().hex, parameters, asyncio.get_running_loop().create_future()
        )
        # 接管没有 await；明确停止与新 speak 的先后不由后台合成完成时间猜测。
        if self._active is None:
            self._active = speech
        else:
            self._queue.append(speech)
        speech.admission = asyncio.create_task(
            self._accepted(speech), name="audio-admit:" + context.call_id
        )
        self._admissions.add(speech.admission)
        speech.admission.add_done_callback(self._admission_done)
        speech.expiration = asyncio.get_running_loop().call_later(
            max(0.0, context.deadline - self._clock()), self._expire, speech
        )
        if self._runner is None or self._runner.done():
            self._runner = asyncio.create_task(self._run(), name="audio:" + self.instance_id)
            self._runner.add_done_callback(self._runner_done)
        try:
            return await asyncio.shield(speech.result)
        except asyncio.CancelledError:
            # 等待者撤销不代表世界动作撤销；同身份最终事实仍由 runner 保存。
            speech.late = True
            raise

    def _admission_done(self, task: asyncio.Task[None]) -> None:
        """接管短事务异常由队列和close继续报告，不留无人观察的Future。"""
        if not task.cancelled() and (error := task.exception()) is not None:
            self._failure = error
        else:
            self._admissions.discard(task)

    def _expire(self, speech: _Speech) -> None:
        """期限撤资格并立即释放候播位置，不等前一项同步合成返回。"""
        if speech.result.done():
            return
        if speech.revoked is None:
            speech.revoked = "DeadlineExpired"
        if speech is self._active:
            self._stop_player(speech)
            return
        else:
            if speech in self._queue:
                self._queue.remove(speech)
            operation = self._cancel_queued(speech)
        task = asyncio.create_task(operation, name="audio-expire:" + speech.context.call_id)
        self._cleanup.add(task)
        task.add_done_callback(self._cleanup_done)

    def _stop_player(self, speech: _Speech) -> asyncio.Task[None]:
        """同播放只持有一份定向停音任务，旧控制不能停掉后来新声音。"""
        if speech.player_stop is None:
            speech.player_stop = asyncio.create_task(
                self._player.interrupt(speech.playback_id),
                name="audio-player-stop:" + speech.playback_id,
            )
            self._cleanup.add(speech.player_stop)
            speech.player_stop.add_done_callback(self._cleanup_done)
        return speech.player_stop

    def _cleanup_done(self, task: asyncio.Task[None]) -> None:
        """收尾失败保留原型到close，成功任务及时收回。"""
        if not task.cancelled() and (error := task.exception()) is not None:
            self._failure = error
            _LOG.error(
                "音频期限收尾失败。",
                exc_info=(type(error), error, error.__traceback__),
                extra={"plugin_id": self.instance_id},
            )
        else:
            self._cleanup.discard(task)

    def _runner_done(self, task: asyncio.Task[None]) -> None:
        """后台程序/权威失败保留并记录一次，close 仍等待并报告原异常。"""
        if not task.cancelled() and (error := task.exception()) is not None:
            self._failure = error
            _LOG.error(
                "现场音频任务失败。",
                exc_info=(type(error), error, error.__traceback__),
                extra={"plugin_id": self.instance_id},
            )

    async def _run(self) -> None:
        """串行前进，不预先并行合成候播；错误取消资格而不吞掉故障。"""
        try:
            while (speech := self._active) is not None:
                outcome = await self._execute(speech)
                await self._finish(speech, outcome)
                self._active = self._queue.popleft() if self._queue else None
        except BaseException as error:
            # 资源所有者终止整个序列，等待者收到原错误；这不是错误转成功。
            for speech in (() if self._active is None else (self._active,)) + tuple(self._queue):
                speech.revoked = "AudioOwnerFailed"
                if not speech.result.done():
                    speech.result.set_exception(error)
                    speech.result.exception()
            self._queue.clear()
            raise

    async def _accepted(self, speech: _Speech) -> None:
        """正式接管在入队时立即保存，排队身份不等TTS开始才出现。"""
        await self._fact(
            speech.playback_id,
            "accepted",
            {
                "action_id": speech.context.action_id,
                "call_id": speech.context.call_id,
                "input_id": speech.context.input_id,
                "deadline": speech.context.deadline,
                "accepted": True,
            },
        )

    def _eligible(self, speech: _Speech) -> bool:
        if speech.revoked is None and speech.context.deadline <= self._clock():
            speech.revoked = "DeadlineExpired"
        return speech.revoked is None and not self._stopping

    async def _execute(self, speech: _Speech) -> CallOutcome:
        """接受/依赖/合成/播放阶段保持可见，资格在每个外部交接前复核。"""
        if speech.admission is None:
            raise RuntimeError("现场语音尚未登记接管事务。")
        await asyncio.shield(speech.admission)
        if not self._eligible(speech):
            return CallOutcome(
                "confirmed", self._payload(speech, PlaybackResult("not_started", False))
            )
        try:
            self._player.check_available()
            if self._decode:
                check_program(self.settings.processing_executable)
            profile = await asyncio.to_thread(read_profile, self._profile_path)
            profile.filters(self.settings.sample_rate)
            if not self._eligible(speech):
                return CallOutcome(
                    "confirmed", self._payload(speech, PlaybackResult("not_started", False))
                )

            speech.stage, speech.synthesis_started = "synthesizing", True
            await self._fact(speech.playback_id, "synthesis_started", {"synthesis_started": True})
            if not self._eligible(speech):
                return CallOutcome(
                    "confirmed", self._payload(speech, PlaybackResult("not_started", False))
                )
            if self.settings.playback_backend == "streaming":
                encoded = self._stream(speech)
            else:
                audio = await self._source.synthesize(speech.parameters, speech.context.call_id)
                speech.synthesized, speech.byte_size = True, len(audio.data)
                await self._fact(
                    speech.playback_id,
                    "synthesized",
                    {"synthesized": True, "byte_size": speech.byte_size},
                )
                if not self._eligible(speech):
                    return CallOutcome(
                        "confirmed", self._payload(speech, PlaybackResult("not_started", False))
                    )
                encoded = self._chunks(audio)

            async with aclosing(encoded):
                pcm = (
                    decode_pcm(
                        encoded,
                        executable=self.settings.processing_executable,
                        sample_rate=self.settings.sample_rate,
                        profile=profile,
                    )
                    if self._decode
                    else encoded
                )
                async with aclosing(pcm):
                    async with aclosing(self._body_pcm(pcm, speech)) as tracked:
                        result = await self._player.play(
                            tracked,
                            playback_id=speech.playback_id,
                            deadline=min(
                                speech.context.deadline,
                                self._clock() + self.settings.playback_timeout_seconds,
                            ),
                            started=lambda: self._started(speech),
                            progress=lambda elapsed, energy: self._progress(
                                speech, elapsed, energy
                            ),
                        )
            return CallOutcome(
                "failed" if result.status == "failed" else "confirmed",
                self._payload(speech, result),
                "AudioPlaybackFailed" if result.status == "failed" else None,
                result.error if result.status == "failed" else None,
            )
        except AudioError as error:
            result = PlaybackResult("failed", speech.started, speech.elapsed_seconds, str(error))
            return CallOutcome(
                "failed", self._payload(speech, result), type(error).__name__, str(error)
            )
        finally:
            if self._body is not None and speech.started:
                speech.body = self._body.playback_finished(speech.playback_id)

    async def _chunks(self, audio: SpeechAudio) -> AsyncGenerator[bytes, None]:
        yield audio.data

    async def _body_pcm(
        self, pcm: AsyncGenerator[bytes, None], speech: _Speech
    ) -> AsyncGenerator[bytes, None]:
        """调音后20ms块借给身体分析；视觉接管不等于实际播放开始。

        BodyPort只登记有界采样，禁止同步DLL或网络等待；原字节顺序及
        音频背压仍由播放器拥有。身体分析失败/落后不能阻断声音。
        """
        pending = bytearray()
        offset, size = 0, self.settings.sample_rate // 50 * 2
        async for chunk in pcm:
            if self._body is not None and self._eligible(speech):
                pending.extend(chunk)
                while len(pending) >= size:
                    frame = bytes(pending[:size])
                    del pending[:size]
                    self._body.playback_pcm(
                        speech.playback_id, self.settings.sample_rate, offset, frame
                    )
                    offset += size // 2
            yield chunk

    async def _stream(self, speech: _Speech) -> AsyncGenerator[bytes, None]:
        """stream 只有自然 completed 才记录合成完成，撤资格不刷新旧尾帧。"""
        async with aclosing(
            self._source.stream(speech.parameters, speech.context.call_id)
        ) as source:
            async for chunk in source:
                speech.byte_size += len(chunk)
                if not self._eligible(speech):
                    return
                yield chunk
        speech.synthesized = True
        await self._fact(
            speech.playback_id,
            "synthesized",
            {"synthesized": True, "byte_size": speech.byte_size},
        )

    async def _started(self, speech: _Speech) -> None:
        """只由实际媒体时钟触发，先存真实事实再交身体最新姿态。"""
        speech.started, speech.stage = True, "playing"
        await self._fact(
            speech.playback_id,
            "playback_started",
            {"started": True, "progress_basis": "player_media_clock"},
        )
        if self._body is not None and self._eligible(speech):
            speech.body = self._body.playback_started(
                speech.playback_id, speech.parameters.body, speech.context.deadline
            )

    def _progress(self, speech: _Speech, elapsed: float, energy: float) -> None:
        speech.elapsed_seconds = elapsed
        if self._body is not None and self._eligible(speech):
            self._body.playback_progress(speech.playback_id, elapsed, energy)

    async def _finish(self, speech: _Speech, outcome: CallOutcome) -> None:
        """声音事实不依赖 Worker/会话成功；取消等待者追加真实迟到逐次回执。"""
        if not isinstance(outcome.value, dict):
            raise RuntimeError("现场播放必须返回结构化真实结果。")
        outcome.value["body"] = speech.body
        await self._fact(speech.playback_id, "finished", outcome.value)
        if speech.expiration is not None:
            speech.expiration.cancel()
        speech.stage, self._last = "finished", outcome.value
        if not speech.result.done():
            speech.result.set_result(outcome)
        if speech.late and self._late_results is not None:
            await self._late_results.record(speech.context, outcome)

    async def _cancel_queued(self, speech: _Speech) -> None:
        """明确停音和期限竞争共用一个候播收尾，不能写两份不同终态。"""
        if speech.cancellation is None:
            speech.cancellation = asyncio.create_task(
                self._finish_cancel(speech), name="audio-cancel:" + speech.context.call_id
            )
            self._cleanup.add(speech.cancellation)
            speech.cancellation.add_done_callback(self._cleanup_done)
        await asyncio.shield(speech.cancellation)

    async def _finish_cancel(self, speech: _Speech) -> None:
        """候播只取得accepted/finished事实，永不进入合成阶段。"""
        if speech.admission is None:
            raise RuntimeError("候播取消缺少接管事务。")
        try:
            await asyncio.shield(speech.admission)
            await self._finish(
                speech,
                CallOutcome(
                    "confirmed", self._payload(speech, PlaybackResult("not_started", False))
                ),
            )
        except BaseException as error:
            # 权威失败也必须唤醒候播等待者，不能让被移出队列的Future永远悬空。
            if not speech.result.done():
                speech.result.set_exception(error)
                speech.result.exception()
            raise

    async def interrupt(self, context: CallContext, *, record_late: bool = False) -> CallOutcome:
        """先撤当前及已有候播资格。

        Args:
            context: 宿主或 Core 注入的可信调用上下文，不由模型自行填写。
            record_late: 是否将稍后取得的真实结果写回原调用历史。 默认 False。

        Returns:
            实际确认、失败或未知结论，以及可 JSON 化的业务值和原生媒体引用。

        Raises:
            HostStateError: 音频正在释放资源，不再接管新的输出控制。

        Note:
            期限内未收回同步合成则报告 pending。

            清理等待不保留队列锁，因此停止后新接管的 speak 仍可排队；旧活动
            合成完成只归入原身份，不能重新取得新请求的播放资格。
        """
        if self._close_task is not None:
            raise HostStateError("音频正在释放资源，不再接管新的输出控制。")
        if len(self._controls) >= 16:
            return CallOutcome(
                "failed",
                {"accepted": False, "confirmed": False, "status": "control_full"},
                "AudioControlFull",
                "音频未确认控制通道已满。",
            )
        active = self._active
        affected = (() if active is None else (active,)) + tuple(self._queue)
        self._queue.clear()
        self._revision += 1
        for speech in affected:
            if speech.revoked is None:
                speech.revoked = "ExplicitInterrupt"
        # 收尾任务在首次await前归所有者登记。调用者取消不能让当前声音
        # 留在已经撤销资格、却没人真正停止播放器的中间状态。
        stop = _Stop(
            context,
            affected,
            None if active is None else self._stop_player(active),
            record_late,
            self._revision,
        )
        self._last_control = self._stop_result(stop, confirmed=False)
        task = asyncio.create_task(self._interrupt(stop), name="audio-stop:" + context.call_id)
        self._controls.add(task)
        task.add_done_callback(self._control_done)
        try:
            done, _ = await asyncio.wait(
                (task,), timeout=max(0.0, context.deadline - self._clock())
            )
            if done:
                return task.result()
            stop.pending_reported = True
            return CallOutcome("unknown", self._stop_result(stop, confirmed=False))
        except asyncio.CancelledError:
            stop.pending_reported = True
            raise

    def _control_done(self, task: asyncio.Task[CallOutcome]) -> None:
        """控制等待者可离开，真实任务和错误仍由音频所有者收回。"""
        if not task.cancelled() and (error := task.exception()) is not None:
            self._failure = error
            _LOG.error(
                "音频控制收尾失败。",
                exc_info=(type(error), error, error.__traceback__),
                extra={"plugin_id": self.instance_id},
            )
        else:
            self._controls.discard(task)

    async def _interrupt(self, stop: _Stop) -> CallOutcome:
        """已撤资格后的正式控制接管和实际收尾，不要求actions/calls已有行。"""
        await self._fact(
            stop.context.call_id,
            "control_accepted",
            {
                "accepted": True,
                "affected": [speech.playback_id for speech in stop.affected],
                "input_id": stop.context.input_id,
            },
        )
        await self._fact(
            stop.context.call_id, "control_result", self._stop_result(stop, confirmed=False)
        )
        if stop.player_stop is not None:
            await asyncio.shield(stop.player_stop)
        for speech in stop.affected:
            if speech is not self._active and not speech.result.done():
                await self._cancel_queued(speech)
        if stop.affected:
            await asyncio.gather(*(asyncio.shield(speech.result) for speech in stop.affected))
        result = self._stop_result(stop, confirmed=True)
        await self._fact(stop.context.call_id, "control_confirmed", result)
        if stop.revision == self._revision:
            self._last_control = result
        outcome = CallOutcome("confirmed", result)
        if stop.pending_reported and stop.record_late and self._late_results is not None:
            await self._late_results.record(stop.context, outcome)
        return outcome

    def _stop_result(self, stop: _Stop, *, confirmed: bool) -> JsonObject:
        """控制整体确认须实际清理完成，逐个播放仍保留部分效果。"""
        results: list[JsonValue] = []
        for speech in stop.affected:
            if speech.result.done():
                outcome = speech.result.result()
                if isinstance(outcome.value, dict):
                    results.append(outcome.value)
            else:
                results.append(
                    {
                        "playback_id": speech.playback_id,
                        "status": "stopping",
                        "started": speech.started,
                        "synthesis_started": speech.synthesis_started,
                        "synthesized": speech.synthesized,
                        "confirmed": False,
                    }
                )
        return {
            "control_id": stop.context.call_id,
            "accepted": True,
            "confirmed": confirmed,
            "status": "confirmed" if confirmed else "pending",
            "affected": results,
        }

    async def record(self, parameters: RecordParameters, context: CallContext) -> CallOutcome:
        """合成本次完整音频文件，返回保存的素材与真实回执。

        Args:
            parameters: 已通过能力参数模型校验的调用参数。
            context: 宿主或 Core 注入的可信调用上下文，不由模型自行填写。

        Returns:
            实际确认、失败或未知结论，以及可 JSON 化的业务值和原生媒体引用。

        Raises:
            HostStateError: 音频已停止新合成。

        Note:
            独立文件合成不加入现场队列；取消等待仍持有真实网络/文件任务。
        """
        if self._stopping:
            raise HostStateError("音频已停止新合成。")
        recording = _Recording()
        task = asyncio.create_task(
            self._record(parameters, context, recording), name="audio-record:" + context.call_id
        )
        self._records.add(task)
        task.add_done_callback(self._record_done)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            recording.late = True
            if task.done() and not task.cancelled() and task.exception() is None:
                late = asyncio.create_task(
                    self._record_receipt(context, task.result()),
                    name="audio-record-late:" + context.call_id,
                )
                self._cleanup.add(late)
                late.add_done_callback(self._cleanup_done)
            raise

    def _record_done(self, task: asyncio.Task[CallOutcome]) -> None:
        """已清理的业务失败不阻止关闭；内部异常仍保留原型供close报告。"""
        if task.cancelled():
            return
        error = task.exception()
        if error is None or isinstance(error, AudioError):
            self._records.discard(task)

    async def _record(
        self, parameters: RecordParameters, context: CallContext, recording: _Recording
    ) -> CallOutcome:
        """导出实际结果不依赖等待者；已离开时追加同调用真实回执。"""
        try:
            outcome = await self._render_record(parameters, context)
        except AudioError as error:
            outcome = CallOutcome("failed", None, type(error).__name__, str(error))
        if recording.late:
            await self._record_receipt(context, outcome)
        return outcome

    async def _record_receipt(self, context: CallContext, outcome: CallOutcome) -> None:
        """只有真实record能力拥有CallJournal身份；VAD不会进入此分支。"""
        if self._late_results is not None:
            await self._late_results.record(context, outcome)

    async def _render_record(
        self, parameters: RecordParameters, context: CallContext
    ) -> CallOutcome:
        """先拒绝沙箱逃逸，再开始合成；真实文件写入不随等待者取消回滚。"""
        if context.deadline <= self._clock():
            raise AudioError("文件合成期限已到。")
        target: Path | None = None
        if parameters.save_as:
            relative = Path(parameters.save_as)
            target = (self._output_dir / relative).resolve()
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not target.is_relative_to(self._output_dir)
            ):
                raise AudioError("音频保存路径超出沙箱。")
        profile = await asyncio.to_thread(read_profile, self._profile_path)
        profile.filters(self.settings.sample_rate)
        if profile.enabled:
            check_program(self.settings.processing_executable)
        if context.deadline <= self._clock():
            raise AudioError("预设读取后文件合成期限已到。")
        audio = await self._source.synthesize(parameters, context.call_id)
        audio = await process_audio(
            audio,
            executable=self.settings.processing_executable,
            sample_rate=self.settings.sample_rate,
            profile=profile,
        )
        if target is None:
            return CallOutcome("confirmed", base64.b64encode(audio.data).decode("ascii"))
        if not target.suffix:
            target = target.with_suffix("." + audio.format)
        await asyncio.to_thread(self._write_audio, target, audio.data)
        return CallOutcome("confirmed", str(target))

    def _write_audio(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def snapshot(self) -> JsonObject:
        """读取真实阶段和候播身份，不回显密钥、音频或全部说话正文。

        Returns:
            业务对象，按实际结果提供 ready、revision、active、queued、max_pending、last、
            control、pending_controls、error 字段。
        """
        active = self._active
        return {
            "ready": self._ready and not self._stopping,
            "revision": self._revision,
            "active": None
            if active is None
            else {
                "playback_id": active.playback_id,
                "call_id": active.context.call_id,
                "stage": active.stage,
                "started": active.started,
                "synthesized": active.synthesized,
                "elapsed_seconds": active.elapsed_seconds,
                "revoked": active.revoked,
            },
            "queued": [
                {
                    "playback_id": speech.playback_id,
                    "call_id": speech.context.call_id,
                    "deadline": speech.context.deadline,
                    "stage": speech.stage,
                }
                for speech in self._queue
            ],
            "max_pending": self.settings.max_pending,
            "last": self._last,
            "control": self._last_control,
            "pending_controls": len(self._controls),
            "error": None
            if self._failure is None
            else {"error_type": type(self._failure).__name__, "message": str(self._failure)},
        }

    async def prepare_request(self) -> JsonObject:
        """保留当前播放阶段、排队身份及真实收尾回执，不暴露内部单调截止。

        Returns:
            业务对象，按实际结果提供 ready、active、queued、last、control、pending_controls、
            error 字段。

        Note:
            last/control 是实际播放与停音结果，不能按诊断字段一并丢弃；在途
            业务状态仍不表示声音已被听见，执行额度和停止资格由宿主继续核验。
        """
        state = self.snapshot()
        return {
            "ready": state["ready"],
            "active": state["active"],
            "queued": [
                {
                    "playback_id": speech.playback_id,
                    "call_id": speech.context.call_id,
                    "stage": speech.stage,
                }
                for speech in self._queue
            ],
            "last": state["last"],
            "control": state["control"],
            "pending_controls": state["pending_controls"],
            "error": state["error"],
        }

    async def stop_inputs(self) -> None:
        """宿主关闭先撤全部现场资格，再收回输出。

        Raises:
            BaseExceptionGroup: 音频停止接入失败。

        Note:
            不关闭身体或 Store。
        """
        self._stopping, self._ready = True, False
        affected = (() if self._active is None else (self._active,)) + tuple(self._queue)
        self._queue.clear()
        for speech in affected:
            if speech.revoked is None:
                speech.revoked = "HostClosing"
        failures: list[BaseException] = []
        if self._active is not None:
            try:
                await asyncio.shield(self._stop_player(self._active))
            except BaseException as error:
                failures.append(error)
        for speech in affected:
            if speech is not self._active and not speech.result.done():
                try:
                    await self._cancel_queued(speech)
                except BaseException as error:
                    failures.append(error)
        if failures:
            raise BaseExceptionGroup("音频停止接入失败。", failures)

    async def close(self) -> None:
        """重复调用等同一收尾，未结束合成/导出不能冒充 closed。"""
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(), name="audio-close:" + self.instance_id
            )
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        """先归还全部任务借用，再取得实际关闭明证，最后报告历史故障。"""
        failures: list[BaseException] = []
        try:
            await self.stop_inputs()
        except BaseException as error:
            failures.append(error)

        # 队列故障可能已经移除候播，但它们的入库和取消任务仍借用 Store。
        # 收尾任务还可产生迟到回执，所以每轮等待后复核新的真实任务。
        observed: set[_OwnedTask] = set()
        while tasks := [task for task in self._owned_tasks() if task not in observed]:
            observed.update(tasks)
            operations: list[Awaitable[object]] = list(tasks)
            results = await asyncio.gather(*operations, return_exceptions=True)
            failures.extend(result for result in results if isinstance(result, BaseException))

        closed = 0
        for operation in (self._player.close(), self._source.close()):
            try:
                await operation
            except BaseException as error:
                failures.append(error)
            else:
                closed += 1
        # 实际 close 成功是独立证据；原 runner/control 错误不推翻它，也不能
        # 用已结束任务数量替代播放器/连接的关闭确认。
        self._resources_released = closed == 2 and all(task.done() for task in self._owned_tasks())
        if failures:
            raise BaseExceptionGroup("音频资源收尾失败。", failures)

    def _owned_tasks(self) -> tuple[_OwnedTask, ...]:
        """列出仍归本资源所有者的任务，包括已脱离候播列表的事务。"""
        tasks: list[_OwnedTask] = []
        tasks.extend(self._admissions)
        tasks.extend(self._records)
        tasks.extend(self._cleanup)
        tasks.extend(self._controls)
        if self._runner is not None:
            tasks.append(self._runner)
        return tuple(tasks)
