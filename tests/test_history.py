"""真实Store与唯一模型请求包装验证无工具摘要、冻结预算和失败覆盖。

模型后端只返回离线脚本，Core连续Conversation不参与摘要计算。实际请求、
尝试、原历史和候选仍提交到同一真实SQLite连接，所有HTTP由测试基础设施拦截。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from purrcept_core.models import (
    ConversationCheckpoint,
    ConversationState,
    Message,
    MessageRole,
    ModelEventSink,
    ModelRequest,
    ModelResponse,
    ModelSettings,
    ModelUnavailableError,
    ReasoningBlock,
    ReminderState,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)

from purrcept_engine.app.config import RetrySettings
from purrcept_engine.context.capacity import GenerationCapacity, assess_capacity, check_capacity
from purrcept_engine.context.checkpoints import decode_checkpoint, encode_checkpoint
from purrcept_engine.context.maintenance import SummaryError, SummarySource, freeze_summary_source
from purrcept_engine.execution.protocol import JsonObject
from purrcept_engine.storage import (
    InputSource,
    InputSubmission,
)
from purrcept_engine.storage.store import Store
from purrcept_engine.subject.history import HistoryMaintenance
from purrcept_engine.subject.model_requests import RequestBudgetExceeded


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    database = await Store.open(tmp_path / "history.sqlite3")
    try:
        yield database
    finally:
        await database.close()


async def saved_source(store: Store) -> SummarySource:
    now = datetime.now(UTC)
    admitted = await store.admit(
        InputSubmission(
            InputSource("test", "account", "chat", "text"), "summary dependency", now, now
        )
    )
    messages: list[Message] = []
    boundaries: list[int] = []
    for index in range(5):
        messages.append(
            Message.user(
                "source-" + str(index) + ":" + "original " * 150,
                metadata={"evidence": (admitted.input_id,)},
            )
        )
        if index == 0:
            call = ToolCallBlock(
                "old-call", "play_action", arguments={"action_code": "historical only"}
            )
            messages.append(
                Message(MessageRole.ASSISTANT, (ReasoningBlock("hidden-provider-reasoning"), call))
            )
            messages.append(
                Message(
                    MessageRole.TOOL,
                    (ToolResultBlock("old-call", content=(TextBlock("confirmed past"),)),),
                    name="play_action",
                )
            )
        messages.append(Message.assistant("complete historical response"))
        boundaries.append(len(messages))
    checkpoint = ConversationCheckpoint(
        ConversationState(tuple(messages), turn_index=5, turn_boundaries=tuple(boundaries)),
        ReminderState(),
    )
    turn = await store.begin_turn("seed", "foreground", 0, 1, ())
    await store.finish_turn(turn, "succeeded", checkpoint_json=encode_checkpoint(checkpoint))
    source = freeze_summary_source(checkpoint, owner_id="foreground", source_version=1)
    assert source is not None
    return source


def layout(checkpoint: ConversationCheckpoint) -> ModelRequest:
    """回放同一完整冻结前缀/尾部，候选只改变历史，工具schema和输出仍参与计量。"""
    return ModelRequest(
        (*checkpoint.history.messages, Message.user("same next input")),
        settings=ModelSettings(max_output_tokens=100),
    )


def source_capacity(payload: JsonObject):
    return assess_capacity(payload, GenerationCapacity(100_000, 100, "reference-text-only-v1"))


def summary_capacity(payload: JsonObject):
    return check_capacity(payload, GenerationCapacity(100_000, 256, "reference-text-only-v1"))


class Backend:
    """只返回离线脚本，记录实际无工具请求而不复制模型工具循环。"""

    def __init__(self, responses: tuple[ModelResponse | Exception, ...]) -> None:
        self.responses = deque(responses)
        self.requests: list[ModelRequest] = []

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        self.requests.append(request)
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def service(store: Store, backend: Backend) -> HistoryMaintenance:
    return HistoryMaintenance(
        store,
        store,
        backend,
        model="openai/offline",
        summary_capacity=summary_capacity,
        retry=RetrySettings(attempts=1),
        output_tokens=256,
        diagnostic_bytes=1024,
    )


@pytest.mark.asyncio
async def test_history_uses_one_real_recorded_request_no_tools_and_full_candidate_check(
    store: Store,
) -> None:
    source = await saved_source(store)
    backend = Backend((ModelResponse(Message.assistant('{"summary":"保留真实回执与未决来源。"}')),))
    maintenance = service(store, backend)
    deadline = asyncio.get_running_loop().time() + 600
    try:
        result = await maintenance.run(
            source,
            source_layout=layout,
            source_model="openai/offline",
            source_capacity=source_capacity,
            deadline=deadline,
        )
    finally:
        await maintenance.close()
    assert result.installed_version == 2 and result.proposal is not None
    assert len(backend.requests) == 1 and backend.requests[0].tools == ()
    assert (
        len(backend.requests[0].messages) == 1
        and backend.requests[0].messages[0].role is MessageRole.USER
    )
    material = backend.requests[0].messages[0].text
    assert "historical only" in material and "confirmed past" in material
    assert "hidden-provider-reasoning" not in material and "provider_reasoning_omitted" in material
    assert "source-3:" not in material and "source-4:" not in material
    records = await store.requests_for_turn("foreground", "summary:" + source.coverage_id)
    assert len(records) == 1 and records[0].state == "succeeded"
    assert records[0].request.scope.material_refs == ("summary:" + source.coverage_id,)
    assert len(records[0].attempts) == 1 and records[0].attempts[0].start.deadline <= deadline
    assert records[0].request.capacity_json is not None
    current = await store.get_session("foreground")
    assert current.version == 2 and current.checkpoint_json is not None
    assert decode_checkpoint(current.checkpoint_json) == result.proposal.candidate
    assert result.proposal.after.estimate.input_units < result.proposal.before.estimate.input_units


@pytest.mark.asyncio
async def test_invalid_complete_summary_does_not_retry_same_coverage(store: Store) -> None:
    source = await saved_source(store)
    backend = Backend((ModelResponse(Message.assistant('{"summary":""}')),))
    maintenance = service(store, backend)
    try:
        with pytest.raises(SummaryError, match="summary"):
            await maintenance.run(
                source,
                source_layout=layout,
                source_model="openai/offline",
                source_capacity=source_capacity,
                deadline=asyncio.get_running_loop().time() + 600,
            )
        again = await maintenance.run(
            source,
            source_layout=layout,
            source_model="openai/offline",
            source_capacity=source_capacity,
            deadline=asyncio.get_running_loop().time() + 600,
        )
    finally:
        await maintenance.close()
    assert again.reservation == "already_attempted" and len(backend.requests) == 1
    assert (await store.get_session("foreground")).version == 1
    assert len(await store.requests_for_turn("foreground", "summary:" + source.coverage_id)) == 1


@pytest.mark.asyncio
async def test_tool_proposal_in_summary_response_is_never_executed(store: Store) -> None:
    source = await saved_source(store)
    response = ModelResponse(
        Message(
            MessageRole.ASSISTANT,
            (ToolCallBlock("new", "play_action", arguments={"action_code": "must not run"}),),
        )
    )
    backend = Backend((response,))
    maintenance = service(store, backend)
    try:
        with pytest.raises(SummaryError, match="无工具"):
            await maintenance.run(
                source,
                source_layout=layout,
                source_model="openai/offline",
                source_capacity=source_capacity,
                deadline=asyncio.get_running_loop().time() + 600,
            )
    finally:
        await maintenance.close()
    assert len(backend.requests) == 1 and (await store.get_session("foreground")).version == 1


@pytest.mark.asyncio
async def test_database_wait_does_not_restart_the_original_600_second_deadline(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = await saved_source(store)
    backend = Backend(())
    maintenance = service(store, backend)
    loop = asyncio.get_running_loop()
    original_time = loop.time
    elapsed = 0.0
    monkeypatch.setattr(loop, "time", lambda: original_time() + elapsed)
    reserve = store.reserve_summary

    async def delayed(frozen: SummarySource):
        nonlocal elapsed
        result = await reserve(frozen)
        elapsed = 601.0
        return result

    monkeypatch.setattr(store, "reserve_summary", delayed)
    deadline = loop.time() + 600
    try:
        with pytest.raises(RequestBudgetExceeded):
            await maintenance.run(
                source,
                source_layout=layout,
                source_model="openai/offline",
                source_capacity=source_capacity,
                deadline=deadline,
            )
    finally:
        monkeypatch.setattr(loop, "time", original_time)
        await maintenance.close()
    assert backend.requests == [] and (await store.get_session("foreground")).version == 1
    assert await reserve(source) == "already_attempted"


@pytest.mark.asyncio
async def test_growing_candidate_is_rejected_after_real_response_commit(store: Store) -> None:
    source = await saved_source(store)
    backend = Backend(
        (ModelResponse(Message.assistant('{"summary":"' + "growing " * 2000 + '"}')),)
    )
    maintenance = service(store, backend)
    try:
        with pytest.raises(SummaryError, match="缩小"):
            await maintenance.run(
                source,
                source_layout=layout,
                source_model="openai/offline",
                source_capacity=source_capacity,
                deadline=asyncio.get_running_loop().time() + 600,
            )
    finally:
        await maintenance.close()
    assert (await store.get_session("foreground")).version == 1
    records = await store.requests_for_turn("foreground", "summary:" + source.coverage_id)
    assert records[0].state == "succeeded" and records[0].attempts[0].result is not None


class BusyBackend(Backend):
    """模型等待期间出现真实前台轮次，使最终安装丧失idle资格。"""

    def __init__(self, store: Store) -> None:
        super().__init__((ModelResponse(Message.assistant('{"summary":"有效但已过期"}')),))
        self.store = store

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        await self.store.begin_turn("concurrent", "foreground", 1, 1, ())
        return await super().generate(request, model=model, emit=emit)


@pytest.mark.asyncio
async def test_model_success_does_not_install_while_target_is_busy(store: Store) -> None:
    source = await saved_source(store)
    backend = BusyBackend(store)
    maintenance = service(store, backend)
    try:
        result = await maintenance.run(
            source,
            source_layout=layout,
            source_model="openai/offline",
            source_capacity=source_capacity,
            deadline=asyncio.get_running_loop().time() + 600,
        )
    finally:
        await maintenance.close()
    assert result.reservation == "reserved" and result.proposal is not None
    assert result.installed_version is None
    assert (await store.get_session("foreground")).version == 1
    turn = await store.get_turn("concurrent")
    assert turn is not None
    await store.finish_turn(turn, "failed", error_json='{"error_type":"test_interruption"}')


@pytest.mark.asyncio
async def test_single_logical_request_uses_only_the_existing_transport_retry(store: Store) -> None:
    source = await saved_source(store)
    backend = Backend(
        (
            ModelUnavailableError("offline first failure", provider="test"),
            ModelResponse(Message.assistant('{"summary":"真实同一逻辑请求"}')),
        )
    )
    maintenance = HistoryMaintenance(
        store,
        store,
        backend,
        model="openai/offline",
        summary_capacity=summary_capacity,
        retry=RetrySettings(),
        output_tokens=256,
        diagnostic_bytes=1024,
    )
    try:
        result = await maintenance.run(
            source,
            source_layout=layout,
            source_model="openai/offline",
            source_capacity=source_capacity,
            deadline=asyncio.get_running_loop().time() + 600,
        )
    finally:
        await maintenance.close()
    assert result.installed_version == 2 and len(backend.requests) == 2
    assert backend.requests[0] is backend.requests[1]
    records = await store.requests_for_turn("foreground", "summary:" + source.coverage_id)
    assert len(records) == 1 and len(records[0].attempts) == 2
    assert (
        records[0].attempts[0].result is not None
        and records[0].attempts[0].result.state == "failed"
    )


class WaitingBackend(Backend):
    """取消后立即释放借用，不吞取消或伪造已完成摘要。"""

    def __init__(self) -> None:
        super().__init__(())
        self.started = asyncio.Event()
        self.wait = asyncio.Event()

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        self.requests.append(request)
        self.started.set()
        await self.wait.wait()
        raise AssertionError("取消场景不能生成摘要")


@pytest.mark.asyncio
async def test_cancelled_summary_retains_coverage_and_real_cancelled_attempt(store: Store) -> None:
    source = await saved_source(store)
    backend = WaitingBackend()
    maintenance = service(store, backend)
    task = asyncio.create_task(
        maintenance.run(
            source,
            source_layout=layout,
            source_model="openai/offline",
            source_capacity=source_capacity,
            deadline=asyncio.get_running_loop().time() + 600,
        )
    )
    await backend.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await maintenance.close()
    records = await store.requests_for_turn("foreground", "summary:" + source.coverage_id)
    assert len(records) == 1 and records[0].state == "cancelled"
    assert (
        records[0].attempts[0].result is not None
        and records[0].attempts[0].result.state == "cancelled"
    )
    assert (await store.get_session("foreground")).version == 1
    assert await store.reserve_summary(source) == "already_attempted"
