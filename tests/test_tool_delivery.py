"""以真实 Core、SDK 编译/响应转换及临时 Store 验证工具交付门槛。

仅 CompletionCallable 替代最终模型传输；请求布局、顺序、媒体转换、证据
账本和连续检查点均走生产实现。容量规则覆盖完整 SDK payload，不从字符
页长或远端 usage 推断本地额度，测试不会调用网络或设备。
"""

from __future__ import annotations

import hashlib
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from purrcept_core.models import (
    ContentBlock,
    ImageBlock,
    ImageBytes,
    Message,
    ModelRequest,
    SystemInstruction,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolSet,
)

from purrcept_engine.app.config import RetrySettings
from purrcept_engine.context.capacity import (
    CapacityDeclaration,
    CapacityError,
    CapacityEstimate,
    GenerationCapacity,
    check_capacity,
)
from purrcept_engine.domain.execution.models import ActionRecord, CallOutcome
from purrcept_engine.domain.inputs.models import MediaReference
from purrcept_engine.domain.knowledge.models import (
    EvidenceSnapshot,
    KnowledgeInputError,
    MemoryPatch,
    SupervisionRecord,
    WakeupPlan,
)
from purrcept_engine.domain.subject.models import SessionScope
from purrcept_engine.errors import StorageError
from purrcept_engine.serialization import encode
from purrcept_engine.storage import InputContext, InputSource, InputSubmission, MediaAsset
from purrcept_engine.storage.store import Store
from purrcept_engine.subject.delivery import ToolDeliveryGate
from purrcept_engine.subject.foreground import ForegroundTools
from purrcept_engine.subject.knowledge import KnowledgeService
from purrcept_engine.subject.model_requests import RequestBudget
from purrcept_engine.subject.reflection import ReflectionTools
from purrcept_engine.subject.session import (
    CapacityCheck,
    ContinuousSession,
    DynamicState,
    PresentedGate,
    RefreshState,
    SessionResult,
)
from purrcept_engine.values import JsonObject, JsonValue, decode_object


@pytest_asyncio.fixture
async def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Store]:
    """每项测试持有独立数据库线程，并在断言失败后也等待真实关闭。"""
    # SDK 导入使用安装包本地价格表，不能因测试编译而下载第三方元数据。
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    database = await Store.open(tmp_path / "engine.sqlite3")
    try:
        yield database
    finally:
        await database.close()


class SDKTransport:
    """记录 SDK 传输的完整参数，返回同一协议的合成 Provider 响应。"""

    def __init__(self, responses: tuple[tuple[ToolCallBlock, ...] | str, ...]) -> None:
        self.responses = deque(responses)
        self.payloads: list[JsonObject] = []

    async def __call__(self, **kwargs: object) -> object:
        self.payloads.append(decode_object(encode(kwargs).encode("utf-8")))
        response = self.responses.popleft()
        if isinstance(response, str):
            message: dict[str, object] = {"role": "assistant", "content": response}
            finish = "stop"
        else:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": encode(call.arguments)},
                    }
                    for call in response
                ],
            }
            finish = "tool_calls"
        return {
            "id": f"response-{len(self.payloads)}",
            "model": "synthetic",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        }


