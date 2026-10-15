"""真实 Core 工具、临时 SQLite 与重开验证未决认识的模型读写闭环。

合成响应只替代模型传输；认识修订、参数 schema、来源资格、版本查询和
双向关系使用生产入口。关系不能裁决双方或将派生认识冒充已读原文。
"""

from __future__ import annotations

import sqlite3
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import pytest
import pytest_asyncio
from purrcept_core import AgentDriver, ExecutionContext, InlineExecutor
from purrcept_core.models import (
    ContentBlock,
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
from purrcept_core.models.tools import InvokeTool, ToolResult, ToolSet

from purrcept_engine.domain.execution.models import ActionRecord
from purrcept_engine.domain.inputs.models import MediaReference
from purrcept_engine.domain.knowledge.models import (
    EvidencePage,
    EvidenceSnapshot,
    MemoryConflictTarget,
    MemoryPatch,
    MemoryRecord,
    SupervisionRecord,
)
from purrcept_engine.infrastructure.sqlite import knowledge as ledger
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.subject.delivery import evidence_fallback
from purrcept_engine.subject.foreground import ForegroundTools
from purrcept_engine.subject.knowledge import KnowledgeService
from purrcept_engine.subject.reflection import ReflectionTools
from purrcept_engine.values import JsonObject, JsonValue, decode_object, encode_json


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    """每个场景使用隔离数据库，失败也等待唯一线程退出。"""
    database = await Store.open(tmp_path / "engine.sqlite3")
    try:
        yield database
    finally:
        await database.close()


class Ports:
    """认识场景不借用设备；任何意外世界效果直接令测试失败。"""

    async def play_action(self, source: str) -> ActionRecord:
        """本组场景不允许产生世界行动。"""
        raise AssertionError("unexpected world action")

    async def media_blocks(self, media: tuple[MediaReference, ...]) -> tuple[ContentBlock, ...]:
        """文本证据没有原生媒体，也不允许静默省略媒体。"""
        assert not media
        return ()

    def wakeup_committed(self) -> None:
        """本组场景不允许创建唤起计划。"""
        raise AssertionError("unexpected wakeup")

    def foreground_generation(self) -> int:
        """认识关系不触发世界控制或紧急抢占。"""
        return 0

    async def supervision_committed(
        self, record: SupervisionRecord, *, observed_generation: int
    ) -> None:
        """本组场景不允许伪造监管效果。"""
        raise AssertionError("unexpected supervision")


async def source(database: Store, identity: str, text: str) -> EvidencePage:
    """先保存来源，再由真实读取入口创建不可变片段。"""
    await database.knowledge.put_evidence(
        EvidenceSnapshot(
            evidence_id=identity,
            version=1,
            kind="test",
            text=text,
            source_json='{"adapter":"test"}',
            occurred_at=10.0,
        )
    )
    return await database.knowledge.read_evidence(identity)


async def reflection(
    database: Store, page: EvidencePage, identity: str
) -> tuple[ReflectionTools, Message]:
    """真实初始材料仅登记候选，仍须在实际请求门槛确认。"""
    assert page.slice is not None
    batch = await database.knowledge.begin_reflection(identity, (page.slice.slice_ref,))
    tools = ReflectionTools(database.knowledge, batch, Ports())
    return tools, await tools.initial_message((page,))


async def presented(database: Store, tools: ReflectionTools, message: Message) -> None:
    """对照确切请求值确认本批原文，不能仅凭片段身份授予资格。"""
    await database.knowledge.mark_presented(
        tools.batch.batch_id,
        tools.batch.version,
        tools.presented_refs(ModelRequest((message,))),
    )


async def peer(database: Store, *, valid_until: float | None = None) -> MemoryRecord:
    """旧认识来自另一批原文，后续冲突声明不会自动看见其来源。"""
    page = await source(database, "peer-original", "旧偏好的原文反证")
    tools, initial = await reflection(database, page, "peer-batch")
    await presented(database, tools, initial)
    assert page.slice is not None
    return await database.knowledge.revise_memory(
        tools.batch.batch_id,
        tools.batch.version,
        MemoryPatch(
            id="peer",
            content="旧认识的完整反证正文，后续修订不得混入历史引用。",
            sources=(page.slice.slice_ref,),
            valid_until=valid_until,
        ),
    )


async def invoke(
    toolset: ToolSet,
    name: str,
    arguments: Mapping[str, JsonValue],
    identity: str = "call",
) -> ToolResult:
    """由真实 FunctionTool 解析参数并注入可信调用身份。"""
    call = ToolCallBlock(identity, name, arguments=arguments)
    return await InvokeTool(toolset.get(name), call).execute(
        ExecutionContext("run", "effect", 0, host=None)
    )


def obj(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def payload(result: ToolResult | ToolResultBlock) -> JsonObject:
    assert isinstance(result.content[0], TextBlock)
    return decode_object(result.content[0].text.encode("utf-8"))


def calls(*blocks: ToolCallBlock) -> ModelResponse:
    return ModelResponse(Message(MessageRole.ASSISTANT, tuple(blocks)))


class Backend:
    """合成响应使用真实请求的已读门槛，然后交给 Core 的原工具循环。"""

    def __init__(
        self, database: Store, tools: ReflectionTools, responses: tuple[ModelResponse, ...]
    ) -> None:
        self.database = database
        self.tools = tools
        self.responses = deque(responses)
        self.requests: list[ModelRequest] = []

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        """保存本次实际模型输入；不依据未来工具结果提前授予资格。"""
        self.requests.append(request)
        batch = self.tools.batch
        await self.database.knowledge.mark_presented(
            batch.batch_id, batch.version, self.tools.presented_refs(request)
        )
        return self.responses.popleft()


def result_for(request: ModelRequest, identity: str) -> ToolResultBlock:
    results = tuple(
        block
        for message in request.messages
        for block in message.content
        if isinstance(block, ToolResultBlock) and block.tool_call_id == identity
    )
    assert len(results) == 1
    return results[0]


@pytest.mark.asyncio
async def test_core_conflict_revision_recall_and_read_survive_reopen(
    store: Store, tmp_path: Path
) -> None:
    old = await peer(store)
    page = await source(store, "new-original", "本批保留独立的相关新材料")
    assert page.slice is not None
    tools, initial = await reflection(store, page, "new-batch")
    backend = Backend(
        store,
        tools,
        (
            calls(
                ToolCallBlock(
                    "revision",
                    "revise_memory",
                    arguments={
                        "id": "new-memory",
                        "content": "exclusive-new-fact：保留尚未解决的新认识。",
                        "sources": [page.slice.slice_ref],
                        "conflicts": [{"kind": "memory", "id": "peer", "version": 1}],
                    },
                )
            ),
            calls(ToolCallBlock("recall", "recall", arguments={"query": "exclusive-new-fact"})),
            calls(ToolCallBlock("read", "read_evidence", arguments={"id": "new-memory"})),
            calls(
                ToolCallBlock(
                    "finish",
                    "finish_reflection",
                    arguments={
                        "reviews": [
                            {
                                "issue": "未决认识",
                                "evidence": [page.slice.slice_ref],
                                "conclusion": "新认识与旧版本反证并列保留。",
                                "memory_decision": "written",
                                "reason": "新认识及未决关系已真实提交。",
                            }
                        ],
                        "deferred": [],
                        "findings": [],
                    },
                )
            ),
            ModelResponse(Message.assistant("内部复核结束。")),
        ),
    )
    conversation = Model(backend, "test").conversation(tools=tools.toolset(), max_model_rounds=8)
    await AgentDriver(InlineExecutor()).run(conversation.ask(initial), host=None)

    revision = result_for(backend.requests[1], "revision")
    assert not revision.is_error
    relation: JsonObject = {
        "kind": "memory",
        "id": "peer",
        "version": 1,
        "status": "unresolved",
        "active": True,
        "inactive_reason": None,
    }
    recall_result = payload(result_for(backend.requests[2], "recall"))
    items = recall_result["items"]
    assert isinstance(items, list) and len(items) == 1
    assert obj(items[0])["id"] == "new-memory"
    assert obj(items[0])["conflicts"] == [relation]
    read_result = payload(result_for(backend.requests[3], "read"))
    assert obj(read_result["item"])["conflicts"] == [relation]
    assert old.patch.content not in str(read_result)
    assert read_result["derived"] is True and read_result["slice_ref"] is None
    assert old.patch.sources[0] not in tools.presented_refs(backend.requests[3])
    counterpart = await store.knowledge.get_memory("peer")
    assert counterpart is not None and counterpart.active
    assert counterpart.conflicts[0].id == "new-memory"
    assert counterpart.conflicts[0].version == 1
    assert counterpart.conflicts[0].status == "unresolved"
    batch = await store.knowledge.get_reflection("new-batch", 1)
    assert batch.receipt is not None and batch.state == "completed"

    await store.close()
    reopened = await Store.open(tmp_path / "engine.sqlite3")
    try:
        current = await reopened.knowledge.get_memory("new-memory")
        other = await reopened.knowledge.get_memory("peer")
        assert current is not None and other is not None
        assert current.active and other.active
        assert current.conflicts[0].model_dump(mode="json") == relation
        assert other.conflicts[0].id == "new-memory"
    finally:
        await reopened.close()


async def conflicted(database: Store) -> tuple[ReflectionTools, MemoryRecord, MemoryRecord]:
    """使用生产反思修订入口建立关系，返回后续版本与资格测试的准备状态。"""
    old = await peer(database)
    page = await source(database, "new-original", "本批原文与旧来源独立。")
    tools, initial = await reflection(database, page, "new-batch")
    await presented(database, tools, initial)
    assert page.slice is not None
    result = await invoke(
        tools.toolset(),
        "revise_memory",
        {
            "id": "new-memory",
            "content": "exclusive-new-fact：未决认识。",
            "sources": [page.slice.slice_ref],
            "conflicts": [{"kind": "memory", "id": "peer", "version": 1}],
        },
    )
    assert not result.is_error
    current = await database.knowledge.get_memory("new-memory")
    assert current is not None
    return tools, old, current


@pytest.mark.asyncio
@pytest.mark.parametrize("foreground", [False, True])
async def test_exact_historical_peer_version_and_cursor_are_model_accessible(
    store: Store, foreground: bool
) -> None:
    reflection_tools, old, current = await conflicted(store)
    await store.knowledge.revise_memory(
        "new-batch",
        1,
        MemoryPatch(
            id="peer",
            content="另一版本的较新认识正文。",
            base_version=1,
            sources=current.patch.sources,
        ),
    )
    toolset = (
        ForegroundTools(
            KnowledgeService(store.knowledge), store.knowledge, Ports(), check=lambda: None
        ).tools
        if foreground
        else reflection_tools.toolset()
    )
    retrieved = payload(await invoke(toolset, "recall", {"query": "exclusive-new-fact"}, "recall"))[
        "items"
    ]
    assert isinstance(retrieved, list) and len(retrieved) == 1
    assert obj(retrieved[0])["conflicts"] == [
        {
            "kind": "memory",
            "id": "peer",
            "version": 1,
            "status": "unresolved",
            "active": False,
            "inactive_reason": "revised",
        }
    ]
    latest = payload(await invoke(toolset, "read_evidence", {"id": "peer"}, "latest"))
    assert obj(latest["item"])["version"] == 2
    first = await invoke(
        toolset, "read_evidence", {"id": "peer", "version": 1, "max_chars": 5}, "historical"
    )
    first_payload = payload(first)
    assert obj(first_payload["item"])["text"] == old.patch.content[:5]
    assert obj(first_payload["item"])["version"] == 1
    assert obj(first_payload["item"])["active"] is False
    cursor = first_payload["next_cursor"]
    assert isinstance(cursor, str)
    # 首读后再次修订，续读仍绑定原历史正文；模型不能用版本参数改指新材料。
    await store.knowledge.revise_memory(
        "new-batch",
        1,
        MemoryPatch(
            id="peer", content="第三版正文。", base_version=2, sources=current.patch.sources
        ),
    )
    second = await invoke(
        toolset,
        "read_evidence",
        {"id": "peer", "cursor": cursor, "version": 1, "max_chars": 5},
        "continued",
    )
    assert obj(payload(second)["item"])["text"] == old.patch.content[5:10]
    mismatch = await invoke(
        toolset,
        "read_evidence",
        {"id": "peer", "cursor": cursor, "version": 2},
        "wrong-version",
    )
    assert mismatch.is_error
    if not foreground:
        request = ModelRequest(
            (Message(MessageRole.TOOL, (first.to_block("historical"),), name="read_evidence"),)
        )
        assert not reflection_tools.presented_refs(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conflicts,supersedes",
    [
        ([{"kind": "memory", "id": "missing", "version": 1}], None),
        ([{"kind": "memory", "id": "peer", "version": 2}], None),
        ([{"kind": "evidence", "id": "peer-original", "version": 1}], None),
        ([{"kind": "memory", "id": "rejected", "version": 1}], None),
        (
            [
                {"kind": "memory", "id": "peer", "version": 1},
                {"kind": "memory", "id": "peer", "version": 1},
            ],
            None,
        ),
        ([{"kind": "memory", "id": "peer", "version": True}], None),
        ([{"kind": "memory", "id": "peer", "version": 1}], "peer"),
    ],
)
async def test_invalid_conflict_target_rejects_entire_model_revision(
    store: Store, conflicts: list[JsonObject], supersedes: str | None
) -> None:
    await peer(store)
    page = await source(store, "new-original", "本批原文。")
    tools, initial = await reflection(store, page, "new-batch")
    await presented(store, tools, initial)
    assert page.slice is not None
    before = await store.knowledge.pending_index()
    result = await invoke(
        tools.toolset(),
        "revise_memory",
        {
            "id": "rejected",
            "content": "不得部分提交的提案。",
            "sources": [page.slice.slice_ref],
            "conflicts": list(conflicts),
            "supersedes": supersedes,
        },
    )
    assert result.is_error
    assert await store.knowledge.get_memory("rejected", include_inactive=True) is None
    assert await store.knowledge.pending_index() == before
    other = await store.knowledge.get_memory("peer")
    assert other is not None and other.active and not other.conflicts


@pytest.mark.asyncio
async def test_relation_and_derived_read_do_not_authorize_counterparty_sources(
    store: Store,
) -> None:
    tools, old, _ = await conflicted(store)
    derived = await invoke(tools.toolset(), "read_evidence", {"id": "peer"}, "derived")
    assert not derived.is_error
    actual = Message(MessageRole.TOOL, (derived.to_block("derived"),), name="read_evidence")
    await presented(store, tools, actual)
    result = await invoke(
        tools.toolset(),
        "revise_memory",
        {"id": "unread", "content": "不能冒充已读。", "sources": list(old.patch.sources)},
    )
    assert result.is_error
    assert await store.knowledge.get_memory("unread") is None


@pytest.mark.asyncio
async def test_expired_target_cannot_enter_a_new_model_relation(store: Store) -> None:
    old = await peer(store, valid_until=time.time() - 1)
    assert not old.active and old.inactive_reason == "expired"
    page = await source(store, "new-original", "本批仍有效的独立材料。")
    tools, initial = await reflection(store, page, "new-batch")
    await presented(store, tools, initial)
    assert page.slice is not None
    result = await invoke(
        tools.toolset(),
        "revise_memory",
        {
            "id": "rejected",
            "content": "不能借用已经失效的反证版本。",
            "sources": [page.slice.slice_ref],
            "conflicts": [{"kind": "memory", "id": "peer", "version": 1}],
        },
    )
    assert result.is_error and await store.knowledge.get_memory("rejected") is None
    historical = await store.knowledge.get_memory("peer", version=1, include_inactive=True)
    assert historical is not None and historical.inactive_reason == "expired"
    assert not historical.conflicts


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["revised", "superseded", "source_revoked", "expired"])
async def test_inactive_peer_keeps_unresolved_exact_reference(store: Store, reason: str) -> None:
    expires = time.time() + 60
    old = await peer(store, valid_until=expires)
    page = await source(store, "new-original", "本批原文。")
    tools, initial = await reflection(store, page, "new-batch")
    await presented(store, tools, initial)
    assert page.slice is not None
    current = await store.knowledge.revise_memory(
        "new-batch",
        1,
        MemoryPatch(
            id="new-memory",
            content="exclusive-new-fact。",
            sources=(page.slice.slice_ref,),
            conflicts=(MemoryConflictTarget(kind="memory", id="peer", version=1),),
        ),
    )
    if reason == "revised":
        await store.knowledge.revise_memory(
            "new-batch",
            1,
            MemoryPatch(
                id="peer", base_version=1, content="修订正文。", sources=current.patch.sources
            ),
        )
    elif reason == "superseded":
        await store.knowledge.revise_memory(
            "new-batch",
            1,
            MemoryPatch(
                id="replacement",
                content="明确替代。",
                sources=current.patch.sources,
                supersedes="peer",
            ),
        )
    elif reason == "source_revoked":
        assert old.patch.sources
        await store.knowledge.revoke_evidence("peer-original", 1)
    result = await KnowledgeService(store.knowledge).recall(
        "exclusive-new-fact", at=expires + 1 if reason == "expired" else None
    )
    assert len(result.items) == 1
    memory = result.items[0].record
    assert isinstance(memory, MemoryRecord) and memory.active
    relation = memory.conflicts[0]
    assert relation.status == "unresolved" and relation.id == "peer" and relation.version == 1
    assert not relation.active and relation.inactive_reason == reason
    # 已保存关系保留历史；失效目标不能被新的关系提案借用或改指其他版本。
    if reason != "expired":
        rejected = await invoke(
            tools.toolset(),
            "revise_memory",
            {
                "id": "rejected",
                "content": "迟到关系。",
                "sources": [page.slice.slice_ref],
                "conflicts": [{"kind": "memory", "id": "peer", "version": 1}],
            },
        )
        assert rejected.is_error and await store.knowledge.get_memory("rejected") is None


@pytest.mark.asyncio
async def test_model_schema_and_capacity_retry_preserve_exact_version(store: Store) -> None:
    tools, _, _ = await conflicted(store)
    specs = {spec.name: spec for spec in tools.toolset().specs}
    revision_schema = decode_object(encode_json(specs["revise_memory"].parameters))
    properties = obj(revision_schema["properties"])
    assert "conflicts" in properties
    definitions = obj(revision_schema["$defs"])
    target_properties = obj(obj(definitions["MemoryConflictTarget"])["properties"])
    assert obj(target_properties["kind"])["const"] == "memory"
    assert obj(target_properties["version"])["minimum"] == 1
    read_properties = obj(
        decode_object(encode_json(specs["read_evidence"].parameters))["properties"]
    )
    assert "version" in read_properties and "context" not in read_properties
    page = await store.knowledge.read_evidence("peer", version=1, max_chars=5)
    retry = obj(decode_object(encode_json(evidence_fallback(page, cursor=None)))["retry"])
    assert retry["id"] == "peer" and retry["version"] == 1 and retry["cursor"] is None
    foreground = ForegroundTools(
        KnowledgeService(store.knowledge), store.knowledge, Ports(), check=lambda: None
    )
    for version in (True, 0, -1, "1"):
        for toolset in (tools.toolset(), foreground.tools):
            invalid = await invoke(toolset, "read_evidence", {"id": "peer", "version": version})
            assert invalid.is_error


def test_relation_failure_rolls_back_version_replacement_sources_indexes_and_receipt(
    tmp_path: Path,
) -> None:
    """在关系插入阶段注入 SQLite 错误，验证真实事务全部恢复到写入前。"""
    connection = sqlite3.connect(tmp_path / "atomic.sqlite3")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE media(media_id TEXT PRIMARY KEY, content BLOB NOT NULL)")
        for statement in ledger.SCHEMA:
            connection.execute(statement)
        original = EvidenceSnapshot(
            evidence_id="original",
            version=1,
            kind="test",
            text="真实测试原文。",
            source_json="{}",
            occurred_at=10.0,
        )
        ledger.put_evidence(connection, original)
        page = ledger.read_evidence(connection, "original")
        assert page.slice is not None
        batch = ledger.begin_reflection(connection, "batch", (page.slice.slice_ref,))
        ledger.mark_presented(connection, "batch", batch.version, (page.slice.slice_ref,))
        for identity in ("old", "peer"):
            ledger.revise_memory(
                connection,
                "batch",
                batch.version,
                MemoryPatch(id=identity, content="现有认识。", sources=(page.slice.slice_ref,)),
            )
        counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "memories",
                "memory_sources",
                "pending_knowledge_index",
                "reflection_writes",
            )
        )
        connection.execute(
            "CREATE TRIGGER fail_relation BEFORE INSERT ON memory_conflicts "
            "BEGIN SELECT RAISE(ABORT,'injected relation failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected relation failure"):
            ledger.revise_memory(
                connection,
                "batch",
                batch.version,
                MemoryPatch(
                    id="new",
                    content="事务中失败的提案。",
                    sources=(page.slice.slice_ref,),
                    supersedes="old",
                    conflicts=(MemoryConflictTarget(kind="memory", id="peer", version=1),),
                ),
            )
        assert ledger.get_memory(connection, "new", include_inactive=True) is None
        old = ledger.get_memory(connection, "old")
        assert old is not None and old.active
        assert counts == tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "memories",
                "memory_sources",
                "pending_knowledge_index",
                "reflection_writes",
            )
        )
        assert connection.execute("SELECT COUNT(*) FROM memory_conflicts").fetchone() == (0,)
    finally:
        connection.close()
