"""DeferredInputService 的固定工作流程；共享协调事实只借用同一领域运行时。"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from purrcept_engine.errors import HostStateError

if TYPE_CHECKING:
    from purrcept_engine.domain.subject.service import SubjectService


class DeferredInputService:
    """等待最早延期到期，将原输入身份交回前台一次。

    Note:
        SubjectService 持有唯一固定等待任务；延期沿用权威待办，不另建输入状态
        或为每条输入创建定时任务，也不在到期时重新发起注意模型判断。
    """

    def __init__(self, runtime: SubjectService) -> None:
        """借用同一协调状态，不复制会话、生成资格或失败事实。

        Args:
            runtime: 创建此协作者的领域或执行运行实例，不跨实例共享状态。
        """
        self.runtime = runtime
        self.task: asyncio.Task[None] | None = None

    def start(self) -> asyncio.Task[None]:
        """创建唯一固定任务并交给运行时汇总关闭。

        Returns:
            当前所有者持有的工作观察入口，等待取消不等于实际资源已归还。

        Raises:
            HostStateError: 领域工作器已经启动。

        Note:
            重复启动明确拒绝。
        """
        if self.task is not None:
            raise HostStateError("领域工作器已经启动。")
        self.task = asyncio.create_task(self.deferred(), name="subject-deferred")
        return self.task

    async def deferred(self) -> None:
        """一个固定交接者等待最早持久期限，不因模型忙碌漏掉到期事实。"""
        while True:
            self.runtime.check()
            revision = self.runtime.store.change_revision
            due = await self.runtime.store.next_deferred_input()
            if due is None:
                await self.runtime.wait(revision)
                continue
            remaining = due - time.time()
            if remaining > 0:
                await self.runtime.wait(revision, seconds=remaining)
            else:
                await self.runtime.commit(self.runtime.store.deliver_due_inputs(time.time()))
