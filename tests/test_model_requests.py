"""核验真实 Core 值、单请求恢复与 SQLite 证据提交的交接边界。

合成后端只控制暂时故障、取消和片段时序，不替代真实 SDK 编码验收。HTTP
测试保留首批 SDK 客户端与编译路径，仅替换最终 HTTPX 传输，并禁止其他网络。
临时库直接调用 Store 将借用的事务函数，不创建生产第二连接或测试真实数据。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import httpx
import pytest
from purrcept_core.context import ExecutionContext
from purrcept_core.models import (
    AudioBlock,
    AudioBytes,
    ConversationCheckpoint,
    ConversationState,
    DecisionRefusal,
    DecisionRequest,
    DecisionResponse,
    DecisionResponseError,
    ImageBlock,
    ImageBytes,
    ImageUrl,
    Message,
    MessageRole,
    Model,
    ModelAuthenticationError,
    ModelContextWindowError,
    ModelError,
    ModelEventSink,
    ModelRateLimitError,
    ModelRequest,
    ModelRequestError,
    ModelResponse,
    ModelStreamCompleted,
    ModelStreamEvent,
    ModelStreamStarted,
    ModelUnavailableError,
    PredicateQuestion,
    ReminderPlacement,
    ReminderState,
    SystemInstruction,
    SystemReminder,
    TextBlock,
    TextDelta,
    ToolCallBlock,
    ToolCallDelta,
    ToolResultBlock,
    ToolSpec,
)

from purrcept_engine.app.config import DecisionModelSettings, GenerationModelSettings, RetrySettings
from purrcept_engine.app.models import OpenAIGenerationResource, TypeSafeDecisionResource
from purrcept_engine.errors import StorageError
from purrcept_engine.storage import (
    InputSource,
    InputSubmission,
    MediaAsset,
)
from purrcept_engine.storage import model_requests as sql
from purrcept_engine.storage.model_requests import (
    AttemptEvidence,
    AttemptStart,
    RequestEvidence,
    RequestOutcome,
    RequestScope,
)
from purrcept_engine.storage.store import Store
from purrcept_engine.subject.model_requests import (
    ModelDecisionBackend,
    ModelGenerationBackend,
    RequestBudget,
    RequestBudgetExceeded,
    classify_failure,
    compile_materials,
)


@dataclass
class _Sink:
    """仅测试使用的可故障提交替身，生产使用 Store 权威事务队列。"""

    requests: list[RequestEvidence] = field(default_factory=lambda: list[RequestEvidence]())
    starts: list[AttemptStart] = field(default_factory=lambda: list[AttemptStart]())
    results: list[AttemptEvidence] = field(default_factory=lambda: list[AttemptEvidence]())
    outcomes: list[tuple[str, RequestOutcome]] = field(
        default_factory=lambda: list[tuple[str, RequestOutcome]]()
    )
    fail_at: Literal["begin", "start", "result", "finish"] | None = None
    failure: StorageError = field(default_factory=lambda: StorageError("synthetic write failure"))
    connection: sqlite3.Connection | None = None

    async def begin_request(self, request: RequestEvidence) -> None:
        """模拟确认提交，并可复用临时 SQLite 的真实事务。"""
        if self.fail_at == "begin":
            raise self.failure
        if self.connection is not None:
            sql.begin_request(self.connection, request)
        self.requests.append(request)

    async def begin_attempt(self, start: AttemptStart) -> None:
        """失败时不允许后端进入，成功时固定实际尝试身份。"""
        if self.fail_at == "start":
            raise self.failure
        if self.connection is not None:
            sql.begin_attempt(self.connection, start)
        self.starts.append(start)

    async def finish_attempt(self, result: AttemptEvidence) -> None:
        """结果失败用于核验禁止后续重试与保留权威故障原型。"""
        if self.fail_at == "result":
            raise self.failure
        if self.connection is not None:
            sql.finish_attempt(self.connection, result)
        self.results.append(result)

    async def finish_request(self, request_id: str, state: RequestOutcome) -> None:
        """确认逻辑终态，不把记录存在当成实际模型成功。"""
        if self.fail_at == "finish":
            raise self.failure
        if self.connection is not None:
            sql.finish_request(self.connection, request_id, state)
        self.outcomes.append((request_id, state))


class _Backend:
    """按尝试输出真实 Core 事件和值，所有参数和模型对象可核对。"""

    def __init__(self, errors: tuple[Exception | None, ...] = (None,)) -> None:
        self.errors = errors
        self.requests: list[ModelRequest] = []

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        """首尝试半成品不会成为后续响应；最终解析参数与原文本分别存在。"""
        index = len(self.requests)
        self.requests.append(request)
        if emit is not None:
            emit(ModelStreamStarted(model=model))
            emit(TextDelta(f"attempt-{index}"))
            emit(
                ToolCallDelta(0, '{code: "pass"}', tool_call_id=f"call-{index}", name="play_action")
            )
        error = self.errors[min(index, len(self.errors) - 1)]
        if error is not None:
            raise error
        response = ModelResponse(
            Message(
                MessageRole.ASSISTANT,
                content=(
                    TextBlock("complete"),
                    ToolCallBlock(f"call-{index}", "play_action", arguments={"code": "pass"}),
                ),
            )
        )
        if emit is not None:
            emit(ModelStreamCompleted(response))
        return response


async def _no_wait(seconds: float) -> None:
    assert seconds >= 1.0


def _budget(*, count: int = 8, seconds: float = 600.0) -> RequestBudget:
    return RequestBudget(max_requests=count, deadline=asyncio.get_running_loop().time() + seconds)


def _request() -> ModelRequest:
    return ModelRequest(
        (Message(MessageRole.USER, content=(TextBlock("human input"),)),),
        instructions=(SystemInstruction((TextBlock("fixed catalog"),), key="catalog"),),
        reminders=(
            SystemReminder((TextBlock("current state"),), placement=ReminderPlacement.TAIL),
        ),
        tools=(
            ToolSpec(
                "play_action",
                parameters={
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                    },
                },
            ),
        ),
    )


def _wrapper(
    backend: _Backend,
    sink: _Sink,
    *,
    budget: RequestBudget | None = None,
    policy: RetrySettings | None = None,
    diagnostic_bytes: int = 4096,
    sleep: Callable[[float], Awaitable[None]] = _no_wait,
) -> ModelGenerationBackend:
    return ModelGenerationBackend(
        backend,
        sink,
        scope=RequestScope("foreground", "turn", ("input-id",), ("snapshot-id",)),
        budget=budget or _budget(),
        policy=policy or RetrySettings(),
        diagnostic_bytes=diagnostic_bytes,
        sleep=sleep,
    )


async def test_retry_reuses_request_identity_and_keeps_attempts_separate() -> None:
    """同一请求材料与逻辑名额不重建，失败片段不混入成功 Core 事件。"""
    backend = _Backend((ModelUnavailableError("synthetic unavailable"), None))
    sink = _Sink()
    budget = _budget(count=1)
    wrapper = _wrapper(backend, sink, budget=budget)
    request = _request()
    events: list[ModelStreamEvent] = []
    response = await wrapper.generate(request, model="openai/synthetic", emit=events.append)
    assert backend.requests == [request, request]
    assert all(item is request for item in backend.requests)
    assert budget.used == 1
    assert len(sink.requests) == 1
    assert [item.sequence for item in sink.starts] == [1, 2]
    assert len({item.attempt_id for item in sink.starts}) == 2
    assert all(item.request_id == sink.requests[0].request_id for item in sink.starts)
    assert [item.state for item in sink.results] == ["failed", "succeeded"]
    assert sink.results[0].retry_wait_seconds == 1.0
    success = json.loads(cast(str, sink.results[1].response_json))
    assert success["message"]["content"][1]["arguments"] == {"code": "pass"}
    assert (
        json.loads(sink.results[0].argument_events_json)[0]["arguments_delta"] == '{code: "pass"}'
    )
    assert [item.delta for item in events if isinstance(item, TextDelta)] == ["attempt-1"]
    assert len([item for item in events if isinstance(item, ModelStreamStarted)]) == 1
    assert isinstance(events[-1], ModelStreamCompleted)
    assert events[-1].response == response
    with pytest.raises(RequestBudgetExceeded):
        await wrapper.generate(request, model="openai/synthetic")
    assert len(backend.requests) == 2
    await wrapper.close()


@pytest.mark.parametrize(
    "error",
    [
        ModelAuthenticationError("synthetic authentication"),
        ModelRequestError("invalid"),
        ModelContextWindowError("too large"),
        DecisionResponseError("structure"),
        ModelError("unknown"),
        ValueError("implementation"),
        TypeError("implementation"),
    ],
)
async def test_permanent_and_internal_failures_preserve_original_exception(
    error: Exception,
) -> None:
    """参数、凭据、结构与内部异常不做传输重试，也不转换成工具失败。"""
    backend, sink = _Backend((error,)), _Sink()
    wrapper = _wrapper(backend, sink)
    with pytest.raises(type(error)) as caught:
        await wrapper.generate(_request(), model="openai/synthetic")
    assert caught.value is error
    assert len(backend.requests) == 1
    assert sink.results[0].failure is not None
    assert sink.results[0].failure.error_type.endswith(type(error).__name__)
    assert "implementation" not in sql.encode(sink.results[0])
    await wrapper.close()


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("local"),
        httpx.ReadTimeout("local"),
        ModelRateLimitError("unknown limit"),
        ModelUnavailableError("503"),
    ],
)
async def test_recognized_transient_failure_has_at_most_two_attempts(error: Exception) -> None:
    """明确暂时传输故障有限重试，第二次失败保留两项独立事实。"""
    backend, sink = _Backend((error,)), _Sink()
    wrapper = _wrapper(backend, sink)
    with pytest.raises(type(error)) as caught:
        await wrapper.generate(_request(), model="openai/synthetic")
    assert caught.value is error
    assert len(backend.requests) == 2
    assert [item.state for item in sink.results] == ["failed", "failed"]
    await wrapper.close()


async def test_retry_after_beyond_remaining_deadline_does_not_resend() -> None:
    """等待建议超过剩余逻辑时间时直接结束，不提前再次发送。"""
    error = ModelRateLimitError("synthetic rate", retry_after_seconds=10.0)
    backend, sink = _Backend((error,)), _Sink()
    wrapper = _wrapper(backend, sink, policy=RetrySettings(total_seconds=2.0))
    with pytest.raises(ModelRateLimitError):
        await wrapper.generate(_request(), model="openai/synthetic")
    assert len(backend.requests) == 1
    assert sink.results[0].retry_wait_seconds is None
    await wrapper.close()


def test_structured_quota_and_http_date_wait_are_classified_without_text_guessing() -> None:
    """只用明确 code/type 判断额度，公开 HTTP 日期等待建议仍可恢复。"""
    response = httpx.Response(
        429,
        json={"error": {"code": "insufficient_quota"}},
        headers={"Retry-After": "Wed, 21 Oct 2037 07:28:00 GMT"},
    )
    cause = httpx.HTTPStatusError(
        "do not inspect message",
        request=httpx.Request("POST", "https://local.invalid"),
        response=response,
    )
    error = ModelRateLimitError("synthetic")
    error.__cause__ = cause
    evidence, retry = classify_failure(error)
    assert not retry
    assert evidence.category == "quota_exhausted"
    assert evidence.status_code == 429
    assert evidence.retry_after_seconds is not None and evidence.retry_after_seconds > 100
    unknown = ModelRateLimitError("insufficient_quota appears only in text")
    assert classify_failure(unknown)[1]


@pytest.mark.parametrize(
    "where,expected", [("begin", 0), ("start", 0), ("result", 1), ("finish", 1)]
)
async def test_authority_failure_blocks_further_attempts_and_new_requests(
    where: Literal["begin", "start", "result", "finish"], expected: int
) -> None:
    """各提交门槛失败均保留 StorageError 原型，并锁住本所有者的新请求。"""
    backend, sink = _Backend((ModelUnavailableError("synthetic"),)), _Sink(fail_at=where)
    if where == "finish":
        backend = _Backend()
    wrapper = _wrapper(backend, sink)
    for _ in range(2):
        with pytest.raises(StorageError) as caught:
            await wrapper.generate(_request(), model="openai/synthetic")
        assert caught.value is sink.failure
    assert len(backend.requests) == expected
    await wrapper.close()


async def test_diagnostic_overflow_keeps_authoritative_response_and_original_arguments() -> None:
    """普通预览容量不足只记录缺口，真实响应和修复前参数仍完整提交。"""
    sink = _Sink()
    wrapper = _wrapper(_Backend(), sink, diagnostic_bytes=0)
    response = await wrapper.generate(_request(), model="openai/synthetic")
    assert response.text == "complete"
    result = sink.results[0]
    assert result.state == "succeeded" and not result.diagnostics_complete
    assert result.omitted_events == 3
    assert result.diagnostic_events_json == "[]"
    assert json.loads(result.argument_events_json)[0]["arguments_delta"] == '{code: "pass"}'
    assert json.loads(cast(str, result.response_json))["message"]["content"][1]["arguments"] == {
        "code": "pass"
    }
    await wrapper.close()


async def test_cancellation_during_backoff_stops_retry_and_records_logical_cancellation() -> None:
    """抢占等待原样传播取消，不继续下一次传输。"""
    entered = asyncio.Event()

    async def wait(seconds: float) -> None:
        entered.set()
        await asyncio.Future[None]()

    backend, sink = _Backend((ModelUnavailableError("temporary"),)), _Sink()
    wrapper = _wrapper(backend, sink, sleep=wait)
    task = asyncio.create_task(wrapper.generate(_request(), model="openai/synthetic"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(backend.requests) == 1
    assert sink.outcomes[-1][1] == "cancelled"
    await wrapper.close()


async def test_timeout_discards_late_response_and_keeps_resource_borrow_visible() -> None:
    """底层吞取消不能突破采用截止，真实退出前 pending 借用仍可观察。"""
    release = asyncio.Event()
    entered = asyncio.Event()

    class LateBackend:
        async def generate(
            self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
        ) -> ModelResponse:
            entered.set()
            try:
                await asyncio.Future[None]()
            except asyncio.CancelledError:
                await release.wait()
            return ModelResponse(Message(MessageRole.ASSISTANT, content=(TextBlock("late"),)))

    sink = _Sink()
    wrapper = ModelGenerationBackend(
        LateBackend(),
        sink,
        scope=RequestScope("foreground", "turn"),
        budget=_budget(),
        policy=RetrySettings(attempts=1, attempt_seconds=0.02, total_seconds=0.1),
        diagnostic_bytes=100,
    )
    task = asyncio.create_task(wrapper.generate(_request(), model="openai/synthetic"))
    await entered.wait()
    with pytest.raises(TimeoutError):
        await task
    assert wrapper.pending_attempts == 1
    assert sink.results[0].state == "timed_out" and sink.results[0].response_json is None
    release.set()
    await wrapper.close()
    assert wrapper.pending_attempts == 0
    with pytest.raises(ModelRequestError):
        await wrapper.generate(_request(), model="openai/synthetic")


async def test_decision_retains_refusal_and_uses_one_logical_budget() -> None:
    """专用判断复用 Core 值，不将拒答变成概率零或自动换成聊天模型。"""

    class DecisionBackend:
        def __init__(self) -> None:
            self.requests: list[DecisionRequest] = []

        async def decide(self, request: DecisionRequest, *, model: str) -> DecisionResponse:
            self.requests.append(request)
            if len(self.requests) == 1:
                raise ModelUnavailableError("temporary")
            return DecisionResponse((DecisionRefusal("attend"),))

    backend, sink, budget = DecisionBackend(), _Sink(), _budget(count=1, seconds=8)
    wrapper = ModelDecisionBackend(
        backend,
        sink,
        scope=RequestScope("attention", "input"),
        budget=budget,
        policy=RetrySettings(attempt_seconds=5, total_seconds=8),
        diagnostic_bytes=0,
        sleep=_no_wait,
    )
    request = DecisionRequest("evidence", (PredicateQuestion("attend", "Should attend?"),))
    response = await wrapper.decide(request, model="typesafe/synthetic")
    assert isinstance(response.answers[0], DecisionRefusal)
    assert backend.requests[0] is backend.requests[1] is request
    assert budget.used == 1
    assert sink.requests[0].attempt_seconds == 5 and sink.requests[0].total_seconds == 8
    assert json.loads(sink.requests[0].materials_json)["payload"]["input"] == "evidence"
    await wrapper.close()


async def test_sqlite_roundtrip_and_interruption_preserve_attempt_facts(tmp_path: Path) -> None:
    """权威材料在真实数据库可重建，重启只补中断事实不重新请求。"""
    connection = sqlite3.connect(tmp_path / "evidence.sqlite")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("CREATE TABLE media (media_id TEXT PRIMARY KEY, content BLOB NOT NULL)")
    for statement in sql.SCHEMA:
        connection.execute(statement)
    connection.commit()
    sink = _Sink(connection=connection)
    wrapper = _wrapper(_Backend((ModelUnavailableError("transient"), None)), sink)
    await wrapper.generate(_request(), model="openai/synthetic")
    record = sql.get_request(connection, sink.requests[0].request_id)
    assert record is not None and record.request == sink.requests[0]
    assert tuple(item.result for item in record.attempts) == tuple(sink.results)
    assert record.state == "succeeded"
    with pytest.raises(StorageError, match="冲突"):
        sql.finish_request(connection, record.request.request_id, "failed")
    request = RequestEvidence(
        "interrupted",
        RequestScope("reflection", "turn"),
        "generation",
        "openai/synthetic",
        record.request.materials_json,
        "frozen",
        2,
        120,
        180,
    )
    sql.begin_request(connection, request)
    start = AttemptStart("interrupted", "unfinished-attempt", 1, 100)
    sql.begin_attempt(connection, start)
    connection.close()
    connection = sqlite3.connect(tmp_path / "evidence.sqlite")
    sql.initialize(connection)
    interrupted = sql.get_request(connection, "interrupted")
    assert interrupted is not None and interrupted.state == "interrupted"
    assert interrupted.attempts[0].result is not None
    assert interrupted.attempts[0].result.state == "interrupted"
    assert sql.get_request(connection, record.request.request_id) == record
    sql.initialize(connection)
    assert sql.get_request(connection, "interrupted") == interrupted
    connection.close()
    await wrapper.close()


async def test_actual_sdk_http_payload_matches_recorded_ordered_materials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """记录与真实 SDK 最终 HTTP 消息/schema 对比，不凭高层传参数断言等价。"""
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "generation.invalid"
        requests.append(request)
        chunk = {
            "id": "test",
            "model": "synthetic",
            "object": "chat.completion.chunk",
            "created": 1,
            "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
            request=request,
        )

    def reject(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "raw.githubusercontent.com"
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(respond)

    def async_transport(client: httpx.AsyncClient, url: httpx.URL) -> httpx.MockTransport:
        return transport

    def sync_transport(client: httpx.Client, url: httpx.URL) -> httpx.MockTransport:
        return httpx.MockTransport(reject)

    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", async_transport)
    monkeypatch.setattr(httpx.Client, "_transport_for_url", sync_transport)
    settings = GenerationModelSettings.model_validate(
        {
            "kind": "generation",
            "model": "openai/gemini-3.8-flash-high",
            "api_key": "synthetic-secret-never-recorded",
            "api_base": "https://generation.invalid/v1",
            "context_window": 10000,
            "max_output_tokens": 1000,
            "capacity_rule": "synthetic-test-rule",
        }
    )
    resource = OpenAIGenerationResource.create(settings)
    sink = _Sink()
    wrapper = ModelGenerationBackend(
        resource,
        sink,
        scope=RequestScope("foreground", "turn"),
        budget=_budget(),
        policy=RetrySettings(),
        diagnostic_bytes=4096,
    )
    try:
        response = await wrapper.generate(_request(), model=settings.model)
        assert response.text == "ok" and len(requests) == 1
        actual = cast(dict[str, object], json.loads(requests[0].content))
        expected = cast(dict[str, object], json.loads(sink.requests[0].materials_json)["payload"])
        assert actual["messages"] == expected["messages"]
        assert actual["tools"] == expected["tools"]
        assert "synthetic-secret-never-recorded" not in sql.encode(sink.requests)
        assert sink.requests[0].compiler.startswith("purrcept_litellm/0.2.")
    finally:
        await wrapper.close()
        await resource.close()


def test_compiler_excludes_transport_credentials_without_altering_prompt() -> None:
    """记录剔除传输字段，同时保持含相同字段词语的模型正文原文。"""
    request = ModelRequest(
        (Message(MessageRole.USER, content=(TextBlock("api_key is a word"),)),),
        provider_options={"purrcept_litellm": {"extra_headers": {"secret": "synthetic"}}},
    )
    encoded, _ = compile_materials(request, model="openai/synthetic")
    value = json.loads(encoded)
    assert value["payload"]["messages"][0]["content"] == "api_key is a word"
    assert value["excluded_transport_fields"] == ["extra_headers"]
    assert "synthetic" not in encoded.replace("openai/synthetic", "")


@pytest.mark.parametrize("path", ["generation", "decision"])
async def test_real_sdk_retry_has_two_http_attempts_and_preserves_same_payload(
    path: Literal["generation", "decision"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两条路径真实经过 SDK 错误翻译，Engine 重试仅增加第二次 HTTP 发送。"""
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "model.invalid"
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                503, json={"error": {"message": "temporary unavailable"}}, request=request
            )
        if path == "decision":
            return httpx.Response(
                200,
                json={
                    "model": "jev-1.13.0",
                    "answers": {
                        "attend": {"type": "noul", "noul": 0.8},
                    },
                },
                request=request,
            )
        chunk = {
            "id": "test",
            "model": "synthetic",
            "object": "chat.completion.chunk",
            "created": 1,
            "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
            request=request,
        )

    transport = httpx.MockTransport(respond)

    def select(client: httpx.AsyncClient, url: httpx.URL) -> httpx.MockTransport:
        return transport

    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", select)
    sink = _Sink()
    if path == "generation":
        generation_settings = GenerationModelSettings.model_validate(
            {
                "kind": "generation",
                "model": "openai/gemini-3.8-flash-high",
                "api_key": "synthetic-key",
                "api_base": "https://model.invalid/v1",
                "capacity_rule": "synthetic-test-rule",
                "context_window": 10000,
                "max_output_tokens": 1000,
            }
        )
        resource = OpenAIGenerationResource.create(generation_settings)
        wrapper = ModelGenerationBackend(
            resource,
            sink,
            scope=RequestScope("foreground", "turn"),
            budget=_budget(),
            policy=RetrySettings(),
            diagnostic_bytes=100,
            sleep=_no_wait,
        )
        try:
            await wrapper.generate(_request(), model=generation_settings.model)
        finally:
            await wrapper.close()
            await resource.close()
    else:
        decision_settings = DecisionModelSettings.model_validate(
            {
                "kind": "decision",
                "model": "typesafe/jev-1.13.0",
                "api_key": "synthetic-key",
                "api_base": "https://model.invalid/v1",
                "input_limit": 10000,
                "capacity_rule": "synthetic-test-rule",
            }
        )
        decision_resource = TypeSafeDecisionResource.create(decision_settings)
        decision_wrapper = ModelDecisionBackend(
            decision_resource,
            sink,
            scope=RequestScope("attention", "input"),
            budget=_budget(),
            policy=RetrySettings(attempt_seconds=5, total_seconds=8),
            diagnostic_bytes=0,
            sleep=_no_wait,
        )
        request = DecisionRequest(
            (TextBlock("first source"), TextBlock("second source")),
            (PredicateQuestion("attend", "Should attend?"),),
        )
        try:
            response = await decision_wrapper.decide(request, model=decision_settings.model)
            assert response.answers[0].name == "attend"
        finally:
            await decision_wrapper.close()
            await decision_resource.close()
        materials = json.loads(sink.requests[0].materials_json)["payload"]
        assert materials["input"][0]["content"] == [
            {"type": "input_text", "text": "first source"},
            {"type": "input_text", "text": "second source"},
        ]
        actual = json.loads(requests[-1].content)
        # 冻结 LiteLLM 的 _message_text 用空行保留输入块边界。
        assert actual["state"] == "first source\n\nsecond source"
        assert actual["questions"]["attend"]["instructions"] == "Should attend?"
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert [item.state for item in sink.results] == ["failed", "succeeded"]


