"""使用真实 Core 工具循环与临时库验证连续历史、预算和取消边界。"""

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from purrcept_core.models import (
    FunctionTool,
    Message,
    MessageRole,
    ModelEventSink,
    ModelRequest,
    ModelResponse,
    SystemInstruction,
    TextBlock,
    ToolCallBlock,
    ToolLoopLimitError,
    ToolSet,
)

from purrcept_engine.app.config import RetrySettings
from purrcept_engine.domain.context.capacity import GenerationCapacity, check_capacity
from purrcept_engine.domain.inputs.models import InputContext, InputSource, InputSubmission
from purrcept_engine.domain.model_requests.service import RequestBudget
from purrcept_engine.domain.subject.session import ContinuousSession, DynamicState
from purrcept_engine.infrastructure.models.compiler import compile_materials
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.values import JsonObject


@pytest_asyncio.fixture
async def session_storage(tmp_path: Path) -> AsyncIterator[tuple[Store, ContinuousSession]]:
    """断言失败也等待会话和数据库实际收回，不留下非守护数据库线程。"""
    store = await Store.open(tmp_path / "engine.sqlite3")
    session = ContinuousSession(
        store,
        "foreground",
        can_start=lambda: None,
        compiler=compile_materials,
    )
    try:
        yield store, session
    finally:
        await session.close()
        await store.close()


class ScriptedBackend:
    """只返回可重放 Core 响应，不连接模型服务。"""

    def __init__(self, responses: tuple[ModelResponse | Exception, ...]) -> None:
        self.responses = deque(responses)
        self.requests: list[ModelRequest] = []

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        """完整记录实际调用材料，并按脚本保持真实失败类型。"""
        self.requests.append(request)
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


async def admitted(store: Store, native_id: str, text: str) -> str:
    """接管一个可信最低唤起输入，返回真实身份供会话事务使用。"""
    now = datetime.now(UTC)
    result = await store.admit(
        InputSubmission(
            InputSource("test", "a", "c", "text", native_id),
            text,
            now,
            now,
            context=InputContext(minimum_wake=True),
        )
    )
    return result.input_id


def capacity(payload: JsonObject):
    """真实纯参考计量包含固定前缀、工具和尾部，不用零估算放行。"""
    return check_capacity(payload, GenerationCapacity(100_000, 1000, "reference-text-only-v1"))


async def refresh(budget: RequestBudget) -> DynamicState:
    """提交后内存投影中只变化预算，正文来源标签保持可识别。"""
    return DynamicState(f"宿主已提交状态（user 材料）：已使用请求 {budget.used}")


@pytest.mark.asyncio
async def test_core_history_continues_and_dynamic_tail_does_not_become_history(
    session_storage: tuple[Store, ContinuousSession],
) -> None:
    store, session = session_storage
    backend = ScriptedBackend(
        (ModelResponse(Message.assistant("内部一")), ModelResponse(Message.assistant("内部二")))
    )
    for ordinal, text in enumerate(("第一项", "第二项")):
        input_id = await admitted(store, str(ordinal), text)
        result = await session.run(
            Message.user(text),
            turn_id=f"turn{ordinal}",
            input_ids=(input_id,),
            backend=backend,
            evidence=store,
            model="openai/test",
            instructions=(SystemInstruction.from_text("稳定前缀"),),
            tools=ToolSet(),
            output_tokens=1000,
            max_requests=8,
            turn_seconds=600,
            retry=RetrySettings(),
            diagnostic_bytes=4096,
            refresh=refresh,
            capacity=capacity,
        )
        assert result.checkpoint.history.turn_index == ordinal + 1
    assert tuple(message.text for message in backend.requests[1].messages) == (
        "第一项",
        "内部一",
        "第二项",
    )
    assert backend.requests[0].instructions == backend.requests[1].instructions
    current = await store.get_session("foreground")
    assert current.checkpoint_json is not None and "宿主已提交" not in current.checkpoint_json
    tail = backend.requests[1].reminders[-1].content[0]
    assert isinstance(tail, TextBlock) and tail.text.startswith("宿主已提交")
    requests = await store.requests_for_turn("foreground", "turn1")
    assert len(requests) == 1 and requests[0].request.capacity_json is not None
    assert "reference-text-only-v1" in requests[0].request.capacity_json
    await session.close()
    await store.close()


@pytest.mark.asyncio
async def test_core_last_allowed_response_cannot_execute_tools(
    session_storage: tuple[Store, ContinuousSession],
) -> None:
    store, session = session_storage
    calls: list[str] = []

    async def action(text: str) -> str:
        """记录一个真实工具入口，最后一轮不得开始。"""
        calls.append(text)
        return text

    backend = ScriptedBackend(
        (
            ModelResponse(
                Message(
                    MessageRole.ASSISTANT,
                    (ToolCallBlock("proposal", "action", arguments={"text": "不应开始"}),),
                )
            ),
        )
    )
    input_id = await admitted(store, "one", "输入")
    with pytest.raises(ToolLoopLimitError):
        await session.run(
            Message.user("输入"),
            turn_id="last",
            input_ids=(input_id,),
            backend=backend,
            evidence=store,
            model="openai/test",
            instructions=(SystemInstruction.from_text("稳定前缀"),),
            tools=ToolSet((FunctionTool(action),)),
            output_tokens=1000,
            max_requests=1,
            turn_seconds=600,
            retry=RetrySettings(),
            diagnostic_bytes=4096,
            refresh=refresh,
            capacity=capacity,
        )
    assert calls == []
    assert (await store.get_session("foreground")).version == 0
    assert len(await store.failed_turns("foreground")) == 1
    failed = await store.get_turn("last")
    assert failed is not None and failed.error_json is not None
    assert '"category":"budget_exhausted"' in failed.error_json
    await session.close()
    await store.close()


async def test_actual_whole_turn_deadline_has_distinct_budget_evidence(
    session_storage: tuple[Store, ContinuousSession],
) -> None:
    """真实整轮截止的原因可供反馈分类，不能用所有TimeoutError推断。"""
    store, session = session_storage

    class WaitingBackend:
        async def generate(
            self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
        ) -> ModelResponse:
            await asyncio.Event().wait()
            raise AssertionError("截止后的后端不能返回结果。")

    input_id = await admitted(store, "deadline", "输入")
    with pytest.raises(TimeoutError):
        await session.run(
            Message.user("输入"),
            turn_id="deadline",
            input_ids=(input_id,),
            backend=WaitingBackend(),
            evidence=store,
            model="openai/test",
            instructions=(),
            tools=ToolSet(),
            output_tokens=1000,
            max_requests=8,
            turn_seconds=0.1,
            retry=RetrySettings(),
            diagnostic_bytes=4096,
            refresh=refresh,
            capacity=capacity,
        )
    failed = await store.get_turn("deadline")
    assert failed is not None and failed.error_json is not None
    assert '"category":"budget_exhausted"' in failed.error_json
    assert (await store.get_session("foreground")).version == 0
