"""接收外部刺激输入，持久化至收件箱并交接 VAD 停播与注意唤醒信号。"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from purrcept_engine.app.host_state import EngineState
from purrcept_engine.app.lifecycle import HostLifecycle
from purrcept_engine.domain.execution.providers import (
    CallContext,
    CallOutcome,
)
from purrcept_engine.domain.inputs.models import (
    Admission,
    InputSubmission,
)
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite.store import Store

_logger = logging.getLogger("purrcept_engine.app.ingress")


def _preview_submission(submission: InputSubmission) -> str:
    """生成紧凑可读的单行输入预览用于控制台日志。"""
    text = " ".join(submission.text.split())
    if len(text) > 56:
        text = text[:56] + "…"
    parts: list[str] = []
    if text:
        parts.append(f'"{text}"')
    if submission.media:
        parts.append(f"[附件×{len(submission.media)}]")
    if submission.context.speech_started and not parts:
        parts.append("[语音起音信号]")
    return " ".join(parts) if parts else "[空内容]"


class InputAdmissionService:
    """接收外部输入并写入收件箱，异步触发停播打断与注意评估。"""

    def __init__(self, state: EngineState, lifecycle: HostLifecycle) -> None:
        self.state = state
        self.lifecycle = lifecycle

    async def submit(self, submission: InputSubmission) -> Admission:
        """提交一条外部输入并返回包含 ``input_id`` 的受理凭证。

        Args:
            submission: 待接管的输入内容、来源与附件。

        Returns:
            包含 ``input_id`` 与去重标记的受理凭证。

        Raises:
            StorageError: 待处理收件箱容量已满或存储写入失败。
            HostStateError: 宿主尚未启动或正在关闭。
        """
        store = self.lifecycle.running_store()
        self.lifecycle.check_dispatch()
        subject = self.state.subject
        observed = 0 if subject is None else subject.service.generation
        if len(self.state.input_tasks) >= self.state.config.execution.max_pending_inputs:
            raise StorageError("输入接管所有者已满。", code="input_capacity")
        task = asyncio.create_task(self.admit_input(store, submission, observed))
        self.state.input_tasks.add(task)
        task.add_done_callback(self.input_done)
        return await asyncio.shield(task)

    async def admit_input(
        self, store: Store, submission: InputSubmission, observed: int
    ) -> Admission:
        """持久化输入记录，并在非重复输入时触发语音打断与主脑协调器通知。"""
        admission = await store.admit(submission)
        subject = self.state.subject
        if not admission.duplicate:
            sender = submission.source.display_name or submission.source.sender_id or ""
            source_label = (
                f"{submission.source.adapter_id}:{sender}"
                if sender
                else submission.source.adapter_id
            )
            _logger.info(
                "[刺激入站] %s -> %s",
                source_label,
                _preview_submission(submission),
                extra={"input_id": admission.input_id, "source": submission.source.adapter_id},
            )
        if subject is not None and not admission.duplicate:
            if submission.context.speech_started and self.state.audio_output is not None:
                identity = "vad-audio-" + uuid4().hex
                context = CallContext(
                    identity,
                    "vad-" + uuid4().hex,
                    asyncio.get_running_loop().time() + self.state.config.host.close_seconds,
                    admission.input_id,
                )
                stopped = asyncio.create_task(self.state.audio_output.interrupt(context))
                self.state.vad_tasks.add(stopped)
                stopped.add_done_callback(self.vad_done)
            await subject.service.input_admitted(
                admission.input_id,
                observed_generation=observed,
            )
        return admission

    def input_done(self, task: asyncio.Task[Admission]) -> None:
        """回收已完成任务的引用，忽略正常的容量或重复拒收异常。"""
        self.state.input_tasks.discard(task)
        if task.cancelled() or (error := task.exception()) is None:
            return
        if isinstance(error, StorageError) and error.code in {
            "input_capacity",
            "invalid_input",
            "source_conflict",
            "store_busy",
        }:
            return
        self.state.input_failure = self.state.input_failure or error
        self.state.input_health.fail(error)

    def vad_done(self, task: asyncio.Task[CallOutcome]) -> None:
        """回收语音打断任务，记录非预期异常。"""
        self.state.vad_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self.state.input_failure = self.state.input_failure or error
            self.state.input_health.fail(error)