async def test_store_start_commit_deadline_prevents_backend_entry() -> None:
    """提交耗时也受截止约束，落下开始记录不意味着后端仍有资格启动。"""

    class SlowSink(_Sink):
        async def begin_attempt(self, start: AttemptStart) -> None:
            await super().begin_attempt(start)
            await asyncio.sleep(0.03)

    backend, sink = _Backend(), SlowSink()
    wrapper = _wrapper(backend, sink, policy=RetrySettings(attempts=1, attempt_seconds=0.01))
    with pytest.raises(TimeoutError):
        await wrapper.generate(_request(), model="openai/synthetic")
    assert backend.requests == []
    assert sink.results[0].state == "timed_out"
    await wrapper.close()


async def test_buffered_protocol_violation_is_not_hidden_by_diagnostic_overflow() -> None:
    """预览省略仍须拒绝错误 Started 顺序，即使后端吞掉回调 TypeError。"""

    class InvalidStream(_Backend):
        async def generate(
            self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
        ) -> ModelResponse:
            self.requests.append(request)
            assert emit is not None
            emit(TextDelta("first"))
            with pytest.raises(TypeError):
                emit(ModelStreamStarted())
            return ModelResponse(Message(MessageRole.ASSISTANT, content=(TextBlock("invalid"),)))

    backend, sink = InvalidStream(), _Sink()
    wrapper = _wrapper(backend, sink, diagnostic_bytes=0)
    with pytest.raises(TypeError):
        await wrapper.generate(_request(), model="openai/synthetic")
    assert len(backend.requests) == 1 and sink.results[0].state == "failed"
    await wrapper.close()


