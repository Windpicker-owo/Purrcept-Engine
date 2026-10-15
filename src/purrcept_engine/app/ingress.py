"""正式接管输入，并交接可信停音与原代次生成抢占信号。

原文入账之后才采用控制信号。任务独立于提交者的观察取消，HostLifecycle
等待这些所有者结束后才关闭依赖；不把 Admission 当作模型已经处理或回应。
"""

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

_logger = logging.getLogger("purrcept_engine.app.host")


class InputAdmissionService:
    """应用创建的InputAdmissionService；职责和借用只属于当前 Engine 实例。"""

    def __init__(self, state: EngineState, lifecycle: HostLifecycle) -> None:
        """固定应用实例借用；资源创建、观察和归还不跨宿主共享。"""
        self.state = state
        self.lifecycle = lifecycle

    async def submit(self, submission: InputSubmission) -> Admission:
        """接管原文与可信信号；取消等待不撤销提交后必要的抢占和停音交接。

        宿主在首次等待前持有完整接管所有者，不只持有SQLite事务。否则来源
        等待取消可能留下已接管VAD，却丢失停音通知。返回后仍须查询真实处理。
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
        """正式提交成功后只交接一次可信信号，停止观察不会让已接管任务失主。"""
        admission = await store.admit(submission)
        subject = self.state.subject
        if subject is not None and not admission.duplicate:
            if submission.context.speech_started and self.state.audio_output is not None:
                identity = "vad-audio-" + uuid4().hex
                context = CallContext(
                    identity,
                    "vad-" + uuid4().hex,
                    asyncio.get_running_loop().time() + self.state.config.host.close_seconds,
                    admission.input_id,
                )
                # 音频控制在独立通道同步撤候播资格。它不排在普通Provider名额
                # 后，也不等声音实际退出才向主体交接原代次抢占。
                stopped = asyncio.create_task(self.state.audio_output.interrupt(context))
                self.state.vad_tasks.add(stopped)
                stopped.add_done_callback(self.vad_done)
            # 抢占绑定接管前观察的旧代次；等待数据库期间新轮次不受迟到信号影响。
            await subject.service.input_admitted(
                admission.input_id,
                observed_generation=observed,
            )
        return admission

    def input_done(self, task: asyncio.Task[Admission]) -> None:
        """收回接管所有者；明确容量拒收不关闭健康宿主，内部错误仍锁存。"""
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

    def vad_done(self, task: asyncio.Task[CallOutcome]) -> None:
        """停音观察结束不重发；AudioLedger仍保存受理、未知及后续真实确认。"""
        self.state.vad_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self.state.input_failure = self.state.input_failure or error