class Ports:
    """真实 Store 媒体与合成行动端口；所有状态先提交再返回工具。"""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.actions = 0
        self.failure: RuntimeError | None = None

    async def media_blocks(self, media: tuple[MediaReference, ...]) -> tuple[ContentBlock, ...]:
        if self.failure is not None:
            raise self.failure
        blocks: list[ContentBlock] = []
        for reference in media:
            content = await self.store.get_media(reference.media_id)
            assert content is not None
            blocks.append(ImageBlock(ImageBytes(content, reference.media_type)))
        return tuple(blocks)

    async def play_action(self, source: str) -> ActionRecord:
        self.actions += 1
        await self.store.begin_action("actual-action", catalog_version="catalog", source=source)
        outcomes: tuple[CallOutcome, ...] = ("confirmed", "unknown")
        for sequence, state in enumerate(outcomes):
            identity = f"actual-call-{sequence}"
            await self.store.begin_call(
                identity,
                action_id="actual-action",
                sequence=sequence,
                path="world.actual",
                target_id="actual-device",
                arguments_json="{}",
            )
            await self.store.mark_call_started(identity)
            await self.store.record_call_result(
                identity,
                state=state,
                result_json='{"content":"' + "X" * 60_000 + '"}' if sequence == 0 else None,
                error_type=None,
                error=None,
            )
        await self.store.finish_action(
            "actual-action", state="completed", result_json="null", output="", error=None
        )
        result = await self.store.get_action("actual-action")
        assert result is not None
        return result

    def wakeup_committed(self) -> None:
        pass

    def foreground_generation(self) -> int:
        return 0

    async def supervision_committed(
        self, record: SupervisionRecord, *, observed_generation: int
    ) -> None:
        pass


async def source(store: Store, identity: str, text: str) -> None:
    await store.knowledge.put_evidence(
        EvidenceSnapshot(
            evidence_id=identity,
            version=1,
            kind="actual-test-source",
            text=text,
            source_json='{"source":"test"}',
            occurred_at=10.0,
        )
    )


async def refresh(budget: RequestBudget) -> DynamicState:
    return DynamicState(f"实际动态尾部，已使用请求 {budget.used}")


async def run(
    store: Store,
    tools: ToolSet,
    delivery: ToolDeliveryGate,
    responses: tuple[tuple[ToolCallBlock, ...] | str, ...],
    *,
    scope: SessionScope = "foreground",
    refresh_state: RefreshState = refresh,
    presented: PresentedGate | None = None,
    window: int = 32_000,
    capacity_check: CapacityCheck | None = None,
    turn_id: str = "actual-turn",
    foreground_input: str | None = None,
) -> tuple[SessionResult, SDKTransport]:
    """通过生产会话启动真实 Core 循环，传输和设备资源均由测试局部拥有。"""
    from purrcept_litellm import LiteLLMBackend

    transport = SDKTransport(responses)
    backend = LiteLLMBackend(
        completion=transport,
        default_options={"num_retries": 0, "max_retries": 0, "drop_params": False},
    )
    session = ContinuousSession(store, scope, can_start=lambda: None)
    input_ids: tuple[str, ...] = ()
    if scope == "foreground":
        if foreground_input is None:
            now = datetime.now(UTC)
            admitted = await store.admit(
                InputSubmission(
                    InputSource("test", "test", "test", "text", turn_id),
                    "实际输入",
                    now,
                    now,
                    context=InputContext(minimum_wake=True),
                )
            )
            foreground_input = admitted.input_id
        input_ids = (foreground_input,)
    try:
        result = await session.run(
            Message.user("请核对已存材料。"),
            turn_id=turn_id,
            input_ids=input_ids,
            backend=backend,
            evidence=store,
            model="openai/synthetic",
            instructions=(SystemInstruction.from_text("固定规则与目录必须共同计量。"),),
            tools=tools,
            output_tokens=1000,
            max_requests=8,
            turn_seconds=60,
            retry=RetrySettings(attempts=1),
            diagnostic_bytes=0,
            refresh=refresh_state,
            capacity=capacity_check
            or (
                lambda payload: check_capacity(
                    payload, GenerationCapacity(window, 1000, "reference-text-only-v1")
                )
            ),
            presented=presented,
            delivery=delivery,
        )
        return result, transport
    finally:
        await session.close()


def foreground(store: Store, gate: ToolDeliveryGate, ports: Ports | None = None) -> ForegroundTools:
    return ForegroundTools(
        KnowledgeService(store.knowledge),
        store.knowledge,
        ports or Ports(store),
        check=lambda: None,
        delivery=gate,
    )