async def test_dispatch_qualification_is_checked_inside_actual_backend_task() -> None:
    """开始事务已提交后仍会在真实 SDK 借用前重验轮次或宿主资格。"""
    checks = 0
    failure = ModelRequestError("generation no longer eligible", provider="engine")

    def can_start() -> None:
        nonlocal checks
        checks += 1
        if checks == 4:
            raise failure

    backend, sink = _Backend(), _Sink()
    wrapper = ModelGenerationBackend(
        backend,
        sink,
        scope=RequestScope("foreground", "turn"),
        budget=_budget(),
        policy=RetrySettings(),
        diagnostic_bytes=100,
        can_start=can_start,
    )
    with pytest.raises(ModelRequestError) as caught:
        await wrapper.generate(_request(), model="openai/synthetic")
    assert caught.value is failure
    assert len(sink.starts) == 1 and backend.requests == []
    assert sink.results[0].state == "failed"
    await wrapper.close()


async def test_request_cannot_reenable_hidden_sdk_retries() -> None:
    """请求级透传不能突破 Engine 唯一重试循环的边界。"""
    request = ModelRequest(
        (Message(MessageRole.USER, content=(TextBlock("input"),)),),
        provider_options={"purrcept_litellm": {"num_retries": 3}},
    )
    backend, sink = _Backend(), _Sink()
    wrapper = _wrapper(backend, sink)
    with pytest.raises(ModelRequestError, match="自动重试"):
        await wrapper.generate(request, model="openai/synthetic")
    assert backend.requests == [] and sink.requests == []
    await wrapper.close()


