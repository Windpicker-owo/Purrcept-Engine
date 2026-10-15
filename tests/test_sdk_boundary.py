"""核验首批 SDK 路径的真实编码、传输次数和可观察参数证据。

测试保留 LiteLLM 与 OpenAI SDK 的请求构造、错误翻译和重试流程，只在
HTTPX 选择网络传输时替换为 MockTransport。模型凭据均为合成值，不读取
运行配置；OpenRouter 官方 URL 也只交给替身，不连接服务。非流式替身只用于核验 SDK 的
参数观察契约，不能作为真实传输次数的证据。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import httpx
import pytest
from purrcept_core.models import (
    AudioBlock,
    AudioBytes,
    CacheMode,
    ChoiceAnswer,
    ChoiceOption,
    ChoiceQuestion,
    DecisionRefusal,
    DecisionRequest,
    ImageBlock,
    ImageBytes,
    Message,
    MessageRole,
    ModelError,
    ModelRequest,
    ModelRequestError,
    ModelStreamCompleted,
    ModelStreamEvent,
    PredicateQuestion,
    PromptCachePolicy,
    ReminderPlacement,
    SystemInstruction,
    SystemReminder,
    TextBlock,
    ToolCallDelta,
    ToolSpec,
)

if TYPE_CHECKING:
    from purrcept_engine.infrastructure.models.clients import (
        DecisionResource,
        OpenAIGenerationResource,
    )

_GENERATION_BASE = "https://generation.invalid/v1"
_DECISION_BASE = "https://decision.invalid/v1"
_EXPLICIT_KEY = "synthetic-explicit-key"
_MODEL = "openai/gemini-3.8-flash-high"
_DECISION_MODEL = "typesafe/jev-1.13.0"
_DECISION_TARGETS = {
    "decision": (_DECISION_MODEL, _DECISION_BASE),
    "openrouter": ("openrouter/typesafe/jev-1.13", "https://openrouter.ai/api"),
}


@dataclass
class _Wire:
    """在 HTTP 边界记录实际尝试，SDK 上层仍执行真实流程。"""

    mode: Literal["ok", "connect_error", "timeout", "pending"] = "ok"
    status: int = 200
    requests: list[httpx.Request] = field(default_factory=lambda: list[httpx.Request]())
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    async def respond(self, request: httpx.Request) -> httpx.Response:
        """只记录已声明的测试目标，并让取消越过实际 SDK 调用栈。"""
        assert request.url.host in {"generation.invalid", "decision.invalid", "openrouter.ai"}
        self.requests.append(request)
        self.entered.set()
        if self.mode == "connect_error":
            raise httpx.ConnectError("synthetic connection failure", request=request)
        if self.mode == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if self.mode == "pending":
            try:
                await asyncio.Future[None]()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

        if self.status != 200:
            return httpx.Response(
                self.status,
                json={"error": {"message": "synthetic failure", "type": "invalid_request_error"}},
                request=request,
            )
        if request.url.path.endswith(("/systemone", "/alpha/decisions")):
            return httpx.Response(
                200,
                json={
                    "model": "jev-1.13.0",
                    "usage": {"input_tokens": 20, "output_tokens": 3},
                    "answers": {
                        "attend": {"type": "noul", "noul": 0.8},
                        "route": {
                            "type": "choice",
                            "choice": "foreground",
                            "confidence": 0.9,
                            "probabilities": {"foreground": 0.9, "observe": 0.1},
                        },
                        # 上游会将缺失答案规范化为拒答；不宣称原始响应完整。
                    },
                },
                request=request,
            )

        chunk = {
            "id": "synthetic-chat",
            "model": "synthetic-generation",
            "object": "chat.completion.chunk",
            "created": 1,
            "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
        }
        content = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=content,
            request=request,
        )


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> _Wire:
    """在 SDK 构建客户端前安装传输，禁止一切意外真实网络请求。"""
    recorded = _Wire()
    transport = httpx.MockTransport(recorded.respond)

    def async_transport(client: httpx.AsyncClient, url: httpx.URL) -> httpx.MockTransport:
        del client, url
        return transport

    def reject_sync(request: httpx.Request) -> httpx.Response:
        # LiteLLM 导入可能刷新公共价格表。拒绝连接而允许它读取随包附带的
        # 表，不替换模型传输或修改进程环境；其他同步请求仍属于测试错误。
        assert request.url.host == "raw.githubusercontent.com"
        return httpx.Response(404, request=request)

    sync_transport = httpx.MockTransport(reject_sync)

    def select_sync(client: httpx.Client, url: httpx.URL) -> httpx.MockTransport:
        del client, url
        return sync_transport

    class AsyncTransport(httpx.MockTransport):
        """替代原生传输构造，同时保留 SDK 使用的运行时类型契约。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            super().__init__(recorded.respond)

    class SyncTransport(httpx.MockTransport):
        """拒绝导入期公共目录以外的同步请求。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            super().__init__(reject_sync)

    def initialize_async(
        client: httpx.AsyncClient, *args: object, **kwargs: object
    ) -> httpx.MockTransport:
        del client, args, kwargs
        return transport

    def initialize_sync(
        client: httpx.Client, *args: object, **kwargs: object
    ) -> httpx.MockTransport:
        del client, args, kwargs
        return sync_transport

    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", async_transport)
    monkeypatch.setattr(httpx.Client, "_transport_for_url", select_sync)
    # 测试不要求安装机器当前代理的依赖。所有 HTTPX 传输构造都在此边界
    # 替换，避免代理初始化先于 MockTransport 选择而触及网络或原生库。
    monkeypatch.setattr(httpx.AsyncClient, "_init_transport", initialize_async)
    monkeypatch.setattr(httpx.AsyncClient, "_init_proxy_transport", initialize_async)
    monkeypatch.setattr(httpx.Client, "_init_transport", initialize_sync)
    monkeypatch.setattr(httpx.Client, "_init_proxy_transport", initialize_sync)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", AsyncTransport)
    monkeypatch.setattr(httpx, "HTTPTransport", SyncTransport)
    return recorded


def _generation_request() -> ModelRequest:
    """以完整历史及动态尾部表达已批准布局，音频保持原生块。"""
    return ModelRequest(
        (
            Message.user("old input"),
            Message.assistant("old answer"),
            Message(
                MessageRole.USER,
                (
                    TextBlock("source=microphone; input=synthetic-input"),
                    AudioBlock(AudioBytes(b"synthetic-wav", "audio/wav")),
                ),
            ),
        ),
        instructions=(SystemInstruction.from_text("persona\nfull capability catalog"),),
        reminders=(
            SystemReminder(
                (TextBlock("host-state; version=1; budget=8"),),
                placement=ReminderPlacement.TAIL,
            ),
        ),
        tools=(ToolSpec("play_action", description="Execute an explicit action."),),
        cache=PromptCachePolicy(mode=CacheMode.PREFER),
    )


def _decision_request() -> DecisionRequest:
    """多问题共享一份完整文字材料，TypeSafe 不支持图片输入。"""
    return DecisionRequest(
        (
            TextBlock("source=qq; input=synthetic-input"),
            TextBlock("last evidence"),
        ),
        (
            PredicateQuestion("attend", "Should this input receive attention?"),
            ChoiceQuestion(
                "route",
                "Choose a route.",
                (ChoiceOption("foreground", "Engage."), ChoiceOption("observe", "Record.")),
            ),
            PredicateQuestion("restricted", "Evaluate restricted evidence."),
        ),
    )


def _body(request: httpx.Request) -> dict[str, object]:
    """解析 MockTransport 收到的实际 JSON 请求，而不是高层调用参数。"""
    body: object = json.loads(request.content)
    assert isinstance(body, dict)
    return cast(dict[str, object], body)


async def _invoke(path: Literal["generation", "decision", "openrouter"]) -> None:
    """经 Engine 资源所有者进入真实 SDK，传输替身仅置于 HTTP 边界。"""
    if path == "generation":
        generation = _generation_resource()
        try:
            await generation.backend.generate(_generation_request(), model=_MODEL)
        finally:
            await generation.close()
    else:
        decision = _decision_resource(path)
        try:
            await decision.backend.decide(_decision_request(), model=_DECISION_TARGETS[path][0])
        finally:
            await decision.close()


def _generation_resource() -> OpenAIGenerationResource:
    """用显式网关配置建立生成所有者，不触及真实运行配置。"""
    from pydantic import SecretStr

    from purrcept_engine.app.config import GenerationModelSettings
    from purrcept_engine.infrastructure.models.clients import OpenAIGenerationResource

    return OpenAIGenerationResource.create(
        GenerationModelSettings(
            kind="generation",
            model=_MODEL,
            api_key=SecretStr(_EXPLICIT_KEY),
            api_base=_GENERATION_BASE,
            capacity_rule="synthetic-test-rule",
            context_window=1000000,
            max_output_tokens=100,
        )
    )


def _decision_resource(path: Literal["decision", "openrouter"] = "decision") -> DecisionResource:
    """用同一显式配置建立待测 Engine 判断所有者，不提供第三套传输。"""
    from pydantic import SecretStr

    from purrcept_engine.app.config import DecisionModelSettings
    from purrcept_engine.infrastructure.models.clients import DecisionResource

    model, api_base = _DECISION_TARGETS[path]
    return DecisionResource.create(
        DecisionModelSettings(
            kind="decision",
            model=model,
            api_key=SecretStr(_EXPLICIT_KEY),
            api_base=api_base,
            capacity_rule="synthetic-test-rule",
            input_limit=12000,
        )
    )


async def test_generation_wire_preserves_catalog_history_tail_and_native_audio(wire: _Wire) -> None:
    """证明布局与音频穿过真实 LiteLLM/OpenAI SDK 后仍进入实际请求。"""
    await _invoke("generation")
    assert len(wire.requests) == 1
    request = wire.requests[0]
    assert str(request.url) == f"{_GENERATION_BASE}/chat/completions"
    assert request.headers["authorization"] == f"Bearer {_EXPLICIT_KEY}"
    assert request.headers.get("openai-organization", "") == ""
    assert request.headers.get("openai-project", "") == ""
    assert request.headers["x-stainless-retry-count"] == "0"
    body = _body(request)
    messages = cast(list[dict[str, object]], body["messages"])
    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "user",
    ]
    assert messages[0]["content"] == "persona\nfull capability catalog"
    assert messages[1]["content"] == "old input"
    assert messages[2]["content"] == "old answer"
    audio_content = cast(list[dict[str, object]], messages[3]["content"])
    assert audio_content[0]["text"] == "source=microphone; input=synthetic-input"
    assert audio_content[1]["type"] == "input_audio"
    assert audio_content[1]["input_audio"] == {
        "data": base64.b64encode(b"synthetic-wav").decode("ascii"),
        "format": "wav",
    }
    assert messages[4]["content"] == "host-state; version=1; budget=8"
    assert "cache_control" not in messages[4]
    assert cast(list[dict[str, object]], body["tools"])[0]["function"] == {
        "name": "play_action",
        "description": "Execute an explicit action.",
        "parameters": {},
    }


@pytest.mark.parametrize("path", ["decision", "openrouter"])
async def test_decision_wire_preserves_order_and_sdk_refusal(
    wire: _Wire, path: Literal["decision", "openrouter"]
) -> None:
    """两条判断路径保留问题和上游模型身份，拒答不解释为概率零。"""
    resource = _decision_resource(path)
    model, api_base = _DECISION_TARGETS[path]
    try:
        result = await resource.backend.decide(_decision_request(), model=model)
    finally:
        await resource.close()
    assert len(wire.requests) == 1
    request = wire.requests[0]
    endpoint = "/alpha/decisions" if path == "openrouter" else "/systemone"
    assert str(request.url) == api_base + endpoint
    assert request.headers["authorization"] == f"Bearer {_EXPLICIT_KEY}"
    body = _body(request)
    assert body["model"] == model.split("/", 1)[1]
    assert body["state"] == "source=qq; input=synthetic-input\n\nlast evidence"
    questions = cast(dict[str, dict[str, object]], body["questions"])
    assert list(questions) == ["attend", "route", "restricted"]
    assert questions["route"]["criteria"] == {"foreground": "Engage.", "observe": "Record."}
    assert isinstance(result.answer("route"), ChoiceAnswer)
    assert isinstance(result.answer("restricted"), DecisionRefusal)


def test_explicit_configuration_ignores_conflicting_environment() -> None:
    """独立进程提供冲突环境，避免修改嵌入宿主的全进程环境。"""
    environment = _isolated_environment()
    test_file = Path(__file__).resolve()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(test_file),
            "-q",
            "--tb=short",
            "-o",
            "pythonpath=src",
            "-k",
            "generation_wire_preserves or decision_wire_preserves",
        ],
        cwd=test_file.parent.parent,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _isolated_environment() -> dict[str, str]:
    """显式保留所选依赖与操作系统目录，账号来源均为合成冲突值。"""
    # 只继承操作系统运行 Python 所需的非秘密目录，不读取或继承真实账号。
    inherited_names = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA")
    environment = {name: os.environ[name] for name in inherited_names if name in os.environ}
    environment.update(
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        OPENAI_API_KEY="synthetic-environment-key",
        OPENAI_API_BASE="https://environment.invalid/v1",
        OPENAI_BASE_URL="https://environment.invalid/v1",
        OPENAI_ORGANIZATION="synthetic-environment-organization",
        OPENAI_ORG_ID="synthetic-environment-organization",
        OPENAI_PROJECT_ID="synthetic-environment-project",
        TYPESAFE_API_KEY="synthetic-environment-key",
        TYPESAFE_API_BASE="https://environment.invalid/v1",
        OPENROUTER_API_KEY="synthetic-environment-key",
        OPENROUTER_API_BASE="https://environment.invalid/api",
        PYTHONIOENCODING="utf-8",
    )
    # 包叠加目录属于本轮明确选中的依赖证据，不继承任意 PYTHONPATH。
    # 子进程与父测试必须核验同一实际 LiteLLM 包，而不能退回兄弟环境版本。
    dependency = find_spec("litellm")
    assert dependency is not None and dependency.origin is not None
    environment["PYTHONPATH"] = os.pathsep.join(
        (
            str(Path(dependency.origin).resolve().parent.parent),
            str(Path(__file__).resolve().parent.parent / "src"),
        )
    )
    return environment


@pytest.mark.parametrize("model", [_DECISION_MODEL, _DECISION_TARGETS["openrouter"][0]])
def test_fresh_resource_construction_and_close_do_not_import_or_contact_sdk(model: str) -> None:
    """未导入 SDK 的新进程直接证明资源装配与关闭不会获取价格表。"""
    script = """
