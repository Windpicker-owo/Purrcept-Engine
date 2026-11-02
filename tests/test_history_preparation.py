"""在实际连续会话入口核对完整请求预览、idle安装和准备失败结案。"""

import time
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
    ReminderState,
    SystemInstruction,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolSet,
)

from purrcept_engine.app.config import ContextSettings, RetrySettings
from purrcept_engine.domain.context.capacity import (
    CapacityAssessment,
    CapacityError,
    GenerationCapacity,
    assess_capacity,
    check_capacity,
)
from purrcept_engine.domain.context.checkpoints import decode_checkpoint, encode_checkpoint
from purrcept_engine.domain.context.history import HistoryMaintenance
from purrcept_engine.domain.context.history_preparation import HistoryPreparation, preview_request
from purrcept_engine.domain.inputs.models import InputContext, InputSource, InputSubmission
from purrcept_engine.domain.knowledge.models import EvidenceSnapshot, WakeupPlan
from purrcept_engine.domain.model_requests.service import RequestBudget
from purrcept_engine.domain.subject.models import SessionRecord
from purrcept_engine.domain.subject.session import ContinuousSession, DynamicState
from purrcept_engine.infrastructure.models.compiler import compile_materials
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.serialization import encode
from purrcept_engine.values import JsonObject, decode_object