_MEDIA = b"isolated-shared-image-and-audio"


def _media_request(content: bytes = _MEDIA) -> ModelRequest:
    """构造不同媒体语义共享同一字节的材料，并保留 URL 与正文。"""
    uri = "data:image/png;base64," + base64.b64encode(content).decode("ascii")
    return ModelRequest(
        (
            Message(
                MessageRole.USER,
                content=(
                    TextBlock("literal media URL: " + uri),
                    ImageBlock(ImageBytes(content, "image/png")),
                    AudioBlock(AudioBytes(content)),
                    ImageBlock(ImageUrl("https://remote.invalid/never-fetched.png")),
                ),
            ),
        ),
        tools=_request().tools,
    )


class _MediaBackend:
    """提供完整 Core 媒体响应；测试不运行工具或读取真实设备。"""

    def __init__(self, content: bytes = _MEDIA, *, fail_first: bool = False) -> None:
        self.content = content
        self.fail_first = fail_first
        self.requests: list[ModelRequest] = []

    async def generate(
        self,
        request: ModelRequest,
        *,
        model: str,
        emit: ModelEventSink | None = None,
    ) -> ModelResponse:
        """第一次可报告暂时故障，成功材料包含必须保持原义的工具 JSON。"""
        self.requests.append(request)
        if emit is not None:
            emit(ModelStreamStarted(model=model))
            emit(ToolCallDelta(0, '{"code":"pass",}', tool_call_id="call", name="play_action"))
        if self.fail_first and len(self.requests) == 1:
            raise ModelUnavailableError("synthetic transport failure")
        response = ModelResponse(
            Message(
                MessageRole.ASSISTANT,
                content=(
                    ImageBlock(ImageBytes(self.content, "image/png")),
                    AudioBlock(AudioBytes(self.content)),
                    ToolCallBlock(
                        "call",
                        "play_action",
                        arguments={
                            "code": "pass",
                            "opaque": {
                                "type": "ImageBytes",
                                "data": {"type": "bytes", "base64": "keep this text"},
                            },
                        },
                    ),
                ),
            )
        )
        if emit is not None:
            emit(ModelStreamCompleted(response))
        return response