import asyncio
import sys
import httpx
from pydantic import SecretStr

def forbid_sync(client, request, **kwargs):
    raise AssertionError('Unexpected HTTP during pure construction')

async def forbid_async(client, request, **kwargs):
    raise AssertionError('Unexpected HTTP during pure construction')

httpx.Client.send = forbid_sync
httpx.AsyncClient.send = forbid_async

from purrcept_engine.app.config import DecisionModelSettings, GenerationModelSettings
from purrcept_engine.infrastructure.models.clients import OpenAIGenerationResource, DecisionResource
assert 'litellm' not in sys.modules
assert 'purrcept_litellm' not in sys.modules

generation = OpenAIGenerationResource.create(GenerationModelSettings(
    kind='generation', model='openai/synthetic', api_key=SecretStr('synthetic'),
    api_base='https://generation.invalid/v1', capacity_rule='synthetic',
    context_window=1000, max_output_tokens=100,
))
decision = DecisionResource.create(DecisionModelSettings(
    kind='decision', model=sys.argv[1], api_key=SecretStr('synthetic'),
    api_base='https://decision.invalid/v1', capacity_rule='synthetic', input_limit=1000,
))
assert generation.backend is generation
assert decision.backend is decision

async def close_all():
    await generation.close()
    await generation.close()
    await decision.close()
    await decision.close()

