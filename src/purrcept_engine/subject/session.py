"""串行借用 Core 连续会话，冻结整轮预算并在权威提交后采用新历史。

每轮创建一个借用相同模型资源的新 facade，恢复唯一已提交检查点；Core 仍
独占工具循环和完整轮次边界。失败或取消丢弃本轮临时 facade，真实行动和
模型尝试已经独立入账。动态尾部由同步 ReminderSource 投影内存值，在实际
请求边界异步刷新后重投影一次；同一逻辑请求的重试不再次刷新。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from typing import Protocol

from purrcept_core import AgentDriver, InlineExecutor
from purrcept_core.models import (
    ConversationCheckpoint,
    Message,
    Model,
    ModelBackend,
    ModelEventSink,
    ModelRequest,
    ModelResponse,
    ModelSettings,
    ModelStreamCompleted,
    ModelStreamEvent,
    ReminderPlacement,
    ReminderResolutionContext,
    SystemInstruction,
    SystemReminder,
    TextBlock,
    ToolLoopLimitError,
    ToolSet,
)

from purrcept_engine.context.capacity import CapacityAssessment
from purrcept_engine.context.checkpoints import decode_checkpoint, encode_checkpoint
from purrcept_engine.errors import HostStateError
from purrcept_engine.execution.protocol import JsonObject, decode_object
from purrcept_engine.storage.model_requests import RequestScope, encode
from purrcept_engine.storage.subject import SessionRecord, SessionScope, TurnOutcome, TurnRecord

from .model_requests import (
    ModelGenerationBackend,
    RequestBudget,
    RequestBudgetExceeded,
    RequestEvidenceSink,
    RetryPolicy,
    compile_materials,
)


class SessionStore(Protocol):
    """主体借用的版本提交端口，生产由 Store 单连接事务实现。

    begin_turn 先于 Core；finish_turn 只接受原会话版本及在途身份，成功时
    检查点/媒体/输入结案原子安装。失败保留旧检查点和真实失败经历。取消
    等待不能撤销已接管事务，宿主须持有提交任务直至实际结束。
    """

    async def get_session(self, scope: SessionScope) -> SessionRecord:
        """还原已有权威版本；未知或损坏格式不得以空会话替代。"""
        ...

    async def get_turn(self, turn_id: str) -> TurnRecord | None:
        """开始等待取消后核对已接管批次，不能留下无人处理的活跃身份。"""
        ...

    async def begin_turn(
        self,
        turn_id: str,
        scope: SessionScope,
        expected_version: int,
        generation: int,
        input_ids: tuple[str, ...],
    ) -> TurnRecord:
        """接管一个主动处理批次，拒绝版本冲突或重复输入交接。"""
        ...

    async def finish_turn(
        self,
        turn: TurnRecord,
        outcome: TurnOutcome,
        *,
        checkpoint_json: str | None = None,
        error_json: str | None = None,
        accepted_failures: tuple[str, ...] = (),
    ) -> SessionRecord:
        """提交整轮成功或明确失败，返回实际已提交会话。"""
        ...


@dataclass(frozen=True, slots=True)
class DynamicState:
    """已提交状态的内存 JSON；其中嵌入原文须保留明确来源标签。"""

    text: str


class DynamicReminder:
    """Core 同步调用的请求局部投影，resolve 不访问文件、数据库或设备。

    Session 和异步请求准备边界更新 state；重试只使用已冻结 ModelRequest。
    提醒是 user 尾部材料，不拥有 system 权限，也不写入连续历史。
    """

    source_id = "engine.committed_state"

    def __init__(self, state: DynamicState) -> None:
        """借用当前批次的内存状态，不注册后台任务。"""
        self.state = state

    def reminder(self) -> SystemReminder:
        """构造唯一 Engine 尾部，保留预算和来源文字的当前表示。"""
        return SystemReminder(
            (TextBlock(self.state.text),), key=self.source_id, placement=ReminderPlacement.TAIL
        )

    def resolve(self, context: ReminderResolutionContext) -> Iterable[SystemReminder]:
        """同步返回已准备材料；context 不用于额外查询外部状态。"""
        return (self.reminder(),)


type CapacityCheck = Callable[[JsonObject], CapacityAssessment]
type RefreshState = Callable[[RequestBudget], Awaitable[DynamicState]]
type PresentedGate = Callable[[ModelRequest], Awaitable[None]]
type TurnInput = Message | Callable[[], Awaitable[Message]]


class PrepareHistory(Protocol):
    """在会话仍为空闲时维护已提交历史，不能执行工具或推进临时 Core。

    Session 已开始同一主动预算并完整组装本批材料，app 借用此钩子计量真实
    下一请求布局。返回值必须来自权威 get_session，摘要安装采用 idle/CAS；
    失败仍由本轮记录，不把准备时间移到另一份 600 秒预算。
    """

    async def __call__(
        self,
        session: SessionRecord,
        message: Message,
        *,
        instructions: tuple[SystemInstruction, ...],
        tools: ToolSet,
        settings: ModelSettings,
        dynamic: DynamicState,
        budget: RequestBudget,
    ) -> SessionRecord:
        """返回实际安装后的版本或原版本；当前输入及受保护内容不能被压缩。"""
        ...


class PreparedGenerationBackend:
    """Core 的实际请求门槛，借用证据/重试包装而不重写 SDK 协议。

    先刷新已提交尾部和技能，再用相同 SDK 编译材料进行容量预检；只有通过
    后才进入唯一重试循环。成功尝试已提交后，反思阅读范围才获得使用资格。
    Core 的进度事件也等到此门槛完成，不能因事件提前交付执行迟到工具。
    """

    def __init__(
        self,
        backend: ModelGenerationBackend,
        reminder: DynamicReminder,
        *,
        refresh: RefreshState,
        capacity: CapacityCheck,
        presented: PresentedGate | None = None,
    ) -> None:
        """借用本轮所有者和纯计量入口；不取得客户端或持久化连接。"""
        self.backend = backend
        self.reminder = reminder
        self.refresh = refresh
        self.capacity = capacity
        self.presented = presented
        self.last_capacity: CapacityAssessment | None = None

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        """冻结每个逻辑请求的真实材料，成功范围提交后才将响应交回 Core。"""
        self.backend.budget.check()
        self.reminder.state = await self.refresh(self.backend.budget)
        self.backend.budget.check()
        # Core 编译提醒的过程必须同步。文件与数据库刷新在这里完成，然后仅
        # 替换 Engine 自己的请求局部提醒；指令、历史与工具结果关联保持原值。
        current = replace(
            request,
            reminders=(
                *(
                    reminder
                    for reminder in request.reminders
                    if reminder.key != self.reminder.source_id
                ),
                self.reminder.reminder(),
            ),
        )
        materials, _ = compile_materials(current, model=model)
        envelope = decode_object(materials.encode("utf-8"))
        payload = envelope["payload"]
        if not isinstance(payload, dict):
            raise TypeError("SDK 编译材料必须包含 payload 对象。")
        self.last_capacity = self.capacity(payload)
        # 计量依据独立保存为 Engine 元数据，不插入模型可见正文，也不把远端
        # usage 当成本地预检结果。请求证据与开始意图一起保留该方法版本。
        current = replace(
            current, metadata={**current.metadata, "engine.capacity": encode(self.last_capacity)}
        )

        events: list[ModelStreamEvent] = []
        response = await self.backend.generate(
            current, model=model, emit=events.append if emit is not None else None
        )
        if self.presented is not None:
            await self.presented(current)
        self.backend.budget.check()
        if emit is not None:
            for event in events:
                emit(event)
            if not events:
                emit(ModelStreamCompleted(response))
        return response


@dataclass(frozen=True, slots=True)
class SessionResult:
    """权威历史已安装的内部结果；final_text 不代表已向外界输出。"""

    turn: TurnRecord
    session: SessionRecord
    checkpoint: ConversationCheckpoint
    final_text: str


class ContinuousSession:
    """主体创建并串行使用的连续历史所有者，前台与反思各持有一份。

    工具集和固定指令由调用方显式选择。每个 run 借用独立整轮预算、请求
    证据包装和 facade；代次失效后不采用迟到历史。close 撤销新 run，取消
    当前 Core 等待并持有所有提交/底层借用，资源释放仍归 app。
    """

    def __init__(
        self, store: SessionStore, scope: SessionScope, *, can_start: Callable[[], None]
    ) -> None:
        """保存有限账本和宿主资格，不在构造时打开库或调用模型。"""
        self.store = store
        self.scope: SessionScope = scope
        self.can_start = can_start
        self._generation = 0
        self._interrupted_generation = 0
        self._active: asyncio.Task[SessionResult] | None = None
        self._budgets: set[RequestBudget] = set()
        self._backends: set[ModelGenerationBackend] = set()
        self._commits: set[asyncio.Future[SessionRecord]] = set()
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def generation(self) -> int:
        """返回当前处理代次，仅用于核对观察后的紧急抢占。"""
        return self._generation

    @property
    def busy(self) -> bool:
        """报告 Core 主动处理等待，不把独立已开始行动视为同一代次。"""
        return self._active is not None and not self._active.done()

    def eligible(self, generation: int) -> bool:
        """给新行动派发使用的同步资格，不撤销已经实际开始的效果。"""
        return (
            not self._closing
            and self.busy
            and generation == self._generation
            and generation != self._interrupted_generation
            and any(asyncio.get_running_loop().time() < budget.deadline for budget in self._budgets)
        )

    def snapshot(self) -> JsonObject:
        """读取当前处理与真实整轮预算，不返回提示/历史，也不推进会话。"""
        now = asyncio.get_running_loop().time()
        return {
            "scope": self.scope,
            "generation": self.generation,
            "busy": self.busy,
            "closing": self._closing,
            "budgets": [
                {
                    "max_requests": budget.max_requests,
                    "used_requests": budget.used,
                    "remaining_seconds": max(0.0, budget.deadline - now),
                }
                for budget in self._budgets
            ],
        }

    async def run(
        self,
        message: TurnInput,
        *,
        turn_id: str,
        input_ids: tuple[str, ...],
        backend: ModelBackend,
        evidence: RequestEvidenceSink,
        model: str,
        instructions: tuple[SystemInstruction, ...],
        tools: ToolSet,
        output_tokens: int,
        max_requests: int,
        turn_seconds: float,
        retry: RetryPolicy,
        diagnostic_bytes: int,
        refresh: RefreshState,
        capacity: CapacityCheck,
        presented: PresentedGate | None = None,
        accepted_failures: tuple[str, ...] = (),
        prepare_history: PrepareHistory | None = None,
    ) -> SessionResult:
        """从主动处理起计预算，成功提交后返回；重叠调用明确拒绝。"""
        if self._closing or self.busy:
            raise HostStateError("会话正在处理或收尾。")
        self.can_start()
        self._generation += 1
        generation = self._generation
        budget = RequestBudget(
            max_requests=max_requests, deadline=asyncio.get_running_loop().time() + turn_seconds
        )
        self._budgets.add(budget)
        self._active = asyncio.create_task(
            self._run(
                message,
                turn_id=turn_id,
                input_ids=input_ids,
                backend=backend,
                evidence=evidence,
                model=model,
                instructions=instructions,
                tools=tools,
                output_tokens=output_tokens,
                budget=budget,
                generation=generation,
                retry=retry,
                diagnostic_bytes=diagnostic_bytes,
                refresh=refresh,
                capacity=capacity,
                presented=presented,
                accepted_failures=accepted_failures,
                prepare_history=prepare_history,
            ),
            name=f"subject-{self.scope}-{generation}",
        )
        return await asyncio.shield(self._active)

    async def _run(
        self,
        message: TurnInput,
        *,
        turn_id: str,
        input_ids: tuple[str, ...],
        backend: ModelBackend,
        evidence: RequestEvidenceSink,
        model: str,
        instructions: tuple[SystemInstruction, ...],
        tools: ToolSet,
        output_tokens: int,
        budget: RequestBudget,
        generation: int,
        retry: RetryPolicy,
        diagnostic_bytes: int,
        refresh: RefreshState,
        capacity: CapacityCheck,
        presented: PresentedGate | None,
        accepted_failures: tuple[str, ...],
        prepare_history: PrepareHistory | None,
    ) -> SessionResult:
        """只采用当前权威版本；Core 临时成功不是 Engine 安装历史的提交点。"""
        wrapped: ModelGenerationBackend | None = None
        turn: TurnRecord | None = None
        turn_timeout: asyncio.Timeout | None = None
        try:
            previous = await self.store.get_session(self.scope)
            budget.check()
            # 材料展开属于主动处理时间。接入交来的不可变引用在已接管批次内
            # 展开，不能先花完组装时间再启动一份全新的 600 秒预算。
            prepared_message = message if isinstance(message, Message) else await message()
            budget.check()
            dynamic = await refresh(budget)
            settings = ModelSettings(max_output_tokens=output_tokens)
            if prepare_history is not None:
                # Ordering: 摘要只安装空闲的权威会话；正式 begin_turn 之后不能
                # 替换 Core 正在使用的历史，也不能重启已经发生工具效果的循环。
                previous = await prepare_history(
                    previous,
                    prepared_message,
                    instructions=instructions,
                    tools=tools,
                    settings=settings,
                    dynamic=dynamic,
                    budget=budget,
                )
                budget.check()
            turn = await self.store.begin_turn(
                turn_id, self.scope, previous.version, generation, input_ids
            )
            wrapped = ModelGenerationBackend(
                backend,
                evidence,
                scope=RequestScope(self.scope, turn_id, input_ids),
                budget=budget,
                policy=retry,
                diagnostic_bytes=diagnostic_bytes,
                can_start=self.can_start,
            )
            self._backends.add(wrapped)
            reminder = DynamicReminder(dynamic)
            prepared = PreparedGenerationBackend(
                wrapped, reminder, refresh=refresh, capacity=capacity, presented=presented
            )
            conversation = Model(prepared, model).conversation(
                instructions=instructions,
                tools=tools,
                settings=settings,
                reminder_sources=(reminder,),
                max_model_rounds=budget.max_requests,
                continuation_policy="client_managed",
                request_budget=None,
            )
            if previous.checkpoint_json is not None:
                conversation.restore(decode_checkpoint(previous.checkpoint_json))
            turn_timeout = asyncio.timeout_at(budget.deadline)
            async with turn_timeout:
                result = await AgentDriver[None](InlineExecutor()).run(
                    conversation.ask(prepared_message), host=None
                )
            budget.check()
            checkpoint = conversation.snapshot()
            committed = await self._commit(
                self.store.finish_turn(
                    turn,
                    "succeeded",
                    checkpoint_json=encode_checkpoint(checkpoint),
                    accepted_failures=accepted_failures,
                )
            )
            return SessionResult(turn, committed, checkpoint, result.response.message.text)
        except BaseException as error:
            # Core、取消和内部故障都保留本来的异常；仅在权威账本仍可提交时
            # 记录本轮失败。失败提交也失败时保留两条错误，不制造成功状态。
            if turn is None:
                turn = await self.store.get_turn(turn_id)
            if turn is None:
                # 准备失败也要结束已接管输入，不能留在 foreground 让固定工作器
                # 无限重做同一准备。这里只写失败身份，不调用模型或任何世界能力。
                try:
                    current = await self.store.get_session(self.scope)
                    turn = await self.store.begin_turn(
                        turn_id, self.scope, current.version, generation, input_ids
                    )
                except BaseException as commit_error:
                    raise BaseExceptionGroup(
                        "会话准备及失败身份提交失败。", [error, commit_error]
                    ) from None
            from purrcept_engine.execution.protocol import encode_json

            outcome = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
            failure: JsonObject = {"error_type": type(error).__name__}
            if isinstance(error, (RequestBudgetExceeded, ToolLoopLimitError)) or (
                isinstance(error, TimeoutError)
                and turn_timeout is not None
                and turn_timeout.expired()
            ):
                # 整轮耗尽属于既定停止机制，不能像独立异常一样立即触发一批
                # 新反思。单请求/工具自己的 TimeoutError 不继承这个类别。
                failure["category"] = "budget_exhausted"
            try:
                current = await self.store.get_session(self.scope)
                if current.active_turn == turn.turn_id:
                    await self._commit(
                        self.store.finish_turn(
                            turn,
                            outcome,
                            error_json=encode_json(failure).decode("utf-8"),
                        )
                    )
            except BaseException as commit_error:
                raise BaseExceptionGroup(
                    "会话处理及失败经历提交失败。", [error, commit_error]
                ) from None
            raise
        finally:
            budget.revoke()
            self._budgets.discard(budget)
            if wrapped is not None:
                await wrapped.close()
                self._backends.discard(wrapped)

    async def _commit(self, operation: Awaitable[SessionRecord]) -> SessionRecord:
        """提交任务独立持有，第二次取消也不能使已受理写入失去所有者。"""
        task = asyncio.ensure_future(operation)
        self._commits.add(task)
        task.add_done_callback(self._commit_done)
        return await asyncio.shield(task)

    def _commit_done(self, task: asyncio.Future[SessionRecord]) -> None:
        self._commits.discard(task)
        if not task.cancelled():
            task.exception()

    def interrupt(self, generation: int) -> bool:
        """只抢占仍匹配的生成，迟到注意判断不能取消新轮次。"""
        if (
            generation != self._generation
            or generation == self._interrupted_generation
            or not self.busy
        ):
            return False
        self._interrupted_generation = generation
        for budget in self._budgets:
            budget.revoke()
        assert self._active is not None
        self._active.cancel()
        return True

    async def close(self) -> None:
        """终止新逻辑请求并等待当前 Core、已接管提交与 SDK 借用。"""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        """同一次收尾只撤销一次；重复等待不能再次取消已经开始的提交。"""
        self._closing = True
        self.interrupt(self._generation)
        if self._active is not None:
            await asyncio.gather(self._active, return_exceptions=True)
        if self._commits:
            await asyncio.gather(*tuple(self._commits))
        for backend in tuple(self._backends):
            await backend.close()
