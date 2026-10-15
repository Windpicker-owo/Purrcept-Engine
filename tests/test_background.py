"""真实 Store/Core 核验可靠后台交接、五工具完成和无新事实不自循环。

模型脚本只替代远端响应，原文、媒体、呈现资格、认识修订和会话提交均走真实
唯一数据库线程。每个测试持有关闭责任，不访问真实服务或已有用户数据。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Literal

import pytest
from purrcept_core.models import (
    ContentBlock,
    ImageBlock,
    ImageBytes,
    Message,
    MessageRole,
    ModelEventSink,
    ModelRequest,
    ModelRequestError,
    ModelResponse,
    SystemInstruction,
    TextBlock,
    ToolCallBlock,
    ToolSet,
)

import purrcept_engine.domain.subject.feedback_models as subject_feedback_models
from purrcept_engine.app.config import RetrySettings
from purrcept_engine.context.capacity import CapacityAssessment, GenerationCapacity, check_capacity
from purrcept_engine.domain.activities.models import ActivityFeedback, ActivityRecord
from purrcept_engine.domain.inputs.models import (
    InputContext,
    InputSource,
    InputSubmission,
    MediaAsset,
    MediaReference,
)
from purrcept_engine.domain.knowledge.models import (
    Deferred,
    EvidenceSnapshot,
    FindingAssessment,
    FinishSubmission,
    Review,
    SupervisionNotice,
    WakeupPlan,
)
from purrcept_engine.domain.subject.models import TurnOutcome
from purrcept_engine.errors import HostStateError, StorageError
from purrcept_engine.storage import feedback as feedback_sql
from purrcept_engine.storage.store import Store
from purrcept_engine.storage.time import AdmitInput
from purrcept_engine.subject import background as background_module
from purrcept_engine.subject.background import BackgroundService, BackgroundSettings
from purrcept_engine.subject.coordinator import SubjectModel
from purrcept_engine.subject.knowledge import KnowledgeService
from purrcept_engine.subject.model_requests import RequestBudget
from purrcept_engine.subject.session import ContinuousSession, DynamicState
from purrcept_engine.values import JsonObject, decode_object


@pytest.fixture(autouse=True)
def prepared_request_compiler(forbid_external_network: None) -> None:
    """在反思计时前准备冻结 SDK，导入期 HTTP 仍由共同替身拦截。

    场景的三秒等待检查已启动反思批次的提交与停机，不检查 SDK 模块冷导入
    性能。编译、真实请求证据和反思工具仍在每个场景的实际路径执行。
    """
    import_module("purrcept_litellm._request")


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    database = await Store.open(tmp_path / "state.sqlite3")
    try:
        yield database
    finally:
        await database.close()


async def _source(store: Store, identity: str, text: str = "真实来源") -> None:
    await store.knowledge.put_evidence(
        EvidenceSnapshot(
            evidence_id=identity,
            version=1,
            kind="external",
            text=text,
            source_json='{"adapter":"local"}',
            occurred_at=time.time(),
        )
    )


async def _admit(store: Store, identity: str, *, media: tuple[MediaAsset, ...] = ()) -> str:
    now = datetime.now(UTC)
    result = await store.admit(
        InputSubmission(
            InputSource("test", "account", "conversation", "text", identity),
            "实际输入",
            now,
            now,
            media=media,
            context=InputContext(minimum_wake=True),
        )
    )
    return result.input_id


async def _call(store: Store, identity: str = "call", *, input_id: str | None = None) -> None:
    await store.begin_action(
        f"action:{identity}", catalog_version="catalog", source="return None", input_id=input_id
    )
    await store.begin_call(
        identity,
        action_id=f"action:{identity}",
        sequence=0,
        path="me.console.say",
        target_id="console",
        arguments_json='{"text":"x"}',
    )
    await store.mark_call_started(identity)


async def _result(
    store: Store,
    identity: str,
    state: Literal["unknown", "confirmed", "failed"],
    *,
    media: tuple[MediaReference, ...] = (),
) -> None:
    await store.record_call_result(
        identity,
        state=state,
        result_json='{"receipt":"actual"}' if state == "confirmed" else None,
        error_type="Uncertain" if state == "unknown" else None,
        error="尚待核对" if state == "unknown" else None,
        media=media,
    )


def _capacity(payload: JsonObject) -> CapacityAssessment:
    return check_capacity(payload, GenerationCapacity(200_000, 1000, "reference-text-only-v1"))


async def _refresh(budget: RequestBudget) -> DynamicState:
    return DynamicState(f"宿主已提交状态，请求已使用 {budget.used}。")


async def _wait_for(predicate: Callable[[], Awaitable[bool]]) -> None:
    async with asyncio.timeout(3):
        while not await predicate():
            try:
                async with asyncio.timeout(0.005):
                    await asyncio.Event().wait()
            except TimeoutError:
                pass


class Backend:
    """按实际请求里的可信批次元数据构造工具结果，保留真实 Core 解析与执行。"""

    def __init__(
        self,
        store: Store,
        mode: Literal[
            "finish",
            "final",
            "defer",
            "write_fail",
            "finish_fail",
            "block",
            "internal",
        ] = "finish",
    ) -> None:
        self.store, self.mode = store, mode
        self.requests: list[ModelRequest] = []
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        self.requests.append(request)
        initial = next(
            message
            for message in reversed(request.messages)
            if "reflection_batch" in message.metadata
        )
        identity = initial.metadata["reflection_batch"]
        assert isinstance(identity, str)
        references = initial.metadata["source_slice_refs"]
        assert isinstance(references, tuple) and all(isinstance(item, str) for item in references)
        source_refs = tuple(item for item in references if isinstance(item, str))
        batch = await self.store.knowledge.get_reflection(identity, 1)
        self.entered.set()
        if self.mode == "block":
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()
        if self.mode == "internal":
            raise LookupError("程序原型")
        if self.mode == "final":
            return ModelResponse(Message.assistant("完成了"))
        if request.messages[-1].role is MessageRole.TOOL:
            if self.mode in ("write_fail", "finish_fail"):
                raise ModelRequestError("合成明确响应错误", provider="test")
            return ModelResponse(Message.assistant("内部收尾"))
        if self.mode == "write_fail":
            call = ToolCallBlock(
                "write",
                "revise_memory",
                arguments={
                    "id": "memory",
                    "content": "保留否定和对象。",
                    "sources": list(references),
                    "base_version": 0,
                },
            )
        else:
            submission = FinishSubmission(
                reviews=()
                if self.mode == "defer"
                else (
                    Review(
                        issue="当前来源",
                        evidence=source_refs,
                        conclusion="保留真实范围。",
                        memory_decision="no_change",
                        reason="当前无需形成新认识。",
                    ),
                ),
                deferred=(Deferred(evidence=source_refs, reason="需要后续独立来源。"),)
                if self.mode == "defer"
                else (),
                findings=tuple(
                    FindingAssessment(id=item.id, status="uncertain", reason="事实仍需核对。")
                    for item in batch.findings
                ),
            )
            arguments = decode_object(submission.model_dump_json().encode("utf-8"))
            call = ToolCallBlock("finish", "finish_reflection", arguments=arguments)
        return ModelResponse(Message(MessageRole.ASSISTANT, (call,)))


async def _background(
    store: Store,
    stack: AsyncExitStack,
    backend: Backend,
    *,
    seconds: float = 0.01,
    page_chars: int = 8000,
    interrupts: list[int] | None = None,
) -> BackgroundService:
    async def media(references: tuple[MediaReference, ...]) -> tuple[ContentBlock, ...]:
        blocks: list[ContentBlock] = []
        for reference in references:
            content = await store.get_media(reference.media_id)
            if content is None:
                raise StorageError("媒体缺失", code="invalid_record")
            blocks.append(ImageBlock(ImageBytes(content, reference.media_type)))
        return tuple(blocks)

    def interrupt(generation: int) -> bool:
        if interrupts is not None:
            interrupts.append(generation)
        return generation == 7

    value = BackgroundService(
        store,
        store.feedback,
        KnowledgeService(store.knowledge),
        store.knowledge,
        reflection=SubjectModel(backend, "openai/test", 1000, _capacity, RetrySettings()),
        instructions=(SystemInstruction.from_text("独立反思固定指令"),),
        settings=BackgroundSettings(4096, page_chars=page_chars, batch_seconds=seconds),
        media=media,
        refresh=_refresh,
        prepare_history=None,
        foreground_generation=lambda: 7,
        interrupt_foreground=interrupt,
        can_start=lambda: None,
    )
    stack.push_async_callback(value.close)
    return value


async def _finished(store: Store, backend: Backend, *, state: str = "completed") -> bool:
    if not backend.requests:
        return False
    metadata = next(
        message.metadata
        for message in backend.requests[0].messages
        if "reflection_batch" in message.metadata
    )
    identity = metadata["reflection_batch"]
    assert isinstance(identity, str)
    return (await store.knowledge.get_reflection(identity, 1)).state == state


async def test_first_receipt_reflects_only_and_late_version_wakes_once(store: Store) -> None:
    await _call(store)
    await _result(store, "call", "unknown")
    first = await store.feedback.bridge(now=time.time())
    assert first.foreground_ids == () and first.changed
    assert await store.pending_call_receipts() == ()
    raw = await store.knowledge.get_evidence("call:call:1")
    assert raw is not None and '"state":"unknown"' in raw.text
    assert (await store.feedback.reflection_state()).priority
    await _result(store, "call", "confirmed")
    second = await store.feedback.bridge(now=time.time())
    assert len(second.foreground_ids) == 1
    wake = await store.get_input(second.foreground_ids[0])
    assert wake is not None and wake.context.minimum_wake and '"state":"confirmed"' in wake.text
    assert (await store.feedback.bridge(now=time.time())).foreground_ids == ()
    assert (await store.knowledge.get_evidence("call:call:1")) == raw
    pending = await store.knowledge.pending_reflection()
    assert {item.evidence_id for item in pending} == {"call:call:1", "call:call:2"}


async def test_receipt_media_is_real_same_blob_for_evidence_and_foreground(store: Store) -> None:
    input_id = await _admit(store, "media", media=(MediaAsset(b"actual-pixels", "image/png"),))
    original = await store.get_input(input_id)
    assert original is not None
    await _call(store, input_id=input_id)
    await _result(store, "call", "unknown")
    await store.feedback.bridge(now=time.time())
    await _result(store, "call", "confirmed", media=original.media)
    result = await store.feedback.bridge(now=time.time())
    wake = await store.get_input(result.foreground_ids[0])
    snapshot = await store.knowledge.get_evidence("call:call:2")
    assert wake is not None and snapshot is not None
    assert wake.media == original.media == snapshot.media
    assert await store.get_media(wake.media[0].media_id) == b"actual-pixels"


async def test_finish_tool_only_confirms_batch_and_no_new_epoch_does_not_read_next_page(
    store: Store,
) -> None:
    await _source(store, "long", "完整来源需要多页。" * 10)
    backend = Backend(store)
    async with AsyncExitStack() as stack:
        background = await _background(store, stack, backend, page_chars=10)
        await background.start()
        await _wait_for(lambda: _finished(store, backend))
        await _wait_for(lambda: _session_idle(background))
        assert len(backend.requests) == 2
        assert tuple(tool.name for tool in backend.requests[0].tools) == (
            "recall",
            "read_evidence",
            "revise_memory",
            "supervise",
            "finish_reflection",
        )
        pending = await store.knowledge.pending_reflection()
        assert len(pending) == 1 and pending[0].reviewed_slice_refs
        await asyncio.sleep(0.03)
        assert len(backend.requests) == 2
        assert (await store.feedback.reflection_state()).oldest_queued_at is None


async def _session_idle(background: BackgroundService) -> bool:
    return not background.session.busy


@pytest.mark.parametrize("mode", ["final", "defer", "write_fail", "finish_fail"])
async def test_completion_and_partial_writes_remain_independent_after_batch_failure(
    store: Store,
    mode: Literal["final", "defer", "write_fail", "finish_fail"],
) -> None:
    await _source(store, "source")
    backend = Backend(store, mode)
    async with AsyncExitStack() as stack:
        background = await _background(store, stack, backend)
        await background.start()
        expected = "interrupted" if mode in ("final", "write_fail") else "completed"
        await _wait_for(lambda: _finished(store, backend, state=expected))
        await _wait_for(lambda: _session_idle(background))
        count = len(backend.requests)
        await asyncio.sleep(0.03)
        assert len(backend.requests) == count and background.failure is None
        if mode == "write_fail":
            memory = await store.knowledge.get_memory("memory")
            assert memory is not None and memory.patch.content == "保留否定和对象。"
        if mode == "finish_fail":
            assert await store.knowledge.pending_reflection() == ()
            assert (await store.get_session("reflection")).version == 0
        if mode == "final":
            pending = await store.knowledge.pending_reflection()
            assert len(pending) == 1 and pending[0].reviewed_slice_refs == ()


async def test_new_independent_source_allows_new_batch_and_history_remains_continuous(
    store: Store,
) -> None:
    await _source(store, "first")
    backend = Backend(store)
    async with AsyncExitStack() as stack:
        background = await _background(store, stack, backend)
        await background.start()
        await _wait_for(lambda: _finished(store, backend))
        await _wait_for(lambda: _session_idle(background))
        await _source(store, "second", "新的独立材料")

        async def twice() -> bool:
            return (await store.get_session("reflection")).version == 2

        await _wait_for(twice)
        assert len(backend.requests) == 4
        assert any(message.text == "内部收尾" for message in backend.requests[2].messages)
        assert backend.requests[0].instructions == backend.requests[2].instructions


async def test_actual_unknown_is_priority_even_with_long_ordinary_timer(store: Store) -> None:
    await _call(store)
    await _result(store, "call", "unknown")
    backend = Backend(store)
    async with AsyncExitStack() as stack:
        background = await _background(store, stack, backend, seconds=60)
        await background.start()
        await _wait_for(lambda: _finished(store, backend))
        initial = backend.requests[0].messages[0]
        block = initial.content[-1]
        assert isinstance(block, TextBlock)
        page = decode_object(block.text.encode("utf-8"))
        item = page["item"]
        assert isinstance(item, dict)
        text = item["text"]
        assert isinstance(text, str)
        receipt = decode_object(text.encode("utf-8"))["call"]
        assert isinstance(receipt, dict) and receipt["state"] == "unknown"
        assert "需要复核" in initial.text


async def test_due_plan_once_foreground_does_not_resolve_plan(store: Store) -> None:
    await _source(store, "source")
    plan = WakeupPlan(id="plan", due=time.time() + 0.01, reason="独立后续约定", evidence="source")
    await store.knowledge.plan_wakeup(plan)
    assert not (await store.feedback.bridge(now=plan.due - 1)).foreground_ids
    actual = await store.feedback.bridge(now=plan.due)
    assert len(actual.foreground_ids) == 1
    assert await store.knowledge.pending_plans() == (plan,)
    assert not (await store.feedback.bridge(now=plan.due + 100)).foreground_ids


async def test_supervision_scan_does_not_steal_original_urgent_generation(store: Store) -> None:
    await _source(store, "source")
    page = await store.knowledge.read_evidence("source")
    assert page.slice is not None
    batch = await store.knowledge.begin_reflection("batch", (page.slice.slice_ref,))
    await store.knowledge.mark_presented(batch.batch_id, batch.version, (page.slice.slice_ref,))
    record = await store.knowledge.supervise(
        batch.batch_id,
        batch.version,
        SupervisionNotice(
            id="notice",
            issue="具体问题",
            evidence=(page.slice.slice_ref,),
            suggestion="重新考虑",
            level="urgent",
        ),
    )
    scan = await store.feedback.bridge(now=time.time())
    assert len(scan.foreground_ids) == 1 and scan.urgent_generations == ()
    actual = await store.feedback.dispatch_supervision(record, observed_generation=7)
    assert actual.urgent_generations == (7,)
    assert (
        await store.feedback.dispatch_supervision(record, observed_generation=99)
    ).urgent_generations == ()
    assert (await store.feedback.bridge(now=time.time())).urgent_generations == ()


async def test_empty_bridge_is_readonly_and_close_cancels_owned_model(store: Store) -> None:
    revision = store.change_revision
    assert not (await store.feedback.bridge(now=time.time())).changed
    assert store.change_revision == revision
    await _source(store, "source")
    backend = Backend(store, "block")
    async with AsyncExitStack() as stack:
        background = await _background(store, stack, backend)
        await background.start()
        async with asyncio.timeout(3):
            await backend.entered.wait()
        await asyncio.gather(background.close(), background.close())
        assert backend.cancelled.is_set() and background.pending_resources == 0
        with pytest.raises(HostStateError, match="启动"):
            await background.start()


async def test_one_epoch_freezes_limited_sources_and_keeps_overflow_for_new_facts(
    store: Store,
) -> None:
    """一批有限来源共享预算，未纳入者只有新增独立事实才重新成为候选。"""
    for ordinal in range(33):
        await _source(store, f"source:{ordinal}")
    backend = Backend(store)
    async with AsyncExitStack() as stack:
        background = await _background(store, stack, backend)
        await background.start()
        await _wait_for(lambda: _finished(store, backend))
        await _wait_for(lambda: _session_idle(background))
        assert len(backend.requests) == 2
        pending = await store.knowledge.pending_reflection()
        assert tuple(item.evidence_id for item in pending) == ("source:32",)
        await asyncio.sleep(0.03)
        assert len(backend.requests) == 2
        await _source(store, "fresh", "新增独立事实")

        async def twice() -> bool:
            return (await store.get_session("reflection")).version == 2

        await _wait_for(twice)
        assert len(backend.requests) == 4
        assert await store.knowledge.pending_reflection() == ()


@pytest.mark.parametrize("mode", ["finish", "defer", "final"])
async def test_new_fact_resumes_reviewed_prefix_but_retries_failed_or_deferred_page(
    store: Store, mode: Literal["finish", "defer", "final"]
) -> None:
    """取得游标不是理解；新事实后只跨过真正复核成功的前缀。"""
    await _source(store, "long", "abcdefghijklmnopqrstuvwx")
    backend = Backend(store, mode)
    async with AsyncExitStack() as stack:
        background = await _background(store, stack, backend, page_chars=10)
        await background.start()
        await _wait_for(
            lambda: _finished(
                store, backend, state="interrupted" if mode == "final" else "completed"
            )
        )
        await _wait_for(lambda: _session_idle(background))
        count = len(backend.requests)
        backend.mode = "finish"
        await _source(store, "fresh", "new")

        async def twice() -> bool:
            return (await store.get_session("reflection")).version == 2

        await _wait_for(twice)
        latest = next(
            message
            for message in reversed(backend.requests[count].messages)
            if "reflection_batch" in message.metadata
        )
        references = latest.metadata["source_slice_refs"]
        assert isinstance(references, tuple)
        fragments = [
            await store.knowledge.get_slice(reference)
            for reference in references
            if isinstance(reference, str)
        ]
        original = next(
            fragment
            for fragment in fragments
            if fragment is not None and fragment.evidence_id == "long"
        )
        assert original.start == (10 if mode == "finish" else 0)
        await asyncio.sleep(0.03)
        assert len(backend.requests) == count + 2


class HistoryBackend(Backend):
    """第一批真实展开额外原文，重开后新批只引用已有检查点中的该工具结果。"""

    def __init__(self, store: Store, *, previous_reference: str | None = None) -> None:
        super().__init__(store)
        self.previous_reference = previous_reference

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        step = len(self.requests)
        self.requests.append(request)
        self.entered.set()
        if step == 0:
            if self.previous_reference is None:
                call = ToolCallBlock(
                    "historical-read", "read_evidence", arguments={"id": "expanded"}
                )
            else:
                call = ToolCallBlock(
                    "historical-write",
                    "revise_memory",
                    arguments={
                        "id": "recovered",
                        "content": "引用原工具结果。",
                        "sources": [self.previous_reference],
                    },
                )
        elif step == 1:
            initial = next(
                message
                for message in reversed(request.messages)
                if "reflection_batch" in message.metadata
            )
            references = initial.metadata["source_slice_refs"]
            assert isinstance(references, tuple)
            submission = FinishSubmission(
                reviews=(
                    Review(
                        issue="当前来源",
                        evidence=tuple(item for item in references if isinstance(item, str)),
                        conclusion="保持真实覆盖。",
                        memory_decision="no_change",
                        reason="当前无需新认识。",
                    ),
                ),
                deferred=(),
                findings=(),
            )
            call = ToolCallBlock(
                "finish",
                "finish_reflection",
                arguments=decode_object(submission.model_dump_json().encode("utf-8")),
            )
        else:
            return ModelResponse(Message.assistant("内部收尾"))
        return ModelResponse(Message(MessageRole.ASSISTANT, (call,)))


@pytest.mark.parametrize("revoked", [False, True])
async def test_real_core_checkpoint_reopens_and_restores_exact_prior_tool_delivery(
    tmp_path: Path,
    revoked: bool,
) -> None:
    """真实 Core 历史跨进程保留完整工具结果，后台新批据此取得确切切片覆盖。"""
    path = tmp_path / "history.sqlite3"
    first = await Store.open(path)
    try:
        await _source(first, "first")
        await first.knowledge.put_evidence(
            EvidenceSnapshot(
                evidence_id="expanded",
                version=1,
                kind="test",
                text="工具展开的原始事实",
                source_json='{"plugin":"test"}',
                occurred_at=10.0,
            ),
            needs_reflection=False,
        )
        expanded = await first.knowledge.read_evidence("expanded")
        assert expanded.slice is not None
        reference = expanded.slice.slice_ref
        backend = HistoryBackend(first)
        async with AsyncExitStack() as stack:
            background = await _background(first, stack, backend)
            await background.start()

            async def committed() -> bool:
                return (await first.get_session("reflection")).version == 1

            await _wait_for(committed)
            assert len(backend.requests) == 3
    finally:
        await first.close()
    reopened = await Store.open(path)
    try:
        await _source(reopened, "fresh")
        if revoked:
            await reopened.knowledge.revoke_evidence("expanded", 1)
        backend = HistoryBackend(reopened, previous_reference=reference)
        async with AsyncExitStack() as stack:
            background = await _background(reopened, stack, backend)
            await background.start()

            async def restored() -> bool:
                return (await reopened.get_session("reflection")).version == 2

            await _wait_for(restored)
            memory = await reopened.knowledge.get_memory("recovered")
            if revoked:
                assert memory is None
            else:
                assert memory is not None and memory.patch.sources == (reference,)
            assert any(message.name == "read_evidence" for message in backend.requests[0].messages)
            assert len(backend.requests) == 3
    finally:
        await reopened.close()


async def test_reopen_receipt_and_epoch_claims_do_not_replay(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path)
    try:
        await _call(store)
        await _result(store, "call", "unknown")
        await store.feedback.bridge(now=time.time())
        state = await store.feedback.reflection_state()
        page = await store.knowledge.read_evidence("call:call:1")
        assert await store.feedback.reserve_reflection(
            batch_id="batch", epoch=state.epoch, pages=(page,), successful_turns=0
        )
    finally:
        await store.close()
    reopened = await Store.open(path)
    try:
        assert not (await reopened.feedback.bridge(now=time.time())).changed
        state = await reopened.feedback.reflection_state()
        assert await reopened.feedback.reflection_sources(epoch=state.epoch) == ()
        snapshot = await reopened.knowledge.get_evidence("call:call:1")
        assert snapshot is not None and '"state":"unknown"' in snapshot.text
    finally:
        await reopened.close()


async def test_atomic_outbox_survives_input_capacity_and_failed_admission(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path, max_inputs=1)
    try:
        occupied = await _admit(store, "occupied")
        await _call(store)
        await _result(store, "call", "unknown")
        await store.feedback.bridge(now=time.time())
        await _result(store, "call", "confirmed")
        blocked = await store.feedback.bridge(now=time.time())
        assert blocked.blocked and blocked.foreground_ids == ()
        assert await store.pending_call_receipts() == ()
        assert await store.knowledge.get_evidence("call:call:2") is not None
        await store.complete_input(occupied)
        recovered = await store.feedback.bridge(now=time.time())
        assert len(recovered.foreground_ids) == 1 and not recovered.blocked
    finally:
        await store.close()
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT input_id FROM feedback_deliveries WHERE kind='late_call'"
        ).fetchone()
        assert row is not None and row[0] == recovered.foreground_ids[0]


async def test_activity_two_responsibilities_transfer_to_durable_foreground_and_reflection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path, max_inputs=1)
    try:
        occupied = await _admit(store, "occupied")
        now = time.time()
        record = ActivityRecord(
            activity_id="activity",
            definition="local",
            goal="实际目标",
            state="awaiting_decision",
            version=1,
            run_generation=0,
            granted_seconds=10,
            used_seconds=0,
            checkpoint_at=None,
            clock_running=False,
            timing_incomplete=False,
            needs_reconciliation=False,
            last_command_id="command",
            work_json="{}",
            progress_json=None,
            created_at=now,
            updated_at=now,
        )
        feedback = ActivityFeedback(
            "feedback",
            "activity",
            None,
            0,
            1,
            "needs_decision",
            '{"problem":"实际需要选择"}',
            now,
            True,
        )
        await store.commit_activity(record, expected_version=None, feedback=feedback)
        assert await store.pending_activity_feedback("foreground") == (feedback,)
        assert await store.pending_activity_feedback("reflection") == (feedback,)
        result = await store.feedback.bridge(now=now)
        assert result.blocked and not result.foreground_ids
        snapshot = await store.knowledge.get_evidence("activity:feedback")
        assert snapshot is not None and "实际需要选择" in snapshot.text
        assert await store.pending_activity_feedback("foreground") == ()
        assert await store.pending_activity_feedback("reflection") == ()
        await store.complete_input(occupied)
        actual = await store.feedback.bridge(now=now)
        assert len(actual.foreground_ids) == 1
        assert (await store.feedback.reflection_state()).priority
    finally:
        await store.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM activity_feedback").fetchone() == (1,)
        assert connection.execute(
            "SELECT input_id FROM feedback_deliveries WHERE kind='activity'"
        ).fetchone() == (actual.foreground_ids[0],)


async def test_feedback_sql_failure_retains_original_receipt_and_stops_new_writes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path)
    try:
        await _call(store)
        await _result(store, "call", "confirmed")
        with sqlite3.connect(path) as sample:
            sample.execute(
                "CREATE TRIGGER feedback_fault BEFORE INSERT ON knowledge_evidence "
                "WHEN json_extract(NEW.payload_json,'$.kind')='call_receipt' "
                "BEGIN SELECT RAISE(ABORT,'sample bridge failure'); END"
            )
        with pytest.raises(StorageError):
            await store.feedback.bridge(now=time.time())
        pending = await store.pending_call_receipts()
        assert len(pending) == 1 and pending[0].state == "confirmed"
        assert await store.knowledge.get_evidence("call:call:1") is None
        with pytest.raises(StorageError):
            await store.begin_action("blocked", catalog_version="catalog", source="return None")
    finally:
        await store.close()
    with sqlite3.connect(path) as sample:
        assert sample.execute("SELECT count(*) FROM feedback_deliveries").fetchone() == (0,)
        assert sample.execute("SELECT count(*) FROM pending_call_receipts").fetchone() == (1,)


async def test_later_sources_do_not_reset_original_sixty_second_window(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Clock:
        seconds = time.time()

        def time(self) -> float:
            return self.seconds

    clock = Clock()
    monkeypatch.setattr(background_module, "time", clock)
    await _source(store, "first")
    backend = Backend(store)
    async with AsyncExitStack() as stack:
        background = await _background(store, stack, backend, seconds=60)
        await background.start()
        clock.seconds += 59
        await _source(store, "later")
        await asyncio.sleep(0.01)
        assert not backend.entered.is_set()
        clock.seconds += 2
        await _source(store, "last")
        async with asyncio.timeout(3):
            await backend.entered.wait()
        assert "first" in backend.requests[0].messages[0].text


@pytest.mark.parametrize("terminal_error", ["ModelRequestError", "TimeoutError"])
async def test_terminal_foreground_failure_is_priority_but_budget_and_interrupt_are_not(
    store: Store,
    terminal_error: str,
) -> None:
    records: tuple[tuple[str, TurnOutcome, str], ...] = (
        ("budget", "failed", '{"error_type":"RequestBudgetExceeded"}'),
        ("core_budget", "failed", '{"error_type":"ToolLoopLimitError"}'),
        ("deadline", "failed", '{"error_type":"TimeoutError","category":"budget_exhausted"}'),
        ("vad", "interrupted", '{"error_type":"CancelledError"}'),
    )
    for identity, state, error in records:
        turn = await store.begin_turn(identity, "foreground", 0, 1, ())
        await store.finish_turn(turn, state, error_json=error)
    await store.feedback.bridge(now=time.time())
    assert not (await store.feedback.reflection_state()).priority
    turn = await store.begin_turn("failed", "foreground", 0, 2, ())
    await store.finish_turn(turn, "failed", error_json='{"error_type":"' + terminal_error + '"}')
    await store.feedback.bridge(now=time.time())
    assert (await store.feedback.reflection_state()).priority


async def test_more_than_thirty_two_unanswered_notices_do_not_starve_new_delivery(
    store: Store,
) -> None:
    await _source(store, "source")
    page = await store.knowledge.read_evidence("source")
    assert page.slice is not None
    batch = await store.knowledge.begin_reflection("batch", (page.slice.slice_ref,))
    await store.knowledge.mark_presented(batch.batch_id, batch.version, (page.slice.slice_ref,))
    for ordinal in range(35):
        await store.knowledge.supervise(
            batch.batch_id,
            batch.version,
            SupervisionNotice(
                id=f"notice:{ordinal}",
                issue="具体问题",
                evidence=(page.slice.slice_ref,),
                suggestion=f"意见 {ordinal}",
                level="reminder",
            ),
        )
    ids: list[str] = []
    for _ in range(3):
        ids.extend((await store.feedback.bridge(now=time.time())).foreground_ids)
    assert len(ids) == 35 and len(set(ids)) == 35
    assert len(await store.knowledge.pending_supervision()) == 32
    assert not (await store.feedback.bridge(now=time.time())).foreground_ids


async def test_eight_actual_successful_foreground_turns_trigger_before_sixty_seconds(
    store: Store,
) -> None:
    class ForegroundBackend:
        async def generate(
            self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
        ) -> ModelResponse:
            return ModelResponse(Message.assistant("内部决定"))

    backend = Backend(store)
    async with AsyncExitStack() as stack:
        foreground = ContinuousSession(store, "foreground", can_start=lambda: None)
        stack.push_async_callback(foreground.close)
        for ordinal in range(8):
            identity = await _admit(store, f"foreground:{ordinal}")
            await foreground.run(
                Message.user(f"真实外部交互 {ordinal}"),
                turn_id=f"turn:{ordinal}",
                input_ids=(identity,),
                backend=ForegroundBackend(),
                evidence=store,
                model="openai/test",
                instructions=(SystemInstruction.from_text("固定指令"),),
                tools=ToolSet(),
                output_tokens=1000,
                max_requests=8,
                turn_seconds=600,
                retry=RetrySettings(),
                diagnostic_bytes=4096,
                refresh=_refresh,
                capacity=_capacity,
            )
        state = await store.feedback.reflection_state()
        assert state.successful_turns == 8 and state.last_taken_successful_turns == 0
        background = await _background(store, stack, backend, seconds=60)
        await background.start()
        async with asyncio.timeout(3):
            await backend.entered.wait()
        state = await store.feedback.reflection_state()
        assert state.last_taken_successful_turns == 8


async def test_blocked_urgent_signal_keeps_original_generation_until_real_admission(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "state.sqlite3", max_inputs=1)
    try:
        occupied = await _admit(store, "occupied")
        await _source(store, "source")
        page = await store.knowledge.read_evidence("source")
        assert page.slice is not None
        batch = await store.knowledge.begin_reflection("batch", (page.slice.slice_ref,))
        await store.knowledge.mark_presented(batch.batch_id, batch.version, (page.slice.slice_ref,))
        notice = await store.knowledge.supervise(
            batch.batch_id,
            batch.version,
            SupervisionNotice(
                id="notice",
                issue="具体问题",
                evidence=(page.slice.slice_ref,),
                suggestion="重思考",
                level="urgent",
            ),
        )
        blocked = await store.feedback.dispatch_supervision(notice, observed_generation=7)
        assert blocked.blocked and blocked.urgent_generations == ()
        await store.complete_input(occupied)
        admitted = await store.feedback.bridge(now=time.time())
        assert len(admitted.foreground_ids) == 1 and admitted.urgent_generations == (7,)
        assert (
            await store.feedback.dispatch_supervision(notice, observed_generation=99)
        ).urgent_generations == ()
    finally:
        await store.close()


async def test_supervision_source_revoked_before_quota_recovers_is_not_delivered(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "state.sqlite3", max_inputs=1)
    try:
        occupied = await _admit(store, "occupied")
        await _source(store, "source")
        page = await store.knowledge.read_evidence("source")
        assert page.slice is not None
        batch = await store.knowledge.begin_reflection("batch", (page.slice.slice_ref,))
        await store.knowledge.mark_presented(batch.batch_id, batch.version, (page.slice.slice_ref,))
        record = await store.knowledge.supervise(
            batch.batch_id,
            batch.version,
            SupervisionNotice(
                id="notice",
                issue="具体问题",
                evidence=(page.slice.slice_ref,),
                suggestion="重思考",
                level="urgent",
            ),
        )
        assert (await store.feedback.dispatch_supervision(record, observed_generation=7)).blocked
        await store.knowledge.revoke_evidence("source", 1)
        await store.complete_input(occupied)
        result = await store.feedback.bridge(now=time.time())
        assert result.foreground_ids == () and result.urgent_generations == ()
        assert not (await store.feedback.bridge(now=time.time())).changed
    finally:
        await store.close()


async def test_internal_model_failure_keeps_prototype_and_stops_background_dispatch(
    store: Store,
) -> None:
    await _source(store, "source")
    backend = Backend(store, "internal")
    background: BackgroundService | None = None
    with pytest.raises(LookupError, match="程序原型"):
        async with AsyncExitStack() as stack:
            background = await _background(store, stack, backend)
            await background.start()

            async def faulted() -> bool:
                return background.failure is not None

            await _wait_for(faulted)
            assert isinstance(background.failure, LookupError)
            await _source(store, "independent")
            await asyncio.sleep(0.02)
            assert len(backend.requests) == 1
    assert background is not None and background.pending_resources == 0


async def test_close_holds_cancelled_feedback_commit_until_real_database_return(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _call(store)
    await _result(store, "call", "confirmed")
    entered, release = threading.Event(), threading.Event()
    original = feedback_sql.bridge

    def blocked(
        connection: sqlite3.Connection,
        admit: AdmitInput,
        work: subject_feedback_models.FeedbackWork,
        *,
        now: float,
        limit: int = 16,
    ) -> subject_feedback_models.FeedbackBridge:
        entered.set()
        if not release.wait(3):
            raise TimeoutError("测试没有释放数据库交接。")
        return original(connection, admit, work, now=now, limit=limit)

    monkeypatch.setattr(feedback_sql, "bridge", blocked)
    backend = Backend(store)
    async with AsyncExitStack() as stack:
        background = await _background(store, stack, backend, seconds=60)
        await background.start()
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            closing = asyncio.create_task(background.close())
            await asyncio.sleep(0.01)
            assert not closing.done() and background.pending_resources > 0
        finally:
            release.set()
        await closing
        assert background.pending_resources == 0
        assert await store.pending_call_receipts() == ()
        assert await store.knowledge.get_evidence("call:call:1") is not None


@pytest.mark.parametrize("limit", [0, -1, 33])
async def test_feedback_public_scan_limit_rejects_unbounded_queries(
    store: Store, limit: int
) -> None:
    revision = store.change_revision
    with pytest.raises(ValueError, match="限额"):
        await store.feedback.bridge(now=time.time(), limit=limit)
    assert store.change_revision == revision


async def test_actual_eight_request_limit_abandons_once_and_new_fact_gets_next_batch(
    store: Store,
) -> None:
    """Core 最后轮拒绝继续工具，预算耗尽不故障锁存或自行刷新额度。"""

    class RepeatedReads(Backend):
        repeat = True

        async def generate(
            self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
        ) -> ModelResponse:
            if not self.repeat:
                return await super().generate(request, model=model, emit=emit)
            self.requests.append(request)
            self.entered.set()
            return ModelResponse(
                Message(
                    MessageRole.ASSISTANT,
                    (ToolCallBlock("read", "read_evidence", arguments={"id": "source"}),),
                )
            )

    await _source(store, "source")
    backend = RepeatedReads(store)
    async with AsyncExitStack() as stack:
        background = await _background(store, stack, backend)
        await background.start()
        await _wait_for(lambda: _finished(store, backend, state="interrupted"))
        await _wait_for(lambda: _session_idle(background))
        assert len(backend.requests) == 8 and background.failure is None
        await asyncio.sleep(0.03)
        assert len(backend.requests) == 8
        backend.repeat = False
        await _source(store, "fresh")

        async def committed() -> bool:
            return (await store.get_session("reflection")).version == 1

        await _wait_for(committed)
        assert len(backend.requests) == 10 and background.failure is None
