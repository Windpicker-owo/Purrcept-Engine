"""独立后台的可靠反馈桥接和串行反思会话，不取得世界行动权限。

两个固定工作器分别交接真实反馈和推进反思。普通材料按最早待办的 60 秒或
八个成功前台轮次触发；明确执行、设备与终态失败优先。每批冻结有限来源各一页，
它们共用八次请求和 600 秒，后续未覆盖页面保持待办。认识修订逐次生效；
只有 finish_reflection 确认覆盖；
失败和延期没有新独立来源时不重新执行相同范围。Core 仍拥有五工具循环。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from purrcept_core.models import (
    ContentBlock,
    SystemInstruction,
)

from purrcept_engine.domain.inputs.models import MediaReference
from purrcept_engine.domain.knowledge.models import (
    SupervisionRecord,
)
from purrcept_engine.domain.knowledge.ports import KnowledgeStore
from purrcept_engine.domain.knowledge.service import KnowledgeService
from purrcept_engine.domain.subject.attention import NativeMedia
from purrcept_engine.domain.subject.background_ports import BackgroundSettings, BackgroundStore
from purrcept_engine.domain.subject.feedback_ports import FeedbackPort
from purrcept_engine.domain.subject.feedback_service import FeedbackService
from purrcept_engine.domain.subject.reflection_service import ReflectionService
from purrcept_engine.domain.subject.reflection_tools import EvidenceDeliveries
from purrcept_engine.domain.subject.session import ContinuousSession, PrepareHistory, RefreshState
from purrcept_engine.domain.subject.settings import SubjectModel
from purrcept_engine.errors import HostStateError


class BackgroundService:
    """Host 创建并关闭的后台所有者，客户端和数据库仍由 app 关闭。

    start 仅在 Host 已允许派发后调用。reflection 配置、固定指令、媒体及
    刷新钩子显式借入；前台代次只在真实监管提交前观察，用于同代次 urgent
    抢占。重启扫描旧意见只投一次前台，不给它新的抢占身份。
    """

    def __init__(
        self,
        store: BackgroundStore,
        feedback: FeedbackPort,
        knowledge: KnowledgeService,
        knowledge_store: KnowledgeStore,
        *,
        reflection: SubjectModel,
        instructions: tuple[SystemInstruction, ...],
        settings: BackgroundSettings,
        media: NativeMedia,
        refresh: RefreshState,
        prepare_history: PrepareHistory | None,
        foreground_generation: Callable[[], int],
        interrupt_foreground: Callable[[int], bool],
        can_start: Callable[[], None],
    ) -> None:
        """保存明确端口；构造不建立任务、不读取材料、不调用模型。"""
        self.store, self.feedback = store, feedback
        self.knowledge, self.knowledge_store = knowledge, knowledge_store
        self.reflection, self.instructions, self.settings = reflection, instructions, settings
        self.media, self.refresh, self.prepare_history = media, refresh, prepare_history
        self.observe_foreground_generation = foreground_generation
        self.interrupt_foreground, self.can_start = interrupt_foreground, can_start
        self.session = ContinuousSession(
            store,
            "reflection",
            can_start=self.check,
            compiler=reflection.compiler,
        )
        self.deliveries = EvidenceDeliveries()
        self.tasks: set[asyncio.Task[None]] = set()
        self.commits: set[asyncio.Task[object]] = set()
        self.failure_error: BaseException | None = None
        self.closing = False
        self.started = False
        self.close_task: asyncio.Task[None] | None = None

        self.feedback_service = FeedbackService(self)
        self.reflection_service = ReflectionService(self)

    @property
    def failure(self) -> BaseException | None:
        """返回停止派发的原始内部/权威故障。"""
        return self.failure_error

    @property
    def pending_resources(self) -> int:
        """报告固定工作器、接管提交和仍借用模型的 Core 会话。"""
        return len(self.tasks) + len(self.commits) + int(self.session.busy)

    def check(self) -> None:
        """在新工作和真实模型边界撤销故障或关闭后的派发资格。"""
        if self.failure_error is not None:
            raise self.failure_error
        if self.closing:
            raise HostStateError("后台已开始关闭。")
        self.can_start()

    async def start(self) -> None:
        """建立两个固定工作器；重复启动不能形成平行反思。"""
        if self.started:
            raise HostStateError("后台已启动。")
        self.check()
        self.started = True
        for worker in (self.feedback_service, self.reflection_service):
            task = worker.start()
            self.tasks.add(task)
            task.add_done_callback(self.worker_done)

    def worker_done(self, task: asyncio.Task[None]) -> None:
        """锁存原型并停止其他派发，不能把程序故障表述为普通延期。"""
        self.tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self.failure_error = self.failure_error or error
            self.session.interrupt(self.session.generation)
            for other in self.tasks:
                other.cancel()

    async def commit[Result](self, operation: Awaitable[Result]) -> Result:
        """持有已接管的副作用任务；等待取消不让事务成为无主后台工作。"""
        task = asyncio.ensure_future(operation)
        self.commits.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self.commits.discard(task)

    async def wait(self, revision: int, seconds: float | None = None) -> None:
        """有限定时器服务节奏和全部计划，查询期间的通知仍立即可见。"""
        if seconds is None:
            await self.store.wait_for_change(revision)
        elif seconds > 0:
            try:
                async with asyncio.timeout(seconds):
                    await self.store.wait_for_change(revision)
            except TimeoutError:
                pass

    async def media_blocks(self, media: tuple[MediaReference, ...]) -> tuple[ContentBlock, ...]:
        """借用原生媒体端口；字节一致性仍由 ReflectionTools 与 Store 核验。"""
        return await self.media(media)

    def foreground_generation(self) -> int:
        """只在实际监管提交前观察前台代次，不根据意见文本推断。"""
        return self.observe_foreground_generation()

    async def supervision_committed(
        self, record: SupervisionRecord, *, observed_generation: int
    ) -> None:
        """真实新交接才允许同代次 urgent 抢占；迟到意见不追认新代次。"""
        result = await self.commit(
            self.feedback.dispatch_supervision(
                record,
                observed_generation=observed_generation,
            )
        )
        for generation in result.urgent_generations:
            self.interrupt_foreground(generation)

    async def close(self) -> None:
        """共同等待唯一收尾任务，关闭观察取消不能撤销资源归还。"""
        if self.close_task is None:
            self.closing = True
            self.close_task = asyncio.create_task(self.finish_close(), name="background-close")
        await asyncio.shield(self.close_task)

    async def finish_close(self) -> None:
        """先撤销模型/工作资格，再持有提交到实际结束，最后报告原始故障。"""
        workers = tuple(self.tasks)
        for task in workers:
            task.cancel()
        try:
            await self.session.close()
        finally:
            results: list[object] = list(await asyncio.gather(*workers, return_exceptions=True))
            self.tasks.clear()
            commits = tuple(self.commits)
            results.extend(await asyncio.gather(*commits, return_exceptions=True))
            self.commits.clear()
            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    self.failure_error = self.failure_error or result
        if self.failure_error is not None:
            raise self.failure_error