asyncio.run(close_all())
assert 'litellm' not in sys.modules
assert 'purrcept_litellm' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script, model],
        env=_isolated_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("path", ["generation", "decision", "openrouter"])
async def test_resource_close_is_shared_and_rejects_new_calls(
    wire: _Wire,
    monkeypatch: pytest.MonkeyPatch,
    path: Literal["generation", "decision", "openrouter"],
) -> None:
    """取消关闭等待者不取消清理，重复关闭不释放第二次或再启动模型。"""
    from purrcept_engine.infrastructure.models.clients import DecisionResource

    resource = _generation_resource() if path == "generation" else _decision_resource(path)
    if isinstance(resource, DecisionResource):
        await resource.backend.decide(_decision_request(), model=_DECISION_TARGETS[path][0])
    attempts_before_close = len(wire.requests)
    entered = asyncio.Event()
    release = asyncio.Event()
    close_count = 0
    original_close = httpx.AsyncClient.aclose

    async def delayed_close(client: httpx.AsyncClient) -> None:
        nonlocal close_count
        close_count += 1
        entered.set()
        await release.wait()
        await original_close(client)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", delayed_close)
    waiting = asyncio.create_task(resource.close())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        joined = asyncio.create_task(resource.close())
        with pytest.raises(ModelRequestError, match="资源已开始关闭"):
            if isinstance(resource, DecisionResource):
                await resource.backend.decide(_decision_request(), model=_DECISION_TARGETS[path][0])
            else:
                await resource.backend.generate(_generation_request(), model=_MODEL)
        assert close_count == 1
        assert not joined.done()
        assert len(wire.requests) == attempts_before_close
        release.set()
        await asyncio.wait_for(joined, 5)
        await resource.close()
        assert close_count == 1
    finally:
        release.set()
        if not waiting.done():
            await waiting
        await resource.close()