def delivered(result: SessionResult) -> tuple[ToolResultBlock, ...]:
    """检查最终权威检查点中的工具内容，不从 Provider 字符串推断覆盖。"""
    return tuple(
        block
        for message in result.checkpoint.history.messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    )


def payload(block: ToolResultBlock) -> JsonObject:
    assert isinstance(block.content[0], TextBlock)
    return decode_object(block.content[0].text.encode("utf-8"))


def obj(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def items(value: JsonValue) -> list[JsonValue]:
    assert isinstance(value, list)
    return value


async def test_oversized_page_can_retry_and_reused_call_id_does_not_rewrite_history(
    store: Store,
) -> None:
    text = "原文前缀：" + "X" * 60_000
    await source(store, "large", text)
    gate = ToolDeliveryGate()
    tools = foreground(store, gate)
    result, transport = await run(
        store,
        tools.tools,
        gate,
        (
            (
                ToolCallBlock(
                    "reused", "read_evidence", arguments={"id": "large", "max_chars": 60000}
                ),
            ),
            (ToolCallBlock("reused", "read_evidence", arguments={"id": "large", "max_chars": 32}),),
            (ToolCallBlock("third", "read_evidence", arguments={"id": "large", "max_chars": 16}),),
            "结束",
        ),
    )
    first, second, _ = delivered(result)
    assert first.is_error and not second.is_error
    assert obj(payload(first)["capacity"])["code"] == "over_capacity"
    assert payload(first)["stored_ref"] == {"id": "large", "version": 1}
    assert obj(payload(first)["retry"])["cursor"] is None
    assert "slice_ref" not in payload(first) and "next_cursor" not in payload(first)
    assert obj(payload(second)["item"])["text"] == text[:32]
    first_delivery = obj(items(transport.payloads[1]["messages"])[3])
    later_delivery = obj(items(transport.payloads[3]["messages"])[3])
    assert first_delivery["content"] == later_delivery["content"]
    assert first_delivery["tool_call_id"] == later_delivery["tool_call_id"] == "reused"
    stored = await store.knowledge.get_evidence("large")
    assert stored is not None and stored.text == text
    requests = await store.requests_for_turn("foreground", "actual-turn")
    assert len(requests) == 4
    for record, actual in zip(requests, transport.payloads, strict=True):
        expected = obj(decode_object(record.request.materials_json.encode("utf-8"))["payload"])
        assert expected["messages"] == actual["messages"]
        assert expected["tools"] == actual["tools"]

    resumed_gate = ToolDeliveryGate()
    resumed_tools = foreground(store, resumed_gate)
    resumed, resumed_transport = await run(
        store,
        resumed_tools.tools,
        resumed_gate,
        ("下一批结束",),
        turn_id="resumed-turn",
    )
    assert resumed.checkpoint.history.turn_index == 2
    assert delivered(resumed) == delivered(result)
    restored_first = obj(items(resumed_transport.payloads[0]["messages"])[3])
    assert restored_first["content"] == first_delivery["content"]


async def test_multiple_real_results_and_fresh_tail_are_measured_as_one_request(
    store: Store,
) -> None:
    await source(store, "first", "A" * 8000)
    await source(store, "second", "B" * 8000)
    gate = ToolDeliveryGate()
    tools = foreground(store, gate)

    async def fresh_tail(budget: RequestBudget) -> DynamicState:
        return DynamicState("F" * (8000 if budget.used else 1))

    result, transport = await run(
        store,
        tools.tools,
        gate,
        (
            (
                ToolCallBlock("one", "read_evidence", arguments={"id": "first"}),
                ToolCallBlock("two", "read_evidence", arguments={"id": "second"}),
            ),
            "结束",
        ),
        refresh_state=fresh_tail,
        window=32_000,
    )
    first, second = delivered(result)
    assert not first.is_error and second.is_error
    assert obj(payload(first)["item"])["text"] == "A" * 8000
    assert payload(second)["stored_ref"] == {"id": "second", "version": 1}
    assessment = check_capacity(
        transport.payloads[1], GenerationCapacity(32000, 1000, "reference-text-only-v1")
    )
    assert assessment.within_budget
    assert "F" * 8000 in encode(transport.payloads[1]["messages"])


async def test_unknown_media_keeps_native_blob_and_does_not_grant_reflection_coverage(
    store: Store,
) -> None:
    await source(store, "initial", "已有材料")
    initial = await store.knowledge.read_evidence("initial")
    assert initial.slice is not None
    batch = await store.knowledge.begin_reflection("batch", (initial.slice.slice_ref,))
    asset = MediaAsset(b"actual-native-pixels", "image/png")
    reference = MediaReference(
        hashlib.sha256(asset.content).hexdigest(), "image/png", len(asset.content)
    )
    await store.knowledge.capture(
        EvidenceSnapshot(
            evidence_id="image",
            version=1,
            kind="native-image",
            text="原生媒体",
            source_json='{"source":"test"}',
            occurred_at=10.0,
            media=(reference,),
        ),
        (asset,),
    )
    gate = ToolDeliveryGate()
    tools = ReflectionTools(store.knowledge, batch, Ports(store), delivery=gate)

    async def presented(request: ModelRequest) -> None:
        await store.knowledge.mark_presented(
            batch.batch_id, batch.version, tools.presented_refs(request)
        )

    result, transport = await run(
        store,
        tools.toolset(),
        gate,
        ((ToolCallBlock("image-read", "read_evidence", arguments={"id": "image"}),), "结束"),
        scope="reflection",
        presented=presented,
    )
    block = delivered(result)[0]
    assert block.is_error and obj(payload(block)["capacity"])["code"] == "unsupported_media"
    assert payload(block)["stored_ref"] == {"id": "image", "version": 1}
    assert await store.get_media(reference.media_id) == asset.content
    assert tools.presented_refs(ModelRequest(result.checkpoint.history.messages)) == ()
    page = await store.knowledge.read_evidence("image")
    assert page.slice is not None
    with pytest.raises(KnowledgeInputError, match=r"呈现|来源"):
        await store.knowledge.revise_memory(
            batch.batch_id,
            batch.version,
            MemoryPatch(id="unread-memory", content="不应写入", sources=(page.slice.slice_ref,)),
        )
    assert "image_url" not in encode(transport.payloads[1]["messages"])


async def test_action_capacity_receipt_preserves_each_actual_effect_and_saved_original(
    store: Store,
) -> None:
    gate = ToolDeliveryGate()
    ports = Ports(store)
    tools = foreground(store, gate, ports)
    result, _ = await run(
        store,
        tools.tools,
        gate,
        ((ToolCallBlock("action", "play_action", arguments={"action_code": "return 1"}),), "结束"),
    )
    block = delivered(result)[0]
    assert not block.is_error
    receipt = payload(block)
    assert receipt["state"] == "completed"
    assert receipt["calls"] == [
        {"call_id": "actual-call-0", "state": "confirmed", "receipt_revision": 1},
        {"call_id": "actual-call-1", "state": "unknown", "receipt_revision": 1},
    ]
    assert obj(receipt["capacity"])["code"] == "over_capacity" and ports.actions == 1
    original = await store.knowledge.get_evidence("action_result:actual-action")
    action = await store.get_action("actual-action")
    assert action is not None and original is not None and original.text == encode(action)
    assert action.source == "return 1" and action.calls[1].state == "unknown"


async def test_internal_media_failure_is_not_a_correctable_capacity_receipt(store: Store) -> None:
    await source(store, "source", "原文")
    gate = ToolDeliveryGate()
    ports = Ports(store)
    ports.failure = RuntimeError("真实媒体内部故障")
    tools = foreground(store, gate, ports)
    with pytest.raises(RuntimeError) as caught:
        await run(
            store,
            tools.tools,
            gate,
            ((ToolCallBlock("read", "read_evidence", arguments={"id": "source"}),), "不会请求"),
        )
    assert caught.value is ports.failure
    assert len(await store.requests_for_turn("foreground", "actual-turn")) == 1


async def test_direct_tools_preserve_registered_parameters_and_explicit_read_version(
    store: Store,
) -> None:
    await source(store, "source", "原文")
    gate = ToolDeliveryGate()
    tools = foreground(store, gate)
    read = await tools.read_evidence("source")
    assert isinstance(read.content[0], TextBlock) and "原文" in read.content[0].text
    specifications = {spec.name: spec for spec in tools.tools.specs}
    schema = decode_object(encode(specifications["read_evidence"].parameters).encode("utf-8"))
    assert set(obj(schema["properties"])) == {"id", "level", "cursor", "max_chars", "version"}
    action = decode_object(encode(specifications["play_action"].parameters).encode("utf-8"))
    assert set(obj(action["properties"])) == {"action_code"}


async def test_capacity_receipt_itself_cannot_hide_an_unusable_existing_request(
    store: Store,
) -> None:
    await source(store, "source", "X" * 60_000)
    gate = ToolDeliveryGate()
    tools = foreground(store, gate)

    async def oversized_tail(budget: RequestBudget) -> DynamicState:
        return DynamicState("Z" * (60_000 if budget.used else 1))

    with pytest.raises(CapacityError) as caught:
        await run(
            store,
            tools.tools,
            gate,
            ((ToolCallBlock("read", "read_evidence", arguments={"id": "source"}),), "不会请求"),
            refresh_state=oversized_tail,
        )
    assert caught.value.code == "over_capacity"
    assert len(await store.requests_for_turn("foreground", "actual-turn")) == 1
    snapshot = await store.knowledge.get_evidence("source")
    assert snapshot is not None and snapshot.text == "X" * 60_000


async def test_native_media_and_adjacent_tool_order_survive_accepted_delivery(store: Store) -> None:
    """媒体实际 SDK 附件与全部相邻工具结果一并计量，Gate 不自行重排协议。"""
    content = b"known-test-native-pixels"
    reference = MediaReference(hashlib.sha256(content).hexdigest(), "image/png", len(content))
    await store.knowledge.capture(
        EvidenceSnapshot(
            evidence_id="native",
            version=1,
            kind="known-test-image",
            text="实际媒体来源",
            source_json='{"source":"test"}',
            occurred_at=10.0,
            media=(reference,),
        ),
        (MediaAsset(content, "image/png"),),
    )
    await source(store, "huge", "X" * 60_000)
    gate = ToolDeliveryGate()
    tools = foreground(store, gate)

    def registered_rule(
        payload: Mapping[str, object], declaration: CapacityDeclaration
    ) -> CapacityEstimate:
        # 测试只声明这一固定语料的完整 JSON 字节参考；原生 data URL 全部
        # 入账，不使用生产 Provider token 公式，也不推广到其它模型或媒体。
        assert declaration.media_rule == "test-native-reference-v1"
        return CapacityEstimate(
            len(encode(payload).encode("utf-8")),
            "test-native-reference-v1",
            "complete_test_sdk_json_with_known_native_media",
            "utf8_byte_reference",
        )

    def capacity(payload: JsonObject):
        return check_capacity(
            payload,
            GenerationCapacity(
                32_000,
                1000,
                "test-native-reference-v1",
                media_rule="test-native-reference-v1",
            ),
            rules={"test-native-reference-v1": registered_rule},
        )

    result, transport = await run(
        store,
        tools.tools,
        gate,
        (
            (
                ToolCallBlock("native", "read_evidence", arguments={"id": "native"}),
                ToolCallBlock(
                    "oversized", "read_evidence", arguments={"id": "huge", "max_chars": 60000}
                ),
            ),
            "结束",
        ),
        capacity_check=capacity,
    )
    first, second = delivered(result)
    assert not first.is_error and second.is_error
    assert isinstance(first.content[1], ImageBlock)
    assert isinstance(first.content[1].source, ImageBytes)
    assert first.content[1].source.data == content
    sdk_messages = items(transport.payloads[1]["messages"])
    tool_positions = [
        index for index, message in enumerate(sdk_messages) if obj(message)["role"] == "tool"
    ]
    assert [obj(sdk_messages[index])["tool_call_id"] for index in tool_positions] == [
        "native",
        "oversized",
    ]
    assert tool_positions[1] == tool_positions[0] + 1
    companion = obj(sdk_messages[tool_positions[-1] + 1])
    assert companion["role"] == "user" and "image_url" in encode(companion)
    assert capacity(transport.payloads[1]).within_budget


async def test_action_snapshot_save_failure_keeps_actual_effect_and_original_error(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不可展开的引用不交付；额外原文保存失败也不能覆盖已发生行动。"""
    gate = ToolDeliveryGate()
    ports = Ports(store)
    tools = foreground(store, gate, ports)
    failure = StorageError("保存真实结果快照失败", code="input_capacity")

    async def reject(
        snapshot: EvidenceSnapshot, *, needs_reflection: bool = True
    ) -> EvidenceSnapshot:
        raise failure

    monkeypatch.setattr(store.knowledge, "put_evidence", reject)
    with pytest.raises(StorageError) as caught:
        await run(
            store,
            tools.tools,
            gate,
            (
                (ToolCallBlock("action", "play_action", arguments={"action_code": "return 1"}),),
                "不会请求",
            ),
        )
    assert caught.value is failure and ports.actions == 1
    action = await store.get_action("actual-action")
    assert action is not None and action.state == "completed"
    assert [call.state for call in action.calls] == ["confirmed", "unknown"]
    assert await store.knowledge.get_evidence("action_result:actual-action") is None
    assert len(await store.requests_for_turn("foreground", "actual-turn")) == 1


async def test_reflection_small_retry_grants_only_the_actually_presented_range(
    store: Store,
) -> None:
    await source(store, "initial", "初始材料")
    initial = await store.knowledge.read_evidence("initial")
    assert initial.slice is not None
    batch = await store.knowledge.begin_reflection("batch", (initial.slice.slice_ref,))
    await source(store, "large", "L" * 60_000)
    gate = ToolDeliveryGate()
    tools = ReflectionTools(store.knowledge, batch, Ports(store), delivery=gate)
    observed: list[tuple[str, ...]] = []

    async def presented(request: ModelRequest) -> None:
        references = tools.presented_refs(request)
        observed.append(references)
        await store.knowledge.mark_presented(batch.batch_id, batch.version, references)

    result, _ = await run(
        store,
        tools.toolset(),
        gate,
        (
            (
                ToolCallBlock(
                    "read", "read_evidence", arguments={"id": "large", "max_chars": 60000}
                ),
            ),
            (ToolCallBlock("read", "read_evidence", arguments={"id": "large", "max_chars": 16}),),
            "结束",
        ),
        scope="reflection",
        presented=presented,
    )
    first, second = delivered(result)
    assert first.is_error and not second.is_error
    small_reference = payload(second)["slice_ref"]
    assert isinstance(small_reference, str)
    assert observed == [(), (), (small_reference,)]
    memory = await store.knowledge.revise_memory(
        batch.batch_id,
        batch.version,
        MemoryPatch(id="actually-read", content="只根据十六字符", sources=(small_reference,)),
    )
    assert memory.version == 1
    full_page = await store.knowledge.read_evidence("large", max_chars=60000)
    assert full_page.slice is not None
    with pytest.raises(KnowledgeInputError, match=r"呈现|来源"):
        await store.knowledge.revise_memory(
            batch.batch_id,
            batch.version,
            MemoryPatch(
                id="not-read", content="不能引用全页", sources=(full_page.slice.slice_ref,)
            ),
        )


class WorkPorts(Ports):
    """同一真实行动包含大读取、平台确认和未知发送，保留输入因果关系。"""

    async def play_action(self, source: str) -> ActionRecord:
        self.actions += 1
        await self.store.begin_action(
            "work-action", catalog_version="catalog", source=source, input_id=self.input_id
        )
        calls: tuple[tuple[str, CallOutcome, str, str | None], ...] = (
            ("long-read", "confirmed", '{"max_chars":60000}', encode({"content": "L" * 60_000})),
            (
                "platform-send",
                "confirmed",
                '{"target_id":"qq-scope","text":"实际发送内容"}',
                '{"message_id":"platform-confirmed-42","status":"confirmed"}',
            ),
            ("unknown-send", "unknown", '{"target_id":"qq-scope","text":"未知发送内容"}', None),
        )
        for sequence, (identity, state, arguments, result) in enumerate(calls):
            await self.store.begin_call(
                identity,
                action_id="work-action",
                sequence=sequence,
                path="world.read" if sequence == 0 else "world.send",
                target_id="platform-instance",
                arguments_json=arguments,
            )
            await self.store.mark_call_started(identity)
            await self.store.record_call_result(
                identity, state=state, result_json=result, error_type=None, error=None
            )
        await self.store.finish_action(
            "work-action", state="completed", result_json="null", output="", error=None
        )
        action = await self.store.get_action("work-action")
        assert action is not None
        return action

    def __init__(self, store: Store, input_id: str) -> None:
        super().__init__(store)
        self.input_id = input_id


async def actual_work_state(store: Store, budget: RequestBudget) -> DynamicState:
    """使用生产 subject_work 查询和明确宿主入口，不自制轻量调用事实。"""
    return DynamicState.from_payload(
        {
            "material_kind": "engine_committed_state",
            "work": await store.subject_work(),
            "plugin_states": {},
            "request_materials": {},
            "budget": {"used_requests": budget.used, "max_requests": budget.max_requests},
        }
    )


def tail_text(sdk: JsonObject) -> str:
    """读取最终 SDK user 尾部；呈现文本不再承担 JSON 存储协议。"""
    last = obj(items(sdk["messages"])[-1])
    assert last["role"] == "user" and isinstance(last["content"], str)
    assert last["content"].startswith("## 宿主当前状态")
    return last["content"]


@pytest.mark.parametrize("full_delivery", (False, True))
async def test_current_tail_does_not_duplicate_or_bypass_frozen_action_delivery(
    store: Store, full_delivery: bool
) -> None:
    """同一大读取/平台确认/未知行动跨轮复用，迟到版本仍在实际请求中可见。"""
    now = datetime.now(UTC)
    admission = await store.admit(
        InputSubmission(
            InputSource("test", "test", "test", "text", "work-source"),
            "仍待核对的实际工作",
            now,
            now,
            context=InputContext(minimum_wake=True),
        )
    )
    await store.knowledge.plan_wakeup(
        WakeupPlan(
            id="unfinished-plan",
            due=time.time() + 1000,
            reason="未完事项仍须保留真实效果",
            evidence=admission.input_id,
        )
    )
    ports = WorkPorts(store, admission.input_id)
    gate = ToolDeliveryGate()
    tools = foreground(store, gate, ports)

    async def current(budget: RequestBudget) -> DynamicState:
        return await actual_work_state(store, budget)

    result, transport = await run(
        store,
        tools.tools,
        gate,
        ((ToolCallBlock("work", "play_action", arguments={"action_code": "actual code"}),), "结束"),
        window=200_000 if full_delivery else 32_000,
        foreground_input=admission.input_id,
        refresh_state=current,
    )
    call_material = tail_text(transport.payloads[1])
    assert call_material.count('"state": > confirmed') == 2
    assert call_material.count('"state": > unknown') == 1
    assert (
        call_material.index('"call_id": > long-read')
        < call_material.index('"call_id": > platform-send')
        < call_material.index('"call_id": > unknown-send')
    )
    assert '"result_json"' not in call_material
    assert call_material.count('"id": > action_result:work-action') == 6
    expected_mode = "full_tool_message" if full_delivery else "reference_only"
    assert call_material.count(f'"mode": > {expected_mode}') == 3
    assert '"arguments_json": > {"target_id":"qq-scope","text":"实际发送内容"}' in call_material
    assert '"target_id": > platform-instance' in call_material
    transmitted = encode(transport.payloads[1]["messages"])
    assert transmitted.count("L" * 60_000) == (2 if full_delivery else 0)
    assert ("platform-confirmed-42" in transmitted) is full_delivery
    original = await store.knowledge.get_evidence("action_result:work-action")
    assert original is not None
    page = await store.knowledge.read_evidence(original.evidence_id, max_chars=len(original.text))
    assert page.text == original.text and "platform-confirmed-42" in page.text
    if not full_delivery:
        instruction = obj(payload(delivered(result)[0])["retry"])["instruction"]
        assert isinstance(instruction, str) and "平台详细字段尚未交付" in instruction
        assert call_material.count('"details_presented": false') == 3

    resumed_gate = ToolDeliveryGate()
    resumed_tools = foreground(store, resumed_gate)
    _, resumed_transport = await run(
        store,
        resumed_tools.tools,
        resumed_gate,
        ("下一批结束",),
        turn_id="work-resumed",
        window=200_000 if full_delivery else 32_000,
        refresh_state=current,
    )
    resumed_calls = tail_text(resumed_transport.payloads[0])
    assert '"result_json"' not in resumed_calls
    assert resumed_calls.count(f'"mode": > {expected_mode}') == 3

    await store.record_call_result(
        "unknown-send",
        state="confirmed",
        result_json='{"message_id":"platform-late-99","status":"confirmed"}',
        error_type=None,
        error=None,
    )
    late_gate = ToolDeliveryGate()
    late_tools = foreground(store, late_gate)
    _, late_transport = await run(
        store,
        late_tools.tools,
        late_gate,
        ("核对迟到事实",),
        turn_id="work-late",
        window=200_000 if full_delivery else 32_000,
        refresh_state=current,
    )
    late_calls = tail_text(late_transport.payloads[0])
    assert '"call_id": > unknown-send' in late_calls
    assert late_calls.count('"state": > confirmed') == 3
    assert '"receipt_revision": 2' in late_calls
    assert late_calls.count('"result_json":') == 1
    assert '"result_json": > {"message_id":"platform-late-99","status":"confirmed"}' in late_calls
    assert late_calls.count('"result_delivery":') == 2
    assert "platform-late-99" in encode(late_transport.payloads[0]["messages"])
    assert ports.actions == 1

    if not full_delivery:
        # 真实 bridge 之后才有最新调用原文；旧 action_result 仍固定在未知版。
        await store.feedback.bridge(now=time.time())
        receipt_page = await store.knowledge.read_evidence("call:unknown-send:2")
        assert receipt_page.next_cursor is None and "platform-late-99" in receipt_page.text
        receipt_gate = ToolDeliveryGate()
        receipt_tools = foreground(store, receipt_gate)
        _, receipt_transport = await run(
            store,
            receipt_tools.tools,
            receipt_gate,
            (
                (
                    ToolCallBlock(
                        "late-receipt", "read_evidence", arguments={"id": "call:unknown-send:2"}
                    ),
                ),
                "已读最新原文",
            ),
            turn_id="work-receipt",
            refresh_state=current,
        )
        before = tail_text(receipt_transport.payloads[0])
        after = tail_text(receipt_transport.payloads[1])
        assert '"result_json": > {"message_id":"platform-late-99","status":"confirmed"}' in before
        assert '"result_json"' not in after and '"receipt_revision": 2' in after
        assert '"id": > call:unknown-send:2' in after and '"version": 1' in after
        assert after.count('"mode": > full_tool_message') == 1
        assert after.count('"mode": > reference_only') == 2
        assert "platform-late-99" in encode(receipt_transport.payloads[1]["messages"])
