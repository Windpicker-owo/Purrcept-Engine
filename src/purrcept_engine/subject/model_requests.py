"""在 Core 后端边界保存请求证据并执行唯一的有界传输重试。

主体为一个连续批次创建 RequestBudget，前台和反思各自持有预算与后端包装。
包装不复制 Core 工具循环、不修改提示或更新会话历史；它冻结一次实际编译材料，
各尝试借用同一 Core 请求。开始与结果先经权威 sink 提交，成功流才交回 Core。
底层资源必须已经关闭 SDK 隐式重试。本模块不取得凭据、客户端或 SQLite 连接。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from importlib import import_module
from importlib.metadata import version
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import uuid4

import httpx
from purrcept_core.models import (
    DecisionBackend,
    DecisionRequest,
    DecisionResponse,
    ModelAuthenticationError,
    ModelBackend,
    ModelContextWindowError,
    ModelEventSink,
    ModelRateLimitError,
    ModelRequest,
    ModelRequestError,
    ModelResponse,
    ModelStreamCompleted,
    ModelStreamEvent,
    ModelStreamStarted,
    ModelUnavailableError,
    ToolCallDelta,
)

from purrcept_engine.storage.model_requests import (
    AttemptEvidence,
    AttemptStart,
    FailureEvidence,
    RequestEvidence,
    RequestKind,
    RequestOutcome,
    RequestScope,
    encode,
)


class RetryPolicy(Protocol):
    """已校验配置的最小只读面；app 配置与隔离测试可直接实现。

    attempts 包含首次，attempt_seconds 限制单次，total_seconds 包含全部尝试和
    等待。主体选择用途对应配置；注意判断使用 2/5/8，生成使用 2/120/180。
    包装不接受模型内容修改这些值，也不将一次重试算为新的逻辑请求。
    """

    @property
    def attempts(self) -> int:
        """返回含首次的已校验尝试上限。"""
        ...

    @property
    def attempt_seconds(self) -> float:
        """返回每次尝试的最长等待时间。"""
        ...

    @property
    def total_seconds(self) -> float:
        """返回同一逻辑请求含退避的最长时间。"""
        ...


class RequestEvidenceSink(Protocol):
    """主体借用的权威提交端口，生产实现复用 Store 单线程队列。

    所有方法返回意味着材料已提交，而不是只加入诊断缓冲。失败原样传播并
    阻止包装的后续调用。方法须按 Store 契约保留已接管写入：等待者取消不能
    撤销已接受事务。测试可用内存实现，生产不能以它替代失效的权威库。
    接口值可用 storage.model_requests.encode 计算实际 UTF-8 接管字节量。
    """

    async def begin_request(self, request: RequestEvidence) -> None:
        """保存同一逻辑请求的不可变公开编译材料与因果引用。"""
        ...

    async def begin_attempt(self, start: AttemptStart) -> None:
        """先保存一次尝试身份；成功前不得调用底层后端。"""
        ...

    async def finish_attempt(self, result: AttemptEvidence) -> None:
        """保存本次独立结果、原参数与有限诊断覆盖。"""
        ...

    async def finish_request(self, request_id: str, state: RequestOutcome) -> None:
        """保存逻辑终态，不覆盖各次尝试。"""
        ...


class RequestBudgetExceeded(ModelRequestError):
    """连续批次或请求截止耗尽；不会触发一次新传输重试。"""


class RequestBudget:
    """由主体创建、单事件循环使用的整轮逻辑数量与固定截止。

    主体在开始主动处理时创建；组装材料、工具等待和退避都消耗此截止。
    generation 包装只 claim 一次，不自动重置预算。摘要和注意判断可各用
    单请求预算，不借前台整轮名额。撤销使以后 claim/attempt 立即失去资格，
    主体仍须取消当前调用以及时传播抢占或关闭。
    """

    def __init__(self, *, max_requests: int, deadline: float) -> None:
        """接收已确定的批次上限和单调截止，不在包装中重新起算整轮。"""
        if isinstance(max_requests, bool) or max_requests <= 0 or not math.isfinite(deadline):
            raise ValueError("请求预算需要正数上限及有限截止。")
        self._max_requests = max_requests
        self._deadline = deadline
        self._used = 0
        self._revoked = False

    @property
    def max_requests(self) -> int:
        """返回本轮固定的逻辑名额上限。"""
        return self._max_requests

    @property
    def deadline(self) -> float:
        """返回本轮原始单调截止，逐请求重试不会延长。"""
        return self._deadline

    @property
    def used(self) -> int:
        """返回已经接管的逻辑调用数，传输重试不再占名额。"""
        return self._used

    def claim(self) -> float:
        """原子消耗一个逻辑名额并返回原批次截止，重试不得再次调用。"""
        self.check()
        if self.used >= self.max_requests:
            raise RequestBudgetExceeded("连续批次的逻辑模型请求额度耗尽", provider="engine")
        self._used += 1
        return self.deadline

    def check(self) -> None:
        """在新尝试和等待后核验原轮次仍有效。"""
        if self._revoked or asyncio.get_running_loop().time() >= self.deadline:
            raise RequestBudgetExceeded("连续批次已结束或达到截止", provider="engine")

    def revoke(self) -> None:
        """主体取消、抢占或关闭时撤销后续尝试资格，不伪报服务端停止。"""
        self._revoked = True


class _GenerationCompiler(Protocol):
    """冻结 SDK 纯编译入口的精确借用签名，不持有传输资源。"""

    def __call__(
        self, request: ModelRequest, *, model: str, config: object
    ) -> dict[str, object]: ...


class _DecisionCompiler(Protocol):
    """专用判断纯编译接缝，保持 SDK 候选与输入表示。"""

    def __call__(
        self, request: DecisionRequest, *, model: str, config: object
    ) -> dict[str, object]: ...


_TRANSPORT_FIELDS = frozenset(
    {
        "api_key",
        "api_base",
        "api_version",
        "timeout",
        "client",
        "http_client",
        "headers",
        "extra_headers",
        "authorization",
        "organization",
        "project",
        "success_callback",
        "failure_callback",
        "callbacks",
        "callback",
        "metadata",
        "access_token",
        "refresh_token",
        "password",
        "credentials",
    }
)


async def prepare_off_loop[T](operation: Callable[[], T]) -> T:
    """在线程中串行完成一次本地请求准备，避免阻塞实时收音和停音。

    调用方在返回前独占材料及交付状态；操作不得启动模型、提交事务或
    操作设备。取消不能停止 Python 线程，因此仍等待原工作归还，再传播
    取消，禁止其与下一轮共享状态竞争。没有逐帧任务或后台准备队列。
    """
    task = asyncio.create_task(asyncio.to_thread(operation), name="model-local-preparation")
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def compile_materials(request: ModelRequest | DecisionRequest, *, model: str) -> tuple[str, str]:
    """借用冻结 SDK 的纯编译器，记录真实顺序且不给编译器凭据。

    当前 SDK 未公开编译入口，因此这处接缝精确绑定 0.2.x 的两个模块并记录
    源码摘要。它不再实现消息或 TypeSafe 协议转换。公开生成参数保持原值；
    明确传输字段仅记录排除名称，不能将秘密写入原始请求证据。
    """
    if isinstance(request, ModelRequest):
        module = import_module("purrcept_litellm._request")
        compiler = cast(_GenerationCompiler, module.__dict__["compile_request"])
        config_module = import_module("purrcept_litellm.config")
        factory = cast(Callable[..., object], config_module.__dict__["LiteLLMConfig"])
        config = factory(default_options={"num_retries": 0, "max_retries": 0, "drop_params": False})
        payload = compiler(request, model=model, config=config)
    else:
        module = import_module("purrcept_litellm._decision")
        decision_compiler = cast(_DecisionCompiler, module.__dict__["compile_decision"])
        config_module = import_module("purrcept_litellm.decision")
        factory = cast(Callable[..., object], config_module.__dict__["LiteLLMDecisionConfig"])
        config = factory(default_options={"num_retries": 0, "max_retries": 0})
        payload = decision_compiler(request, model=model, config=config)

    for field in ("num_retries", "max_retries"):
        if payload.get(field, 0) != 0:
            raise ModelRequestError("SDK 自动重试必须显式关闭", provider="engine", model=model)
    public = {key: value for key, value in payload.items() if key not in _TRANSPORT_FIELDS}
    excluded = sorted(key for key in payload if key in _TRANSPORT_FIELDS)
    filename: object = module.__dict__.get("__file__")
    if not isinstance(filename, str):
        raise TypeError("冻结 SDK 编译器没有可核验源码。")
    fingerprint = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
    identity = (
        f"purrcept_litellm/{version('purrcept_litellm')}:litellm/{version('litellm')}:"
        f"{module.__name__}:{fingerprint}"
    )
    return encode({"payload": public, "excluded_transport_fields": excluded}), identity


class _EventBuffer:
    """一次尝试的流材料；原参数权威保存，预览缓存按诊断字节有限保留。"""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.bytes = 0
        self.events: list[ModelStreamEvent] = []
        self.arguments: list[ModelStreamEvent] = []
        self.omitted = 0
        self.active = True
        self.completed: ModelResponse | None = None
        self.seen_event = False
        self.protocol_error: TypeError | None = None

    def __call__(self, event: ModelStreamEvent) -> None:
        # 超时或取消撤销采用资格；迟到材料不能被交回 Core 或拼进新尝试。
        if not self.active:
            return
        if self.protocol_error is not None:
            raise self.protocol_error
        # 缓冲隔离重试并可省略诊断片段，因此 Core 看不到完整失败时序。
        # 在此真实后端边界核验原流，不能让缓存省略掩盖后端协议违约。
        try:
            if not isinstance(event, ModelStreamEvent):
                raise TypeError("后端必须报告 ModelStreamEvent。")
            if self.completed is not None:
                raise TypeError("后端不能在 Completed 之后报告事件。")
            if isinstance(event, ModelStreamStarted) and self.seen_event:
                raise TypeError("Started 必须是每次尝试的第一个且唯一开始事件。")
            self.seen_event = True
        except TypeError as error:
            self.protocol_error = error
            raise
        if isinstance(event, ToolCallDelta) or (
            isinstance(event, ModelStreamStarted)
            and "purrcept_tool_arguments" in event.provider_metadata
        ):
            self.arguments.append(event)
        # Completed 内含完整响应，由权威 response_json 保存，避免诊断重复复制。
        if isinstance(event, ModelStreamCompleted):
            self.completed = event.response
            return
        size = len(encode(event).encode("utf-8"))
        if self.bytes + size > self.limit:
            self.omitted += 1
            return
        self.bytes += size
        self.events.append(event)

    def validate_response(self, response: ModelResponse | DecisionResponse) -> None:
        """不能采用吞掉观察回调错误或 Completed 与返回值冲突的响应。"""
        if self.protocol_error is not None:
            raise self.protocol_error
        if self.completed is not None and self.completed != response:
            raise TypeError("Completed 的响应必须等于后端返回值。")


class _RetryRunner:
    """共享生成与判断的尝试事务和生命周期，不改变二者的协议值。"""

    def __init__(
        self,
        sink: RequestEvidenceSink,
        *,
        scope: RequestScope,
        budget: RequestBudget,
        policy: RetryPolicy,
        diagnostic_bytes: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        can_start: Callable[[], None] | None = None,
    ) -> None:
        if diagnostic_bytes < 0:
            raise ValueError("诊断字节上限不能为负。")
        self.sink = sink
        self.scope = scope
        self.budget = budget
        self.policy = policy
        self.diagnostic_bytes = diagnostic_bytes
        self.sleep = sleep
        self.can_start = can_start
        self._evidence_failure: Exception | None = None
        self._closed = False
        self._tasks: set[asyncio.Task[object]] = set()
        self._commits: set[asyncio.Future[None]] = set()
        self._close_task: asyncio.Task[None] | None = None

    @property
    def pending_attempts(self) -> int:
        """返回仍未实际结束的后端任务数，超时不等于已经收回借用。"""
        return len(self._tasks)

    async def close(self) -> None:
        """停止新调用并收尾已登记尝试；等待取消不撤销同一个清理任务。

        此关闭收回本包装的在途借用并等待已接管证据提交，不关闭 app 拥有的
        SDK 资源或 Store。宿主以其总关闭截止等待；后端或提交未实际结束时
        本任务继续存在，不能报告已释放。
        """
        self._closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._drain(), name="model-attempts-close")
        await asyncio.shield(self._close_task)

    async def _drain(self) -> None:
        pending = tuple(self._tasks)
        for task in pending:
            task.cancel()
        if pending:
            # 异常已经属于各尝试，关闭只等待借用结束，不将它们改为资源关闭故障。
            await asyncio.gather(*pending, return_exceptions=True)
        # 等待者可能被再次取消而已离开 _commit，但已接管写入仍必须有所有者。
        # 不取消这些提交；Store 自己的排空规则与这里的借用寿命保持一致。
        while self._commits:
            await asyncio.gather(*tuple(self._commits), return_exceptions=True)

    async def _commit(self, operation: Awaitable[None]) -> None:
        """记录权威失败并保留原型；取消等候者不取消已经交接的事务。"""
        task = asyncio.ensure_future(operation)
        self._commits.add(task)
        task.add_done_callback(self._commit_done)
        try:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await asyncio.shield(task)
                raise
        except Exception as error:
            # 这是唯一权威边界的失败锁存，不转换内部异常，不重试存储。
            self._evidence_failure = error
            raise

    def _commit_done(self, task: asyncio.Future[None]) -> None:
        """收取被取消等待者留下的提交异常，权威失败仍锁住以后请求。"""
        self._commits.discard(task)
        if not task.cancelled():
            error = task.exception()
            if isinstance(error, Exception):
                self._evidence_failure = error

    def _check(self) -> None:
        if self._evidence_failure is not None:
            raise self._evidence_failure
        if self._closed:
            raise ModelRequestError("模型请求所有者已开始关闭", provider="engine")
        self.budget.check()
        if self.can_start is not None:
            self.can_start()

    async def _invoke[Response](
        self,
        invoke: Callable[[_EventBuffer], Awaitable[Response]],
        buffer: _EventBuffer,
        deadline: float,
    ) -> Response:
        """截止撤销采用资格，保留拒绝取消的后端借用直到实际退出。"""

        async def call_backend() -> Response:
            # create_task 到调度运行之间可能发生抢占或关闭；资格必须在真正
            # 借用 SDK 资源前重验，不能只依赖创建任务时的有效快照。
            self._check()
            return await invoke(buffer)

        task = asyncio.create_task(call_backend(), name="model-attempt")
        owned = cast(asyncio.Task[object], task)
        self._tasks.add(owned)
        task.add_done_callback(self._attempt_done)
        try:
            done, _ = await asyncio.wait(
                (task,), timeout=max(0.0, deadline - asyncio.get_running_loop().time())
            )
            if not done:
                buffer.active = False
                task.cancel()
                raise TimeoutError("单次模型尝试达到截止")
            response = task.result()
            # 即使底层吞掉取消并返回，失效或迟到的响应也不能采用。
            self._check()
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("模型响应在截止后到达")
            return response
        except asyncio.CancelledError:
            buffer.active = False
            task.cancel()
            raise

    def _attempt_done(self, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            # 迟到任务可能已无人 await；取得异常防止丢失所有者和未观察告警。
            task.exception()

    async def _run[Response: ModelResponse | DecisionResponse](
        self,
        request: ModelRequest | DecisionRequest,
        *,
        kind: RequestKind,
        model: str,
        invoke: Callable[[_EventBuffer], Awaitable[Response]],
        emit: ModelEventSink | None = None,
    ) -> Response:
        self._check()
        turn_deadline = self.budget.claim()
        started = asyncio.get_running_loop().time()
        deadline = min(turn_deadline, started + self.policy.total_seconds)
        materials, compiler = await prepare_off_loop(
            lambda: compile_materials(request, model=model)
        )
        self._check()
        capacity_json = request.metadata.get("engine.capacity")
        if capacity_json is not None and not isinstance(capacity_json, str):
            raise TypeError("Engine 容量事实必须是完整 JSON 文本。")
        evidence = RequestEvidence(
            str(uuid4()),
            self.scope,
            kind,
            model,
            materials,
            compiler,
            self.policy.attempts,
            self.policy.attempt_seconds,
            self.policy.total_seconds,
            capacity_json,
        )
        await self._commit(self.sink.begin_request(evidence))

        for sequence in range(1, self.policy.attempts + 1):
            try:
                self._check()
            except Exception as error:
                gate_state: RequestOutcome = (
                    "cancelled"
                    if self._closed
                    else "timed_out"
                    if isinstance(error, RequestBudgetExceeded)
                    else "failed"
                )
                await self._commit(self.sink.finish_request(evidence.request_id, gate_state))
                raise
            attempt_deadline = min(
                deadline, asyncio.get_running_loop().time() + self.policy.attempt_seconds
            )
            if attempt_deadline <= asyncio.get_running_loop().time():
                await self._commit(self.sink.finish_request(evidence.request_id, "timed_out"))
                raise RequestBudgetExceeded("逻辑模型请求达到截止", provider="engine")
            start = AttemptStart(evidence.request_id, str(uuid4()), sequence, attempt_deadline)
            await self._commit(self.sink.begin_attempt(start))
            buffer = _EventBuffer(self.diagnostic_bytes)
            try:
                self._check()
                if asyncio.get_running_loop().time() >= attempt_deadline:
                    raise TimeoutError("模型尝试开始记录提交后已达到截止")
                response = await self._invoke(invoke, buffer, attempt_deadline)
                buffer.validate_response(response)
            except asyncio.CancelledError:
                await self._commit(
                    self.sink.finish_attempt(_attempt_result(start, buffer, "cancelled"))
                )
                await self._commit(self.sink.finish_request(evidence.request_id, "cancelled"))
                raise
            except Exception as error:
                # 主体边界保存原故障后原样抛出；只有明确的传输分类进入重试分支。
                failure, retryable = classify_failure(error)
                state: Literal["failed", "timed_out"] = (
                    "timed_out" if isinstance(error, TimeoutError) else "failed"
                )
                wait = max(1.0, failure.retry_after_seconds or 0.0)
                remaining = deadline - asyncio.get_running_loop().time()
                retry = retryable and sequence < self.policy.attempts and wait < remaining
                await self._commit(
                    self.sink.finish_attempt(
                        _attempt_result(
                            start,
                            buffer,
                            state,
                            failure=failure,
                            retry_wait=wait if retry else None,
                        )
                    )
                )
                if not retry:
                    await self._commit(self.sink.finish_request(evidence.request_id, state))
                    raise
                try:
                    # 权威提交也消耗逻辑时间。不能依据提交前的剩余量开始一个
                    # 已无法完整等待的 Retry-After，更不能提前重发。
                    if asyncio.get_running_loop().time() + wait >= deadline:
                        await self._commit(self.sink.finish_request(evidence.request_id, state))
                        raise error
                    await self.sleep(wait)
                    self._check()
                except asyncio.CancelledError:
                    await self._commit(self.sink.finish_request(evidence.request_id, "cancelled"))
                    raise
                except RequestBudgetExceeded:
                    await self._commit(self.sink.finish_request(evidence.request_id, "timed_out"))
                    raise
                continue

            buffer.active = False
            await self._commit(
                self.sink.finish_attempt(
                    _attempt_result(start, buffer, "succeeded", response=response)
                )
            )
            await self._commit(self.sink.finish_request(evidence.request_id, "succeeded"))
            self._check()
            # 响应事实仍真实成功，但权威提交等待可能已经耗尽逻辑期限。
            # 迟到交付不能让 Core 在过期请求之后开始新的工具副作用。
            if asyncio.get_running_loop().time() >= deadline:
                raise RequestBudgetExceeded("逻辑模型请求在结果交付前达到截止", provider="engine")
            if emit is not None and isinstance(response, ModelResponse):
                for event in buffer.events:
                    emit(event)
                emit(ModelStreamCompleted(response))
            return response
        raise AssertionError("已校验重试配置必须包含首次尝试。")


class ModelGenerationBackend(_RetryRunner):
    """主体创建的 Core 生成后端；同轮各 generate 共用构造时借入的预算。

    成功尝试的预览在提交后按原顺序回放，详细诊断超限不会拒绝有效响应。
    半成品只留各自尝试记录，不交回 Core。底层资源由 app 拥有，主体关闭本
    包装后，app 才能在没有活跃借用时关闭真正客户端。
    """

    def __init__(
        self,
        backend: ModelBackend,
        sink: RequestEvidenceSink,
        *,
        scope: RequestScope,
        budget: RequestBudget,
        policy: RetryPolicy,
        diagnostic_bytes: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        can_start: Callable[[], None] | None = None,
    ) -> None:
        """借用底层、权威 sink 与已校验的生成用途预算，不复制配置秘密。"""
        super().__init__(
            sink,
            scope=scope,
            budget=budget,
            policy=policy,
            diagnostic_bytes=diagnostic_bytes,
            sleep=sleep,
            can_start=can_start,
        )
        self._backend = backend

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        """冻结一次材料，按同一请求对象有界重试，并返回一次完整响应。"""

        async def invoke(buffer: _EventBuffer) -> ModelResponse:
            return await self._backend.generate(request, model=model, emit=buffer)

        return await self._run(request, kind="generation", model=model, invoke=invoke, emit=emit)


class ModelDecisionBackend(_RetryRunner):
    """主体的 Core 专用判断后端，概率与拒答解释留给原调用者。

    预算与证据流程共用生成实现，但不转成聊天请求或自动换模型。构造接收
    注意判断用途的已校验 2/5/8 配置；同一请求重试保留输入与候选顺序。
    """

    def __init__(
        self,
        backend: DecisionBackend,
        sink: RequestEvidenceSink,
        *,
        scope: RequestScope,
        budget: RequestBudget,
        policy: RetryPolicy,
        diagnostic_bytes: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        can_start: Callable[[], None] | None = None,
    ) -> None:
        """借用专用判断资源，不取得传输客户端或生成采样配置。"""
        super().__init__(
            sink,
            scope=scope,
            budget=budget,
            policy=policy,
            diagnostic_bytes=diagnostic_bytes,
            sleep=sleep,
            can_start=can_start,
        )
        self._backend = backend

    async def decide(self, request: DecisionRequest, *, model: str) -> DecisionResponse:
        """重试一次逻辑判断，保留原始概率及显式拒答。"""

        async def invoke(buffer: _EventBuffer) -> DecisionResponse:
            return await self._backend.decide(request, model=model)

        return await self._run(request, kind="decision", model=model, invoke=invoke)


def _attempt_result(
    start: AttemptStart,
    buffer: _EventBuffer,
    state: Literal["succeeded", "failed", "timed_out", "cancelled"],
    *,
    response: ModelResponse | DecisionResponse | None = None,
    failure: FailureEvidence | None = None,
    retry_wait: float | None = None,
) -> AttemptEvidence:
    buffer.active = False
    return AttemptEvidence(
        start,
        state,
        None if response is None else encode(response),
        failure,
        encode(buffer.arguments),
        encode(buffer.events),
        buffer.omitted == 0,
        buffer.omitted,
        retry_wait,
    )


def classify_failure(error: Exception) -> tuple[FailureEvidence, bool]:
    """仅按 Core 分类或明确传输事实判断，不猜异常正文或 HTTP 文本。

    已知额度耗尽拒绝重试；未知原因的 429 有限尝试。SDK 丢失的 HTTP 日期型
    Retry-After 可从公开 cause.response.headers 取得，至少等待本地一秒。
    内部程序异常保留类型，没有通用失败自动重试。
    """
    status = None
    code = None
    retry_after = error.retry_after_seconds if isinstance(error, ModelRateLimitError) else None
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response: object = getattr(current, "response", None)
        if isinstance(response, httpx.Response):
            status = response.status_code
            header = response.headers.get("retry-after")
            if header is not None:
                suggested = _retry_after(header)
                if suggested is not None:
                    retry_after = max(retry_after or 0.0, suggested)
            try:
                body: object = response.json()
            except ValueError:
                body = None
            if isinstance(body, Mapping):
                item: object = cast(Mapping[str, object], body).get("error")
                if isinstance(item, Mapping):
                    fields = cast(Mapping[str, object], item)
                    for name in ("code", "type"):
                        value = fields.get(name)
                        if isinstance(value, str) and value == "insufficient_quota":
                            code = value
        current = current.__cause__
    if code == "insufficient_quota":
        category, retryable = "quota_exhausted", False
    elif isinstance(error, (ModelAuthenticationError, ModelRequestError, ModelContextWindowError)):
        category, retryable = "permanent_request", False
    elif isinstance(
        error,
        (
            ModelUnavailableError,
            ModelRateLimitError,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
            httpx.TimeoutException,
            TimeoutError,
        ),
    ):
        category, retryable = "transient_transport", True
    else:
        category, retryable = "unclassified", False
    return FailureEvidence(
        f"{type(error).__module__}.{type(error).__qualname__}", category, status, code, retry_after
    ), retryable


def _retry_after(header: str) -> float | None:
    try:
        seconds = float(header)
    except ValueError:
        try:
            date = parsedate_to_datetime(header)
        except (ValueError, TypeError, OverflowError):
            return None
        if date.tzinfo is None:
            return None
        seconds = max(0.0, (date - datetime.now(UTC)).total_seconds())
    return seconds if math.isfinite(seconds) and seconds >= 0 else None
