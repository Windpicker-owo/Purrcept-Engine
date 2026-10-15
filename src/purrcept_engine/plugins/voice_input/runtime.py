"""拥有一次收音会话的设备、VAD、有限识别任务与发言接管顺序。

起音只取得缓存名额，持续人声正式入账后才由宿主停音和抢占；最终文字或
原生音频使用同一发言关系，不再次抢占。未接管 PCM 随取消释放，已接管
事务不撤销。开始/停止由明确能力控制，页面刷新和断线不改变资源寿命。
"""

from __future__ import annotations

import asyncio
import math
import struct
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from websockets.exceptions import WebSocketException

from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.audio.wav import encode_wav
from purrcept_engine.plugin_system import (
    Admission,
    InputContext,
    InputPort,
    InputSource,
    InputSubmission,
    MediaAsset,
)
from purrcept_engine.plugins.voice_input.models import (
    CaptureDriver,
    InputDevice,
    InputMode,
    StreamingTranscriber,
    TranscriptUpdate,
    VoiceDetector,
    VoiceInputError,
    VoiceInputSettings,
)
from purrcept_engine.plugins.voice_input.vad import Segmenter
from purrcept_engine.values import JsonObject, encode_json


@dataclass(slots=True)
class _Utterance:
    """单发言缓存归运行时，识别任务读取队列，交付任务按 sequence 串行入账。"""

    identity: str
    session: str
    sequence: int
    mode: InputMode
    queue: asyncio.Queue[bytes | None]
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    pcm: bytearray = field(default_factory=bytearray)
    ended: bool = False
    started_input_id: str | None = None
    final_input_id: str | None = None
    task: asyncio.Task[None] | None = None
    start_task: asyncio.Task[None] | None = None
    result: tuple[str, tuple[MediaAsset, ...]] | None = None
    finished: bool = False
    failure: str | None = None


