"""独立后台的可靠反馈桥接和串行反思会话，不取得世界行动权限。

两个固定工作器分别交接真实反馈和推进反思。普通材料按最早待办的 60 秒或
八个成功前台轮次触发；明确执行、设备与终态失败优先。每批冻结有限来源各一页，
它们共用八次请求和 600 秒，后续未覆盖页面保持待办。认识修订逐次生效；
只有 finish_reflection 确认覆盖；
失败和延期没有新独立来源时不重新执行相同范围。Core 仍拥有五工具循环。
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

import httpx
from purrcept_core.models import (
    ContentBlock,
    Message,
    ModelError,
    ModelRequest,
    SystemInstruction,
    ToolLoopLimitError,
)

from purrcept_engine.context.capacity import CapacityError
from purrcept_engine.context.materials import UnsupportedMaterial
from purrcept_engine.errors import HostStateError
from purrcept_engine.storage.feedback import ReflectionSource, ReflectionState
from purrcept_engine.storage.feedback_port import FeedbackLedger
from purrcept_engine.storage.knowledge import (
    EvidencePage,
    KnowledgeConflict,
    KnowledgeInputError,
    OperationalFinding,
    SupervisionRecord,
)
from purrcept_engine.storage.models import MediaReference

from .attention import NativeMedia
from .coordinator import SubjectModel
from .delivery import ToolDeliveryGate
from .knowledge import KnowledgeService, KnowledgeStore
from .model_requests import RequestEvidenceSink
from .reflection import EvidenceDeliveries, ReflectionTools
from .semantic import EmbeddingProtocolError
from .session import ContinuousSession, PrepareHistory, RefreshState, SessionStore


class BackgroundStore(SessionStore, RequestEvidenceSink, Protocol):
    """app 借出的单连接会话与请求事实端口，不公开 SQL 或客户端。

    变化序号只服务不漏通知的等待，不代替查询或完成事实。写入等待取消不
    撤销已接管事务，后台持有提交任务直到真实归还。
    """

    @property
    def change_revision(self) -> int:
        """返回已提交事务通知的单调序号。"""
        ...

    async def wait_for_change(self, after_revision: int) -> int:
        """等待新事实或明确关闭；调用前先读取序号，再查询账本。"""
        ...


@dataclass(frozen=True, slots=True)
class BackgroundSettings:
    """后台节奏和单批额度，由装配显式指定，不从前台模型推测能力。"""

    diagnostic_bytes: int
    page_chars: int = 8000
    batch_seconds: float = 60.0
    successful_turns: int = 8
    max_sources: int = 32
    max_requests: int = 8
    turn_seconds: float = 600.0

    def __post_init__(self) -> None:
        """在公开构造边界拒绝布尔值、无穷时间和无界扫描。"""
        for value in (self.page_chars, self.successful_turns, self.max_sources, self.max_requests):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("后台页长、批量和请求次数必须是正整数。")
        if self.max_sources > 32:
            raise ValueError("每批反思来源数不能超过有限扫描的 32 条边界。")
        if (
            isinstance(self.diagnostic_bytes, bool)
            or not isinstance(self.diagnostic_bytes, int)
            or self.diagnostic_bytes < 0
        ):
            raise ValueError("诊断额度必须是非负整数。")
        for value in (self.batch_seconds, self.turn_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("后台时间必须是有限正数。")


class BackgroundService:
    """Host 创建并关闭的后台所有者，客户端和数据库仍由 app 关闭。

    start 仅在 Host 已允许派发后调用。reflection 配置、固定指令、媒体及
    刷新钩子显式借入；前台代次只在真实监管提交前观察，用于同代次 urgent
    抢占。重启扫描旧意见只投一次前台，不给它新的抢占身份。
    """

    def __init__(
        self,
        store: BackgroundStore,
        feedback: FeedbackLedger,
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
        self._foreground_generation = foreground_generation
        self.interrupt_foreground, self.can_start = interrupt_foreground, can_start
        self.session = ContinuousSession(store, "reflection", can_start=self._check)
        self.deliveries = EvidenceDeliveries()
        self._tasks: set[asyncio.Task[None]] = set()
        self._commits: set[asyncio.Task[object]] = set()
        self._failure: BaseException | None = None
        self._closing = False
        self._started = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def failure(self) -> BaseException | None:
        """返回停止派发的原始内部/权威故障。"""
        return self._failure

    @property
    def pending_resources(self) -> int:
        """报告固定工作器、接管提交和仍借用模型的 Core 会话。"""
        return len(self._tasks) + len(self._commits) + int(self.session.busy)

    def _check(self) -> None:
        """在新工作和真实模型边界撤销故障或关闭后的派发资格。"""
        if self._failure is not None:
            raise self._failure
        if self._closing:
            raise HostStateError("后台已开始关闭。")
        self.can_start()

    async def start(self) -> None:
        """建立两个固定工作器；重复启动不能形成平行反思。"""
        if self._started:
            raise HostStateError("后台已启动。")
        self._check()
        self._started = True
        for name, operation in (
            ("feedback", self._bridge_loop()),
            ("reflection", self._reflection_loop()),
        ):
            task = asyncio.create_task(operation, name=f"background-{name}")
            self._tasks.add(task)
            task.add_done_callback(self._worker_done)

    def _worker_done(self, task: asyncio.Task[None]) -> None:
        """锁存原型并停止其他派发，不能把程序故障表述为普通延期。"""
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self._failure = self._failure or error
            self.session.interrupt(self.session.generation)
            for other in self._tasks:
                other.cancel()

    async def _commit[Result](self, operation: Awaitable[Result]) -> Result:
        """持有已接管的副作用任务；等待取消不让事务成为无主后台工作。"""
        task = asyncio.ensure_future(operation)
        self._commits.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._commits.discard(task)

    async def _wait(self, revision: int, seconds: float | None = None) -> None:
        """有限定时器服务节奏和全部计划，查询期间的通知仍立即可见。"""
        if seconds is None:
            await self.store.wait_for_change(revision)
        elif seconds > 0:
            try:
                async with asyncio.timeout(seconds):
                    await self.store.wait_for_change(revision)
            except TimeoutError:
                pass

    async def _bridge_loop(self) -> None:
        """独立于反思模型等待交接回执、活动和到期计划。"""
        while not self._closing:
            self._check()
            revision = self.store.change_revision
            result = await self._commit(self.feedback.bridge(now=time.time()))
            for generation in result.urgent_generations:
                self.interrupt_foreground(generation)
            if result.changed and not result.blocked:
                continue
            if result.blocked:
                # 额度满时自己的 outbox 写通知不是新的重试原因。后续前台结案
                # 或新的外部提交才唤醒；可靠身份仍保留在同一权威库中。
                revision = self.store.change_revision
            state = await self.feedback.reflection_state()
            seconds = (
                None if state.next_plan_at is None else max(0, state.next_plan_at - time.time())
            )
            await self._wait(revision, seconds)

    async def _reflection_loop(self) -> None:
        """冻结一次范围后用同一 Core 会话执行，不以失败或最终文字重置预算。"""
        while not self._closing:
            self._check()
            revision = self.store.change_revision
            state = await self.feedback.reflection_state()
            if state.oldest_queued_at is None:
                await self._wait(revision)
                continue
            deadline = state.oldest_queued_at + self.settings.batch_seconds
            due = (
                state.priority
                or time.time() >= deadline
                or state.successful_turns - state.last_taken_successful_turns
                >= self.settings.successful_turns
            )
            if not due:
                await self._wait(revision, max(0, deadline - time.time()))
                continue
            sources = await self.feedback.reflection_sources(
                epoch=state.epoch, limit=self.settings.max_sources
            )
            if not sources:
                # 同范围失败/延期并未变成已完成。这里只等待新独立来源，不能
                # 因原始期限已经过去而连续创建新的 600 秒主动预算。
                await self._wait(revision)
                continue
            await self._reflect(sources, state)

    async def _reflect(self, sources: tuple[ReflectionSource, ...], state: ReflectionState) -> None:
        """有限来源共用一个批次和预算，未覆盖部分等待新独立事实再纳入。"""
        started = asyncio.get_running_loop().time()
        pages: list[EvidencePage] = []
        references: list[str] = []
        findings: list[OperationalFinding] = []
        for source in sources:
            page = await self.knowledge_store.read_evidence(
                source.evidence_id,
                version=source.version,
                cursor=source.cursor,
                max_chars=self.settings.page_chars,
            )
            if (
                page.slice is None
                or page.snapshot is None
                or page.snapshot.version != source.version
            ):
                raise KnowledgeConflict("后台候选必须对应确切原文版本和真实范围。")
            pages.append(page)
            references.append(page.slice.slice_ref)
            if source.priority:
                findings.append(
                    OperationalFinding(
                        id=f"actual:{source.evidence_id}",
                        fact=f"宿主实际记录 {page.snapshot.kind} 需要复核。",
                        evidence=(page.slice.slice_ref,),
                    )
                )
        frozen_pages = tuple(pages)
        batch_id = str(uuid4())
        # Ordering: 独立事实 epoch 的唯一预算先持久接管，再建立写资格和模型会话。
        # 一个批次失败或没有读完所有分页，都不能退回该接管身份自动续批。
        if not await self._commit(
            self.feedback.reserve_reflection(
                batch_id=batch_id,
                epoch=state.epoch,
                pages=frozen_pages,
                successful_turns=state.successful_turns,
            )
        ):
            return
        batch = await self._commit(
            self.knowledge_store.begin_reflection(
                batch_id,
                tuple(references),
                tuple(findings),
            )
        )
        delivery = ToolDeliveryGate()
        tools = ReflectionTools(
            self.knowledge_store,
            batch,
            self,
            knowledge=self.knowledge,
            page_chars=self.settings.page_chars,
            deliveries=self.deliveries,
            delivery=delivery,
        )

        async def message() -> Message:
            return await tools.initial_message(frozen_pages)

        async def presented(request: ModelRequest) -> None:
            # Ordering: Core 已恢复并完成容量检查的真实请求决定当前历史。
            # 即使发生进程恢复或摘要安装，也只接回其中完整存在的原交付。
            restored = await self.knowledge_store.deliveries_for_history(request.messages)
            self.deliveries.restore_history(restored)
            self.deliveries.retain_history(request.messages)
            await self._commit(
                self.knowledge_store.mark_presented(
                    batch.batch_id,
                    batch.version,
                    tools.presented_refs(request),
                )
            )

        try:
            remaining = self.settings.turn_seconds - (asyncio.get_running_loop().time() - started)
            if remaining <= 0:
                raise TimeoutError("反思材料组装达到整批期限。")
            await self.session.run(
                message,
                turn_id=batch_id,
                input_ids=(),
                backend=self.reflection.backend,
                evidence=self.store,
                model=self.reflection.model,
                instructions=self.instructions,
                tools=tools.toolset(),
                output_tokens=self.reflection.output_tokens,
                max_requests=self.settings.max_requests,
                turn_seconds=remaining,
                retry=self.reflection.retry,
                diagnostic_bytes=self.settings.diagnostic_bytes,
                refresh=self.refresh,
                capacity=self.reflection.capacity,
                presented=presented,
                delivery=delivery,
                prepare_history=self.prepare_history,
            )
        except (
            ModelError,
            CapacityError,
            KnowledgeInputError,
            UnsupportedMaterial,
            ToolLoopLimitError,
            TimeoutError,
            httpx.HTTPError,
            EmbeddingProtocolError,
        ) as error:
            await self._commit(
                self.knowledge_store.abandon_reflection(
                    batch.batch_id,
                    batch.version,
                    type(error).__name__,
                )
            )
            return
        finally:
            if self._closing:
                await self._commit(
                    self.knowledge_store.abandon_reflection(
                        batch.batch_id,
                        batch.version,
                        "host_closing",
                    )
                )
        current = await self.knowledge_store.get_reflection(batch.batch_id, batch.version)
        if current.state != "completed":
            await self._commit(
                self.knowledge_store.abandon_reflection(
                    batch.batch_id,
                    batch.version,
                    "finish_reflection_not_submitted",
                )
            )

    async def media_blocks(self, media: tuple[MediaReference, ...]) -> tuple[ContentBlock, ...]:
        """借用原生媒体端口；字节一致性仍由 ReflectionTools 与 Store 核验。"""
        return await self.media(media)

    def foreground_generation(self) -> int:
        """只在实际监管提交前观察前台代次，不根据意见文本推断。"""
        return self._foreground_generation()

    async def supervision_committed(
        self, record: SupervisionRecord, *, observed_generation: int
    ) -> None:
        """真实新交接才允许同代次 urgent 抢占；迟到意见不追认新代次。"""
        result = await self._commit(
            self.feedback.dispatch_supervision(
                record,
                observed_generation=observed_generation,
            )
        )
        for generation in result.urgent_generations:
            self.interrupt_foreground(generation)

    async def close(self) -> None:
        """共同等待唯一收尾任务，关闭观察取消不能撤销资源归还。"""
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._close(), name="background-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        """先撤销模型/工作资格，再持有提交到实际结束，最后报告原始故障。"""
        workers = tuple(self._tasks)
        for task in workers:
            task.cancel()
        try:
            await self.session.close()
        finally:
            results: list[object] = list(await asyncio.gather(*workers, return_exceptions=True))
            self._tasks.clear()
            commits = tuple(self._commits)
            results.extend(await asyncio.gather(*commits, return_exceptions=True))
            self._commits.clear()
            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    self._failure = self._failure or result
        if self._failure is not None:
            raise self._failure