def _request_ids(path: Path) -> tuple[str, ...]:
    """只读核对专属线程已经提交的身份，不依赖替身内存投影。"""
    with sqlite3.connect(path) as connection:
        rows: list[tuple[str]] = connection.execute(
            "SELECT request_id FROM model_requests ORDER BY created_at"
        ).fetchall()
        return tuple(row[0] for row in rows)


async def test_real_store_sink_reconstructs_media_and_independent_attempts(tmp_path: Path) -> None:
    """真实 Store 单连接接管输入、请求、响应，同字节不重复保存 BLOB。"""
    path = tmp_path / "model.sqlite"
    store = await Store.open(path)
    request = _media_request()
    material, _ = compile_materials(request, model="openai/synthetic")
    budget = _budget(count=2)
    backend = _MediaBackend(fail_first=True)
    try:
        occurred = datetime(2026, 10, 13, tzinfo=UTC)
        admission = await store.admit(
            InputSubmission(
                InputSource("synthetic", "local", "scope", "message", "event"),
                "input",
                occurred,
                occurred,
                (MediaAsset(_MEDIA, "image/png"),),
            )
        )
        wrapper = ModelGenerationBackend(
            backend,
            store,
            scope=RequestScope("foreground", "turn", (admission.input_id,)),
            budget=budget,
            policy=RetrySettings(),
            diagnostic_bytes=0,
            sleep=_no_wait,
        )
        try:
            responses: list[ModelResponse] = []
            for _ in range(2):
                response = await wrapper.generate(request, model="openai/synthetic")
                responses.append(response)
                assert response.message.content[0] == ImageBlock(ImageBytes(_MEDIA, "image/png"))
            with pytest.raises(RequestBudgetExceeded):
                await wrapper.generate(request, model="openai/synthetic")
            assert budget.used == budget.max_requests == 2
            assert len(backend.requests) == 3
            identities = _request_ids(path)
            assert len(identities) == 2
            records = [await store.get_request(identity) for identity in identities]
            assert all(record is not None and record.state == "succeeded" for record in records)
            for record in records:
                assert record is not None
                assert record.request.materials_json == material
                assert record.request.scope.input_ids == (admission.input_id,)
                result = record.attempts[-1].result
                assert result is not None and result.response_json == sql.encode(responses[-1])
                opaque = json.loads(cast(str, result.response_json))["message"]["content"][2][
                    "arguments"
                ]["opaque"]
                assert opaque == {
                    "type": "ImageBytes",
                    "data": {"type": "bytes", "base64": "keep this text"},
                }
            first = records[0]
            assert first is not None
            assert [item.result.state for item in first.attempts if item.result] == [
                "failed",
                "succeeded",
            ]
            with sqlite3.connect(path) as connection:
                media: list[tuple[str, bytes]] = connection.execute(
                    "SELECT media_id,content FROM media"
                ).fetchall()
                assert media == [(hashlib.sha256(_MEDIA).hexdigest(), _MEDIA)]
                assert connection.execute(
                    "SELECT count(*) FROM model_request_media"
                ).fetchone() == (2,)
                assert connection.execute(
                    "SELECT count(*) FROM model_attempt_media"
                ).fetchone() == (2,)
                raw: str = connection.execute(
                    "SELECT evidence_json FROM model_requests LIMIT 1"
                ).fetchone()[0]
                interned = json.loads(json.loads(raw)["materials_json"])["payload"]["messages"][0][
                    "content"
                ]
                assert (
                    interned[0]["text"]
                    == json.loads(material)["payload"]["messages"][0]["content"][0]["text"]
                )
                assert interned[1]["image_url"]["url"]["purrcept_media_id"] == media[0][0]
                assert interned[2]["input_audio"]["data"]["purrcept_media_id"] == media[0][0]
                assert interned[3]["image_url"]["url"] == "https://remote.invalid/never-fetched.png"
        finally:
            await wrapper.close()
    finally:
        await store.close()
    reopened = await Store.open(path)
    try:
        assert await reopened.get_request(identities[0]) == records[0]
    finally:
        await reopened.close()


