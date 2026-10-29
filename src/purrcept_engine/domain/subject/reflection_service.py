"""ReflectionService 的固定工作流程；共享协调事实只借用同一领域运行时。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import httpx
from purrcept_core.models import (
    Message,
    ModelError,
    ModelRequest,
    ModelSettings,
    ToolLoopLimitError,
)

from purrcept_engine.domain.context.capacity import CapacityError
from purrcept_engine.domain.context.delivery import ToolDeliveryGate
from purrcept_engine.domain.context.history_preparation import preview_request
from purrcept_engine.domain.context.materials import UnsupportedMaterial
from purrcept_engine.domain.health import ComponentHealth
from purrcept_engine.domain.knowledge.models import (
    EvidencePage,
    KnowledgeConflict,
    KnowledgeInputError,
    OperationalFinding,
    ReflectionBatch,
)
from purrcept_engine.domain.knowledge.semantic import EmbeddingProtocolError
from purrcept_engine.domain.model_requests.service import (
    RequestBudget,
    RequestBudgetExceeded,
    prepare_off_loop,
)
from purrcept_engine.domain.subject.feedback_models import ReflectionSource, ReflectionState
from purrcept_engine.domain.subject.reflection_tools import (
    EvidenceDeliveries,
    ReflectionTools,
    reflection_instructions,
)
from purrcept_engine.errors import HostStateError
from purrcept_engine.values import JsonObject, decode_object

if TYPE_CHECKING:
    from purrcept_engine.domain.subject.background import BackgroundService


@dataclass(frozen=True, slots=True)
class _PreparedBatch:
    """已通过容量检查但尚未取得领取／写入资格的一组完整请求材料。"""

    batch_id: str
    pages: tuple[EvidencePage, ...]
    references: tuple[str, ...]
    findings: tuple[OperationalFinding, ...]
    related: JsonObject | None


class ReflectionService:
    """冻结有限复核批次，在独立且不继承前批历史的会话中完成分析。

    Note:
        BackgroundService 持有该协作者和反思会话；批次内共用原预算与材料
        呈现登记。认识逐次提交，结案依赖结构化回执，不提供世界执行工具。
    """

    def __init__(
        self, runtime: BackgroundService, *, purpose: Literal["memory", "behavior"] = "memory"
    ) -> None:
        """借用同一协调状态，不复制会话、生成资格或失败事实。

        Args:
            runtime: 创建此协作者的领域或执行运行实例，不跨实例共享状态。
            purpose: 决定独立队列、会话、工具和结案责任的用途。
        """
        self.runtime = runtime
        self.purpose: Literal["memory", "behavior"] = purpose
        self.model = runtime.reflection if purpose == "memory" else runtime.behavior
        self.session = runtime.session if purpose == "memory" else runtime.behavior_session
        self.instructions = (
            runtime.instructions if purpose == "memory" else reflection_instructions("behavior")
        )
        self.health = ComponentHealth(purpose)
        self.task: asyncio.Task[None] | None = None
        self.wait_reason = "initializing"
        self.observed_at: float | None = None
        self.last_state: ReflectionState | None = None

    def snapshot(self) -> JsonObject:
        """返回最近调度观察及实时会话状态，不因面板轮询再次扫描来源。"""
        state = self.last_state
        return {
            "purpose": self.purpose,
            "state": "failed" if self.health.error is not None else self.wait_reason,
            "observed_at": self.observed_at,
            "budget_remaining": None if state is None else state.budget_remaining,
            "budget_reset_at": None if state is None else state.budget_reset_at,
            "next_attempt_at": None if state is None else state.next_attempt_at,
            "oldest_queued_at": None if state is None else state.oldest_queued_at,
            "blocked_count": 0 if state is None else state.blocked_count,
            "capacity_blocked_count": 0 if state is None else state.capacity_blocked_count,
            "waiting_evidence_count": 0 if state is None else state.waiting_evidence_count,
        }

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
        self.task = asyncio.create_task(self.reflection(), name=f"background-{self.purpose}")
        return self.task

    async def reflection(self) -> None:
        """冻结一次范围后用同一 Core 会话执行，不以失败或最终文字重置预算。"""
        while not self.runtime.closing:
            self.runtime.check()
            revision = self.runtime.store.change_revision
            state = await self.runtime.feedback.reflection_state(purpose=self.purpose)
            self.last_state, self.observed_at = state, time.time()
            if not state.budget_remaining:
                self.wait_reason = "budget_wait"
                await self.runtime.wait(
                    revision, max(0.01, (state.budget_reset_at or time.time() + 3600) - time.time())
                )
                continue
            if state.oldest_queued_at is None:
                self.wait_reason = (
                    "cooldown"
                    if state.next_attempt_at is not None
                    else "capacity_blocked"
                    if state.capacity_blocked_count
                    else "waiting_evidence"
                    if state.blocked_count
                    else "idle"
                )
                delay = (
                    None
                    if state.next_attempt_at is None
                    else max(0.01, state.next_attempt_at - time.time())
                )
                await self.runtime.wait(revision, delay)
                continue
            batch_seconds = (
                self.runtime.settings.batch_seconds
                if self.purpose == "behavior"
                else self.runtime.settings.memory_batch_seconds
            )
            batch_turns = (
                self.runtime.settings.successful_turns
                if self.purpose == "behavior"
                else self.runtime.settings.memory_batch_turns
            )
            deadline = state.oldest_queued_at + batch_seconds
            due = (
                state.priority
                or state.continuation
                or time.time() >= deadline
                or state.successful_turns - state.last_taken_successful_turns >= batch_turns
            )
            if not due:
                self.wait_reason = "batch_wait"
                await self.runtime.wait(revision, max(0, deadline - time.time()))
                continue
            sources = await self.runtime.feedback.reflection_sources(
                epoch=state.epoch, limit=self.runtime.settings.max_sources, purpose=self.purpose
            )
            if not sources:
                # 同范围失败/延期并未变成已完成。这里只等待新独立来源，不能
                # 因原始期限已经过去而连续创建新的 600 秒主动预算。
                await self.runtime.wait(revision)
                continue
            self.wait_reason = "running"
            await self.reflect(sources, state)

    async def reflect(self, sources: tuple[ReflectionSource, ...], state: ReflectionState) -> None:
        """有限来源共用一个批次预算；未尝试来源和成功续页可继续领取。

        Args:
            sources: 为本次修订或监管提供依据的原文片段身份。
            state: 当前所有者持有的状态或待提交状态值。

        Raises:
            KnowledgeConflict: 后台候选必须对应确切原文版本和真实范围。

        Note:
            准备期限耗尽转为有界持久冷却，不向外冒充任务崩溃。
        """
        started = asyncio.get_running_loop().time()
        try:
            async with asyncio.timeout(self.runtime.settings.turn_seconds):
                prepared = await self._prepare_sources(sources, started)
        except (RequestBudgetExceeded, TimeoutError) as error:
            await self.runtime.commit(
                self.runtime.feedback.postpone_preparation(
                    sources,
                    purpose=self.purpose,
                    reason=type(error).__name__,
                )
            )
            return
        if prepared is None:
            return
        batch_id, references = prepared.batch_id, prepared.references
        findings, related = prepared.findings, prepared.related
        frozen_pages = prepared.pages
        # Ordering: 只接管已经通过容量预检的真实范围，再建立工具写资格。
        if not await self.runtime.commit(
            self.runtime.feedback.reserve_reflection(
                batch_id=batch_id,
                epoch=state.epoch,
                pages=frozen_pages,
                successful_turns=state.successful_turns,
                purpose=self.purpose,
            )
        ):
            return
        batch = await self.runtime.commit(
            self.runtime.knowledge_store.begin_reflection(
                batch_id,
                tuple(references),
                tuple(finding for finding in findings if set(finding.evidence) <= set(references)),
                purpose=self.purpose,
            )
        )
        delivery = ToolDeliveryGate(compiler=self.model.compiler)
        tools = ReflectionTools(
            self.runtime.knowledge_store,
            batch,
            self.runtime,
            knowledge=self.runtime.knowledge,
            page_chars=self.runtime.settings.page_chars,
            deliveries=EvidenceDeliveries(),
            delivery=delivery,
        )

        async def message() -> Message:
            """构造本批实际冻结原文页面的完整反思输入。"""
            return await tools.initial_message(frozen_pages, context=related)

        async def presented(request: ModelRequest) -> None:
            # Ordering: Core 已恢复并完成容量检查的真实请求决定当前历史。
            # 即使发生进程恢复或摘要安装，也只接回其中完整存在的原交付。
            """核对当前请求完整材料与持久交付记录，再提交真实呈现范围。"""
            restored = await self.runtime.knowledge_store.deliveries_for_history(request.messages)
            tools.deliveries.restore_history(restored)
            tools.deliveries.retain_history(request.messages)
            await self.runtime.commit(
                self.runtime.knowledge_store.mark_presented(
                    batch.batch_id,
                    batch.version,
                    tools.presented_refs(request),
                )
            )

        try:
            remaining = self.runtime.settings.turn_seconds - (
                asyncio.get_running_loop().time() - started
            )
            if remaining <= 0:
                raise TimeoutError("反思材料组装达到整批期限。")
            await self.session.run(
                message,
                turn_id=batch_id,
                input_ids=(),
                backend=self.model.backend,
                evidence=self.runtime.store,
                model=self.model.model,
                instructions=self.instructions,
                tools=tools.toolset(),
                output_tokens=self.model.output_tokens,
                observe_usage=self.model.observe_usage,
                max_requests=self.runtime.settings.max_requests,
                turn_seconds=remaining,
                retry=self.model.retry,
                diagnostic_bytes=self.runtime.settings.diagnostic_bytes,
                refresh=self.runtime.refresh,
                capacity=self.model.capacity,
                presented=presented,
                delivery=delivery,
                prepare_history=self.runtime.prepare_history,
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
            await self.runtime.commit(
                self.runtime.knowledge_store.abandon_reflection(
                    batch.batch_id,
                    batch.version,
                    type(error).__name__,
                )
            )
            return
        finally:
            if self.runtime.closing:
                await self.runtime.commit(
                    self.runtime.knowledge_store.abandon_reflection(
                        batch.batch_id,
                        batch.version,
                        "host_closing",
                    )
                )
        current = await self.runtime.knowledge_store.get_reflection(batch.batch_id, batch.version)
        if current.state != "completed":
            await self.runtime.commit(
                self.runtime.knowledge_store.abandon_reflection(
                    batch.batch_id,
                    batch.version,
                    "finish_reflection_not_submitted",
                )
            )

    async def _prepare_sources(
        self,
        sources: tuple[ReflectionSource, ...],
        started: float,
    ) -> _PreparedBatch | None:
        """先装入有限来源再实际编译计量，缩小来源或合法页后仍不合适才阻塞。

        所有准备共享调用者的绝对预算，不先领取来源，也不发出远端请求。
        结构化条目保持原子性，不能通过截断正文伪造一个可容纳的证据范围。
        """
        pages: list[EvidencePage] = []
        references: list[str] = []
        findings: list[OperationalFinding] = []
        selected_sources: list[ReflectionSource] = []
        for source in sources:
            try:
                page = await self.runtime.knowledge_store.read_evidence(
                    source.evidence_id,
                    version=source.version,
                    cursor=source.cursor,
                    max_chars=self.runtime.settings.page_chars,
                )
            except KnowledgeInputError:
                await self.runtime.feedback.block_reflection(
                    source_id=source.evidence_id,
                    version=source.version,
                    reason="source_page_does_not_fit",
                    purpose=self.purpose,
                )
                continue
            if (
                page.slice is None
                or page.snapshot is None
                or page.snapshot.version != source.version
            ):
                raise KnowledgeConflict("后台候选必须对应确切原文版本和真实范围。")
            selected_sources.append(source)
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
        if not pages:
            return
        # 初始选材先通过 Core 的真实首发预览；未冻结的候选不取得写资格。
        batch_id = str(uuid4())
        related: JsonObject | None = None
        while pages:
            candidate = ReflectionBatch(
                batch_id=batch_id,
                version=1,
                purpose=self.purpose,
                findings=tuple(
                    finding for finding in findings if set(finding.evidence) <= set(references)
                ),
                required_slice_refs=tuple(
                    page.slice.slice_ref for page in pages if page.slice is not None
                ),
            )
            preview_tools = ReflectionTools(
                self.runtime.knowledge_store,
                candidate,
                self.runtime,
                knowledge=self.runtime.knowledge,
                page_chars=self.runtime.settings.page_chars,
            )
            try:
                related = (
                    await self.runtime.feedback.behavior_context(
                        tuple(
                            page.snapshot.evidence_id for page in pages if page.snapshot is not None
                        )
                    )
                    if self.purpose == "behavior"
                    else None
                )
                initial = await preview_tools.initial_message(
                    tuple(pages), record=False, context=related
                )
                budget = RequestBudget(
                    max_requests=self.runtime.settings.max_requests,
                    deadline=started + self.runtime.settings.turn_seconds,
                )
                dynamic = await self.runtime.refresh(budget)
                request = preview_request(
                    self.model.backend,
                    self.model.model,
                    None,
                    initial,
                    instructions=self.instructions,
                    tools=preview_tools.toolset(),
                    settings=ModelSettings(max_output_tokens=self.model.output_tokens),
                    dynamic=dynamic,
                    max_requests=self.runtime.settings.max_requests,
                )

                def fits(current: ModelRequest = request) -> None:
                    """纯编译计量不借用数据库或消耗远端派发额度。"""
                    materials, _ = self.model.compiler(current, model=self.model.model)
                    payload = decode_object(materials.encode())["payload"]
                    if not isinstance(payload, dict):
                        raise TypeError("SDK 编译结果缺少 payload。")
                    self.model.capacity(payload)

                await prepare_off_loop(fits)
                budget.check()
                break
            except (CapacityError, UnsupportedMaterial):
                if len(pages) > 1:
                    pages.pop()
                    references.pop()
                    continue
                page = pages[0]
                if page.slice is not None and len(page.text) > 256:
                    smaller = max(256, len(page.text) // 2)
                    source = selected_sources[0]
                    try:
                        smaller_page = await self.runtime.knowledge_store.read_evidence(
                            source.evidence_id,
                            version=source.version,
                            cursor=source.cursor,
                            max_chars=smaller,
                        )
                    except KnowledgeInputError:
                        # 结构化证据不能任意截断；缩页已无合法边界时保留待办。
                        smaller_page = None
                    if smaller_page is not None and smaller_page.slice is not None:
                        pages[0] = smaller_page
                        references[0] = smaller_page.slice.slice_ref
                        continue
                await self.runtime.feedback.block_reflection(
                    source_id=selected_sources[0].evidence_id,
                    version=selected_sources[0].version,
                    reason="capacity_blocked",
                    purpose=self.purpose,
                )
                return
        return _PreparedBatch(
            batch_id,
            tuple(pages),
            tuple(references),
            tuple(finding for finding in findings if set(finding.evidence) <= set(references)),
            related,
        )
