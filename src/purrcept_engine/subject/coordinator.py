"""用固定工作器推进权威输入的注意、延期交接和唯一前台 Core 会话。

主体借用 app 已装配的模型、账本、认识读取与行动入口，不拥有客户端或数据库
的关闭。注意按最早接管时刻合批；前台每次拉取取尽开放缓冲池，活动会话在
模型返回和下一请求前继续接收。所有选择以事务中的输入版本为准，迟到注意
不能覆盖已拉取输入。延期由独立固定交接者处理，不被模型等待阻塞。这里没有反思、
活动反馈或行动自动续行；那些可靠来源由各自所有者另行接入。
QQ 直接 @ 的最低唤起先进入前台，再由同一注意工作器核对持久旧代次观察；
升级结果不能撤销最低唤起、重复入队或抢占正在处理该输入的新轮次。
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import uuid4

import httpx
from purrcept_core.models import (
    ChoiceAnswer,
    ContentBlock,
    DecisionBackend,
    DecisionRefusal,
    DecisionRequest,
    DecisionResponseError,
    Message,
    MessageRole,
    ModelBackend,
    ModelError,
    ModelRequest,
    SystemInstruction,
    TextBlock,
    ToolCallBlock,
    ToolLoopLimitError,
)

from purrcept_engine.context.capacity import CapacityError
from purrcept_engine.context.materials import MediaStore, UnsupportedMaterial, input_message
from purrcept_engine.context.presentation import render_material
from purrcept_engine.domain.execution.models import ActionRecord
from purrcept_engine.domain.inputs.models import InputRecord, MediaReference
from purrcept_engine.domain.model_requests.models import RequestRecord, RequestScope
from purrcept_engine.domain.subject.models import (
    AttentionLevel,
    InputProcessing,
    InputStage,
    MinimumWakeAssessment,
    SessionScope,
    TurnRecord,
)
from purrcept_engine.errors import HostStateError, StorageError
from purrcept_engine.execution.catalog import Catalog
from purrcept_engine.serialization import encode
from purrcept_engine.subject.attention import AttentionModel, NativeMedia, attention_request
from purrcept_engine.subject.delivery import ToolDeliveryGate
from purrcept_engine.subject.foreground import ForegroundTools
from purrcept_engine.subject.knowledge import KnowledgeService, KnowledgeStore
from purrcept_engine.subject.model_requests import (
    ModelDecisionBackend,
    ModelGenerationBackend,
    RequestBudget,
    RequestEvidenceSink,
    RetryPolicy,
)
from purrcept_engine.subject.semantic import EmbeddingProtocolError
from purrcept_engine.subject.session import (
    CapacityCheck,
    ContinuousSession,
    PrepareHistory,
    RefreshState,
    SessionStore,
)
from purrcept_engine.values import ActionProtocolError, JsonObject, decode_object


class CoordinatorStore(SessionStore, RequestEvidenceSink, MediaStore, Protocol):
    """Store 单连接提供的有限调度口，写确认后才采用选择或交接。

    app 在相同事件循环借出此端口。stage 查询不取得处理资格；decide_inputs
    按完整 InputProcessing 版本 CAS，begin_turn 再接管前台身份。change_revision
    只用于不漏通知的等待，不能替代数据库状态；取消观察不撤销已接管事务。
    """

    @property
    def change_revision(self) -> int:
        """返回已完成写入通知的单调序号，调用方先取序号再查询。"""
        ...

    async def wait_for_change(self, after_revision: int) -> int:
        """等待新事务通知，数据库关闭按 Store 契约结束等待。"""
        ...

    async def get_input(self, input_id: str) -> InputRecord | None:
        """读取完整接管事实，不存在的已关联身份属于权威损坏。"""
        ...

    async def input_processing(self, input_id: str) -> InputProcessing | None:
        """取得注意及前台处置的当前版本，不产生新交接。"""
        ...

    async def staged_inputs(self, stage: InputStage, *, limit: int = 8) -> tuple[InputRecord, ...]:
        """按接管顺序加载有限批次，不在内存建立全积压副本。"""
        ...

    async def take_foreground_inputs(self, turn_id: str | None = None) -> tuple[InputRecord, ...]:
        """取尽截至本事务的新刺激，活动轮次同时持久接管；不包含明确延期。"""
        ...

    async def decide_inputs(
        self,
        expected: tuple[InputProcessing, ...],
        level: AttentionLevel,
        reason: str,
        *,
        request_id: str | None = None,
        defer_until: float | None = None,
    ) -> bool:
        """同事务采用整批结果与延期；过时版本返回 False 而不重新投递。"""
        ...

    async def register_minimum_wake_assessment(
        self,
        expected: InputProcessing,
        *,
        owner_id: str,
        observed_generation: int,
    ) -> bool:
        """CAS 登记直接唤起的旧代次观察；同一输入只取得一次升级资格。"""
        ...

    async def minimum_wake_assessments(
        self, owner_id: str, *, limit: int = 8
    ) -> tuple[MinimumWakeAssessment, ...]:
        """查询本宿主的有限升级待办，不重建前台投递或猜测重启前代次。"""
        ...

    async def finish_minimum_wake_assessment(
        self,
        assessment: MinimumWakeAssessment,
        level: AttentionLevel,
        reason: str,
        *,
        request_id: str | None = None,
    ) -> bool:
        """结案判断并核对原前台版本，任何结果都不能撤销最低 WAKE。"""
        ...

    async def next_deferred_input(self) -> float | None:
        """返回最早未交接期限，单一定时等待服务全部延后身份。"""
        ...

    async def deliver_due_inputs(self, now: float) -> tuple[str, ...]:
        """原身份到期直投一次，不重新进入注意模型。"""
        ...

    async def requests_for_turn(self, owner_id: str, turn_id: str) -> tuple[RequestRecord, ...]:
        """关联实际逻辑请求及材料，不能自造注意成功请求身份。"""
        ...

    async def failed_turns(self, scope: SessionScope) -> tuple[TurnRecord, ...]:
        """查询尚未由新成功交互承接的失败事实，不自行重试其输入。"""
        ...

    async def actions_for_inputs(self, input_ids: tuple[str, ...]) -> tuple[ActionRecord, ...]:
        """读取已持久因果关联的真实行动和调用，不从文字或 ID 前缀猜归属。"""
        ...


class ActionCallback(Protocol):
    """app 绑定的唯一行动端口，仍由执行服务拥有实际资源和回执。

    只有本轮资格有效时可以开始新脚本。观察取消不能撤销已经开始的行动，
    实际 Provider 的未知或迟到结果由执行器独立保存，不写回临时 Core 历史。
    """

    def __call__(
        self,
        source: str,
        *,
        input_id: str | None,
        can_start: Callable[[], bool],
        presented_call_ids: frozenset[str],
    ) -> Awaitable[ActionRecord]:
        """接管受限脚本，不在宿主 exec 或直接调用插件实现。"""
        ...


@dataclass(frozen=True, slots=True)
class SubjectModel:
    """app 选择的前台资源和预算依据，模型内容不能修改这些参数。"""

    backend: ModelBackend
    model: str
    output_tokens: int
    capacity: CapacityCheck
    retry: RetryPolicy


@dataclass(frozen=True, slots=True)
class CoordinatorSettings:
    """调度的明确初值，独立于 app 配置类型与模型调用方。

    diagnostic_bytes 只控制详细诊断，权威原文和响应由 Store 的真实接管额度
    限制。整轮预算在 ContinuousSession 开始主动处理时一次创建，不因重试
    或工具请求刷新；输入排队和注意判断分别拥有自己的期限。
    """

    diagnostic_bytes: int
    coalesce_seconds: float = 0.1
    attention_batch: int = 8
    defer_seconds: float = 30.0
    max_requests: int = 8
    turn_seconds: float = 600.0

    def __post_init__(self) -> None:
        """校验公开构造边界，禁止布尔值或非有限时间被当作额度。"""
        for value in (self.attention_batch, self.max_requests):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("主体批量和请求次数必须是正整数。")
        if (
            isinstance(self.diagnostic_bytes, bool)
            or not isinstance(self.diagnostic_bytes, int)
            or self.diagnostic_bytes < 0
        ):
            raise ValueError("诊断字节额度必须是非负整数。")
        for value in (self.coalesce_seconds, self.defer_seconds, self.turn_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError("调度时间必须是有限非负数。")
        if self.defer_seconds == 0 or self.turn_seconds == 0:
            raise ValueError("延期和整轮期限必须为正数。")


@dataclass(frozen=True, slots=True)
class _ForegroundBinding:
    """本轮六工具的有限绑定，闭包只借出当前代次的行动资格。"""

    execute: ActionCallback
    media: NativeMedia
    wakeup: Callable[[], None]
    input_id: str
    eligible: Callable[[], bool]
    delivery: ToolDeliveryGate

    async def play_action(self, source: str) -> ActionRecord:
        """传入本轮资格，实际资源与迟到回执仍归 app 的执行器。"""
        return await self.execute(
            source,
            input_id=self.input_id,
            can_start=self.eligible,
            presented_call_ids=self.delivery.presented_call_ids,
        )

    async def media_blocks(self, media: tuple[MediaReference, ...]) -> tuple[ContentBlock, ...]:
        """仅展开已经接管的原生内容，缺口不会变为描述性预览。"""
        return await self.media(media)

    def wakeup_committed(self) -> None:
        """通知计划所属调度者查权威记录，不在这里执行计划。"""
        self.wakeup()


class SubjectCoordinator:
    """app 创建的主体工作所有者，同一事件循环仅启动一次。

    start 建立固定注意、前台和延期交接任务，输入待办始终留在权威库。模型
    暂时不可用只结束本轮并留下经历，内部/权威故障锁住新模型和行动。close
    只撤销本模块的模型借用，等已接管提交真正结束；客户端、执行器、插件
    和 Store 必须由 app 保留到本所有者实际收尾。
    """

    def __init__(
        self,
        store: CoordinatorStore,
        knowledge: KnowledgeService,
        knowledge_store: KnowledgeStore,
        catalog: Catalog,
        *,
        subject: SubjectModel,
        attention: AttentionModel | None,
        settings: CoordinatorSettings,
        instructions: tuple[SystemInstruction, ...],
        execute: ActionCallback,
        media: NativeMedia,
        refresh: RefreshState,
        attention_context: Callable[[], Awaitable[JsonObject]],
        wakeup_committed: Callable[[], None],
        can_start: Callable[[], None],
        prepare_history: PrepareHistory | None = None,
    ) -> None:
        """借用显式资源和纯声明，构造不联网、不推进输入或开启任务。"""
        self.store = store
        self.knowledge = knowledge
        self.knowledge_store = knowledge_store
        self.catalog = catalog
        self.subject = subject
        self.attention = attention
        self.settings = settings
        self.instructions = instructions
        self.execute = execute
        self.media = media
        self.refresh = refresh
        self.attention_context = attention_context
        self.wakeup_committed = wakeup_committed
        self.can_start = can_start
        self.prepare_history = prepare_history
        self.session = ContinuousSession(store, "foreground", can_start=self._check)
        self._attention_owner = uuid4().hex
        self._tasks: set[asyncio.Task[None]] = set()
        self._commits: set[asyncio.Future[object]] = set()
        self._attention_backends: set[ModelDecisionBackend | ModelGenerationBackend] = set()
        self._started = False
        self._closing = False
        self._failure: BaseException | None = None
        self._close_task: asyncio.Task[None] | None = None

    @property
    def generation(self) -> int:
        """提供接管前观察的生成身份，紧急通知只能抢占相同旧代次。"""
        return self.session.generation

    @property
    def failure(self) -> BaseException | None:
        """返回停止派发的原始故障，不将内部错误转为正常离线。"""
        return self._failure

    @property
    def pending_resources(self) -> int:
        """报告工作器、提交、未结束 Core 轮次及实际注意后端借用。"""
        return (
            len(self._tasks)
            + len(self._commits)
            + int(self.session.busy)
            + sum(backend.pending_attempts for backend in self._attention_backends)
        )

    async def start(self) -> None:
        """启动固定工作器；重新 start 不重放任务或建立平行会话。"""
        if self._started:
            raise HostStateError("主体工作器已经启动。")
        self._check()
        self._started = True
        for name, operation in (
            ("attention", self._attention_loop()),
            ("foreground", self._foreground_loop()),
            ("deferred", self._deferred_loop()),
        ):
            task = asyncio.create_task(operation, name=f"subject-{name}")
            self._tasks.add(task)
            task.add_done_callback(self._worker_done)

    async def input_admitted(self, input_id: str, *, observed_generation: int) -> bool:
        """在真实接管后核对可信抢占，正文关键词或缺失通知不产生控制。

        app 在接管前读取 generation，再在成功提交后调用。输入若已经进入
        前台处理或代次已经变化，不抢占新生成；重启后不猜测旧观察身份。
        QQ 直接 @ 已取得最低 WAKE，仍有效的旧代次可登记一次上下文升级，
        由相同注意工作器有限判断。音频停播由音频所有者另行受理；返回值
        只表示本通知是否已经直接抢占，不预报后续模型判断的结果。
        """
        self._check()
        record = await self.store.get_input(input_id)
        processing = await self.store.input_processing(input_id)
        if record is None or processing is None:
            raise StorageError("可信接管通知引用不存在的输入。", code="invalid_record")
        if processing.stage != "foreground":
            return False
        if record.context.interrupt_generation or record.context.speech_started:
            return self.session.interrupt(observed_generation)
        if (
            self.attention is not None
            and record.source.event_kind == "qq.message.received"
            and record.context.minimum_wake
            and self.session.eligible(observed_generation)
        ):
            # 最低 WAKE 已由接管事务完成。升级观察仍绑定接管前的旧代次，
            # 使用权威有限待办交给同一个注意工作器，不逐消息创建模型任务。
            await self._commit(
                self.store.register_minimum_wake_assessment(
                    processing,
                    owner_id=self._attention_owner,
                    observed_generation=observed_generation,
                )
            )
        return False

    def _check(self) -> None:
        """在新工作和实际模型边界同步核验本所有者及宿主的派发资格。"""
        if self._failure is not None:
            raise self._failure
        if self._closing:
            raise HostStateError("主体已开始关闭。")
        self.can_start()

    def _worker_done(self, task: asyncio.Task[None]) -> None:
        """固定工作器的原始故障锁存，不能留下其他工作器继续派发。"""
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self._failure = self._failure or error
            self.session.interrupt(self.session.generation)
            for other in self._tasks:
                if not other.cancelling():
                    other.cancel()

    async def _wait(self, revision: int, *, seconds: float | None = None) -> None:
        """先读序号再查询后等待，保留发生在查询期间的实际写入通知。"""
        if seconds is None:
            await self.store.wait_for_change(revision)
        elif seconds > 0:
            try:
                async with asyncio.timeout(seconds):
                    await self.store.wait_for_change(revision)
            except TimeoutError:
                pass

    async def _processing(self, records: tuple[InputRecord, ...]) -> tuple[InputProcessing, ...]:
        """读取本批版本供处置 CAS，查询本身不授予输入的处理资格。"""
        result: list[InputProcessing] = []
        for record in records:
            item = await self.store.input_processing(record.input_id)
            if item is None:
                raise StorageError("原文缺少处理身份。", code="invalid_record")
            result.append(item)
        return tuple(result)

    async def _wait_foreground(self, revision: int) -> None:
        """等会话释放或可信控制通知；观察者取消不能传播到主脑任务。

        普通刺激在忙碌时保留给主脑；独立中断判断仍可被新事务唤醒。会话
        完成另有等待源，避免最后一次数据库通知早于 busy 清除而漏掉空闲。
        """
        waits = (
            asyncio.create_task(self.store.wait_for_change(revision)),
            asyncio.create_task(self.session.wait_idle()),
        )
        try:
            done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in waits:
                task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)

    async def _attention_loop(self) -> None:
        """空闲时判断普通输入；主脑活动期间保持缓冲开放，由请求边界取尽。"""
        while True:
            self._check()
            revision = self.store.change_revision
            upgrades = await self.store.minimum_wake_assessments(self._attention_owner, limit=1)
            if upgrades:
                await self._upgrade_minimum_wake(upgrades[0])
                continue
            if self.session.busy:
                # 已有主脑交互时，新刺激不再被后台 observe/defer 提前取走。
                await self._wait_foreground(revision)
                continue
            if await self.store.staged_inputs("foreground", limit=1):
                await self._wait(revision)
                continue
            records = await self.store.staged_inputs(
                "attention", limit=self.settings.attention_batch
            )
            if not records:
                await self._wait(revision)
                continue
            expected = await self._processing(records)
            first = min(item.admitted_at for item in expected)
            remaining = min(
                self.settings.coalesce_seconds,
                max(0.0, first + self.settings.coalesce_seconds - time.time()),
            )
            deadline = asyncio.get_running_loop().time() + remaining
            upgrade_ready = False
            while len(records) < self.settings.attention_batch and remaining > 0:
                await self._wait(revision, seconds=remaining)
                self._check()
                revision = self.store.change_revision
                if await self.store.minimum_wake_assessments(self._attention_owner, limit=1):
                    upgrade_ready = True
                    break
                records = await self.store.staged_inputs(
                    "attention", limit=self.settings.attention_batch
                )
                if not records:
                    break
                remaining = deadline - asyncio.get_running_loop().time()
            if upgrade_ready:
                continue
            if not records:
                continue
            if self.session.busy or await self.store.staged_inputs("foreground", limit=1):
                continue
            expected = await self._processing(records)
            if any(item.stage != "attention" for item in expected):
                continue
            if self.attention is None:
                await self._commit(
                    self.store.decide_inputs(expected, "wake", "attention.not_configured")
                )
                continue
            observed = self.session.generation
            background = await self.attention_context()
            while records:
                try:
                    request = await attention_request(
                        records,
                        background=background,
                        media=self.media,
                        model=self.attention,
                        defer_seconds=self.settings.defer_seconds,
                    )
                except (CapacityError, UnsupportedMaterial) as error:
                    if len(records) > 1:
                        records = records[:-1]
                        expected = expected[:-1]
                        continue
                    reason = encode(
                        {
                            "attention": "not_completed",
                            "error_type": type(error).__name__,
                            "reason": str(error),
                            "code": error.code if isinstance(error, CapacityError) else None,
                        }
                    )
                    await self._commit(self.store.decide_inputs(expected, "wake", reason))
                    break
                await self._assess(records, expected, request, observed)
                break

    async def _upgrade_minimum_wake(self, assessment: MinimumWakeAssessment) -> None:
        """仅评估仍在旧生成后排队的直接唤起，失效观察结案而不换代续做。"""
        record = await self.store.get_input(assessment.input_id)
        processing = await self.store.input_processing(assessment.input_id)
        if record is None or processing is None:
            raise StorageError("最低唤起升级引用缺少原文或处置身份。", code="invalid_record")
        if (
            processing.stage != "foreground"
            or processing.version != assessment.input_version
            or not self.session.eligible(assessment.observed_generation)
        ):
            await self._commit(
                self.store.finish_minimum_wake_assessment(
                    assessment, "wake", "attention.minimum_wake_observation_expired"
                )
            )
            return
        if self.attention is None:
            raise RuntimeError("最低唤起升级缺少装配的注意模型。")
        background = await self.attention_context()
        try:
            request = await attention_request(
                (record,),
                background=background,
                media=self.media,
                model=self.attention,
                defer_seconds=self.settings.defer_seconds,
            )
        except (CapacityError, UnsupportedMaterial) as error:
            await self._commit(
                self.store.finish_minimum_wake_assessment(
                    assessment,
                    "wake",
                    encode(
                        {
                            "attention": "not_completed",
                            "minimum_level": "wake",
                            "error_type": type(error).__name__,
                            "reason": str(error),
                            "code": error.code if isinstance(error, CapacityError) else None,
                        }
                    ),
                )
            )
            return
        # 材料组装也可能等待存储。正式请求仍只针对原观察，不因为生成已经
        # 前进而借新代次继续评估；第一次逻辑请求前无需伪造失败模型证据。
        current = await self.store.input_processing(assessment.input_id)
        if current != processing or not self.session.eligible(assessment.observed_generation):
            await self._commit(
                self.store.finish_minimum_wake_assessment(
                    assessment, "wake", "attention.minimum_wake_observation_expired"
                )
            )
            return
        await self._assess(
            (record,),
            (processing,),
            request,
            assessment.observed_generation,
            minimum_wake=assessment,
        )

    async def _assess(
        self,
        records: tuple[InputRecord, ...],
        expected: tuple[InputProcessing, ...],
        request: DecisionRequest | ModelRequest,
        observed_generation: int,
        *,
        minimum_wake: MinimumWakeAssessment | None = None,
    ) -> None:
        """判断证据独立入账，处置 CAS 后才抢占，有限服务失败交前台一次。"""
        if self.attention is None:
            raise RuntimeError("注意工作器缺少装配的模型。")
        identity = uuid4().hex
        budget = RequestBudget(
            max_requests=1,
            deadline=asyncio.get_running_loop().time() + self.attention.retry.total_seconds,
        )

        def can_assess() -> None:
            self._check()
            if minimum_wake is not None and not self.session.eligible(observed_generation):
                # 旧生成结束后不重试升级请求。已启动尝试仍按原证据/借用机制
                # 收尾，不能把迟到的 urgent 采用到正在处理该输入的新轮次。
                budget.revoke()
                budget.check()

        scope = RequestScope("attention", identity, tuple(record.input_id for record in records))
        if self.attention.kind == "decision":
            backend = ModelDecisionBackend(
                cast(DecisionBackend, self.attention.backend),
                self.store,
                scope=scope,
                budget=budget,
                policy=self.attention.retry,
                diagnostic_bytes=self.settings.diagnostic_bytes,
                can_start=can_assess,
            )
        else:
            backend = ModelGenerationBackend(
                cast(ModelBackend, self.attention.backend),
                self.store,
                scope=scope,
                budget=budget,
                policy=self.attention.retry,
                diagnostic_bytes=self.settings.diagnostic_bytes,
                can_start=can_assess,
            )
        self._attention_backends.add(backend)
        level: AttentionLevel = "wake"
        reason = "typesafe.batch_judgment"
        failed = False
        try:
            try:
                if isinstance(backend, ModelDecisionBackend):
                    if not isinstance(request, DecisionRequest):
                        raise TypeError("专用判断必须使用专用请求。")
                    response = await backend.decide(request, model=self.attention.model)
                    response.validate_for(request)
                    answer = response.answer("attention")
                    if isinstance(answer, DecisionRefusal):
                        raise DecisionResponseError("注意判断明确拒答。")
                    if not isinstance(answer, ChoiceAnswer) or answer.choice not in (
                        "observe",
                        "defer",
                        "wake",
                        "urgent",
                    ):
                        raise DecisionResponseError("注意判断未返回声明的选择。")
                    level = answer.choice
                else:
                    if not isinstance(request, ModelRequest):
                        raise TypeError("生成注意必须使用生成请求。")
                    generated = await backend.generate(request, model=self.attention.model)
                    if any(isinstance(block, ToolCallBlock) for block in generated.message.content):
                        raise DecisionResponseError("无工具注意响应不能包含工具调用。")
                    try:
                        parsed = decode_object(generated.message.text.encode("utf-8"))
                    except ActionProtocolError as error:
                        raise DecisionResponseError("生成注意没有返回完整 JSON 对象。") from error
                    choice = parsed.get("attention")
                    if not isinstance(choice, str) or choice not in (
                        "observe",
                        "defer",
                        "wake",
                        "urgent",
                    ):
                        raise DecisionResponseError("生成注意没有返回声明的选择。")
                    value = parsed.get("reason", "model.batch_judgment")
                    if not isinstance(value, str):
                        raise DecisionResponseError("生成注意理由必须是字符串。")
                    level, reason = choice, value
            except (ModelError, TimeoutError) as error:
                failed = True
                reason = encode({"attention": "not_completed", "error_type": type(error).__name__})
            requests = await self.store.requests_for_turn("attention", identity)
            if len(requests) != 1:
                raise StorageError("注意判断缺少唯一逻辑请求证据。", code="invalid_record")
            if failed:
                reason = encode(
                    {
                        "attention": "not_completed",
                        "cause": reason,
                        "request_state": requests[0].state,
                        "attempt_failures": tuple(
                            attempt.result.failure
                            for attempt in requests[0].attempts
                            if attempt.result is not None and attempt.result.failure is not None
                        ),
                    }
                )
            if minimum_wake is None:
                applied = await self._commit(
                    self.store.decide_inputs(
                        expected,
                        level,
                        reason,
                        request_id=requests[0].request.request_id,
                        defer_until=time.time() + self.settings.defer_seconds
                        if level == "defer"
                        else None,
                    )
                )
            else:
                applied = await self._commit(
                    self.store.finish_minimum_wake_assessment(
                        minimum_wake,
                        level,
                        encode(
                            {
                                "attention": "minimum_wake_assessment",
                                "minimum_level": "wake",
                                "assessed_level": level,
                                "reason": reason,
                                "observed_generation": observed_generation,
                            }
                        ),
                        request_id=requests[0].request.request_id,
                    )
                )
            if applied and level == "urgent":
                self.session.interrupt(observed_generation)
        except Exception as error:
            # 内部/权威故障必须先锁存再异步归还后端；关闭取消可能发生在
            # finally 的清理等待中，不能用那次取消覆盖已经发生的原始故障。
            self._failure = self._failure or error
            raise
        finally:
            budget.revoke()
            await backend.close()
            self._attention_backends.discard(backend)

    async def _foreground_message(
        self, records: tuple[InputRecord, ...], failures: tuple[TurnRecord, ...]
    ) -> Message:
        """完整交付当前输入；失败经历带原文、回执和可展开媒体引用。"""
        blocks: list[ContentBlock] = []
        evidence_ids: list[str] = []
        for failure in failures:
            blocks.append(
                TextBlock(
                    render_material(
                        "此前未完成的轮次与真实行动回执",
                        {
                            "material_kind": "prior_failed_turn",
                            "turn": failure,
                            "evidence_refs": failure.input_ids,
                            "actions": await self.store.actions_for_inputs(failure.input_ids),
                        },
                    )
                )
            )
            original: list[InputRecord] = []
            for input_id in failure.input_ids:
                record = await self.store.get_input(input_id)
                if record is None:
                    raise StorageError("失败经历引用缺失原文。", code="invalid_record")
                original.append(record)
                evidence_ids.append(input_id)
            if original:
                # 失败媒体不自动再次进入请求：同一不可计量附件会让后续纯文本
                # 交互永久失败。保留原文和原件引用，明确未展开，由证据工具取回。
                blocks.extend(
                    (
                        await input_message(self.store, tuple(original), media_as_references=True)
                    ).content
                )
        blocks.extend((await input_message(self.store, records)).content)
        evidence_ids.extend(record.input_id for record in records)
        processing = await self._processing(records)
        assembled_at = time.time()
        blocks.append(
            TextBlock(
                render_material(
                    "本批输入的处理与延期情况",
                    {
                        "material_kind": "input_disposition",
                        "processing": processing,
                        "assembled_at": assembled_at,
                        "deferred_delivery": tuple(
                            {
                                "input_id": item.input_id,
                                "due_at": item.due_at,
                                "assembly_delay_seconds": max(0.0, assembled_at - item.due_at),
                            }
                            for item in processing
                            if item.handed_off and item.due_at is not None
                        ),
                    },
                )
            )
        )
        return Message(
            MessageRole.USER,
            tuple(blocks),
            metadata={
                "evidence": evidence_ids,
                "accepted_failures": [failure.turn_id for failure in failures],
            },
        )

    def _turn_eligible(self, generation: int) -> bool:
        """执行器最后核验此闭包，旧代次不能借新会话继续派发脚本。"""
        if self._closing or self._failure is not None or not self.session.eligible(generation):
            return False
        self.can_start()
        return True

    def _check_turn(self, generation: int) -> None:
        """工具进入各业务端口前核验原轮次，权威故障保持原型。"""
        self._check()
        if not self.session.eligible(generation):
            raise HostStateError("前台工具已失去原轮次资格。")

    async def _foreground_loop(self) -> None:
        """唯一会话串行处理已经交接的输入；最终文字仅提交内部检查点。"""
        while True:
            self._check()
            revision = self.store.change_revision
            # 先只读检查唤起，避免空池写事务不断通知注意工作器。真正取批在
            # 单个事务中连同尚未到期/正在判断的普通刺激一并接管。
            ready = await self.store.staged_inputs("foreground", limit=1)
            records = await self.store.take_foreground_inputs() if ready else ()
            if not records:
                await self._wait(revision)
                continue
            try:
                await self._run_foreground(records)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if self._closing or task is None or task.cancelling():
                    raise
                # 匹配的 urgent 只取消本轮临时 Core 历史；新独立输入仍可接续。
            except (
                ModelError,
                CapacityError,
                UnsupportedMaterial,
                ToolLoopLimitError,
                TimeoutError,
                httpx.HTTPError,
                EmbeddingProtocolError,
            ):
                # Session 已保存失败并结案输入；没有新来源不会自动重启预算。
                pass

    async def _run_foreground(self, records: tuple[InputRecord, ...]) -> None:
        """建立本轮工具资格；后续拉取扩充来源但不刷新代次和主动预算。"""
        failures = await self.store.failed_turns("foreground")
        generation = self.session.generation + 1
        delivery = ToolDeliveryGate()
        ports = _ForegroundBinding(
            self.execute,
            self.media,
            self.wakeup_committed,
            records[0].input_id,
            lambda: self._turn_eligible(generation),
            delivery,
        )
        tools = ForegroundTools(
            self.knowledge,
            self.knowledge_store,
            ports,
            check=lambda: self._check_turn(generation),
            delivery=delivery,
        )

        async def receive_inputs(turn: TurnRecord) -> Message | None:
            """原子关联新输入后展开原文；失败仍由当前轮次统一结案。"""
            self._check_turn(generation)
            incoming = await self.store.take_foreground_inputs(turn.turn_id)
            if not incoming:
                return None
            return await self._foreground_message(incoming, ())

        await self.session.run(
            lambda: self._foreground_message(records, failures),
            turn_id=uuid4().hex,
            input_ids=tuple(record.input_id for record in records),
            accepted_failures=tuple(failure.turn_id for failure in failures),
            backend=self.subject.backend,
            evidence=self.store,
            model=self.subject.model,
            instructions=self.instructions,
            tools=tools.tools,
            output_tokens=self.subject.output_tokens,
            max_requests=self.settings.max_requests,
            turn_seconds=self.settings.turn_seconds,
            retry=self.subject.retry,
            diagnostic_bytes=self.settings.diagnostic_bytes,
            refresh=self.refresh,
            capacity=self.subject.capacity,
            delivery=delivery,
            prepare_history=self.prepare_history,
            receive_inputs=receive_inputs,
        )

    async def _deferred_loop(self) -> None:
        """一个固定交接者等待最早持久期限，不因模型忙碌漏掉到期事实。"""
        while True:
            self._check()
            revision = self.store.change_revision
            due = await self.store.next_deferred_input()
            if due is None:
                await self._wait(revision)
                continue
            remaining = due - time.time()
            if remaining > 0:
                await self._wait(revision, seconds=remaining)
            else:
                await self._commit(self.store.deliver_due_inputs(time.time()))

    async def _commit[Result](self, operation: Awaitable[Result]) -> Result:
        """已受理调度写入由本所有者保留至真实提交，第二次取消也不遗失责任。"""
        task = asyncio.ensure_future(operation)
        owned = cast(asyncio.Future[object], task)
        self._commits.add(owned)
        task.add_done_callback(self._commit_done)
        return await asyncio.shield(task)

    def _commit_done[Result](self, task: asyncio.Future[Result]) -> None:
        """取回已取消观察留下的提交错误，后续派发继续受同一失败锁存约束。"""
        self._commits.discard(cast(asyncio.Future[object], task))
        if not task.cancelled() and (error := task.exception()) is not None:
            self._failure = self._failure or error

    async def close(self) -> None:
        """只建立一次实际收尾，取消等待不能使模型借用或写入失去所有者。"""
        if self._close_task is None:
            self._closing = True
            tasks = tuple(self._tasks)
            # Ordering: 撤销派发资格与取消工作器必须在同一同步段完成。已经
            # ready 的 Store 等待可能先于新收尾任务恢复，不能因此把正常停止
            # 锁存为资格故障；已有取消也不能再次打断工作器的资源清理。
            for task in tasks:
                if not task.cancelling():
                    task.cancel()
            self._close_task = asyncio.create_task(self._close(tasks), name="subject-close")
        await asyncio.shield(self._close_task)

    async def _close(self, tasks: tuple[asyncio.Task[None], ...]) -> None:
        """等待已经撤销的工作器和 Core，再收回权威提交及实际注意借用。"""
        errors: list[BaseException] = []
        try:
            await self.session.close()
        except BaseException as error:
            # 收尾是所有权边界；一个资源的失败不能跳过其他已接管提交或借用。
            errors.append(error)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for backend in tuple(self._attention_backends):
            try:
                await backend.close()
            except BaseException as error:
                errors.append(error)
            else:
                self._attention_backends.discard(backend)
        while self._commits:
            await asyncio.gather(*tuple(self._commits), return_exceptions=True)
        if self._failure is not None and all(error is not self._failure for error in errors):
            errors.append(self._failure)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("主体资源收尾失败。", errors)