@pytest.mark.parametrize("table", ["model_request_media", "model_attempt_media"])
async def test_real_store_media_reference_failure_rolls_back_whole_commit(
    tmp_path: Path,
    table: Literal["model_request_media", "model_attempt_media"],
) -> None:
    """引用写失败不能留下新 BLOB、半个请求或虚假成功结果，且阻止再派发。"""
    path = tmp_path / "fault.sqlite"
    store = await Store.open(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"CREATE TRIGGER reject_media BEFORE INSERT ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'synthetic reference write fault'); END"
        )
    backend = _MediaBackend(b"new-response-media")
    wrapper = ModelGenerationBackend(
        backend,
        store,
        scope=RequestScope("foreground", "turn"),
        budget=_budget(),
        policy=RetrySettings(),
        diagnostic_bytes=0,
    )
    try:
        with pytest.raises(StorageError):
            await wrapper.generate(_media_request(b"request-media"), model="openai/synthetic")
        assert not store.authority_available
        with pytest.raises(StorageError):
            await wrapper.generate(_media_request(), model="openai/synthetic")
        with sqlite3.connect(path) as connection:
            content: list[tuple[bytes]] = connection.execute("SELECT content FROM media").fetchall()
            if table == "model_request_media":
                assert backend.requests == [] and content == []
                assert connection.execute("SELECT count(*) FROM model_requests").fetchone() == (0,)
            else:
                assert len(backend.requests) == 1 and content == [(b"request-media",)]
                assert connection.execute("SELECT result_json FROM model_attempts").fetchone() == (
                    None,
                )
    finally:
        await wrapper.close()
        await store.close()


