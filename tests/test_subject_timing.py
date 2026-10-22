"""以真实 Host/Core/SQLite/Worker 请求核验跨批刺激与行动的时间信息。

仅模型传输与本地能力效果由替身提供；异步插件准备、请求重试、尾部刷新、
冻结输入和行动回执走生产入口，不连接真实模型、账号或设备。
"""

import asyncio
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from purrcept_core.models import (
    Message,
    MessageRole,
    ModelEventSink,
    ModelRequest,
    ModelResponse,
    ModelUnavailableError,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from pydantic import BaseModel, ConfigDict

from purrcept_engine import EngineConfig, EngineHost, InputSource, InputSubmission
from purrcept_engine.domain.execution.catalog import CapabilityDeclaration, ObjectDeclaration
from purrcept_engine.domain.execution.providers import CallContext, CallOutcome, FunctionCapability
from purrcept_engine.plugin_system.contracts import (
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
)
from purrcept_engine.values import JsonObject, decode_object


class _Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    text: str


@dataclass
class _Generation:
    """按真实派发顺序提供脚本响应，首请求可阻塞以接入下一批刺激。"""

    responses: deque[ModelResponse | Exception] = field(
        default_factory=lambda: deque[ModelResponse | Exception]()
    )
    blocks: dict[int, asyncio.Event] = field(default_factory=lambda: dict[int, asyncio.Event]())
    arrivals: asyncio.Queue[ModelRequest] = field(
        default_factory=lambda: asyncio.Queue[ModelRequest]()
    )
    count: int = 0

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        self.count += 1
        self.arrivals.put_nowait(request)
        if self.count in self.blocks:
            await self.blocks[self.count].wait()
        response = (
            self.responses.popleft()
            if self.responses
            else ModelResponse(Message.assistant("内部结束"))
        )
        if isinstance(response, Exception):
            raise response
        return response


def _clock(request: ModelRequest, field: str = "current_time") -> datetime:
    """从实际派发提醒读取时钟，而非直接调用时钟或材料渲染助手。"""
    tail = next(item for item in request.reminders if item.key == "engine.committed_state")
    text = "\n".join(block.text for block in tail.content if isinstance(block, TextBlock))
    matches = re.findall(rf'"{field}": > (\d{{4}}-[^\n]+)', text)
    assert matches
    # capabilities 位于 work 之后；prepared_at 取测试插件自己提供的最后一个值。
    return datetime.fromisoformat(matches[-1])


def _text(request: ModelRequest) -> str:
    return "\n".join(
        block.text
        for message in request.messages
        for block in message.content
        if isinstance(block, TextBlock)
    )


async def test_late_stimulus_is_dated_before_previous_reply_and_clock_refreshes(
    tmp_path: Path,
) -> None:
    """msg-2 在旧轮思考期间到达；新轮能核对它早于对 msg-1 的实际行动。"""
    effects: list[datetime] = []
    release = asyncio.Event()
    model = _Generation(
        responses=deque(
            (
                ModelUnavailableError("离线替身的一次临时失败"),
                ModelResponse(
                    Message(
                        MessageRole.ASSISTANT,
                        (
                            ToolCallBlock(
                                "reply-1",
                                "play_action",
                                arguments={
                                    "action_code": "from world import me\n"
                                    'await me.test.echo(text="回复 msg-1")'
                                },
                            ),
                        ),
                    )
                ),
                ModelResponse(Message.assistant("旧轮内部结束")),
                ModelResponse(Message.assistant("下一批内部结束")),
            )
        ),
        blocks={1: release},
    )
    declaration = CapabilityDeclaration(
        "test.echo",
        "test",
        "echo",
        "记录本地测试效果。",
        _Parameters,
        5.0,
        'await me.test.echo(text="回复")',
    )

    async def effect(parameters: _Parameters, context: CallContext) -> CallOutcome:
        effects.append(datetime.now(UTC))
        return CallOutcome("confirmed", {"text": parameters.text})

    async def prepare_request() -> JsonObject:
        # 时钟必须在异步插件材料完成之后采样，不能沿用前面读取 SQLite 的时间。
        await asyncio.sleep(0)
        return {"prepared_at": datetime.now(UTC).isoformat()}

    async def build(ports: PluginPorts) -> PluginRuntime:
        return PluginRuntime(
            (FunctionCapability(declaration, effect),), prepare_request=prepare_request
        )

    def prepare(instance: str, raw: object, base: Path) -> PluginBlueprint:
        return PluginBlueprint(
            (ObjectDeclaration("test", "test", "本地测试", "subject"),), (declaration,), build
        )

    settings = {
        "kind": "generation",
        "model": "openai/offline",
        "api_base": "http://127.0.0.1:9999/v1",
        "api_key": "synthetic",
        "context_window": 100_000,
        "max_output_tokens": 1000,
    }
    config = EngineConfig.validate(
        {
            "host": {"data_dir": tmp_path},
            "models": {"main": settings, "background": settings},
            "subject": {
                "foreground": "main",
                "reflection": "background",
                "summary": "background",
                "attention_batch_seconds": 0.0,
            },
            "plugins": [{"instance_id": "test", "entry": "test"}],
        },
        base_dir=tmp_path,
    )
    host = EngineHost(
        config,
        generation_backends={"main": model, "background": _Generation()},
        plugin_factories={"test": PluginFactory(prepare)},
    )
    await host.start()
    try:
        occurred = datetime.fromisoformat("2026-10-14T23:59:58.123456+08:00")
        received = datetime.now(UTC)
        await host.submit(
            InputSubmission(
                InputSource("test", "self", "chat", "message", "msg-1"),
                "msg-1",
                occurred,
                received,
            )
        )
        first = await asyncio.wait_for(model.arrivals.get(), 10)
        assert occurred.astimezone(UTC).isoformat() in _text(first)
        assert received.isoformat() in _text(first)
        first_clock = _clock(first)
        assert first_clock >= _clock(first, "prepared_at") >= received

        second_time = datetime.now(UTC)
        await host.submit(
            InputSubmission(
                InputSource("test", "self", "chat", "message", "msg-2"),
                "msg-2",
                second_time,
                second_time,
            )
        )
        release.set()
        retry = await asyncio.wait_for(model.arrivals.get(), 10)
        assert retry == first
        after_action = await asyncio.wait_for(model.arrivals.get(), 10)
        assert "msg-2" not in _text(after_action)
        assert len(effects) == 1
        assert second_time < effects[0] <= _clock(after_action, "prepared_at")
        assert _clock(after_action) >= _clock(after_action, "prepared_at") > first_clock

        current = await asyncio.wait_for(model.arrivals.get(), 10)
        assert second_time.isoformat() in _text(current) and "msg-2" in _text(current)
        assert _clock(current) >= _clock(current, "prepared_at") >= _clock(after_action)
        assert first.instructions == after_action.instructions == current.instructions
        assert all(first_clock.isoformat() not in str(item) for item in current.instructions)

        receipts = [
            block
            for message in current.messages
            for block in message.content
            if isinstance(block, ToolResultBlock) and block.tool_call_id == "reply-1"
        ]
        assert len(receipts) == 1
        original = receipts[0].content[0]
        assert isinstance(original, TextBlock)
        action = decode_object(original.text.encode())
        calls = action["calls"]
        assert isinstance(calls, list) and len(calls) == 1 and isinstance(calls[0], dict)
        finished = calls[0]["finished_at"]
        assert isinstance(finished, (float, int))
        receipt_time = datetime.fromtimestamp(finished, UTC)
        assert second_time < effects[0] <= receipt_time <= _clock(after_action)
        assert any(
            isinstance(block, TextBlock) and receipt_time.isoformat() in block.text
            for block in receipts[0].content[1:]
        )
    finally:
        release.set()
        await host.close()
