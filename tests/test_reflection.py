"""真实 Store 和 Core 工具循环验证反思呈现、修订与完成提交边界。

响应脚本只替代模型服务，工具解析、会话循环和唯一数据库线程均使用真实
实现。不根据返回文字判断等价，也不访问真实用户资料或发送模型请求。
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from purrcept_core import AgentDriver, ExecutionContext, InlineExecutor
from purrcept_core.models import (
    ContentBlock,
    ImageBlock,
    ImageBytes,
    Message,
    MessageRole,
    Model,
    ModelEventSink,
    ModelRequest,
    ModelResponse,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from purrcept_core.models.tools import InvokeTool, ToolResult

from purrcept_engine.domain.inputs.models import MediaReference
from purrcept_engine.domain.knowledge.models import (
    EvidencePage,
    EvidenceSnapshot,
    KnowledgeCandidate,
    KnowledgeReference,
    MemoryPatch,
    SupervisionRecord,
)
from purrcept_engine.errors import StorageError
from purrcept_engine.storage import (
    InputSource,
    InputSubmission,
    MediaAsset,
)
from purrcept_engine.storage.store import Store
from purrcept_engine.subject.knowledge import KnowledgeService
from purrcept_engine.subject.reflection import EvidenceDeliveries, ReflectionTools
from purrcept_engine.values import JsonValue, decode_object, encode_json


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    database = await Store.open(tmp_path / "engine.sqlite3")
    try:
        yield database
    finally:
        await database.close()


class Ports:
    """从同一权威 Store 取得原生媒体，并记录已提交监管交接。"""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.notices: list[SupervisionRecord] = []
        self.override: bytes | None = None

    async def media_blocks(self, media: tuple[MediaReference, ...]) -> tuple[ContentBlock, ...]:
        blocks: list[ContentBlock] = []
        for reference in media:
            content = await self.store.get_media(reference.media_id)
            if content is None:
                raise StorageError("原生媒体缺失", code="invalid_record")
            blocks.append(ImageBlock(ImageBytes(self.override or content, reference.media_type)))
        return tuple(blocks)

    def foreground_generation(self) -> int:
        return 0

    async def supervision_committed(
        self, record: SupervisionRecord, *, observed_generation: int
    ) -> None:
        self.notices.append(record)


async def source(store: Store, identity: str, text: str = "真实材料") -> EvidencePage:
    await store.knowledge.put_evidence(
        EvidenceSnapshot(
            evidence_id=identity,
            version=1,
            kind="test",
            text=text,
            source_json='{"plugin":"test"}',
            occurred_at=10.0,
        )
    )
    return await store.knowledge.read_evidence(identity)


async def reflection(
    store: Store,
    page: EvidencePage,
    *,
    identity: str = "batch",
    deliveries: EvidenceDeliveries | None = None,
) -> tuple[ReflectionTools, Ports, Message]:
    assert page.slice is not None
    batch = await store.knowledge.begin_reflection(identity, (page.slice.slice_ref,))
    ports = Ports(store)
    tools = ReflectionTools(store.knowledge, batch, ports, deliveries=deliveries)
    return tools, ports, await tools.initial_message((page,))


async def invoke(
    tools: ReflectionTools, name: str, arguments: Mapping[str, JsonValue], identity: str = "tool"
) -> ToolResult:
    call = ToolCallBlock(identity, name, arguments=arguments)
    effect = InvokeTool(tools.toolset().get(name), call)
    return await effect.execute(ExecutionContext("run", "effect", 0, host=None))


def calls(*blocks: ToolCallBlock) -> ModelResponse:
    return ModelResponse(Message(MessageRole.ASSISTANT, tuple(blocks)))


def review(reference: str, *, decision: str = "no_change") -> dict[str, JsonValue]:
    return {
        "issue": "事实范围",
        "evidence": [reference],
        "conclusion": "保留实际对象和否定。",
        "memory_decision": decision,
        "reason": "根据已呈现原文决定。",
    }


class Backend:
    """有效响应才登记实际请求材料，之后由真实 Core 执行工具。"""

    def __init__(
        self, store: Store, tools: ReflectionTools, responses: tuple[ModelResponse | Exception, ...]
    ) -> None:
        self.store = store
        self.tools = tools
        self.responses = deque(responses)
        self.requests: list[ModelRequest] = []

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        self.requests.append(request)
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        batch = self.tools.batch
        await self.store.knowledge.mark_presented(
            batch.batch_id, batch.version, self.tools.presented_refs(request)
        )
        return response


@pytest.mark.asyncio
async def test_core_same_response_read_then_revise_waits_until_next_request(store: Store) -> None:
    first = await source(store, "first", "原来分配的完整材料")
    second = await source(store, "second", "后读到的完整材料")
    assert first.slice is not None and second.slice is not None
    tools, _, initial = await reflection(store, first)
    revision: dict[str, JsonValue] = {
        "id": "memory",
        "content": "对象的实际偏好",
        "sources": [second.slice.slice_ref],
        "base_version": 0,
    }
    backend = Backend(
        store,
        tools,
        (
            calls(
                ToolCallBlock("read", "read_evidence", arguments={"id": "second"}),
                ToolCallBlock("too-early", "revise_memory", arguments=revision),
            ),
            calls(ToolCallBlock("after-presentation", "revise_memory", arguments=revision)),
            calls(
                ToolCallBlock(
                    "finish",
                    "finish_reflection",
                    arguments={
                        "reviews": [
                            review(first.slice.slice_ref),
                            review(second.slice.slice_ref, decision="written"),
                        ],
                        "deferred": [],
                        "findings": [],
                    },
                )
            ),
            ModelResponse(Message.assistant("已完成内部复核。")),
        ),
    )
    conversation = Model(backend, "test").conversation(tools=tools.toolset(), max_model_rounds=8)
    await AgentDriver(InlineExecutor()).run(conversation.ask(initial), host=None)
    assert len(backend.requests) == 4
    failed = [
        block
        for message in backend.requests[1].messages
        for block in message.content
        if isinstance(block, ToolResultBlock) and block.tool_call_id == "too-early"
    ]
    assert len(failed) == 1 and failed[0].is_error
    memory = await store.knowledge.get_memory("memory")
    assert (
        memory is not None
        and memory.version == 1
        and memory.patch.sources == (second.slice.slice_ref,)
    )
    receipt = await store.knowledge.get_reflection(tools.batch.batch_id, tools.batch.version)
    assert receipt.receipt is not None and receipt.state == "completed"
    assert receipt.receipt.written == (KnowledgeReference(kind="memory", id="memory", version=1),)
    assert not await store.knowledge.pending_reflection()
    assert tuple(spec.name for spec in backend.requests[0].tools) == (
        "recall",
        "read_evidence",
        "revise_memory",
        "supervise",
        "finish_reflection",
    )
    assert conversation.state.turn_index == 1


@pytest.mark.asyncio
async def test_real_tool_schema_is_typed_and_rejects_invalid_arguments(store: Store) -> None:
    page = await source(store, "source")
    tools, _, _ = await reflection(store, page)
    specifications = {spec.name: spec for spec in tools.toolset().specs}
    read_schema = encode_json(specifications["read_evidence"].parameters).decode()
    finish_schema = encode_json(specifications["finish_reflection"].parameters).decode()
    assert '"context"' not in read_schema
    assert '"batch_id"' not in finish_schema and '"memory_id"' not in finish_schema
    assert '"memory_decision"' in finish_schema and '"enum"' in finish_schema
    wrong = await invoke(
        tools,
        "revise_memory",
        {"id": "m", "content": "内容", "sources": ["slice:unknown"], "base_version": True},
    )
    assert wrong.is_error and await store.knowledge.get_memory("m") is None
    extra = await invoke(
        tools,
        "finish_reflection",
        {"reviews": [], "deferred": [], "findings": [], "batch_id": "injected"},
    )
    assert extra.is_error
    empty_query = await invoke(tools, "recall", {"query": "", "start": 1.0, "end": 20.0})
    assert not empty_query.is_error


@pytest.mark.asyncio
async def test_recall_preserves_layered_fields_without_serializing_full_original_record(
    store: Store,
) -> None:
    await store.knowledge.put_evidence(
        EvidenceSnapshot(
            evidence_id="source",
            version=1,
            kind="document",
            text="原文详细茶杯对象",
            summary="明确摘要",
            source_json='{"plugin":"test"}',
            occurred_at=10.0,
            items_json='[{"private_original":"详细茶杯对象"}]',
        )
    )
    page = await store.knowledge.read_evidence("source")
    tools, _, _ = await reflection(store, page)
    result = await invoke(tools, "recall", {"query": "茶杯"})
    assert not result.is_error and isinstance(result.content[0], TextBlock)
    payload = decode_object(result.content[0].text.encode())
    items = payload["items"]
    assert isinstance(items, list)
    selected = items[0]
    assert isinstance(selected, dict)
    assert selected["id"] == "source" and selected["text"] == "明确摘要"
    assert selected["sources"] == ["source"] and selected["version"] == 1
    assert selected["ranks"] == {"keyword": 1} and selected["active"]
    assert "private_original" not in result.content[0].text
    assert not tools.presented_refs(
        ModelRequest((Message(MessageRole.TOOL, (result.to_block("recall"),), name="recall"),))
    )


@pytest.mark.asyncio
async def test_actual_complete_blocks_are_required_for_presentation(store: Store) -> None:
    content = b"immutable-image"
    now = datetime.now(UTC)
    admitted = await store.admit(
        InputSubmission(
            InputSource("test", "account", "chat", "image"),
            "图片事实",
            now,
            now,
            (MediaAsset(content, "image/png"),),
        )
    )
    page = await store.knowledge.read_evidence(admitted.input_id)
    assert page.slice is not None
    tools, ports, initial = await reflection(store, page)
    assert isinstance(initial.content[-1], ImageBlock)
    assert tools.presented_refs(ModelRequest((initial,))) == (page.slice.slice_ref,)
    trimmed = Message(MessageRole.USER, initial.content[:-1])
    assert not tools.presented_refs(ModelRequest((trimmed,)))
    result = await invoke(tools, "read_evidence", {"id": admitted.input_id}, "read-original")
    actual = Message(MessageRole.TOOL, (result.to_block("read-original"),), name="read_evidence")
    assert tools.presented_refs(ModelRequest((actual,))) == (page.slice.slice_ref,)
    changed_id = Message(MessageRole.TOOL, (result.to_block("another-id"),), name="read_evidence")
    assert not tools.presented_refs(ModelRequest((changed_id,)))
    forged = Message(
        MessageRole.TOOL,
        (ToolResultBlock("read-original", content=result.content[:-1]),),
        name="read_evidence",
    )
    assert not tools.presented_refs(ModelRequest((forged,)))
    assert await store.knowledge.get_memory("unread") is None
    unread = await invoke(
        tools,
        "revise_memory",
        {"id": "unread", "content": "不能提前写", "sources": [page.slice.slice_ref]},
    )
    assert unread.is_error
    ports.override = b"different-image"
    with pytest.raises(RuntimeError, match="持久引用"):
        await invoke(tools, "read_evidence", {"id": admitted.input_id}, "bad-media")


@pytest.mark.asyncio
async def test_ordinary_final_text_and_failed_generation_do_not_finish(store: Store) -> None:
    page = await source(store, "source")
    assert page.slice is not None
    tools, _, initial = await reflection(store, page)
    backend = Backend(store, tools, (ModelResponse(Message.assistant('{"completed":true}')),))
    conversation = Model(backend, "test").conversation(tools=tools.toolset())
    await AgentDriver(InlineExecutor()).run(conversation.ask(initial), host=None)
    assert (await store.knowledge.get_reflection("batch", 1)).state == "running"
    assert await store.knowledge.pending_reflection()
    failed_page = await source(store, "other")
    failed_tools, _, failed_initial = await reflection(store, failed_page, identity="failed")
    failed_backend = Backend(store, failed_tools, (RuntimeError("model unavailable"),))
    failed_conversation = Model(failed_backend, "test").conversation(tools=failed_tools.toolset())
    with pytest.raises(RuntimeError, match="model unavailable"):
        await AgentDriver(InlineExecutor()).run(failed_conversation.ask(failed_initial), host=None)
    assert failed_conversation.state.turn_index == 0
    assert failed_page.slice is not None
    refused = await invoke(
        failed_tools,
        "revise_memory",
        {"id": "not-presented", "content": "无资格", "sources": [failed_page.slice.slice_ref]},
    )
    assert refused.is_error


@pytest.mark.asyncio
async def test_revision_and_finish_survive_later_model_failure(store: Store) -> None:
    page = await source(store, "source")
    assert page.slice is not None
    tools, _, initial = await reflection(store, page)
    backend = Backend(
        store,
        tools,
        (
            calls(
                ToolCallBlock(
                    "revise",
                    "revise_memory",
                    arguments={"id": "m", "content": "实际修订", "sources": [page.slice.slice_ref]},
                )
            ),
            calls(
                ToolCallBlock(
                    "finish",
                    "finish_reflection",
                    arguments={
                        "reviews": [review(page.slice.slice_ref, decision="written")],
                        "deferred": [],
                        "findings": [],
                    },
                )
            ),
            RuntimeError("final generation failed"),
        ),
    )
    conversation = Model(backend, "test").conversation(tools=tools.toolset())
    with pytest.raises(RuntimeError, match="final generation failed"):
        await AgentDriver(InlineExecutor()).run(conversation.ask(initial), host=None)
    assert conversation.state.turn_index == 0
    assert await store.knowledge.get_memory("m") is not None
    final_result = [
        block
        for message in backend.requests[-1].messages
        for block in message.content
        if isinstance(block, ToolResultBlock) and block.tool_call_id == "finish"
    ]
    assert len(final_result) == 1 and not final_result[0].is_error, final_result
    assert (await store.knowledge.get_reflection("batch", 1)).state == "completed"
    assert not await store.knowledge.pending_reflection()


@pytest.mark.asyncio
async def test_async_store_abandon_keeps_pending_and_blocks_old_generation(store: Store) -> None:
    page = await source(store, "source")
    assert page.slice is not None
    tools, _, initial = await reflection(store, page)
    await store.knowledge.mark_presented("batch", 1, tools.presented_refs(ModelRequest((initial,))))
    written = await store.knowledge.revise_memory(
        "batch",
        1,
        MemoryPatch(id="m", content="实际已写入", sources=(page.slice.slice_ref,), base_version=0),
    )
    interrupted = await store.knowledge.abandon_reflection("batch", 1, "请求预算耗尽")
    assert interrupted.state == "interrupted" and interrupted.interrupted_reason == "请求预算耗尽"
    assert await store.knowledge.get_memory("m") == written
    refused = await invoke(
        tools,
        "revise_memory",
        {"id": "late", "content": "旧资格", "sources": [page.slice.slice_ref]},
    )
    assert refused.is_error and await store.knowledge.pending_reflection()
    assert (await store.knowledge.get_reflection("batch", 1)).state == "interrupted"


@pytest.mark.asyncio
async def test_supervision_is_immediate_and_duplicate_does_not_redeliver(store: Store) -> None:
    page = await source(store, "source")
    assert page.slice is not None
    tools, ports, initial = await reflection(store, page)
    await store.knowledge.mark_presented("batch", 1, tools.presented_refs(ModelRequest((initial,))))
    arguments: dict[str, JsonValue] = {
        "id": "notice",
        "issue": "实际风险",
        "evidence": [page.slice.slice_ref],
        "suggestion": "前台核对事实",
        "level": "urgent",
    }
    assert not (await invoke(tools, "supervise", arguments)).is_error
    assert not (await invoke(tools, "supervise", arguments)).is_error
    assert len(ports.notices) == 1 and ports.notices[0].notice.level == "urgent"
    assert len(await store.knowledge.pending_supervision()) == 1
    assert (await store.knowledge.get_reflection("batch", 1)).state == "running"


@pytest.mark.asyncio
async def test_authority_failure_propagates_instead_of_becoming_input_error(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = await source(store, "source")
    tools, _, _ = await reflection(store, page)

    async def broken(
        identity: str,
        *,
        level: str = "evidence",
        cursor: str | None = None,
        max_chars: int = 8000,
        version: int | None = None,
    ) -> EvidencePage:
        raise StorageError("authoritative failure", code="database_failure")

    monkeypatch.setattr(store.knowledge, "read_evidence", broken)
    with pytest.raises(StorageError, match="authoritative failure"):
        await invoke(tools, "read_evidence", {"id": "source"})


@pytest.mark.asyncio
async def test_memory_expansion_is_derived_and_does_not_authorize_original(store: Store) -> None:
    page = await source(store, "source")
    assert page.slice is not None
    tools, _, initial = await reflection(store, page)
    await store.knowledge.mark_presented("batch", 1, tools.presented_refs(ModelRequest((initial,))))
    await store.knowledge.revise_memory(
        "batch",
        1,
        MemoryPatch(id="m", content="derived认识", sources=(page.slice.slice_ref,), base_version=0),
    )
    result = await invoke(tools, "read_evidence", {"id": "m"}, "derived")
    assert not result.is_error and isinstance(result.content[0], TextBlock)
    assert '"derived": true' in result.content[0].text
    actual = Message(MessageRole.TOOL, (result.to_block("derived"),), name="read_evidence")
    assert not tools.presented_refs(ModelRequest((actual,)))


class Semantic:
    """显式配置的语义路线，在等待边界撤销来源以验证迟到资格。"""

    def __init__(self, store: Store, reference: KnowledgeReference) -> None:
        self.store = store
        self.reference = reference
        self.error: Exception | None = None

    async def retrieve(self, query: str) -> tuple[KnowledgeReference, ...]:
        if self.error is not None:
            raise self.error
        await self.store.knowledge.revoke_evidence(self.reference.id, self.reference.version)
        return (self.reference,)


class ReverseReranker:
    """只排列真实候选，故意在异步间隙撤销其中一个来源。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def rank(
        self, query: str, candidates: tuple[KnowledgeCandidate, ...]
    ) -> tuple[KnowledgeReference, ...]:
        assert len(candidates) == 2
        await self.store.knowledge.revoke_evidence(candidates[-1].reference.id, 1)
        return tuple(item.reference for item in reversed(candidates))