@pytest.mark.parametrize("fault", ["missing", "corrupt"])
async def test_real_store_request_media_loss_is_explicit(tmp_path: Path, fault: str) -> None:
    """引用缺失或 BLOB 摘要不符均拒绝伪造公开请求，不读取外部媒体。"""
    path = tmp_path / "loss.sqlite"
    store = await Store.open(path)
    wrapper = ModelGenerationBackend(
        _MediaBackend(),
        store,
        scope=RequestScope("foreground", "turn"),
        budget=_budget(),
        policy=RetrySettings(),
        diagnostic_bytes=0,
    )
    try:
        await wrapper.generate(_media_request(), model="openai/synthetic")
        identity = _request_ids(path)[0]
        with sqlite3.connect(path) as connection:
            if fault == "missing":
                connection.execute("DELETE FROM media")
            else:
                connection.execute("UPDATE media SET content=?", (b"corrupted bytes",))
        with pytest.raises(StorageError) as caught:
            await store.get_request(identity)
        assert caught.value.code == "invalid_record"
    finally:
        await wrapper.close()
        await store.close()


def test_core_checkpoint_media_roundtrip_and_transaction_ownership(tmp_path: Path) -> None:
    """检查点沿正式历史/提醒关系引用媒体，正文与工具 JSON 不按样子改写。"""
    checkpoint = ConversationCheckpoint(
        ConversationState(
            (
                Message(
                    MessageRole.USER,
                    content=(
                        TextBlock(
                            "literal data:image/png;base64," + base64.b64encode(_MEDIA).decode()
                        ),
                        ToolResultBlock(
                            "prior-call", content=(ImageBlock(ImageBytes(_MEDIA, "image/png")),)
                        ),
                        ToolCallBlock(
                            "opaque-call",
                            "play_action",
                            arguments={"type": "ImageBytes", "data": "unchanged"},
                        ),
                    ),
                ),
            )
        ),
        ReminderState((SystemReminder((AudioBlock(AudioBytes(_MEDIA)),)),)),
    )
    original = sql.encode(checkpoint)
    connection = sqlite3.connect(tmp_path / "checkpoint.sqlite")
    connection.execute("CREATE TABLE media (media_id TEXT PRIMARY KEY, content BLOB NOT NULL)")
    connection.commit()
    try:
        connection.execute("BEGIN IMMEDIATE")
        interned, references = sql.intern_core_media(connection, original)
        assert connection.in_transaction
        assert references == (hashlib.sha256(_MEDIA).hexdigest(),)
        assert sql.restore_core_media(connection, interned) == original
        assert connection.execute("SELECT count(*) FROM media").fetchone() == (1,)
        connection.rollback()
        assert connection.execute("SELECT count(*) FROM media").fetchone() == (0,)
        with pytest.raises(StorageError, match="缺失"):
            sql.restore_core_media(connection, interned)
    finally:
        connection.close()