@pytest.mark.parametrize("path", ["decision", "openrouter"])
async def test_decision_rejects_images_without_substituting_descriptions(
    wire: _Wire, path: Literal["decision", "openrouter"]
) -> None:
    """注意路径无法表达图片时明确拒绝，不静默降级为仅判断说明文字。"""
    request = DecisionRequest(
        (
            TextBlock("full evidence"),
            ImageBlock(ImageBytes(b"synthetic-image", "image/png"), alt_text="image label"),
        ),
        (PredicateQuestion("attend", "Should this input receive attention?"),),
    )
    resource = _decision_resource(path)
    try:
        with pytest.raises(ModelRequestError, match="input_image content parts are not supported"):
            await resource.backend.decide(request, model=_DECISION_TARGETS[path][0])
    finally:
        await resource.close()
    assert not wire.requests


@pytest.mark.parametrize("path", ["generation", "decision", "openrouter"])
@pytest.mark.parametrize("status", [400, 401, 429, 503])
async def test_http_errors_have_one_actual_attempt(
    wire: _Wire, path: Literal["generation", "decision", "openrouter"], status: int
) -> None:
    """错误响应也只产生一次实际发送，防止 SDK 隐式叠加重试。"""
    wire.status = status
    with pytest.raises(ModelError):
        await _invoke(path)
    assert len(wire.requests) == 1


