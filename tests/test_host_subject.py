"""从实际 Host 输入到 Core、Worker 和权威历史验证默认主体接线。

后端为明确注入的离线脚本；控制台效果仍经生产目录、执行器和真实临时
数据库。最终文字不当作输出，可纠正工具错误不终止整个固定工作器。
"""

import asyncio
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from purrcept_core.models import (
    Message,
    MessageRole,
    ModelEventSink,
    ModelRequest,
    ModelResponse,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)

from purrcept_engine import EngineConfig, EngineHost, InputSource, InputSubmission
from purrcept_engine.app.config import GenerationModelSettings
from purrcept_engine.values import decode_object


class Backend:
    """固定脚本只记录真实 Core 请求，不执行外部服务或代替工具循环。"""

    def __init__(self, responses: tuple[ModelResponse, ...]) -> None:
        self.responses = deque(responses)
        self.requests: list[ModelRequest] = []

    async def generate(
        self,
        request: ModelRequest,
        *,
        model: str,
        emit: ModelEventSink | None = None,
    ) -> ModelResponse:
        """交回完整 Core 响应，借用寿命由真实请求包装负责。"""
        self.requests.append(request)
        return self.responses.popleft()


def config(directory: Path) -> EngineConfig:
    """选择明确文本容量与模型引用，留足六工具实际 schema 的输入额度。"""
    return EngineConfig.validate(
        {
            "host": {"data_dir": directory},
            "console": True,
            "models": {
                "main": {
                    "kind": "generation",
                    "model": "openai/offline",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "context_window": 100_000,
                    "max_output_tokens": 1000,
                    "capacity_rule": "reference-text-only-v1",
                }
            },
            "subject": {"foreground": "main", "reflection": "main", "summary": "main"},
        }
    )


def proposal(identity: str, name: str, arguments: dict[str, str]) -> ModelResponse:
    """创建一次真实工具提案，工具执行仍由 Core 负责。"""
    return ModelResponse(
        Message(MessageRole.ASSISTANT, (ToolCallBlock(identity, name, arguments=arguments),))
    )


async def wait_processed(host: EngineHost, identity: str) -> None:
    """有界查询实际提交结果，不以模型返回或 UI 通知推断输入结案。"""
    async with asyncio.timeout(10):
        while True:
            record = await host.query_input(identity)
            assert record is not None
            if record.state == "processed":
                return
            await asyncio.sleep(0.01)


async def test_configured_embedding_failure_ends_one_turn_and_preserves_host(
    tmp_path: Path,
) -> None:
    """配置语义路径保留真实失败，新输入仍可处理；未把关键词当替代结果。"""
    request = httpx.Request("POST", "http://127.0.0.1:9999/v1/embeddings")
    failure = httpx.ConnectError("synthetic unavailable", request=request)

    class Embedding:
        model_id = "explicit-test-space"

        async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            raise failure

    class Reflection:
        async def generate(
            self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
        ) -> ModelResponse:
            return ModelResponse(Message.assistant("本批没有结构化完成回执。"))

    original = config(tmp_path)
    raw = original.model_dump()
    main = original.models["main"]
    assert isinstance(main, GenerationModelSettings)
    raw["models"]["reflection"] = main.model_dump()
    raw["subject"]["reflection"] = "reflection"
    raw["knowledge"] = {
        "embedding": {
            "model": "openai/synthetic",
            "model_id": "explicit-test-space",
            "api_base": "http://127.0.0.1:9999/v1",
        }
    }
    backend = Backend(
        (
            proposal("recall", "recall", {"query": "needle"}),
            ModelResponse(Message.assistant("新输入正常结束。")),
        )
    )
    host = EngineHost(
        EngineConfig.validate(raw),
        generation_backends={"main": backend, "reflection": Reflection()},
        embedding_provider=Embedding(),
    )
    await host.start()
    try:
        now = datetime.now(UTC)
        first = await host.submit(
            InputSubmission(InputSource("test", "a", "c", "text", "first"), "先检索", now, now)
        )
        await wait_processed(host, first.input_id)
        failed = await host.query_processing(first.input_id)
        assert failed is not None and failed.stage == "failed"
        second = await host.submit(
            InputSubmission(InputSource("test", "a", "c", "text", "second"), "新独立输入", now, now)
        )
        await wait_processed(host, second.input_id)
        completed = await host.query_processing(second.input_id)
        assert completed is not None and completed.stage == "processed"
        assert host.query().phase == "running" and len(backend.requests) == 2
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_host_uses_real_core_worker_and_continuous_history(tmp_path: Path) -> None:
    output: list[str] = []
    backend = Backend(
        (
            proposal(
                "action",
                "play_action",
                {
                    "action_code": "from world import me\n"
                    'return await me.console.output(text="真实输出")'
                },
            ),
            ModelResponse(Message.assistant("内部结束一")),
            ModelResponse(Message.assistant("内部结束二")),
        )
    )
    host = EngineHost(
        config(tmp_path), generation_backends={"main": backend}, console_output=output.append
    )
    await host.start()
    try:
        now = datetime.now(UTC)
        first = await host.submit(
            InputSubmission(InputSource("test", "a", "c", "text", "1"), "输入一", now, now)
        )
        await wait_processed(host, first.input_id)
        second = await host.submit(
            InputSubmission(InputSource("test", "a", "c", "text", "2"), "输入二", now, now)
        )
        await wait_processed(host, second.input_id)
        assert output == ["真实输出"]
        assert len(backend.requests) == 3
        assert "输入一" in backend.requests[2].messages[0].text
        assert "输入二" in backend.requests[2].messages[-1].text
        assert backend.requests[0].instructions == backend.requests[2].instructions
        assert tuple(tool.name for tool in backend.requests[0].tools) == (
            "play_action",
            "recall",
            "read_evidence",
            "plan_wakeup",
            "resolve_plan",
            "respond_supervision",
        )
        assert any(
            "me.console.output" in block.text
            for instruction in backend.requests[0].instructions
            for block in instruction.content
            if isinstance(block, TextBlock)
        )
    finally:
        await host.close()
    assert host.query().phase == "closed"


@pytest.mark.asyncio
async def test_correctable_tool_error_remains_in_original_model_budget(tmp_path: Path) -> None:
    backend = Backend(
        (
            proposal("bad-source", "play_action", {"action_code": "import os"}),
            proposal("bad-evidence", "read_evidence", {"id": "missing-original"}),
            ModelResponse(Message.assistant("内部纠正完成")),
        )
    )
    host = EngineHost(
        config(tmp_path), generation_backends={"main": backend}, console_output=lambda text: None
    )
    await host.start()
    try:
        now = datetime.now(UTC)
        admission = await host.submit(
            InputSubmission(InputSource("test", "a", "c", "text", "1"), "输入", now, now)
        )
        await wait_processed(host, admission.input_id)
        assert len(backend.requests) == 3
        results = [
            block
            for message in backend.requests[-1].messages
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        assert len(results) == 2 and results[1].is_error
        first = results[0].content[0]
        assert isinstance(first, TextBlock)
        failed_action = decode_object(first.text.encode())
        assert failed_action["state"] == "failed" and failed_action["calls"] == []
        assert {result.tool_call_id for result in results} == {"bad-source", "bad-evidence"}
    finally:
        await host.close()