@pytest.mark.asyncio
async def test_optional_routes_preserve_authority_and_report_configured_failure(
    store: Store,
) -> None:
    await source(store, "a", "茶杯偏好")
    await source(store, "b", "茶杯经验")
    reference = KnowledgeReference(kind="evidence", id="b", version=1)
    semantic = Semantic(store, reference)
    service = KnowledgeService(store.knowledge, semantic=semantic)
    result = await service.recall("茶杯")
    assert result.semantic and not result.reranked
    assert tuple(item.reference.id for item in result.items) == ("a",)
    semantic.error = RuntimeError("configured semantic failed")
    with pytest.raises(RuntimeError, match="configured semantic failed"):
        await service.recall("茶杯")
    await source(store, "c", "茶杯后续")
    reranked = await KnowledgeService(store.knowledge, reranker=ReverseReranker(store)).recall(
        "茶杯"
    )
    assert reranked.reranked and len(reranked.items) == 1


@pytest.mark.asyncio
async def test_shared_delivery_tracker_retains_exact_history_across_batches(store: Store) -> None:
    page = await source(store, "source")
    assert page.slice is not None
    shared = EvidenceDeliveries()
    first, _, _ = await reflection(store, page, deliveries=shared)
    result = await invoke(first, "read_evidence", {"id": "source"}, "history-read")
    second, _, _ = await reflection(store, page, identity="another-batch", deliveries=shared)
    history = Message(MessageRole.TOOL, (result.to_block("history-read"),), name="read_evidence")
    assert second.presented_refs(ModelRequest((history,))) == (page.slice.slice_ref,)
    await store.knowledge.mark_presented(
        "another-batch", 1, second.presented_refs(ModelRequest((history,)))
    )
    actual = await store.knowledge.revise_memory(
        "another-batch",
        1,
        MemoryPatch(
            id="history", content="仍来自实际历史", sources=(page.slice.slice_ref,), base_version=0
        ),
    )
    assert actual.active