class VoiceInputRuntime:
    """插件 build 创建的单循环所有者，输入端口只交接事实和查询处理状态。

    control 串行开始/停止，delivery 固定发言入账顺序。最多保留配置数量
    的未结束识别；没有每帧任务或无限候选。过载暂停整场采集，未完成发言
    标为缺口，已经入账的 speech_started 或最终输入仍可查询。
    """

    def __init__(
        self,
        instance_id: str,
        settings: VoiceInputSettings,
        inputs: InputPort,
        capture_factory: Callable[[], CaptureDriver],
        vad_factory: Callable[[], VoiceDetector],
        transcriber: StreamingTranscriber,
        *,
        native_audio_enabled: bool,
    ) -> None:
        """保存明确依赖；构造不打开声卡、VAD 或付费模型。"""
        self.instance_id, self.settings, self.inputs = instance_id, settings, inputs
        self.capture_factory, self.vad_factory, self.transcriber = (
            capture_factory,
            vad_factory,
            transcriber,
        )
        self.native_audio_enabled = native_audio_enabled
        self.session = ""
        self.mode: InputMode = "asr"
        self.device: int | None = None
        self.state: Literal["stopped", "starting", "listening", "stopping", "paused", "failed"] = (
            "stopped"
        )
        self.error: str | None = None
        self.level = 0.0
        self.partial = ""
        self.revision = 0
        self.closed = False
        self.current: _Utterance | None = None
        self.utterances: dict[int, _Utterance] = {}
        self.history: deque[JsonObject] = deque(maxlen=100)
        self.gaps: deque[JsonObject] = deque(maxlen=32)
        self.next_sequence = self.deliver_sequence = 0
        self.driver: asyncio.Task[None] | None = None
        self.ready = asyncio.Event()
        self.control = asyncio.Lock()
        self.delivery = asyncio.Lock()
        self.device_cache: tuple[InputDevice, ...] | None = None

    def snapshot(self) -> JsonObject:
        """读取实际阶段和来源，识别完成不冒充主体已处理或已经回应。"""
        return {
            "state": self.state,
            "session_id": self.session,
            "mode": self.mode,
            "device": self.device,
            "source_name": self.settings.source_name,
            "utterance_id": self.current.identity if self.current else None,
            "level": self.level,
            "partial": self.partial,
            "error": self.error,
            "asr_ready": bool(self.settings.asr_api_key.get_secret_value()),
            "audio_ready": self.native_audio_enabled,
            "pending_utterances": len(self.utterances),
            "interrupt_ms": self.settings.interrupt_ms,
            "history": [item.copy() for item in self.history],
            "gaps": [item.copy() for item in self.gaps],
            "revision": self.revision,
            "devices": [
                {"index": device.device_id, "name": device.name}
                for device in self.device_cache or ()
            ],
            "devices_loaded": self.device_cache is not None,
            "available_modes": ["asr", "audio"] if self.native_audio_enabled else ["asr"],
            "recognition_state": "recognizing" if self.utterances else "idle",
        }

    async def prepare_request(self) -> JsonObject:
        """交付收音能力与实际阶段；不把未定识别文本或历史预览当作新发言。

        已接管发言沿正式输入进入上下文。缺口保留来源和原因，不能因省去
        采样电平、缓存计数和设备枚举而隐藏收音失败。
        """
        state = self.snapshot()
        return {
            key: state[key]
            for key in (
                "state",
                "mode",
                "device",
                "source_name",
                "error",
                "asr_ready",
                "audio_ready",
                "recognition_state",
                "gaps",
            )
        }

    async def status(self) -> JsonObject:
        """按 input_id 查询真实处理阶段，观察不重投已接管音频或文字。"""
        for item in tuple(self.history):
            input_id = item.get("input_id")
            if isinstance(input_id, str):
                processing = await self.inputs.query_processing(input_id)
                item["processing_stage"] = processing.stage if processing else None
                item["processing_reason"] = processing.reason if processing else None
        return self.snapshot()

    async def devices(self) -> JsonObject:
        """明确枚举输入设备，不开始收音。"""
        devices = await self.capture_factory().devices()
        self.device_cache = devices
        return {
            "devices": [{"index": device.device_id, "name": device.name} for device in devices],
            "devices_loaded": True,
        }

    async def start(self, mode: InputMode, device: int | None) -> JsonObject:
        """串行取得设备；第一帧实际到达后才确认 listening，不自动重新启动。"""
        async with self.control:
            if self.closed:
                raise VoiceInputError("收音插件已关闭。")
            if self.driver is not None:
                if not self.driver.done():
                    raise VoiceInputError("收音已经开启，请先停止当前会话。")
                await self.driver
            if mode == "audio" and not self.native_audio_enabled:
                raise VoiceInputError(
                    "直接听原声需要模型音频能力和已注册的媒体计量规则；请使用识别成文字。"
                )
            if mode == "asr" and not self.settings.asr_api_key.get_secret_value():
                raise VoiceInputError("ASR 未配置显式密钥，收音尚未开启。")
            self.session, self.mode, self.device = uuid4().hex, mode, device
            self.next_sequence = self.deliver_sequence = 0
            self.state, self.error, self.partial = "starting", None, ""
            self.revision += 1
            self.ready.clear()
            self.driver = asyncio.create_task(self._run(self.session), name="voice-capture")
            self.driver.add_done_callback(self._driver_finished)
            waiter = asyncio.create_task(self.ready.wait(), name="voice-capture-ready")
            try:
                done, _ = await asyncio.wait(
                    (self.driver, waiter), timeout=20, return_when=asyncio.FIRST_COMPLETED
                )
                if self.driver in done:
                    await self.driver
                    raise VoiceInputError(self.error or "麦克风未开始收音。")
                if not done:
                    raise TimeoutError("麦克风启动超过 20 秒，当前启动已取消。")
                return self.snapshot()
            except BaseException:
                # 清理原样传播启动或取消，不将内部故障改成可重填参数。
                await self._stop()
                raise
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

    def _driver_finished(self, task: asyncio.Task[None]) -> None:
        """取走后台异常并保留原任务，后续控制仍能观察真实内部故障。"""
        if task.cancelled() and self.driver is task and self.state == "stopping":
            self.state, self.session, self.driver = "stopped", "", None
            return
        if not task.cancelled() and (error := task.exception()) is not None:
            self.state, self.error = "failed", "收音内部失败：" + type(error).__name__

    async def stop(self) -> JsonObject:
        """停止新采样并释放未接管缓存，不能承担停止已有音频输出。"""
        async with self.control:
            await self._stop()
            return self.snapshot()

    async def _stop(self) -> None:
        """调用者持控制锁；取消输入任务前封存会话，已接管事务仍等到确认。"""
        driver = self.driver
        if driver is not None:
            if self.state not in {"paused", "failed"}:
                self.state = "stopping"
            if not driver.done() and not driver.cancelling():
                driver.cancel()
            try:
                await asyncio.shield(driver)
            except asyncio.CancelledError:
                # 停止观察被取消时设备收尾继续归原任务；不能清空引用后
                # 开启第二个声卡流，也不能重复 cancel 打断原生 finally。
                if not driver.cancelled():
                    raise
            self.driver = None
        self.session = ""
        self.level, self.partial = 0.0, ""
        if self.state not in {"paused", "failed"}:
            self.state = "stopped"
        self.revision += 1

    async def stop_inputs(self) -> None:
        """宿主关闭时禁止后续开始并等待当前设备收回，不触碰播放资源。"""
        async with self.control:
            self.closed = True
            await self._stop()

    async def close(self) -> None:
        """关闭重复加入同一设备任务，已接管输入归宿主继续处理。"""
        await self.stop_inputs()

    def _source(self, utterance: _Utterance, kind: str) -> InputSource:
        return InputSource(
            self.instance_id,
            "local-microphone",
            utterance.session,
            kind,
            utterance.identity,
            "local-microphone",
            self.settings.source_name,
        )

    def _context(self, utterance: _Utterance, *, started: bool) -> InputContext:
        """可信发言关系由设备状态生成，最终材料沿用关系但不重复授予抢占。"""
        details: JsonObject = {
            "voice": {
                "session_id": utterance.session,
                "utterance_id": utterance.identity,
                "sequence": utterance.sequence,
                "mode": utterance.mode,
                "speech_started_input_id": utterance.started_input_id,
            }
        }
        return InputContext(
            encode_json(details).decode("utf-8"),
            minimum_wake=True,
            interrupt_generation=started,
            speech_started=started,
        )

    async def _submit(self, submission: InputSubmission) -> Admission:
        """取消结束观察前等待原接管任务确认，绝不撤销或重发已提交数据。"""
        task = asyncio.create_task(self.inputs.submit(submission), name="voice-input-admission")
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _admit_start(self, utterance: _Utterance) -> None:
        """独立等待持续人声入账；采样继续，最终材料必须等此身份确认。

        每个有限发言名额最多拥有一个起音任务。停止收音也等已经提交的
        事实确认，不取消它或重新发送，避免丢失真实的抢占事件关系。
        """
        admitted = await self._submit(
            InputSubmission(
                self._source(utterance, "speech_started"),
                "本机麦克风确认持续人声。",
                utterance.occurred_at,
                datetime.now(UTC),
                context=self._context(utterance, started=True),
            )
        )
        utterance.started_input_id = admitted.input_id

    def _gap(self, utterance: _Utterance | None, message: str) -> None:
        self.gaps.append(
            {
                "session_id": utterance.session if utterance else self.session,
                "utterance_id": utterance.identity if utterance else None,
                "message": message,
                "already_admitted_input_id": utterance.started_input_id if utterance else None,
            }
        )

    async def _run(self, session: str) -> None:
        """采样/VAD 主流程始终可见；识别与交付仅在有限发言名额内并行。"""
        capture = self.capture_factory()
        detector = self.vad_factory()
        segmenter = Segmenter(self.settings)
        frames = capture.frames(self.device)
        try:
            async for frame in frames:
                if len(frame) != 1024:
                    raise VoiceInputError("输入设备返回了不完整 VAD 帧，当前发言已释放。")
                for value in tuple(self.utterances.values()):
                    for task in (value.task, value.start_task):
                        if task is not None and task.done() and not task.cancelled():
                            error = task.exception()
                            if error is not None:
                                raise error
                if self.state == "starting":
                    self.state = "listening"
                    self.ready.set()
                samples = struct.unpack("<512h", frame)
                self.level = math.sqrt(sum(value * value for value in samples) / 512) / 32768
                probability = detector.probability(frame)
                for kind, pcm in segmenter.feed(frame, probability):
                    if kind == "begin":
                        if len(self.utterances) >= self.settings.max_pending_utterances:
                            raise VoiceInputError("识别积压达到额度，收音已暂停，当前发言未接管。")
                        utterance = _Utterance(
                            uuid4().hex,
                            session,
                            self.next_sequence,
                            self.mode,
                            asyncio.Queue(maxsize=self.settings.frame_queue_size),
                        )
                        self.next_sequence += 1
                        self.current = utterance
                        self.utterances[utterance.sequence] = utterance
                        self.partial = ""
                        utterance.task = asyncio.create_task(
                            self._recognize(utterance), name="voice-recognition"
                        )
                    elif kind == "pcm" and self.current is not None:
                        utterance = self.current
                        if utterance.mode == "audio":
                            if len(utterance.pcm) + len(pcm) > 16000 * 60 * 2:
                                raise VoiceInputError("原生发言超过 60 秒，收音已暂停。")
                            utterance.pcm.extend(pcm)
                        try:
                            utterance.queue.put_nowait(pcm)
                        except asyncio.QueueFull as error:
                            raise VoiceInputError(
                                "识别上传缓存已满，收音已暂停，当前发言存在缺口。"
                            ) from error
                    elif kind == "speech_started" and self.current is not None:
                        utterance = self.current
                        utterance.start_task = asyncio.create_task(
                            self._admit_start(utterance), name="voice-start-admission"
                        )
                    elif kind in {"end", "discard"} and self.current is not None:
                        utterance, self.current = self.current, None
                        if kind == "end":
                            utterance.ended = True
                            try:
                                utterance.queue.put_nowait(None)
                            except asyncio.QueueFull as error:
                                raise VoiceInputError(
                                    "识别结束标记无法进入有界缓存，收音已暂停。"
                                ) from error
                        else:
                            utterance.task = asyncio.create_task(
                                self._discard(utterance, utterance.task), name="voice-discard"
                            )
                # 已缓存的设备帧可能连续同步 yield。显式归还一次调度机会，
                # 让识别消费者排空队列；不能把自身占满循环误判为上传过载。
                await asyncio.sleep(0)
        except (VoiceInputError, StorageError, OSError, TimeoutError, WebSocketException) as error:
            self.state, self.error = "paused", str(error)
            self._gap(self.current, str(error))
        finally:
            # 先关闭输入流，再收回识别；取消后绝不刷新未完成录音尾部。
            await frames.aclose()
            jobs = tuple(value.task for value in self.utterances.values() if value.task is not None)
            starts = tuple(
                value.start_task
                for value in self.utterances.values()
                if value.start_task is not None
            )
            for value in self.utterances.values():
                if not value.finished:
                    self._gap(value, "收音结束时发言尚未完整接管。")
            for job in jobs:
                if not job.done():
                    job.cancel()
            results = await asyncio.gather(*jobs, *starts, return_exceptions=True)
            self.current = None
            self.utterances.clear()
            self.level, self.partial = 0.0, ""
            for result in results:
                if isinstance(result, Exception) and not isinstance(
                    result,
                    (VoiceInputError, StorageError, OSError, TimeoutError, WebSocketException),
                ):
                    raise result

    async def _discard(self, utterance: _Utterance, recognition: asyncio.Task[None] | None) -> None:
        """短噪声仍按序释放名额，但不让采样等待前一句的入库或识别收尾。"""
        if recognition is not None:
            recognition.cancel()
            await asyncio.gather(recognition, return_exceptions=True)
        utterance.finished = True
        await self._deliver()

    async def _recognize(self, utterance: _Utterance) -> None:
        """只采用完整最终结果，有限句子修订和 PCM 归本发言任务所有。"""
        sentences: dict[str, TranscriptUpdate] = {}

        async def frames() -> AsyncIterator[bytes]:
            while (pcm := await utterance.queue.get()) is not None:
                yield pcm

        try:
            if utterance.mode == "asr":
                async for update in self.transcriber.transcribe(frames()):
                    previous = sentences.get(update.sentence_id)
                    if previous is None or not previous.final or update.final:
                        sentences[update.sentence_id] = update
                    if (
                        len(sentences) > 256
                        or sum(len(value.text) for value in sentences.values()) > 65536
                    ):
                        raise VoiceInputError("ASR 文字超过当前发言缓存额度。")
                    if (
                        utterance.session == self.session
                        and utterance.sequence == self.next_sequence - 1
                    ):
                        self.partial = "".join(value.text for value in sentences.values())
                if not utterance.ended or any(not value.final for value in sentences.values()):
                    raise VoiceInputError("ASR 未返回完整最终识别结果。")
                text = "".join(value.text for value in sentences.values()).strip()
                utterance.result = (text, ()) if text else None
            else:
                async for _ in frames():
                    pass
                if not utterance.ended:
                    raise VoiceInputError("原生发言未正常结束。")
                # 同一发言分成顺序附件，不复制整场收音；媒体最长每段 20 秒。
                frozen = bytes(utterance.pcm)
                segment_bytes = 16000 * 20 * 2
                media = tuple(
                    MediaAsset(
                        encode_wav(frozen[offset : offset + segment_bytes], 16000),
                        "audio/wav",
                        f"voice-{index + 1}.wav",
                    )
                    for index, offset in enumerate(range(0, len(frozen), segment_bytes))
                )
                utterance.result = (
                    "本机麦克风发来原生语音，各附件属于同一次发言，请依顺序理解。",
                    media,
                )
        except (VoiceInputError, OSError, TimeoutError, WebSocketException) as error:
            utterance.failure = str(error)
            self._gap(utterance, str(error))
        finally:
            utterance.pcm.clear()
            utterance.finished = True
        await self._deliver()

    async def _deliver(self) -> None:
        """按起音顺序交最终材料，旧发言不能覆盖新会话局部预览。"""
        async with self.delivery:
            while (
                utterance := self.utterances.get(self.deliver_sequence)
            ) is not None and utterance.finished:
                if utterance.start_task is not None:
                    await asyncio.shield(utterance.start_task)
                if utterance.result is not None:
                    text, media = utterance.result
                    admitted = await self._submit(
                        InputSubmission(
                            self._source(utterance, "utterance"),
                            text,
                            utterance.occurred_at,
                            datetime.now(UTC),
                            media,
                            self._context(utterance, started=False),
                        )
                    )
                    utterance.final_input_id = admitted.input_id
                    self.history.append(
                        {
                            "session_id": utterance.session,
                            "utterance_id": utterance.identity,
                            "sequence": utterance.sequence,
                            "input_id": admitted.input_id,
                            "speech_started_input_id": utterance.started_input_id,
                            "text": text,
                            "processing_stage": "admitted",
                        }
                    )
                self.deliver_sequence += 1
                del self.utterances[utterance.sequence]
                if utterance.sequence == self.next_sequence - 1:
                    self.partial = ""
