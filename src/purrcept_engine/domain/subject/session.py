"""串行借用 Core 连续会话，冻结整轮预算并在权威提交后采用新历史。

每轮创建一个借用相同模型资源的新 facade，恢复唯一已提交检查点；Core 仍
独占工具循环和完整轮次边界。前台可在响应和请求边界拉取追加输入，先关联
权威轮次再放入 Core 临时历史，整轮预算不重置。失败或取消丢弃临时 facade，真实行动和
模型尝试已经独立入账。动态尾部由同步 ReminderSource 投影内存值，在实际
请求边界异步刷新后重投影一次；同一逻辑请求的重试不再次刷新。
Engine 工具原文在完整下一请求首发前经过交付 Gate；之后请求和最终检查点
保留同一实际交付，不能把容量回执重新展开为未经呈现的正文。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Protocol

from purrcept_core import AgentDriver, InlineExecutor
from purrcept_core.context import ExecutionContext
from purrcept_core.effect import Effect
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

from purrcept_engine.domain.context.capacity import CapacityAssessment
from purrcept_engine.domain.context.checkpoints import decode_checkpoint, encode_checkpoint
from purrcept_engine.domain.context.delivery import ToolDeliveryGate
from purrcept_engine.domain.context.presentation import render_host_state
from purrcept_engine.domain.model_requests.models import RequestScope
from purrcept_engine.domain.model_requests.ports import (
    MaterialCompiler,
    RequestEvidenceSink,
    RetryPolicy,
)
from purrcept_engine.domain.model_requests.service import (
    ModelGenerationBackend,
    RequestBudget,
    RequestBudgetExceeded,
    prepare_off_loop,
)
from purrcept_engine.domain.subject.models import (
    SessionRecord,
    SessionScope,
    TurnRecord,
)
from purrcept_engine.domain.subject.session_ports import SessionStore
from purrcept_engine.errors import HostStateError
from purrcept_engine.serialization import encode
from purrcept_engine.values import JsonObject, decode_object


@dataclass(frozen=True, slots=True)
class DynamicState:
    """已提交尾部的内存表示；只有宿主结构化入口允许调用回执投影。

    Attributes:
        text: 本次明确传输、输入或呈现的完整文本。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        普通 text 可表示预算或独立调用方的说明，不从文字中的相似 JSON 标记
        推断宿主权限。from_payload 冻结 Engine 的完整状态对象，请求边界只投影
        其中明确声明的 work.calls；原文、关键参数及当前真实状态仍有来源。
    """

    text: str
    _structured: bool = field(default=False, kw_only=True, repr=False)

    @classmethod
    def from_payload(cls, payload: JsonObject) -> DynamicState:
        """冻结宿主状态对象，不保留调用方可变字典或读取外部资料。

        Args:
            payload: 供当前请求使用的宿主状态对象，调用时编码冻结以隔离后续外部修改。

        Returns:
            当前请求的动态尾部材料，不改变固定 system 前缀。

        Raises:
            ValueError: 结构化尾部必须是 Engine 已提交状态。
        """
        if payload.get("material_kind") != "engine_committed_state":
            raise ValueError("结构化尾部必须是 Engine 已提交状态。")
        return cls(encode(payload), _structured=True)

    @property
    def host_payload(self) -> JsonObject | None:
        """为本次请求返回独立宿主对象。

        Note:
            普通 text 不参加结构化投影。
        """
        return decode_object(self.text.encode("utf-8")) if self._structured else None


class DynamicReminder:
    """Core 同步调用的请求局部投影，resolve 不访问文件、数据库或设备。

    Session 和异步请求准备边界更新 state；重试只使用已冻结 ModelRequest。
    提醒是 user 尾部材料，不拥有 system 权限，也不写入连续历史。
    """

    source_id = "engine.committed_state"

    def __init__(self, state: DynamicState) -> None:
        """借用当前批次的内存状态，不注册后台任务。

        Args:
            state: 当前批次持有的内存状态，resolve 只同步投影、不访问外部资源。
        """
        self.state = state

    def reminder(self) -> SystemReminder:
        """构造唯一 Engine 尾部，保留预算和来源文字的当前表示。

        Returns:
            针对当前请求的动态状态提醒。
        """
        payload = self.state.host_payload
        text = self.state.text if payload is None else render_host_state(payload)
        return SystemReminder(
            (TextBlock(text),), key=self.source_id, placement=ReminderPlacement.TAIL
        )

    def resolve(self, context: ReminderResolutionContext) -> Iterable[SystemReminder]:
        """同步返回已准备材料。

        Args:
            context: 宿主或 Core 注入的可信调用上下文，不由模型自行填写。

        Returns:
            当前请求需要的动态状态提醒序列，不新增持久历史。

        Note:
            context 不用于额外查询外部状态。
        """
        return (self.reminder(),)


type CapacityCheck = Callable[[JsonObject], CapacityAssessment]
type RefreshState = Callable[[RequestBudget], Awaitable[DynamicState]]
type PresentedGate = Callable[[ModelRequest], Awaitable[None]]
type TurnInput = Message | Callable[[], Awaitable[Message]]
type ReceiveInputs = Callable[[TurnRecord], Awaitable[Message | None]]


@dataclass(frozen=True)
class _ReceiveInputs(Effect[tuple[Message, ...]]):
    """把宿主原子拉取接入 Core 请求/响应边界，不启动独立消费任务。"""

    poll: Callable[[], Awaitable[tuple[Message, ...]]]

    async def execute(self, context: ExecutionContext[object]) -> tuple[Message, ...]:
        """串行借用本轮拉取器。

        Args:
            context: 宿主或 Core 注入的可信调用上下文，不由模型自行填写。

        Returns:
            条目集合：供后续 Core 请求使用的完整消息。

        Note:
            取消和权威错误按原异常返回 Core。
        """
        return await self.poll()


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
        """返回实际安装后的版本或原版本。

        Args:
            session: 当前已提交会话版本及完整检查点。
            message: 当前请求中实际存在的完整 Core 消息。
            instructions: 稳定 system 指令序列，实时状态不得混入缓存前缀。
            tools: 本会话允许使用的固定 Core 工具集合。
            settings: 当前组件已校验的配置或纯声明。
            dynamic: 当前逻辑请求实际采用的动态状态材料。
            budget: 当前连续批次共享的请求数量与单调截止预算。

        Returns:
            会话已提交版本与完整检查点，不包含尚未提交的临时历史。

        Note:
            当前输入及受保护内容不能被压缩。
        """
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
        delivery: ToolDeliveryGate | None = None,
    ) -> None:
        """借用本轮所有者和纯计量入口。

        Args:
            backend: 显式注入的模型后端，资源取得和关闭归装配层。
            reminder: 当前请求的局部提醒投影。
            refresh: 每次逻辑请求前刷新已提交动态尾部的异步入口。
            capacity: 对完整实际请求执行容量核验的函数。
            presented: 真实请求成功门槛的完整材料呈现回调。 None 表示没有额外的呈现追踪回调。
            delivery: 本轮工具原文交付门槛；None 不执行候选交付替换。

        Note:
            不取得客户端或持久化连接。
        """
        self.backend = backend
        self.reminder = reminder
        self.refresh = refresh
        self.capacity = capacity
        self.presented = presented
        self.delivery = delivery
        self.last_capacity: CapacityAssessment | None = None

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        """冻结每个逻辑请求的真实材料，成功范围提交后才将响应交回 Core。

        Args:
            request: 已经按 Core 协议组装的完整生成请求。
            model: 本次调用使用的显式模型名，不在领域层推断替代模型。
            emit: 流事件观察回调；None 表示不向外部观察者投递。 None
                表示不向外部观察者发送流事件。

        Returns:
            通过协议与证据提交门槛的真实模型响应。
        """
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
        current = await prepare_off_loop(lambda: self._prepare(current, model))
        self.backend.budget.check()

        events: list[ModelStreamEvent] = []
        response = await self.backend.generate(
            current, model=model, emit=events.append if emit is not None else None
        )
        if self.presented is not None:
            await self.presented(current)
        if self.delivery is not None:
            self.delivery.sent(current)
        self.backend.budget.check()
        if emit is not None:
            for event in events:
                emit(event)
            if not events:
                emit(ModelStreamCompleted(response))
        return response

    def _prepare(self, current: ModelRequest, model: str) -> ModelRequest:
        """本轮独占的纯编译/投影/计量；线程不接触设备、事务或模型。"""
        if self.delivery is not None:
            # Ordering: Core 已执行同响应全部工具，真实动态尾部也已经刷新。
            # 只在首次派发前选择尚未呈现原文；历史和真实效果不参与容量裁剪。
            current = self.delivery.prepare(
                current,
                model=model,
                capacity=self.capacity,
                host_state=self.reminder.state.host_payload,
                reminder_key=self.reminder.source_id,
            )
        materials, _ = self.backend.compiler(current, model=model)
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

        return current


@dataclass(frozen=True, slots=True)
class SessionResult:
    """权威历史已安装的内部结果；final_text 不代表已向外界输出。

    Attributes:
        turn: 已接管的本轮记录，携带提交前版本与生成代次。
        session: 当前已提交会话版本及完整检查点。
        checkpoint: 本轮已实际提交的完整 Core 检查点。
        final_text: Core 返回的内部最终文字，不会自动对外发送。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

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
        self,
        store: SessionStore,
        scope: SessionScope,
        *,
        compiler: MaterialCompiler,
        can_start: Callable[[], None],
    ) -> None:
        """保存有限账本和宿主资格，不在构造时打开库或调用模型。

        Args:
            store: 应用借出的领域存储端口，实际连接和关闭归资源所有者。
            scope: 目标连续会话，foreground 与 reflection 拥有独立历史。
            compiler: 按实际 SDK 布局冻结完整请求材料的纯编译器。
            can_start: 实际派发前重新检查的资格回调，等待后仍须复核。
        """
        self.compiler = compiler
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

    async def wait_idle(self) -> None:
        """等待当前会话释放资格。

        Note:
            等待者取消不取消会话，结果由 run 调用方取得。

            数据库最后一次提交通知可能早于任务真正结束，不能仅凭该通知等待
            空闲，否则注意工作器可能在没有下一次写入时永久睡眠。
        """
        active = self._active
        if active is not None:
            await asyncio.wait((active,))

    def eligible(self, generation: int) -> bool:
        """给新行动派发使用的同步资格，不撤销已经实际开始的效果。

        Args:
            generation: 当前会话或设备的代次，用于拒绝迟到结果更新新状态。

        Returns:
            给定代次是否仍拥有启动新行动的资格。
        """
        return (
            not self._closing
            and self.busy
            and generation == self._generation
            and generation != self._interrupted_generation
            and any(asyncio.get_running_loop().time() < budget.deadline for budget in self._budgets)
        )

    def snapshot(self) -> JsonObject:
        """读取当前处理与真实整轮预算，不返回提示/历史，也不推进会话。

        Returns:
            业务对象，按实际结果提供 scope、generation、busy、closing、budgets 字段。
        """
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
        delivery: ToolDeliveryGate | None = None,
        accepted_failures: tuple[str, ...] = (),
        prepare_history: PrepareHistory | None = None,
        receive_inputs: ReceiveInputs | None = None,
    ) -> SessionResult:
        """从主动处理起计预算，成功提交后返回。

        Args:
            message: 本轮准备接管的原始消息或延迟构造入口。
            turn_id: 连续会话中本轮处理的身份。
            input_ids: 本轮接管的输入身份集合，保留真实因果关联。
            backend: 显式注入的模型后端，资源取得和关闭归装配层。
            evidence: 接收逻辑请求及各次真实尝试的权威提交口。
            model: 本次调用使用的显式模型名，不在领域层推断替代模型。
            instructions: 稳定 system 指令序列，实时状态不得混入缓存前缀。
            tools: 本会话允许使用的固定 Core 工具集合。
            output_tokens: 为本次生成预留的最大输出 token 数。
            max_requests: 本轮逻辑模型请求总上限，传输重试不重复占名额。
            turn_seconds: 本轮连续处理的总秒数预算，包含工具等待和重试退避。
            retry: 唯一传输重试策略，不额外叠加 SDK 隐式重试。
            diagnostic_bytes: 单次尝试允许保留的流诊断字节数，不裁剪原参数或完整响应。
            refresh: 每次逻辑请求前刷新已提交动态尾部的异步入口。
            capacity: 对完整实际请求执行容量核验的函数。
            presented: 真实请求成功门槛的完整材料呈现回调。 None 表示没有额外的呈现追踪回调。
            delivery: 本轮工具原文交付门槛；None 不执行候选交付替换。
            accepted_failures: 当前成功交互明确承接的先前失败轮次身份。 默认不附加此类条目。
            prepare_history: 会话空闲时维护已提交历史的入口；None 不维护。
            receive_inputs: 在 Core 请求边界接管本轮新增输入的回调。 None
                表示本轮不接入新增输入。

        Returns:
            权威历史已提交的内部轮次结果；最终文字不会自动对外发送。

        Raises:
            HostStateError: 会话正在处理或收尾。

        Note:
            重叠调用明确拒绝。

            receive_inputs 由前台调度者提供，在 Core 响应/请求边界串行调用。
            它须先把取得的输入关联到传入轮次，再返回完整 user 材料；没有输入
            时立即返回 None。反思不安装该入口，追加输入不扩充本轮请求/时间额度。
        """
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
                delivery=delivery,
                accepted_failures=accepted_failures,
                prepare_history=prepare_history,
                receive_inputs=receive_inputs,
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
        delivery: ToolDeliveryGate | None,
        accepted_failures: tuple[str, ...],
        prepare_history: PrepareHistory | None,
        receive_inputs: ReceiveInputs | None,
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
                compiler=self.compiler,
            )
            self._backends.add(wrapped)
            reminder = DynamicReminder(dynamic)
            prepared = PreparedGenerationBackend(
                wrapped,
                reminder,
                refresh=refresh,
                capacity=capacity,
                presented=presented,
                delivery=delivery,
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
            active_turn = turn
            active_backend = wrapped

            async def poll() -> tuple[Message, ...]:
                """接管与原文进入历史使用同一预算，后续请求记录扩展后的来源。"""
                nonlocal active_turn, turn
                budget.check()
                self.can_start()
                if receive_inputs is None:
                    return ()
                incoming = await receive_inputs(active_turn)
                if incoming is None:
                    return ()
                latest = await self.store.get_turn(active_turn.turn_id)
                if latest is None:
                    raise HostStateError("拉取输入后缺少权威轮次。")
                active_turn = turn = latest
                active_backend.scope = replace(active_backend.scope, input_ids=latest.input_ids)
                budget.check()
                return (incoming,)

            turn_timeout = asyncio.timeout_at(budget.deadline)
            async with turn_timeout:
                result = await AgentDriver[None](InlineExecutor()).run(
                    conversation.ask(
                        prepared_message,
                        input_poll=_ReceiveInputs(poll) if receive_inputs is not None else None,
                    ),
                    host=None,
                )
            budget.check()
            checkpoint = conversation.snapshot()
            if delivery is not None:
                # Core 保留的是工具取得的候选。权威连续历史只保存真实派发的
                # 交付选择，下一轮不能恢复此前容量拒绝的正文或媒体。
                checkpoint = replace(
                    checkpoint,
                    history=replace(
                        checkpoint.history,
                        messages=delivery.project(checkpoint.history.messages),
                    ),
                )
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
            from purrcept_engine.values import encode_json

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
        """只抢占仍匹配的生成，迟到注意判断不能取消新轮次。

        Args:
            generation: 当前会话或设备的代次，用于拒绝迟到结果更新新状态。

        Returns:
            是否实际撤销了仍匹配的生成代次；旧判断不会抢占新轮次。
        """
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
