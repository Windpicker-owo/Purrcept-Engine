"""用实际 Core 工具循环验证两种分析用途、容量拒绝及故障隔离。

后端只在内存返回结构化工具调用；真实 Store 和 SDK 编译仍参与执行。每个测试
独占临时库，不需要外部模型、运行配置或设备。共享底层后端不等于共享会话历史。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from purrcept_core.models import (
    ContentBlock,
    Message,
    MessageRole,
    ModelEventSink,
    ModelRequest,
    ModelResponse,
    SystemInstruction,
    ToolCallBlock,
)

from purrcept_engine.app.config import RetrySettings
from purrcept_engine.domain.context.capacity import (
    CapacityAssessment,
    GenerationCapacity,
    check_capacity,
)
from purrcept_engine.domain.inputs.models import MediaReference
from purrcept_engine.domain.knowledge.models import (
    EvidenceSnapshot,
    FindingAssessment,
    FinishSubmission,
    Review,
)
from purrcept_engine.domain.knowledge.service import KnowledgeService
from purrcept_engine.domain.model_requests.service import RequestBudget, RequestBudgetExceeded
from purrcept_engine.domain.subject.background import BackgroundService
from purrcept_engine.domain.subject.background_ports import BackgroundSettings
from purrcept_engine.domain.subject.models import SessionRecord, SessionScope
from purrcept_engine.domain.subject.session import DynamicState
from purrcept_engine.domain.subject.settings import SubjectModel
from purrcept_engine.infrastructure.models.compiler import compile_materials
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.values import JsonObject, decode_object


class Backend:
    """依实际批次用途结案；可注入只影响记忆任务的内部故障。"""

    def __init__(self, store: Store, *, crash_memory: bool = False) -> None:
        self.store = store
        self.requests: list[ModelRequest] = []
        self.crash_memory = crash_memory

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        self.requests.append(request)
        initial = next(
            message for message in request.messages if "reflection_batch" in message.metadata
        )
        identity = initial.metadata["reflection_batch"]
        assert isinstance(identity, str)
        batch = await self.store.knowledge.get_reflection(identity, 1)
        if self.crash_memory and batch.purpose == "memory":
            raise LookupError("synthetic-memory-fault")
        if request.messages[-1].role is MessageRole.TOOL:
            return ModelResponse(Message.assistant("本批评审收尾"))
        refs = initial.metadata["source_slice_refs"]
        assert isinstance(refs, tuple)
        references = tuple(value for value in refs if isinstance(value, str))
        if batch.purpose == "behavior":
            name = "submit_behavior_review"
            arguments: JsonObject = {
                "decision": "no_intervention",
                "conclusion": "普通互动没有证据需要纠偏。",
                "evidence": list(references),
            }
        else:
            name = "finish_reflection"
            submission = FinishSubmission(
                reviews=(
                    Review(
                        issue="检查",
                        evidence=references,
                        conclusion="没有新增长期事实。",
                        memory_decision="no_change",
                        reason="无需重复写入。",
                    ),
                ),
                deferred=(),
                findings=tuple(
                    FindingAssessment(id=finding.id, status="uncertain", reason="不做无证据推断。")
                    for finding in batch.findings
                ),
            )
            arguments = decode_object(submission.model_dump_json(exclude_none=True).encode())
        return ModelResponse(
            Message(MessageRole.ASSISTANT, (ToolCallBlock("finish", name, arguments=arguments),))
        )


def runtime(
    store: Store, backend: Backend, *, window: int = 100_000, behavior: bool = True
) -> BackgroundService:
    """构造与生产相同的会话、容量检查和工具服务，缩短本地测试触发间隔。"""

    def capacity(payload: JsonObject) -> CapacityAssessment:
        return check_capacity(payload, GenerationCapacity(window, 1000))

    async def refresh(budget: RequestBudget) -> DynamicState:
        budget.check()
        return DynamicState("合成宿主已提交状态。")

    async def media(references: tuple[MediaReference, ...]) -> tuple[ContentBlock, ...]:
        assert not references
        return ()

    return BackgroundService(
        store,
        store.feedback,
        KnowledgeService(store.knowledge),
        store.knowledge,
        reflection=SubjectModel(
            backend, "openai/synthetic", 1000, capacity, RetrySettings(), compiler=compile_materials
        ),
        instructions=(SystemInstruction.from_text("只整理本批记忆。"),),
        settings=BackgroundSettings(
            4096,
            batch_seconds=0.01,
            memory_batch_seconds=0.01,
            max_sources=1,
            behavior_enabled=behavior,
        ),
        media=media,
        refresh=refresh,
        prepare_history=None,
        foreground_generation=lambda: 0,
        interrupt_foreground=lambda _: False,
        can_start=lambda: None,
    )


async def wait_finished(background: BackgroundService, store: Store) -> None:
    """等待两个实际工作队列完成，并立即报告任何非预期分析故障。"""
    async with asyncio.timeout(5):
        while True:
            for service in (background.reflection_service, background.behavior_service):
                if service.health.error is not None:
                    raise service.health.error
            memory = await store.feedback.reflection_state(purpose="memory")
            behavior = await store.feedback.reflection_state(purpose="behavior")
            if (
                memory.oldest_queued_at is None
                and behavior.oldest_queued_at is None
                and not background.session.busy
                and not background.behavior_session.busy
            ):
                return
            await asyncio.sleep(0.005)


async def test_both_jobs_finish_independently_and_never_restore_previous_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await Store.open(tmp_path / "engine.sqlite3")
    backend = Backend(store)
    background = runtime(store, backend)

    async def forbidden_history(scope: SessionScope) -> SessionRecord:
        raise AssertionError(f"批次不应恢复 {scope} 的已有检查点。")

    monkeypatch.setattr(store, "get_session", forbidden_history)
    try:
        for number in range(2):
            await store.knowledge.put_evidence(
                EvidenceSnapshot(
                    evidence_id=f"source:{number}",
                    version=1,
                    kind="test",
                    source_json="{}",
                    text="普通来源。",
                    occurred_at=time.time(),
                )
            )
        await background.start()
        await wait_finished(background, store)
        first_requests = [
            request for request in backend.requests if request.messages[-1].role is MessageRole.USER
        ]
        assert len(first_requests) == 4
        assert all(
            not any(message.text == "本批评审收尾" for message in request.messages)
            for request in first_requests
        )
        tools = {tuple(spec.name for spec in request.tools) for request in first_requests}
        assert tools == {
            ("recall", "read_evidence", "revise_memory", "finish_reflection"),
            ("recall", "read_evidence", "supervise", "submit_behavior_review"),
        }
    finally:
        await background.close()
        await store.close()


async def test_memory_failure_does_not_stop_behavior_review_or_feedback(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "engine.sqlite3")
    backend = Backend(store, crash_memory=True)
    background = runtime(store, backend)
    try:
        await store.knowledge.put_evidence(
            EvidenceSnapshot(
                evidence_id="fact",
                version=1,
                kind="test",
                source_json="{}",
                text="普通来源。",
                occurred_at=time.time(),
            )
        )
        await background.start()
        async with asyncio.timeout(5):
            while True:
                state = await store.feedback.reflection_state(purpose="behavior")
                if (
                    background.reflection_service.health.error is not None
                    and state.oldest_queued_at is None
                ):
                    break
                await asyncio.sleep(0.005)
        assert background.failure is None and store.authority_available
        assert (
            background.feedback_service.task is not None
            and not background.feedback_service.task.done()
        )
    finally:
        await background.close()
        await store.close()


async def test_capacity_block_never_dispatches_and_reopens_only_for_new_policy(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "engine.sqlite3")
    backend = Backend(store)
    background = runtime(store, backend, window=1100, behavior=False)
    try:
        await store.feedback.configure_capacity("small")
        await store.knowledge.put_evidence(
            EvidenceSnapshot(
                evidence_id="large",
                version=1,
                kind="test",
                source_json="{}",
                text="正文" * 5000,
                occurred_at=time.time(),
            )
        )
        await background.start()
        async with asyncio.timeout(5):
            while True:
                if (await store.feedback.reflection_state()).blocked_count:
                    break
                await asyncio.sleep(0.005)
        assert backend.requests == []
        await store.feedback.configure_capacity("small")
        assert (await store.feedback.reflection_state()).blocked_count == 1
        await background.close()
        await store.feedback.configure_capacity("larger")
        assert (await store.feedback.reflection_state()).blocked_count == 0
    finally:
        await background.close()
        await store.close()


async def test_preparation_budget_exhaustion_cools_down_without_transport_or_component_failure(
    tmp_path: Path,
) -> None:
    """准备期耗尽不能杀死分析工作器，也不能立刻领取新的整批期限空转。"""
    store = await Store.open(tmp_path / "preparation.sqlite3")
    backend = Backend(store)
    background = runtime(store, backend, behavior=False)

    async def exhausted(budget: RequestBudget) -> DynamicState:
        del budget
        raise RequestBudgetExceeded("合成准备期限耗尽。", provider="engine")

    background.refresh = exhausted
    try:
        await store.knowledge.put_evidence(
            EvidenceSnapshot(
                evidence_id="fact",
                version=1,
                kind="test",
                text="待复核材料。",
                source_json="{}",
                occurred_at=time.time(),
            )
        )
        await background.start()
        async with asyncio.timeout(5):
            while True:
                state = await store.feedback.reflection_state()
                if state.next_attempt_at is not None:
                    break
                await asyncio.sleep(0.005)
        assert state.budget_remaining == 32 and state.oldest_queued_at is None
        assert backend.requests == []
        assert background.reflection_service.health.error is None
    finally:
        await background.close()
        await store.close()
