"""FeedbackService 的固定工作流程；共享协调事实只借用同一领域运行时。"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from purrcept_engine.errors import HostStateError

if TYPE_CHECKING:
    from purrcept_engine.domain.subject.background import BackgroundService


class FeedbackService:
    """领域运行时创建的工作所有者，启动一次；取消和汇总关闭由运行时统一协调。"""

    def __init__(self, runtime: BackgroundService) -> None:
        """借用同一协调状态，不复制会话、生成资格或失败事实。"""
        self.runtime = runtime
        self.task: asyncio.Task[None] | None = None

    def start(self) -> asyncio.Task[None]:
        """创建唯一固定任务并交给运行时汇总关闭；重复启动明确拒绝。"""
        if self.task is not None:
            raise HostStateError("领域工作器已经启动。")
        self.task = asyncio.create_task(self.bridge(), name="background-feedback")
        return self.task

    async def bridge(self) -> None:
        """独立于反思模型等待交接回执、活动和到期计划。"""
        while not self.runtime.closing:
            self.runtime.check()
            revision = self.runtime.store.change_revision
            result = await self.runtime.commit(self.runtime.feedback.bridge(now=time.time()))
            for generation in result.urgent_generations:
                self.runtime.interrupt_foreground(generation)
            if result.changed and not result.blocked:
                continue
            if result.blocked:
                # 额度满时自己的 outbox 写通知不是新的重试原因。后续前台结案
                # 或新的外部提交才唤醒；可靠身份仍保留在同一权威库中。
                revision = self.runtime.store.change_revision
            state = await self.runtime.feedback.reflection_state()
            seconds = (
                None if state.next_plan_at is None else max(0, state.next_plan_at - time.time())
            )
            await self.runtime.wait(revision, seconds)
