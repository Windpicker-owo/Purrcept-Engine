"""真实 Store 和 Core 工具循环验证反思呈现、修订与完成提交边界。

响应脚本只替代模型服务，工具解析、会话循环和唯一数据库线程均使用真实
实现。不根据返回文字判断等价，也不访问真实用户资料或发送模型请求。
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

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
    ToolCallBlock,
    ToolResultBlock,
)
from purrcept_core.models.tools import InvokeTool, ToolResult

from purrcept_engine.domain.evidence.models import (
    EvidencePage,
    EvidenceSnapshot,
    SupervisionRecord,
)
from purrcept_engine.domain.inputs.models import (
    InputSource,
    InputSubmission,
    MediaAsset,
    MediaReference,
)
from purrcept_engine.domain.subject.reflection_tools import (
    EvidenceDeliveries,
    ReflectionTools,
)
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.values import JsonValue


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
    await store.evidence.put_evidence(
        EvidenceSnapshot(
            evidence_id=identity,
            version=1,
            kind="test",
            text=text,
            source_json='{"plugin":"test"}',
            occurred_at=10.0,
        )
    )
    return await store.evidence.read_evidence(identity)


async def reflection(
    store: Store,
    page: EvidencePage,
    *,
    identity: str = "batch",
    deliveries: EvidenceDeliveries | None = None,
    purpose: Literal["behavior"] = "behavior",
) -> tuple[ReflectionTools, Ports, Message]:
    assert page.slice is not None
    batch = await store.evidence.begin_reflection(
        identity, (page.slice.slice_ref,), purpose=purpose
    )
    ports = Ports(store)
    tools = ReflectionTools(store.evidence, batch, ports, deliveries=deliveries)
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
        await self.store.evidence.mark_presented(
            batch.batch_id, batch.version, self.tools.presented_refs(request)
        )
        return response


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
    page = await store.evidence.read_evidence(admitted.input_id)
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
    unread = await invoke(
        tools,
        "supervise",
        {
            "id": "unread",
            "issue": "核对原文",
            "suggestion": "不能提前下结论",
            "evidence": [page.slice.slice_ref],
            "scope": {"adapter_id": "test"},
            "expected_change": "读后再判断",
        },
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
    assert (await store.evidence.get_reflection("batch", 1)).state == "running"
    assert await store.evidence.pending_review()
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
        "supervise",
        {
            "issue": "核对原文",
            "scope": {"adapter_id": "test"},
            "expected_change": "依据证据判断",
            "id": "not-presented",
            "suggestion": "无资格",
            "evidence": [failed_page.slice.slice_ref],
        },
    )
    assert refused.is_error


@pytest.mark.asyncio
async def test_supervision_is_immediate_and_duplicate_does_not_redeliver(store: Store) -> None:
    page = await source(store, "source")
    assert page.slice is not None
    tools, ports, initial = await reflection(store, page, purpose="behavior")
    await store.evidence.mark_presented("batch", 1, tools.presented_refs(ModelRequest((initial,))))
    arguments: dict[str, JsonValue] = {
        "id": "notice",
        "issue": "实际风险",
        "evidence": [page.slice.slice_ref],
        "suggestion": "前台核对事实",
        "level": "urgent",
        "scope": {"adapter_id": "test"},
        "expected_change": "前台核对事实。",
    }
    assert not (await invoke(tools, "supervise", arguments)).is_error
    assert not (await invoke(tools, "supervise", arguments)).is_error
    assert len(ports.notices) == 1 and ports.notices[0].notice.level == "urgent"
    assert len(await store.evidence.pending_supervision()) == 1
    assert (await store.evidence.get_reflection("batch", 1)).state == "running"


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

    monkeypatch.setattr(store.evidence, "read_evidence", broken)
    with pytest.raises(StorageError, match="authoritative failure"):
        await invoke(tools, "read_evidence", {"id": "source"})


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
        page = await original.evidence.read_evidence(admitted.input_id)
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
        recovered = await reopened.evidence.deliveries_for_history(history)
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
            "supervise",
            {
                "issue": "核对原文",
                "scope": {"adapter_id": "test"},
                "expected_change": "依据证据判断",
                "id": "new",
                "suggestion": "来源仍然真实。",
                "evidence": [page.slice.slice_ref],
            },
        )
        assert unread.is_error
        request = ModelRequest((*history, new_initial))
        await reopened.evidence.mark_presented("new-batch", 1, second.presented_refs(request))
        written = await invoke(
            second,
            "supervise",
            {
                "issue": "核对原文",
                "scope": {"adapter_id": "test"},
                "expected_change": "依据证据判断",
                "id": "new",
                "suggestion": "来源仍然真实。",
                "evidence": [page.slice.slice_ref],
            },
        )
        assert not written.is_error
        shared.retain_history((Message.user("summary=" + page.slice.slice_ref),))
        assert shared.presented_refs(request) == ()
        shared.restore_history(await reopened.evidence.deliveries_for_history(history))
        assert shared.presented_refs(ModelRequest(history)) == (page.slice.slice_ref,)
    finally:
        await reopened.close()