async def test_finish_commit_deadline_keeps_fact_but_forwards_no_core_events(
    tmp_path: Path,
) -> None:
    """完整响应提交后过期仍留成功事实，但不交给 Core 新工具或任何片段。"""
    path = tmp_path / "slow.sqlite"
    store = await Store.open(path)
    request = _request()
    compile_materials(request, model="openai/synthetic")

    class SlowFinishSink:
        async def begin_request(self, request: RequestEvidence) -> None:
            await store.begin_request(request)

        async def begin_attempt(self, start: AttemptStart) -> None:
            await store.begin_attempt(start)

        async def finish_attempt(self, result: AttemptEvidence) -> None:
            await store.finish_attempt(result)

        async def finish_request(self, request_id: str, state: RequestOutcome) -> None:
            await store.finish_request(request_id, state)
            await asyncio.sleep(0.08)

    budget = _budget(count=1)
    backend = _Backend()
    wrapper = ModelGenerationBackend(
        backend,
        SlowFinishSink(),
        scope=RequestScope("foreground", "turn"),
        budget=budget,
        policy=RetrySettings(attempts=1, total_seconds=0.05),
        diagnostic_bytes=4096,
    )
    progress: list[object] = []
    context = ExecutionContext("run", "effect", 0, object(), _progress_callback=progress.append)
    try:
        with pytest.raises(RequestBudgetExceeded):
            await Model(wrapper, "openai/synthetic").generate(request).execute(context)
        assert progress == [] and len(backend.requests) == 1 and budget.used == 1
        record = await store.get_request(_request_ids(path)[0])
        assert record is not None and record.state == "succeeded"
        result = record.attempts[0].result
        assert (
            result is not None and result.state == "succeeded" and result.response_json is not None
        )
    finally:
        await wrapper.close()
        await store.close()


@pytest.mark.parametrize("diagnostic_bytes", [0, 120, 4096])
async def test_successful_retry_replay_obeys_real_core_progress_protocol(
    diagnostic_bytes: int,
) -> None:
    """使用真正 Generate 协议检查全量、部分及省略预览都只交付成功尝试。"""
    backend, sink = _Backend((ModelUnavailableError("synthetic"), None)), _Sink()
    wrapper = _wrapper(backend, sink, diagnostic_bytes=diagnostic_bytes)
    progress: list[object] = []
    context = ExecutionContext("run", "effect", 0, object(), _progress_callback=progress.append)
    try:
        response = await Model(wrapper, "openai/synthetic").generate(_request()).execute(context)
        assert isinstance(progress[-1], ModelStreamCompleted)
        assert progress[-1].response == response
        assert len([event for event in progress if isinstance(event, ModelStreamCompleted)]) == 1
        assert all(event.delta != "attempt-0" for event in progress if isinstance(event, TextDelta))
        assert [result.state for result in sink.results] == ["failed", "succeeded"]
    finally:
        await wrapper.close()


@pytest.mark.parametrize("commit_fails", [False, True])
async def test_repeated_cancellation_leaves_accepted_commit_owned_until_close(
    commit_fails: bool,
) -> None:
    """两次取消不能使已接管权威提交无人负责，关闭等待真实提交退出。"""
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingSink(_Sink):
        async def finish_attempt(self, result: AttemptEvidence) -> None:
            entered.set()
            await release.wait()
            await super().finish_attempt(result)

    sink = BlockingSink(fail_at="result" if commit_fails else None)
    wrapper = _wrapper(_Backend(), sink)
    task = asyncio.create_task(wrapper.generate(_request(), model="openai/synthetic"))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    closing = asyncio.create_task(wrapper.close())
    try:
        done, _ = await asyncio.wait((closing,), timeout=0.02)
        assert not done
    finally:
        release.set()
        await closing
    if commit_fails:
        with pytest.raises(StorageError) as caught:
            await wrapper.generate(_request(), model="openai/synthetic")
        assert caught.value is sink.failure
    else:
        assert sink.results[0].state == "succeeded"


@pytest.mark.parametrize(
    "encoded",
    [
        "{",
        '{"type":"ModelResponse"}',
        '{"type":"ModelResponse","message":{"type":"Message","content":[]},"extra":NaN}',
        '{"type":"ModelResponse","message":{"type":"Message","content":[]},"extra":1e999}',
        '{"type":"ImageBlock","source":{"type":"ImageBytes","data":{"type":"bytes"}}}',
    ],
)
def test_core_media_codec_rejects_damaged_json_with_explicit_storage_reason(
    tmp_path: Path,
    encoded: str,
) -> None:
    """损坏 JSON、非有限数字或缺少媒体字段报告权威损坏，不伪造空材料。"""
    connection = sqlite3.connect(tmp_path / "damaged.sqlite")
    connection.execute("CREATE TABLE media (media_id TEXT PRIMARY KEY, content BLOB NOT NULL)")
    connection.commit()
    try:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(StorageError) as caught:
            sql.intern_core_media(connection, encoded)
        assert caught.value.code == "invalid_record" and connection.in_transaction
        connection.rollback()
        assert connection.execute("SELECT count(*) FROM media").fetchone() == (0,)
    finally:
        connection.close()


def test_core_media_intern_does_not_reuse_existing_damaged_blob(tmp_path: Path) -> None:
    """现有摘要身份指向错误字节时明确失败，不覆盖旧 BLOB 或续用坏引用。"""
    connection = sqlite3.connect(tmp_path / "existing.sqlite")
    connection.execute("CREATE TABLE media (media_id TEXT PRIMARY KEY, content BLOB NOT NULL)")
    identity = hashlib.sha256(_MEDIA).hexdigest()
    connection.execute("INSERT INTO media VALUES (?, ?)", (identity, b"old corrupt bytes"))
    connection.commit()
    response = ModelResponse(
        Message(MessageRole.ASSISTANT, content=(ImageBlock(ImageBytes(_MEDIA, "image/png")),))
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(StorageError) as caught:
            sql.intern_core_media(connection, sql.encode(response))
        assert caught.value.code == "invalid_record"
        connection.rollback()
        assert connection.execute("SELECT content FROM media").fetchone() == (b"old corrupt bytes",)
    finally:
        connection.close()
