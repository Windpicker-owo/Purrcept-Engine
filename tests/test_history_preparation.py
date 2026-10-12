"""在实际连续会话入口核对完整请求预览、idle安装和准备失败结案。"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from purrcept_core.models import (
    ConversationCheckpoint,
    ConversationState,
    Message,
    ModelEventSink,
    ModelRequest,
    ModelResponse,
    ModelSettings,
    ReminderState,
    SystemInstruction,
    ToolSet,
)

from purrcept_engine.app.config import ContextSettings, RetrySettings
from purrcept_engine.app.history import HistoryPreparation, preview_request
from purrcept_engine.context.capacity import GenerationCapacity, assess_capacity, check_capacity
from purrcept_engine.context.checkpoints import encode_checkpoint
from purrcept_engine.execution.protocol import JsonObject
from purrcept_engine.storage import InputContext, InputSource, InputSubmission
from purrcept_engine.storage.store import Store
from purrcept_engine.storage.subject import SessionRecord
from purrcept_engine.subject.history import HistoryMaintenance
from purrcept_engine.subject.model_requests import RequestBudget
from purrcept_engine.subject.session import ContinuousSession, DynamicState


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
    return assess_capacity(payload, GenerationCapacity(32_000, 100, "reference-text-only-v1"))


def check(payload: JsonObject):
    """真实派发仍核完整输入，不借摘要放宽输出预留或容量门槛。"""
    return check_capacity(payload, GenerationCapacity(32_000, 100, "reference-text-only-v1"))


async def refresh(budget: RequestBudget) -> DynamicState:
    """冻结相同动态材料使前后布局可比较，不注册额外任务。"""
    return DynamicState("已提交来源状态与当前预算")


@pytest.mark.asyncio
async def test_idle_summary_is_installed_before_begin_turn_and_real_core_adopts_it(
    store: Store,
) -> None:
    messages: list[Message] = []
    boundaries: list[int] = []
    for index in range(5):
        identity = await admit(store, str(index), "原始输入" + "x" * 5000)
        messages.extend(
            (
                Message.user("原始输入" + "x" * 5000, metadata={"evidence": (identity,)}),
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
    generation = Backend("本轮结束")
    summary = Backend('{"summary":"保留旧来源与结果"}')
    maintenance = HistoryMaintenance(
        store,
        store,
        summary,
        model="openai/summary",
        retry=RetrySettings(attempts=1),
        summary_capacity=lambda payload: check_capacity(
            payload, GenerationCapacity(100_000, 256, "reference-text-only-v1")
        ),
        output_tokens=256,
        diagnostic_bytes=1024,
    )
    preparation = HistoryPreparation(
        store,
        maintenance,
        backend=generation,
        model="openai/main",
        assess=assess,
        context=ContextSettings(),
    )
    session = ContinuousSession(store, "foreground", can_start=lambda: None)
    current = await admit(store, "current", "本轮完整输入")
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
        assert summary.requests[0].tools == ()
        assert "本轮完整输入" not in summary.requests[0].messages[0].text
        assert "保留旧来源与结果" in generation.requests[0].messages[0].text
        assert result.turn.session_version == 6 and result.session.version == 7
        assert result.checkpoint.history.turn_index == 3
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
    session = ContinuousSession(store, "foreground", can_start=lambda: None)
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