@pytest.mark.parametrize("path", ["generation", "decision", "openrouter"])
@pytest.mark.parametrize("failure", ["connect_error", "timeout"])
async def test_transport_errors_have_one_actual_attempt(
    wire: _Wire,
    path: Literal["generation", "decision", "openrouter"],
    failure: Literal["connect_error", "timeout"],
) -> None:
    """连接和超时路径同样需要关闭重发；失败是接入门槛的直接证据。"""
    wire.mode = failure
    with pytest.raises(ModelError):
        await _invoke(path)
    assert len(wire.requests) == 1


@pytest.mark.parametrize("path", ["generation", "decision", "openrouter"])
async def test_cancellation_reaches_transport_without_a_second_attempt(
    wire: _Wire, path: Literal["generation", "decision", "openrouter"]
) -> None:
    """调用方取消传递到实际传输，不等待结果或产生新的尝试。"""
    wire.mode = "pending"
    task = asyncio.create_task(_invoke(path))
    try:
        await asyncio.wait_for(wire.entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert wire.cancelled.is_set()
        assert len(wire.requests) == 1
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.parametrize("finish_reason", ["tool_calls", "length"])
async def test_nonstream_arguments_are_observed_before_repair_or_length_rejection(
    wire: _Wire, finish_reason: str
) -> None:
    """非流式原参数必须在修复或失败前可见，不能逆序列化解析对象。"""
    from purrcept_litellm import LiteLLMBackend

    del wire
    original_arguments = '{"code": "print(1)",}'
    events: list[ModelStreamEvent] = []

    async def completion(**kwargs: object) -> object:
        del kwargs
        return {
            "id": "synthetic-nonstream",
            "model": "synthetic-generation",
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "synthetic-call",
                                "type": "function",
                                "function": {
                                    "name": "play_action",
                                    "arguments": original_arguments,
                                },
                            }
                        ],
                    },
                }
            ],
        }

    backend = LiteLLMBackend(completion=completion)
    if finish_reason == "length":
        with pytest.raises(ModelRequestError, match="output token limit"):
            await backend.generate(_generation_request(), model=_MODEL, emit=events.append)
        assert not any(isinstance(event, ModelStreamCompleted) for event in events)
    else:
        result = await backend.generate(_generation_request(), model=_MODEL, emit=events.append)
        assert result.tool_calls[0].arguments == {"code": "print(1)"}
    parameters = [event for event in events if isinstance(event, ToolCallDelta)]
    assert len(parameters) == 1
    assert parameters[0].arguments_delta == original_arguments
    assert parameters[0].tool_call_id == "synthetic-call"