@pytest.mark.asyncio
async def test_reopened_delivery_tracker_keeps_original_identity_and_requires_new_presentation(
    tmp_path: Path,
) -> None:
    """新进程只从同库原交付恢复；匹配历史仍须成功请求门槛再授予新批资格。"""
    path = tmp_path / "reopened.sqlite3"
    original = await Store.open(path)
    try:
        now = datetime.now(UTC)
        admitted = await original.admit(
            InputSubmission(
                InputSource("test", "account", "chat", "image"),
                "真实图像",
                now,
                now,
                (MediaAsset(b"persistent-image", "image/png"),),
            )
        )
        page = await original.knowledge.read_evidence(admitted.input_id)
        assert page.slice is not None
        first, _, initial = await reflection(original, page)
        result = await invoke(first, "read_evidence", {"id": admitted.input_id}, "original-call")
        history = (
            initial,
            Message(MessageRole.TOOL, (result.to_block("original-call"),), name="read_evidence"),
        )
    finally:
        await original.close()

    reopened = await Store.open(path)
    try:
        recovered = await reopened.knowledge.deliveries_for_history(history)
        assert len(recovered) == 2
        assert {(item.batch_id, item.version) for item in recovered} == {("batch", 1)}
        shared = EvidenceDeliveries()
        shared.restore_history(recovered)
        shared.retain_history(history)
        second, _, new_initial = await reflection(
            reopened, page, identity="new-batch", deliveries=shared
        )
        unread = await invoke(
            second,
            "revise_memory",
            {"id": "new", "content": "来源仍然真实。", "sources": [page.slice.slice_ref]},
        )
        assert unread.is_error
        request = ModelRequest((*history, new_initial))
        await reopened.knowledge.mark_presented("new-batch", 1, second.presented_refs(request))
        written = await invoke(
            second,
            "revise_memory",
            {"id": "new", "content": "来源仍然真实。", "sources": [page.slice.slice_ref]},
        )
        assert not written.is_error
        shared.retain_history((Message.user("summary=" + page.slice.slice_ref),))
        assert shared.presented_refs(request) == ()
        shared.restore_history(await reopened.knowledge.deliveries_for_history(history))
        assert shared.presented_refs(ModelRequest(history)) == (page.slice.slice_ref,)
    finally:
        await reopened.close()