class Backend:
    """响应脚本按用途记录实际请求，预览不能调用此入口。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[ModelRequest] = []

    async def generate(
        self,
        request: ModelRequest,
        *,
        model: str,
        emit: ModelEventSink | None = None,
    ) -> ModelResponse:
        """返回一个完整内部响应，不执行网络。"""
        self.requests.append(request)
        return ModelResponse(Message.assistant(self.text))


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    """即使断言失败也收回唯一数据库线程。"""
    instance = await Store.open(tmp_path / "history.sqlite3")
    try:
        yield instance
    finally:
        await instance.close()


async def admit(store: Store, identity: str, text: str) -> str:
    """创建进入前台的隔离输入，原文和认识来源同事务保存。"""
    now = datetime.now(UTC)
    admission = await store.admit(
        InputSubmission(
            InputSource("test", "a", "c", "text", identity),
            text,
            now,
            now,
            context=InputContext(minimum_wake=True),
        )
    )
    return admission.input_id


def assess(payload: JsonObject):
    """允许旧完整请求超额供摘要维护，保持同一参考计量分母。"""
    return assess_capacity(payload, GenerationCapacity(8_000, 100))


def check(payload: JsonObject):
    """真实派发仍核完整输入，不借摘要放宽输出预留或容量门槛。"""
    return check_capacity(payload, GenerationCapacity(8_000, 100))


async def refresh(budget: RequestBudget) -> DynamicState:
    """冻结相同动态材料使前后布局可比较，不注册额外任务。"""
    return DynamicState("已提交来源状态与当前预算")


async def test_history_preview_uses_actual_tool_tail_projection(store: Store) -> None:
    """摘要准备与真实发送共用完整已交付事实，不计两份相同大正文。"""
    identity = await admit(store, "source", "实际来源")
    await store.begin_action(
        "action", catalog_version="catalog", source="return 1", input_id=identity
    )
    await store.begin_call(
        "call",
        action_id="action",
        sequence=0,
        path="me.browser.read",
        target_id="browser",
        arguments_json="{}",
    )
    await store.mark_call_started("call")
    body = "原文标记：" + "x" * 16000
    await store.record_call_result(
        "call", state="confirmed", result_json=encode({"body": body}), error_type=None, error=None
    )
    await store.finish_action("action", state="completed", result_json=None, output="", error=None)
    action = await store.get_action("action")
    assert action is not None and action.finished_at is not None
    await store.knowledge.put_evidence(
        EvidenceSnapshot(
            evidence_id="immediate",
            version=1,
            kind="action_result",
            text=encode(action),
            source_json=encode({"source": "actual_action_result", "action_id": "action"}),
            occurred_at=action.finished_at,
        ),
        needs_reflection=False,
    )
    await store.knowledge.plan_wakeup(
        WakeupPlan(id="plan", due=time.time() + 3600, reason="后续处理未完", evidence="immediate")
    )
    checkpoint = ConversationCheckpoint(
        ConversationState(
            (
                Message.user("实际来源", metadata={"evidence": (identity,)}),
                Message(
                    MessageRole.ASSISTANT,
                    (ToolCallBlock("tool", "play_action", arguments={"action_code": "return 1"}),),
                ),
                Message(
                    MessageRole.TOOL,
                    (ToolResultBlock("tool", content=(TextBlock(encode(action)),)),),
                    name="play_action",
                ),
                Message.assistant("内部结束"),
            ),
            turn_index=1,
            turn_boundaries=(4,),
        ),
        ReminderState(),
    )
    turn = await store.begin_turn("old", "foreground", 0, 1, (identity,))
    await store.finish_turn(turn, "succeeded", checkpoint_json=encode_checkpoint(checkpoint))
    captured: list[JsonObject] = []

    def measure(payload: JsonObject) -> CapacityAssessment:
        captured.append(payload)
        return assess_capacity(payload, GenerationCapacity(100_000, 100))

    backend = Backend("不应调用")
    maintenance = HistoryMaintenance(
        store,
        store,
        backend,
        model="openai/summary",
        retry=RetrySettings(attempts=1),
        summary_capacity=measure,
        output_tokens=100,
        diagnostic_bytes=1024,
        compiler=compile_materials,
    )
    preparation = HistoryPreparation(
        store,
        maintenance,
        backend=backend,
        model="openai/main",
        assess=measure,
        context=ContextSettings(),
        compiler=compile_materials,
    )
    session = await store.get_session("foreground")
    try:
        actual = await preparation(
            session,
            Message.user("后续输入"),
            instructions=(),
            tools=ToolSet(),
            settings=ModelSettings(max_output_tokens=100),
            dynamic=DynamicState.from_payload(
                {"material_kind": "engine_committed_state", "work": await store.subject_work()}
            ),
            budget=RequestBudget(max_requests=8, deadline=time.monotonic() + 30),
        )
        assert actual == session and backend.requests == [] and len(captured) == 1
        messages = captured[0]["messages"]
        assert isinstance(messages, list)
        tail = encode(messages[-1])
        assert body not in tail and "result_delivery" in tail and "full_tool_message" in tail
        assert "原文标记：" in encode(messages[:-1])
    finally:
        await preparation.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_idle_summary_is_installed_before_begin_turn_and_real_core_adopts_it(
    store: Store,
    pending: bool,
) -> None:
    messages: list[Message] = []
    boundaries: list[int] = []
    protected_identity: str | None = None
    for index in range(5):
        identity = await admit(store, str(index), "原始输入" + "x" * 6500)
        messages.extend(
            (
                Message.user("原始输入" + "x" * 6500, metadata={"evidence": (identity,)}),
                Message.assistant("完整结束"),
            )
        )
        boundaries.append(len(messages))
        checkpoint = ConversationCheckpoint(
            ConversationState(
                tuple(messages), turn_index=index + 1, turn_boundaries=tuple(boundaries)
            ),
            ReminderState(),
        )
        turn = await store.begin_turn(
            "old" + str(index), "foreground", index, index + 1, (identity,)
        )
        await store.finish_turn(turn, "succeeded", checkpoint_json=encode_checkpoint(checkpoint))
        if index == 0 and pending:
            protected_identity = identity
            await store.knowledge.plan_wakeup(
                WakeupPlan(
                    id="pending", due=time.time() + 3600, reason="尚未到期", evidence=identity
                )
            )
    stored = await store.get_session("foreground")
    assert stored.checkpoint_json is not None
    original = decode_checkpoint(stored.checkpoint_json)
    generation = Backend("本轮结束")
    summary = Backend('{"summary":"保留旧来源与结果"}')
    maintenance = HistoryMaintenance(
        store,
        store,
        summary,
        model="openai/summary",
        retry=RetrySettings(attempts=1),
        summary_capacity=lambda payload: check_capacity(payload, GenerationCapacity(100_000, 256)),
        output_tokens=256,
        diagnostic_bytes=1024,
        compiler=compile_materials,
    )
    preparation = HistoryPreparation(
        store,
        maintenance,
        backend=generation,
        model="openai/main",
        assess=assess,
        context=ContextSettings(),
        compiler=compile_materials,
    )
    session = ContinuousSession(
        store,
        "foreground",
        can_start=lambda: None,
        compiler=compile_materials,
    )
    current = await admit(store, "current", "本轮完整输入")
    initial = preview_request(
        generation,
        "openai/main",
        original,
        Message.user("本轮完整输入"),
        instructions=(SystemInstruction.from_text("稳定前缀" + "z" * 4000),),
        tools=ToolSet(),
        settings=ModelSettings(max_output_tokens=100),
        dynamic=DynamicState("已提交来源状态与当前预算"),
        max_requests=8,
    )
    materials, _ = compile_materials(initial, model="openai/main")
    payload = decode_object(materials.encode())["payload"]
    assert isinstance(payload, dict)
    with pytest.raises(CapacityError) as blocked:
        check(payload)
    assert blocked.value.code == "over_capacity"
    try:
        result = await session.run(
            Message.user("本轮完整输入"),
            turn_id="new",
            input_ids=(current,),
            backend=generation,
            evidence=store,
            model="openai/main",
            instructions=(SystemInstruction.from_text("稳定前缀" + "z" * 4000),),
            tools=ToolSet(),
            output_tokens=100,
            max_requests=8,
            turn_seconds=600,
            retry=RetrySettings(attempts=1),
            diagnostic_bytes=1024,
            refresh=refresh,
            capacity=check,
            prepare_history=preparation,
        )
        assert len(summary.requests) == len(generation.requests) == 1
        assert summary.requests[0].instructions == generation.requests[0].instructions
        assert summary.requests[0].tools == ()
        assert summary.requests[0].messages[-1].role is MessageRole.USER
        assert "【系统内部上下文压缩指令】" in summary.requests[0].messages[-1].text
        assert all("本轮完整输入" not in msg.text for msg in summary.requests[0].messages)
        summary_index = 2 if pending else 0
        assert "保留旧来源与结果" in generation.requests[0].messages[summary_index].text
        if pending:
            assert generation.requests[0].messages[:2] == original.history.messages[:2]
            assert summary.requests[0].messages[:-1] == original.history.messages[2:6]
            assert (await store.knowledge.pending_plans())[0].id == "pending"
            assert protected_identity is not None
            assert await store.protected_input_ids((protected_identity,)) == frozenset(
                (protected_identity,)
            )
            saved = await store.get_session("foreground")
            assert saved.checkpoint_json is not None
            assert decode_checkpoint(saved.checkpoint_json) == result.checkpoint
        assert result.turn.session_version == 6 and result.session.version == 7
        assert result.checkpoint.history.turn_index == (4 if pending else 3)
        assert generation.requests[0].messages[-1].text == "本轮完整输入"
    finally:
        await session.close()
        await preparation.close()


@pytest.mark.asyncio
async def test_preview_is_pure_and_preparation_failure_ends_the_original_input(
    store: Store,
) -> None:
    backend = Backend("不会调用")
    instructions = (SystemInstruction.from_text("稳定目录"),)
    dynamic = DynamicState("完整尾部")
    request = preview_request(
        backend,
        "openai/main",
        None,
        Message.user("原文"),
        instructions=instructions,
        tools=ToolSet(),
        settings=ModelSettings(max_output_tokens=100),
        dynamic=dynamic,
        max_requests=8,
    )
    assert backend.requests == [] and request.instructions == instructions
    assert request.messages[0].text == "原文" and request.reminders

    async def broken(
        session: SessionRecord,
        message: Message,
        *,
        instructions: tuple[SystemInstruction, ...],
        tools: ToolSet,
        settings: ModelSettings,
        dynamic: DynamicState,
        budget: RequestBudget,
    ) -> SessionRecord:
        assert session.active_turn is None
        raise RuntimeError("准备内部错误")

    identity = await admit(store, "new", "不能重做的输入")
    session = ContinuousSession(
        store,
        "foreground",
        can_start=lambda: None,
        compiler=compile_materials,
    )
    try:
        with pytest.raises(RuntimeError, match="准备内部错误"):
            await session.run(
                Message.user("不能重做的输入"),
                turn_id="prepare-failed",
                input_ids=(identity,),
                backend=backend,
                evidence=store,
                model="openai/main",
                instructions=instructions,
                tools=ToolSet(),
                output_tokens=100,
                max_requests=8,
                turn_seconds=600,
                retry=RetrySettings(),
                diagnostic_bytes=1024,
                refresh=refresh,
                capacity=check,
                prepare_history=broken,
            )
        processing = await store.input_processing(identity)
        assert processing is not None and processing.stage == "failed"
        assert not await store.staged_inputs("foreground")
        assert len(await store.failed_turns("foreground")) == 1
        assert backend.requests == []
    finally:
        await session.close()
