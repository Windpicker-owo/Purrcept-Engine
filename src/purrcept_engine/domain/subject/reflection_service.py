"""ReflectionService 的固定工作流程；共享协调事实只借用同一领域运行时。"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx
from purrcept_core.models import (
    Message,
    ModelError,
    ModelRequest,
    ToolLoopLimitError,
)

from purrcept_engine.domain.context.capacity import CapacityError
from purrcept_engine.domain.context.delivery import ToolDeliveryGate
from purrcept_engine.domain.context.materials import UnsupportedMaterial
from purrcept_engine.domain.knowledge.models import (
    EvidencePage,
    KnowledgeConflict,
    KnowledgeInputError,
    OperationalFinding,
)
from purrcept_engine.domain.knowledge.semantic import EmbeddingProtocolError
from purrcept_engine.domain.subject.feedback_models import ReflectionSource, ReflectionState
from purrcept_engine.domain.subject.reflection_tools import ReflectionTools
from purrcept_engine.errors import HostStateError

if TYPE_CHECKING:
    from purrcept_engine.domain.subject.background import BackgroundService


class ReflectionService:
    """冻结有限复核批次，在独立连续会话中完成有据反思。

    Note:
        BackgroundService 持有该协作者和反思会话；批次内共用原预算与材料
        呈现登记。认识逐次提交，结案依赖结构化回执，不提供世界执行工具。
    """

    def __init__(self, runtime: BackgroundService) -> None:
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
        self.task = asyncio.create_task(self.reflection(), name="background-reflection")
        return self.task

    async def reflection(self) -> None:
        """冻结一次范围后用同一 Core 会话执行，不以失败或最终文字重置预算。"""
        while not self.runtime.closing:
            self.runtime.check()
            revision = self.runtime.store.change_revision
            state = await self.runtime.feedback.reflection_state()
            if state.oldest_queued_at is None:
                await self.runtime.wait(revision)
                continue
            deadline = state.oldest_queued_at + self.runtime.settings.batch_seconds
            due = (
                state.priority
                or time.time() >= deadline
                or state.successful_turns - state.last_taken_successful_turns
                >= self.runtime.settings.successful_turns
            )
            if not due:
                await self.runtime.wait(revision, max(0, deadline - time.time()))
                continue
            sources = await self.runtime.feedback.reflection_sources(
                epoch=state.epoch, limit=self.runtime.settings.max_sources
            )
            if not sources:
                # 同范围失败/延期并未变成已完成。这里只等待新独立来源，不能
                # 因原始期限已经过去而连续创建新的 600 秒主动预算。
                await self.runtime.wait(revision)
                continue
            await self.reflect(sources, state)

    async def reflect(self, sources: tuple[ReflectionSource, ...], state: ReflectionState) -> None:
        """有限来源共用一个批次和预算，未覆盖部分等待新独立事实再纳入。

        Args:
            sources: 为本次修订或监管提供依据的原文片段身份。
            state: 当前所有者持有的状态或待提交状态值。

        Raises:
            KnowledgeConflict: 后台候选必须对应确切原文版本和真实范围。
            TimeoutError: 反思材料组装达到整批期限。
        """
        started = asyncio.get_running_loop().time()
        pages: list[EvidencePage] = []
        references: list[str] = []
        findings: list[OperationalFinding] = []
        for source in sources:
            page = await self.runtime.knowledge_store.read_evidence(
                source.evidence_id,
                version=source.version,
                cursor=source.cursor,
                max_chars=self.runtime.settings.page_chars,
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
        if not await self.runtime.commit(
            self.runtime.feedback.reserve_reflection(
                batch_id=batch_id,
                epoch=state.epoch,
                pages=frozen_pages,
                successful_turns=state.successful_turns,
            )
        ):
            return
        batch = await self.runtime.commit(
            self.runtime.knowledge_store.begin_reflection(
                batch_id,
                tuple(references),
                tuple(findings),
            )
        )
        delivery = ToolDeliveryGate(compiler=self.runtime.reflection.compiler)
        tools = ReflectionTools(
            self.runtime.knowledge_store,
            batch,
            self.runtime,
            knowledge=self.runtime.knowledge,
            page_chars=self.runtime.settings.page_chars,
            deliveries=self.runtime.deliveries,
            delivery=delivery,
        )

        async def message() -> Message:
            """构造本批实际冻结原文页面的完整反思输入。"""
            return await tools.initial_message(frozen_pages)

        async def presented(request: ModelRequest) -> None:
            # Ordering: Core 已恢复并完成容量检查的真实请求决定当前历史。
            # 即使发生进程恢复或摘要安装，也只接回其中完整存在的原交付。
            """核对当前请求完整材料与持久交付记录，再提交真实呈现范围。"""
            restored = await self.runtime.knowledge_store.deliveries_for_history(request.messages)
            self.runtime.deliveries.restore_history(restored)
            self.runtime.deliveries.retain_history(request.messages)
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
            await self.runtime.session.run(
                message,
                turn_id=batch_id,
                input_ids=(),
                backend=self.runtime.reflection.backend,
                evidence=self.runtime.store,
                model=self.runtime.reflection.model,
                instructions=self.runtime.instructions,
                tools=tools.toolset(),
                output_tokens=self.runtime.reflection.output_tokens,
                observe_usage=self.runtime.reflection.observe_usage,
                max_requests=self.runtime.settings.max_requests,
                turn_seconds=remaining,
                retry=self.runtime.reflection.retry,
                diagnostic_bytes=self.runtime.settings.diagnostic_bytes,
                refresh=self.runtime.refresh,
                capacity=self.runtime.reflection.capacity,
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
