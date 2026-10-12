"""统一提交活动控制、真实确认与运行预算，不执行专业业务或模型调用。

Host 创建 ActivityService 并提供同连接 ActivityStore 与冻结定义。命令先持久
接管，再由有界所有者在事务外交给驱动；驱动受理和真实确认分开可查。暂停、
结束和预算耗尽立即撤销新步骤资格，在途步骤仍由原执行所有者收尾。

单一计时任务仅在实际运行时维护单调时钟与检查点。重启只取得待核对记录，
不构造驱动、不重发命令。专业驱动通过 ActivityPort 保存工作与反馈，既不取得
SQLite 连接，也不能直接改控制状态或追加额度。
标准能力经 invoke 交到共享执行器，独立步骤所有者在 unknown 或观察取消后
仍等待真实资源归还；不能用已经拿到首版回执替代实际 Provider 收尾。
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Literal, Protocol
from uuid import uuid4

from purrcept_engine.errors import EngineError, HostStateError
from purrcept_engine.execution.protocol import JsonObject, decode_object, encode_json
from purrcept_engine.storage.activities import (
    ActivityCommand,
    ActivityFeedback,
    ActivityRecord,
    ActivityState,
    CommandCause,
    CommandKind,
    FeedbackKind,
)
from purrcept_engine.storage.execution import ActionRecord, validate_json


class ActivityError(EngineError):
    """可定位的活动参数、状态或容量拒绝，不代表已接管工作被撤销。"""

    def __init__(self, message: str, *, code: str = "activity_state") -> None:
        """保留接入方可查询的拒绝类别。"""
        super().__init__(message)
        self.code = code


class ActivityStore(Protocol):
    """Host 提供的同连接存储端口，不由活动模块创建第二个连接。

    所有方法只在服务所属循环调用。commit_activity 在控制队列执行短事务；
    等待取消不回滚已接管提交。失败原样传播并停止活动后续派发，不返回空成功。
    recover_activities 在驱动装配前执行，仅保存中断事实，不回放专业动作。
    """

    async def activity_records(self) -> tuple[ActivityRecord, ...]:
        """读取未结束的有限活动集合。"""
        ...

    async def get_activity(self, activity_id: str) -> ActivityRecord | None:
        """按身份读取当前控制和工作快照。"""
        ...

    async def get_activity_command(self, command_id: str) -> ActivityCommand | None:
        """读取可信命令接管和真实确认，查询不重投。"""
        ...

    async def get_activity_feedback(self, feedback_id: str) -> ActivityFeedback | None:
        """查询原始反馈，用稳定身份去重而不改写首次提交事实。"""
        ...

    async def commit_activity(
        self,
        record: ActivityRecord,
        *,
        expected_version: int | None,
        command: ActivityCommand | None = None,
        feedback: ActivityFeedback | None = None,
    ) -> None:
        """原子提交精确版本、命令与反馈；不存在通用可变状态写入口。"""
        ...

    async def recover_activities(self) -> tuple[ActivityRecord, ...]:
        """保留累计和未确认命令，仅撤销上次运行的派发资格。"""
        ...


class ActivityInvoker(Protocol):
    """Host 注入的单能力交接，只供活动所有者借用，不提供脚本或 Provider。

    宿主同步关联已预留的行动身份，再进入真实共享执行器。before_start 在
    实际 Provider 前重新查询本活动资格，返回剩余时长或拒绝；on_admitted
    不能修改身份或重投。返回首版 ActionRecord 不代表 Provider 已归还，故
    ActivityService 同时借用配对的 wait_invocation_released 资源查询。
    """

    async def __call__(
        self,
        path: str,
        arguments: JsonObject,
        *,
        input_id: str,
        before_start: Callable[[], Awaitable[float | None]],
        on_admitted: Callable[[str], None],
        timeout_seconds: float | None,
    ) -> ActionRecord:
        """真实共享目录/schema/Journal 路径；原型权威与宿主错误直接传播。"""
        ...


@dataclass(frozen=True, slots=True)
class DriverAcceptance:
    """驱动是否已接管控制；accepted=True 不证明已经运行或收尾完成。"""

    accepted: bool
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool) or not isinstance(self.reason, str):
            raise ValueError("驱动受理必须包含布尔结论与文本原因。")


@dataclass(frozen=True, slots=True)
class RecoveryObservation:
    """显式核对取得的真实材料；stopped=None 保留未知并禁止恢复。

    stopped=True 只证明专业驱动已停止派发，不抹掉原命令或未知外部效果。
    evidence_json 留下核对材料，不能通过重做旧写操作生成这个观察。
    """

    stopped: bool | None
    evidence_json: str

    def __post_init__(self) -> None:
        if self.stopped is not None and not isinstance(self.stopped, bool):
            raise ValueError("核对停止状态只能是 bool 或 None。")
        validate_json(self.evidence_json)


class ActivityDriver(Protocol):
    """插件工厂创建的专业流程，接收宿主控制并通过端口报告真实事实。

    工厂不得启动领域步骤。accept 只受理控制，必须有界返回；pause/end 与
    其他 accept 可并发，驱动按命令版本采用最新控制，不能被普通能力队列阻塞。
    受理后通过 port.confirm 报告实际状态，不靠返回字符串宣称已经完成。
    标准能力使用 port.invoke，共用实际 schema/回执与资源所有者；其他本地
    专业步骤经 begin_step/finish_step。取得 Step 不授予绕过执行器调用 Provider
    的权限，不能只检查旧提示快照。
    completed/failed/needs_decision 反馈表示专业驱动已停止继续推进；尚在途的
    宿主能力仍由执行所有者收尾，不能借反馈撤销其真实回执。
    """

    async def accept(self, command: ActivityCommand, port: ActivityPort) -> DriverAcceptance:
        """接管明确命令身份；重复/迟到确认由宿主保存并核验资格。"""
        ...

    async def reconcile(self, record: ActivityRecord) -> RecoveryObservation:
        """显式恢复前只读核对实际状态，不重放旧命令或启动工作。"""
        ...

    async def close(self) -> None:
        """停止专业派发并收回实际后台任务，在途效果仍报告原归属。"""
        ...


@dataclass(frozen=True, slots=True)
class ActivityDefinition:
    """插件装配时提供的冻结定义；single_instance 约束未结束的同定义委托。"""

    definition: str
    factory: Callable[[ActivityPort], ActivityDriver]
    single_instance: bool = False


@dataclass(frozen=True, slots=True)
class ActivityStep:
    """宿主执行服务取得的步骤身份，直到真实收尾才归还并发事实。

    此值只用于本服务所属循环。finish 是幂等的等待，取消外部等待不会把仍在途
    的步骤当作已结束；宿主执行所有者须在取得真实结果之后调用它。
    """

    activity_id: str
    run_generation: int
    step_id: str
    _service: ActivityService

    async def finish(self) -> None:
        """报告宿主实际步骤已结束，不改变其独立回执。"""
        await self._service.finish_step(self)


class ActivityPort:
    """宿主为单项活动创建的驱动端口，没有通用状态或兄弟活动写权限。

    驱动取得当前快照后携命令身份、运行代次和观察版本提交事实。控制状态仅由
    服务核验后提交。checkpoint 保存专业工作，不把局部产物提升为长期认识。
    """

    def __init__(self, service: ActivityService, activity_id: str) -> None:
        """绑定一项不可变活动身份，不启动驱动或建立后台任务。"""
        self._activity_id = activity_id
        self._service = service

    @property
    def activity_id(self) -> str:
        """返回创建时固定的委托身份，公开端口不能改挂到兄弟活动。"""
        return self._activity_id

    async def snapshot(self) -> ActivityRecord:
        """读取本活动的当前权威快照，不产生恢复或控制副作用。"""
        return await self._service.activity(self.activity_id)

    async def begin_step(self, *, run_generation: int) -> ActivityStep:
        """在本活动进入专业步骤前复核资格，并取得直到真实收尾才归还的身份。

        端口固定活动身份，不能借此为兄弟活动派发。驱动保存真实结果后调用
        ActivityStep.finish；取消观察或停止请求不代替实际步骤结束。
        """
        return await self._service.begin_step(self.activity_id, run_generation)

    async def invoke(
        self,
        path: str,
        arguments: JsonObject,
        *,
        run_generation: int,
        timeout_seconds: float | None = None,
    ) -> ActionRecord:
        """本活动经真实执行服务调用能力，首版 unknown 不归还仍在途步骤。

        驱动可在观察超时后停止并报告 needs_decision；实际所有者仍保存迟到
        回执。观察取消不撤销已接管调用，不能借此直接运行 Provider 或脚本。
        """
        return await self._service.invoke(
            self.activity_id,
            path,
            arguments,
            run_generation=run_generation,
            timeout_seconds=timeout_seconds,
        )

    async def confirm(
        self,
        command_id: str,
        *,
        run_generation: int,
        version: int,
        state: ActivityState,
    ) -> ActivityCommand:
        """提交实际控制结果；version 是原命令版本，迟到结果仍保留但不复活。"""
        return await self._service.confirm(
            self.activity_id,
            command_id,
            run_generation=run_generation,
            version=version,
            state=state,
        )

    async def checkpoint(
        self, *, run_generation: int, version: int, work_json: str
    ) -> ActivityRecord:
        """按当前观察版本保存局部工作，旧代次或旧版本不能覆盖新工作。"""
        return await self._service.checkpoint_work(
            self.activity_id,
            run_generation=run_generation,
            version=version,
            work_json=work_json,
        )

    async def feedback(
        self,
        feedback_id: str,
        kind: FeedbackKind,
        payload_json: str,
        *,
        run_generation: int,
        version: int,
        command_id: str | None = None,
    ) -> ActivityFeedback:
        """原始反馈可靠保存；旧版本进展不覆盖当前状态，关键事实仍交付。"""
        return await self._service.feedback(
            self.activity_id,
            feedback_id,
            kind,
            payload_json,
            run_generation=run_generation,
            version=version,
            command_id=command_id,
        )


@dataclass(slots=True)
class _Clock:
    """单调起点仅存于本次宿主运行，不跨崩溃推算未记录时段。"""

    run_generation: int
    used_at_start: float
    started_at: float
    checkpoint_at: float


@dataclass(frozen=True, slots=True)
class _Request:
    command_id: str
    command: CommandKind
    activity_id: str | None
    definition: str | None
    goal: str | None
    additional_seconds: float | None
    reason: str | None
    cause: CommandCause = "explicit"
    expected_version: int | None = None


def _observe[Result](task: asyncio.Future[Result]) -> None:
    """取消等待者不取消所有者；即使没有等待者也收回最终异常。"""
    if not task.cancelled():
        task.exception()


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActivityError(f"{name} 必须是非空字符串。", code="activity_arguments")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ActivityError(f"{name} 不能表示为 UTF-8。", code="activity_arguments") from error
    return value


def _positive(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ActivityError(f"{name} 必须是正有限数。", code="activity_arguments")
    return float(value)


class ActivityService:
    """宿主创建的单一活动状态提交者，不拥有专业业务或普通能力调度队列。

    所有调用在创建它的事件循环。start 在任何插件工厂调用前核验恢复记录；
    close 立即禁止新步骤，再等待实际驱动及控制所有者结束并结清累计。命令
    接管等待可取消，已接管所有者仍继续；重复关闭等待同一收尾，不重做资源操作。
    """

    def __init__(
        self,
        store: ActivityStore,
        definitions: tuple[ActivityDefinition, ...],
        *,
        max_steps: int,
        max_active: int = 8,
        initial_seconds: float = 3600.0,
        max_remaining_seconds: float = 3600.0,
        checkpoint_seconds: float = 5.0,
        can_start: Callable[[], None] | None = None,
        invoke: ActivityInvoker | None = None,
        wait_invocation_released: Callable[[str], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        """绑定冻结定义与时钟，构造不恢复或启动任何专业驱动。

        can_start 是 Host/Store 的同步门槛，在控制派发和步骤提交等待后复核；
        它抛出的原型错误直接传播。已有反馈、确认和收尾不借此拒绝真实事实。
        max_steps 由宿主既有执行等待界注入，覆盖待提交与真实在途步骤，不能
        在驱动内部给每次请求再建立一个无界等待队列。
        invoke 与 wait_invocation_released 必须成对，由 Host 注入同一实际
        执行所有者；只控制的服务可不借用，但不能在缺失时伪造执行成功。
        """
        if isinstance(max_active, bool) or not isinstance(max_active, int) or max_active <= 0:
            raise ValueError("max_active 必须是正整数。")
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
            raise ValueError("max_steps 必须是正整数。")
        if (invoke is None) != (wait_invocation_released is None):
            raise ValueError("活动调用与实际资源归还端口必须成对提供。")
        self._initial = _positive(initial_seconds, "initial_seconds")
        self._maximum = _positive(max_remaining_seconds, "max_remaining_seconds")
        self._checkpoint = _positive(checkpoint_seconds, "checkpoint_seconds")
        if self._initial > self._maximum:
            raise ValueError("活动初始额度不能超过剩余额度上限。")
        self._definitions: dict[str, ActivityDefinition] = {}
        for definition in definitions:
            key = _nonempty(definition.definition, "definition")
            if key in self._definitions:
                raise ValueError("活动定义重复。")
            self._definitions[key] = definition
        self._store = store
        self._max_active = max_active
        self._max_steps = max_steps
        self._step_requests = 0
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._can_start = can_start
        self._invoke = invoke
        self._wait_invocation_released = wait_invocation_released
        self._lock = asyncio.Lock()
        self._changed = asyncio.Condition()
        self._change_version = 0
        self._drivers: dict[str, ActivityDriver] = {}
        self._clocks: dict[str, _Clock] = {}
        self._steps: dict[str, ActivityStep] = {}
        self._invocations: dict[str, asyncio.Task[None]] = {}
        self._steps_changed = asyncio.Event()
        self._requests: dict[str, tuple[_Request, asyncio.Task[ActivityCommand]]] = {}
        self._deliveries: dict[str, asyncio.Task[None]] = {}
        self._delivery_kinds: dict[str, tuple[str, CommandKind]] = {}
        self._retiring: dict[str, asyncio.Task[None]] = {}
        self._driver_closures: dict[str, asyncio.Task[None]] = {}
        self._wake_timer = asyncio.Event()
        self._timer: asyncio.Task[None] | None = None
        self._closing_task: asyncio.Task[None] | None = None
        self._started = False
        self._closing = False
        self._failure: BaseException | None = None

    async def start(self) -> None:
        """装载中断与核对事实，不调用 factory、accept 或自动恢复。"""
        async with self._lock:
            if self._started or self._closing:
                raise ActivityError("活动服务不能重复启动。")
            records = await self._store.recover_activities()
            if len(records) > self._max_active:
                raise ActivityError("已有活动超过配置活跃上限。", code="activity_capacity")
            self._started = True

    def _available(self) -> None:
        """每个真实派发门槛复核宿主与权威健康，原型故障不能变成模型参数拒绝。"""
        if self._failure is not None:
            raise self._failure
        if not self._started or self._closing:
            raise HostStateError("活动服务尚未运行或正在关闭。")
        if self._can_start is not None:
            self._can_start()

    @property
    def pending_resources(self) -> int:
        """只读统计仍归服务的驱动、步骤和接管/控制/计时所有者，关闭等待不重计。

        关闭任务和驱动关闭任务只是等待同一批资源，不再算一份资源。此计数不
        代表数据库提交已经完成；Host 仍单独持有权威连接直到服务实际收尾。
        """
        return (
            len(self._drivers)
            + len(self._steps)
            + self._step_requests
            + len(self._requests)
            + len(self._deliveries)
            + int(self._timer is not None and not self._timer.done())
        )

    async def activity(self, activity_id: str) -> ActivityRecord:
        """读取实际状态与工作，不启动驱动；关闭期间仍可查询已有事实。"""
        record = await self._store.get_activity(_nonempty(activity_id, "activity_id"))
        if record is None:
            raise ActivityError("活动不存在。", code="activity_missing")
        return record

    async def command_record(self, command_id: str) -> ActivityCommand:
        """单独读取接管、受理或真实确认，不把查询解释为再次执行。"""
        record = await self._store.get_activity_command(_nonempty(command_id, "command_id"))
        if record is None:
            raise ActivityError("活动命令不存在。", code="activity_missing")
        return record

    async def wait_command(self, command_id: str) -> ActivityCommand:
        """等待真实确认、明确拒绝或未知结论；取消只结束当前等待者。"""
        while True:
            async with self._changed:
                observed = self._change_version
            record = await self.command_record(command_id)
            if record.status in ("confirmed", "rejected", "unknown"):
                return record
            async with self._changed:
                await self._changed.wait_for(
                    lambda observed=observed: self._change_version != observed
                )

    async def command(
        self,
        command_id: str,
        command: CommandKind,
        *,
        activity_id: str | None = None,
        definition: str | None = None,
        goal: str | None = None,
        additional_seconds: float | None = None,
        reason: str | None = None,
        expected_version: int | None = None,
    ) -> ActivityCommand:
        """用可信身份接管有效参数组合，先返回受理，不等驱动真实完成。

        command_id 由宿主调用身份分配，不是模型参数。start 由宿主分配活动 ID。
        同一身份重投只返回已有记录；参数不同明确冲突，不按目标文本合并请求。
        expected_version 用于页面等已观察当前状态的控制方，核验在同一接管锁内
        完成；计时与反馈仍使用真实数据库版本提交，不允许旧页面覆盖新状态。
        """
        self._available()
        request = self._request(
            command_id,
            command,
            activity_id,
            definition,
            goal,
            additional_seconds,
            reason,
        )
        if expected_version is not None:
            if (
                isinstance(expected_version, bool)
                or not isinstance(expected_version, int)
                or expected_version <= 0
            ):
                raise ActivityError("控制版本必须是正整数。", code="activity_arguments")
            if command == "start":
                raise ActivityError("start 不接受已有活动版本。", code="activity_arguments")
            request = replace(request, expected_version=expected_version)
        pending = self._requests.get(command_id)
        if pending is not None:
            if pending[0] != request:
                raise ActivityError("命令身份对应不同参数。", code="activity_conflict")
            return await asyncio.shield(pending[1])
        if len(self._requests) >= self._max_active * 3:
            raise ActivityError("活动控制接管已满。", code="activity_busy")
        task = asyncio.create_task(self._admit(request), name="purrcept-activity-admit")
        self._requests[command_id] = (request, task)

        def finished(owner: asyncio.Task[ActivityCommand]) -> None:
            self._requests.pop(command_id, None)
            if not owner.cancelled():
                owner.exception()

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    def _request(
        self,
        command_id: str,
        command: CommandKind,
        activity_id: str | None,
        definition: str | None,
        goal: str | None,
        additional_seconds: float | None,
        reason: str | None,
    ) -> _Request:
        """真实边界精确验证组合，不把无关参数忽略为默认值。"""
        _nonempty(command_id, "command_id")
        if command not in ("start", "pause", "resume", "end", "adjust", "extend"):
            raise ActivityError("活动命令无效。", code="activity_arguments")
        if command == "start":
            if activity_id is not None or additional_seconds is not None or reason is not None:
                raise ActivityError("start 只接受 definition 与 goal。", code="activity_arguments")
            _nonempty(definition, "definition")
            _nonempty(goal, "goal")
        else:
            _nonempty(activity_id, "activity_id")
            if definition is not None:
                raise ActivityError("后续命令不接受 definition。", code="activity_arguments")
            if command == "adjust":
                _nonempty(goal, "goal")
                if additional_seconds is not None or reason is not None:
                    raise ActivityError("adjust 只接受新 goal。", code="activity_arguments")
            elif command == "extend":
                if goal is not None:
                    raise ActivityError("extend 不接受 goal。", code="activity_arguments")
                additional_seconds = _positive(additional_seconds, "additional_seconds")
                _nonempty(reason, "reason")
            elif goal is not None or additional_seconds is not None or reason is not None:
                raise ActivityError("该控制只接受 activity_id。", code="activity_arguments")
        return _Request(
            command_id, command, activity_id, definition, goal, additional_seconds, reason
        )

    async def _notify(self) -> None:
        async with self._changed:
            self._change_version += 1
            self._changed.notify_all()
        self._wake_timer.set()

    async def _commit(
        self,
        record: ActivityRecord,
        *,
        expected_version: int | None,
        command: ActivityCommand | None = None,
        feedback: ActivityFeedback | None = None,
    ) -> None:
        """权威失败锁存撤销派发；原异常原样交回，不用内存快照冒充成功提交。"""
        try:
            await self._store.commit_activity(
                record,
                expected_version=expected_version,
                command=command,
                feedback=feedback,
            )
        except Exception as error:
            self._failure = error
            raise
        await self._notify()

    def _used(self, record: ActivityRecord) -> float:
        clock = self._clocks.get(record.activity_id)
        if clock is None:
            return record.used_seconds
        return max(record.used_seconds, clock.used_at_start + self._monotonic() - clock.started_at)

    def _in_flight(self, activity_id: str) -> bool:
        return any(step.activity_id == activity_id for step in self._steps.values())

    async def _admit(self, request: _Request) -> ActivityCommand:
        """接管所有者跨外部等待取消存活，持久提交后才安排实际驱动交接。"""
        async with self._lock:
            self._available()
            previous = await self._store.get_activity_command(request.command_id)
            self._available()
            if previous is not None:
                identity = (
                    previous.command,
                    None if previous.command == "start" else previous.activity_id,
                    previous.definition,
                    previous.goal,
                    previous.additional_seconds,
                    previous.reason,
                )
                if identity != (
                    request.command,
                    request.activity_id,
                    request.definition,
                    request.goal,
                    request.additional_seconds,
                    request.reason,
                ):
                    raise ActivityError("命令身份对应不同参数。", code="activity_conflict")
                return previous

            now = self._wall_time()
            if request.command == "start":
                assert request.definition is not None and request.goal is not None
                spec = self._definitions.get(request.definition)
                if spec is None:
                    raise ActivityError("活动 definition 未登记。", code="activity_definition")
                active = await self._store.activity_records()
                self._available()
                owned = {row.activity_id for row in active} | set(self._drivers)
                if len(owned) >= self._max_active or (
                    spec.single_instance
                    and any(row.definition == spec.definition for row in active)
                ):
                    raise ActivityError("活动数量或单实例额度已满。", code="activity_capacity")
                record = ActivityRecord(
                    "activity-" + uuid4().hex,
                    spec.definition,
                    request.goal,
                    "starting",
                    1,
                    1,
                    self._initial,
                    0.0,
                    None,
                    False,
                    False,
                    False,
                    request.command_id,
                    "{}",
                    None,
                    now,
                    now,
                )
                expected_version = None
                prior_state: ActivityState = "starting"
            else:
                assert request.activity_id is not None
                current = await self.activity(request.activity_id)
                self._available()
                if (
                    request.expected_version is not None
                    and current.version != request.expected_version
                ):
                    raise ActivityError(
                        "活动状态已变化，请读取当前状态。", code="activity_conflict"
                    )
                if request.command in ("resume", "adjust") and any(
                    owner_activity == current.activity_id and kind in ("start", "resume", "adjust")
                    for owner_activity, kind in self._delivery_kinds.values()
                ):
                    raise ActivityError("上一项普通控制尚未完成受理。", code="activity_busy")
                expected_version = current.version
                prior_state = current.state
                record = self._control_record(current, request, now)
            status: Literal["admitted", "confirmed"] = (
                "confirmed" if request.command == "extend" else "admitted"
            )
            record = replace(record, control_version=record.version)
            command = ActivityCommand(
                request.command_id,
                record.activity_id,
                request.command,
                request.definition,
                request.goal,
                request.additional_seconds,
                request.reason,
                request.cause,
                prior_state,
                record.run_generation,
                record.version,
                status,
                now,
                confirmed_at=now if status == "confirmed" else None,
                actual_state=record.state if status == "confirmed" else None,
                applied=True if status == "confirmed" else None,
            )
            self._available()
            await self._commit(record, expected_version=expected_version, command=command)
            if request.command != "extend":
                self._schedule_delivery(command)
            return command

    def _control_record(
        self,
        current: ActivityRecord,
        request: _Request,
        now: float,
    ) -> ActivityRecord:
        """接管控制立即撤销下一步许可；实际运行计时到真实停止确认才结清。"""
        if current.state in ("ended", "completed"):
            raise ActivityError("已结束活动不能恢复或控制。")
        if request.command == "extend":
            if (
                current.state not in ("paused", "budget_exhausted", "interrupted")
                or current.clock_running
                or self._in_flight(current.activity_id)
            ):
                raise ActivityError("追加仅允许已停止且无在途收尾的活动。")
            assert request.additional_seconds is not None
            if current.remaining_seconds + request.additional_seconds > self._maximum:
                raise ActivityError("追加后剩余额度超过上限。", code="activity_budget")
            return replace(
                current,
                version=current.version + 1,
                last_command_id=request.command_id,
                granted_seconds=current.granted_seconds + request.additional_seconds,
                updated_at=now,
            )
        if request.command == "resume":
            if current.state not in (
                "paused",
                "budget_exhausted",
                "interrupted",
                "awaiting_decision",
            ):
                raise ActivityError("当前状态不能恢复。")
            if (
                current.needs_reconciliation
                or current.clock_running
                or self._in_flight(current.activity_id)
            ):
                raise ActivityError("恢复前需要明确核对停止状态与在途效果。")
            if current.remaining_seconds <= 0:
                raise ActivityError("活动预算耗尽，须明确追加后再恢复。", code="activity_budget")
            state: ActivityState = "resume_requested"
        elif request.command == "pause":
            if current.state in (
                "pause_requested",
                "end_requested",
                "paused",
                "budget_exhausted",
                "interrupted",
            ):
                raise ActivityError("当前状态不能再次暂停。")
            state = "pause_requested"
        elif request.command == "end":
            if current.state == "end_requested":
                raise ActivityError("活动正在结束。")
            state = "end_requested"
        else:
            if current.state not in ("running", "paused", "awaiting_decision"):
                raise ActivityError("当前状态不能调整目标。")
            state = "adjust_requested"
        return replace(
            current,
            state=state,
            version=current.version + 1,
            run_generation=current.run_generation + (1 if request.command == "resume" else 0),
            last_command_id=request.command_id,
            goal=request.goal if request.goal is not None else current.goal,
            updated_at=now,
        )

    def _schedule_delivery(self, command: ActivityCommand) -> None:
        """状态限制最多一项普通控制及暂停/结束交接，不建立无界命令任务队列。"""
        task = asyncio.create_task(self._deliver(command), name="purrcept-activity-control")
        self._deliveries[command.command_id] = task
        self._delivery_kinds[command.command_id] = (command.activity_id, command.command)

        def finished(owner: asyncio.Task[None]) -> None:
            self._deliveries.pop(command.command_id, None)
            self._delivery_kinds.pop(command.command_id, None)
            if not owner.cancelled():
                owner.exception()

        task.add_done_callback(finished)

    async def _deliver(self, command: ActivityCommand) -> None:
        """专业等待在提交锁外；没有实际驱动或确认时不宣称已经运行。"""
        try:
            # 工厂装配串行，避免暂停在启动的数据库读取期间误判没有驱动。
            # accept 等待仍必须在锁外，让停止控制优先进入驱动。
            async with self._lock:
                self._available()
                driver = self._drivers.get(command.activity_id)
                if driver is None:
                    if command.command != "start":
                        raise ActivityError("活动驱动未核对，不能重放或猜测控制效果。")
                    record = await self.activity(command.activity_id)
                    self._available()
                    spec = self._definitions[record.definition]
                    driver = spec.factory(ActivityPort(self, command.activity_id))
                    self._drivers[command.activity_id] = driver
            self._available()
            acceptance = await driver.accept(command, ActivityPort(self, command.activity_id))
        except Exception as error:
            # 插件交接失败保留未知控制事实；原错误不转换成业务成功或重发机会。
            await self._acceptance(command, None, error)
            return
        await self._acceptance(command, acceptance, None)

    async def _acceptance(
        self,
        original: ActivityCommand,
        acceptance: DriverAcceptance | None,
        error: Exception | None,
    ) -> None:
        async with self._lock:
            command = await self.command_record(original.command_id)
            if command.status in ("confirmed", "rejected"):
                # confirm 可以在 accept 返回前发生。终结事实优先，迟到受理
                # 不能把 confirmed 改回 accepted 或重写已确认的活动状态。
                return
            current = await self.activity(original.activity_id)
            accepted = acceptance is not None and acceptance.accepted
            updated = replace(
                command,
                status="accepted" if accepted else ("unknown" if error is not None else "rejected"),
                accepted_at=self._wall_time() if accepted else None,
                error_type=None if error is None else type(error).__name__,
                error=str(error)
                if error is not None
                else (None if accepted else acceptance.reason if acceptance else ""),
            )
            record = replace(current, version=current.version + 1, updated_at=self._wall_time())
            if (
                not accepted
                and current.last_command_id == command.command_id
                and current.state not in ("ended", "completed")
            ):
                record = replace(record, state="awaiting_decision", needs_reconciliation=True)
            await self._commit(record, expected_version=current.version, command=updated)

    async def confirm(
        self,
        activity_id: str,
        command_id: str,
        *,
        run_generation: int,
        version: int,
        state: ActivityState,
    ) -> ActivityCommand:
        """保存原控制身份的真实确认，迟到确认不覆盖终态或更新代次。

        普通计时检查点改变当前版本，但不改原命令版本；确认仍核验原命令身份。
        applied=False 保留发生过的控制事实，不将它冒充当前运行状态。
        """
        async with self._lock:
            command = await self.command_record(command_id)
            if (
                command.activity_id != activity_id
                or command.run_generation != run_generation
                or command.version != version
            ):
                raise ActivityError("确认与命令身份或运行代次不符。", code="activity_conflict")
            if command.status == "confirmed":
                if command.actual_state != state:
                    raise ActivityError("同一控制收到冲突确认。", code="activity_conflict")
                return command
            if command.status == "rejected":
                raise ActivityError("已明确拒绝的控制不能追加相反确认。", code="activity_conflict")
            allowed: tuple[ActivityState, ...]
            if command.command == "pause":
                allowed = ("paused", "completed")
            elif command.command == "end":
                allowed = ("ended", "completed")
            else:
                allowed = ("running", "paused", "completed", "awaiting_decision")
            if state not in allowed:
                raise ActivityError("真实确认状态不适用于该控制。", code="activity_arguments")
            current = await self.activity(activity_id)
            applied = (
                current.state not in ("ended", "completed")
                and current.last_command_id == command_id
                and current.run_generation == run_generation
                and not current.needs_reconciliation
                and not self._closing
            )
            record = replace(current, version=current.version + 1, updated_at=self._wall_time())
            if applied:
                target = (
                    "budget_exhausted" if command.cause == "budget" and state == "paused" else state
                )
                record = self._timed_state(record, target)
            updated = replace(
                command,
                status="confirmed",
                confirmed_at=self._wall_time(),
                actual_state=state,
                applied=applied,
                error_type=None,
                error=None,
            )
            await self._commit(record, expected_version=current.version, command=updated)
            self._apply_clock(record)
            return updated

    def _timed_state(self, record: ActivityRecord, state: ActivityState) -> ActivityRecord:
        running = state == "running"
        used = self._used(record)
        return replace(
            record,
            state=state,
            used_seconds=used,
            clock_running=running,
            checkpoint_at=self._wall_time(),
        )

    def _apply_clock(self, record: ActivityRecord) -> None:
        """成功提交后才改变单调起点；提交失败不把未记录累计清零。"""
        if record.clock_running and record.activity_id not in self._clocks:
            now = self._monotonic()
            self._clocks[record.activity_id] = _Clock(
                record.run_generation,
                record.used_seconds,
                now,
                now,
            )
        elif not record.clock_running:
            self._clocks.pop(record.activity_id, None)
        if self._clocks and (self._timer is None or self._timer.done()) and not self._closing:
            self._timer = asyncio.create_task(self._timing_loop(), name="purrcept-activity-clock")
            self._timer.add_done_callback(_observe)
        self._wake_timer.set()
        if record.state in ("ended", "completed") and record.activity_id in self._drivers:
            if record.activity_id not in self._retiring and not self._closing:
                task = asyncio.create_task(
                    self._retire(record.activity_id), name="purrcept-activity-retire"
                )
                self._retiring[record.activity_id] = task
                task.add_done_callback(_observe)

    async def _retire(self, activity_id: str) -> None:
        """终态驱动实际收回后才释放资源额度；不在反馈调用栈中自等待。"""
        driver = self._drivers[activity_id]
        try:
            await self._close_driver(activity_id, driver)
            owners = tuple(
                task
                for command_id, task in self._deliveries.items()
                if self._delivery_kinds[command_id][0] == activity_id
            )
            if owners:
                await asyncio.gather(*owners)
            async with self._lock:
                self._drivers.pop(activity_id)
                self._driver_closures.pop(activity_id)
        except Exception as error:
            self._failure = error
            raise
        finally:
            self._retiring.pop(activity_id, None)

    async def _close_driver(self, activity_id: str, driver: ActivityDriver) -> None:
        """同一驱动只有一次实际 close；失败也保留原关闭进度，不能盲目重试。"""
        owner = self._driver_closures.get(activity_id)
        if owner is None:
            owner = asyncio.create_task(driver.close(), name="purrcept-activity-driver-close")
            self._driver_closures[activity_id] = owner
            owner.add_done_callback(_observe)
        await asyncio.shield(owner)

    async def begin_step(self, activity_id: str, run_generation: int) -> ActivityStep:
        """执行所有者在进入真实 Provider 前取得许可，等待提交后再次核验资格。

        服务不会替执行者释放步骤。关闭或暂停在存储等待期间发生时，禁止启动；
        已取得的步骤继续由执行所有者保存真实结果，再调用 finish。
        """
        self._available()
        if (
            isinstance(run_generation, bool)
            or not isinstance(run_generation, int)
            or run_generation < 1
        ):
            raise ActivityError("活动运行代次必须是正整数。", code="activity_arguments")
        if len(self._steps) + self._step_requests >= self._max_steps:
            raise ActivityError("活动步骤接管名额已满。", code="activity_busy")
        # 在首个 await 前预留，锁/存储等待也计入同一已有等待界，不能通过
        # 并发 begin_step 在实际登记之前积累无限个服务等待者。
        self._step_requests += 1
        try:
            async with self._lock:
                self._available()
                record = await self.activity(activity_id)
                self._available()
                if (
                    record.state != "running"
                    or record.run_generation != run_generation
                    or record.needs_reconciliation
                ):
                    raise ActivityError("活动当前没有新步骤资格。")
                used = self._used(record)
                if used >= record.granted_seconds:
                    await self._expire(record)
                    raise ActivityError("活动运行额度耗尽。", code="activity_budget")

                # 许可门槛也是累计检查点；提交等待结束不能采用过期关闭资格。
                updated = replace(
                    record,
                    version=record.version + 1,
                    used_seconds=used,
                    checkpoint_at=self._wall_time(),
                    updated_at=self._wall_time(),
                )
                await self._commit(updated, expected_version=record.version)
                self._available()
                latest = await self.activity(activity_id)
                self._available()
                if latest.state != "running" or latest.run_generation != run_generation:
                    raise ActivityError("活动步骤在提交等待期间失去资格。")
                if self._used(latest) >= latest.granted_seconds:
                    await self._expire(latest)
                    raise ActivityError("活动运行额度耗尽。", code="activity_budget")
                step_id = "step-" + uuid4().hex
                step = ActivityStep(activity_id, run_generation, step_id, self)
                self._steps[step_id] = step
                return step
        finally:
            self._step_requests -= 1
            self._steps_changed.set()

    async def _execution_budget(self, activity_id: str, run_generation: int) -> float | None:
        """持单一状态锁核当前权威事实；资格 await 后也不能采用旧活动投影。"""
        async with self._lock:
            if self._closing:
                return None
            self._available()
            record = await self.activity(activity_id)
            if self._closing:
                return None
            self._available()
            if (
                record.state != "running"
                or record.run_generation != run_generation
                or record.needs_reconciliation
            ):
                return None
            remaining = record.granted_seconds - self._used(record)
            if remaining <= 0:
                await self._expire(record)
                return None
            return remaining

    async def invoke(
        self,
        activity_id: str,
        path: str,
        arguments: JsonObject,
        *,
        run_generation: int,
        timeout_seconds: float | None = None,
    ) -> ActionRecord:
        """预留既有步骤名额，再由独立所有者交给共用执行器并跟踪真实归还。"""
        self._available()
        invoker, released = self._invoke, self._wait_invocation_released
        if invoker is None or released is None:
            raise ActivityError("活动未装配真实能力调用端口。", code="activity_unavailable")
        if timeout_seconds is not None:
            timeout_seconds = _positive(timeout_seconds, "timeout_seconds")
        # 驱动请求在首个持久化等待前冻结，不能被共享可变参数改写本次意图。
        frozen_arguments = decode_object(encode_json(arguments))
        step = await self.begin_step(activity_id, run_generation)
        observed: asyncio.Future[ActionRecord] = asyncio.get_running_loop().create_future()
        observed.add_done_callback(_observe)
        owner = asyncio.create_task(
            self._invoke_owned(
                step, path, frozen_arguments, timeout_seconds, invoker, released, observed
            ),
            name=f"purrcept-activity-call:{step.step_id}",
        )
        self._invocations[step.step_id] = owner

        def finished(task: asyncio.Task[None]) -> None:
            self._invocations.pop(step.step_id)
            if not task.cancelled() and (error := task.exception()) is not None:
                self._failure = error

        owner.add_done_callback(finished)
        return await asyncio.shield(observed)

    async def _invoke_owned(
        self,
        step: ActivityStep,
        path: str,
        arguments: JsonObject,
        timeout_seconds: float | None,
        invoker: ActivityInvoker,
        released: Callable[[str], Awaitable[None]],
        observed: asyncio.Future[ActionRecord],
    ) -> None:
        """首版结果交付与资源收尾分开；权威提交失败也依已关联身份等待真实归还。"""
        admitted_ids: list[str] = []

        def admitted(identifier: str) -> None:
            admitted_ids.append(identifier)

        async def before_start() -> float | None:
            return await self._execution_budget(step.activity_id, step.run_generation)

        try:
            remaining = await before_start()
            if remaining is None:
                raise ActivityError("活动调用在接管前失去资格。")
            seconds = remaining if timeout_seconds is None else min(remaining, timeout_seconds)
            record = await invoker(
                path,
                arguments,
                input_id="activity:" + step.activity_id,
                before_start=before_start,
                on_admitted=admitted,
                timeout_seconds=seconds,
            )
            if admitted_ids != [record.action_id]:
                raise RuntimeError("活动执行器未关联同一真实行动身份。")
            observed.set_result(record)
        except BaseException as error:
            # 只向观察者原样交回，不翻译成可纠正的活动参数错误。即使观察取消，
            # owned Task 仍继续下面的真实归还；Future 回调收回无人观察的异常。
            observed.set_exception(error)
        finally:
            if admitted_ids:
                await released(admitted_ids[0])
            await step.finish()

    async def finish_step(self, step: ActivityStep) -> None:
        """仅在实际收尾后释放宿主步骤事实，重复完成不会重复扣减或反馈。"""
        async with self._lock:
            current = self._steps.get(step.step_id)
            if current is None:
                return
            if current is not step:
                raise ActivityError("步骤不属于本服务。", code="activity_conflict")
            # 先取得通知锁，再原子归还与唤醒，取消不会在已归还后丢掉关闭通知。
            async with self._changed:
                self._steps.pop(step.step_id)
                self._steps_changed.set()
                self._change_version += 1
                self._changed.notify_all()
            self._wake_timer.set()

    async def checkpoint_work(
        self,
        activity_id: str,
        *,
        run_generation: int,
        version: int,
        work_json: str,
    ) -> ActivityRecord:
        """只提交当前代次和版本的专业工作；它不修改控制或权威长期认识。"""
        validate_json(work_json)
        async with self._lock:
            current = await self.activity(activity_id)
            if current.run_generation != run_generation or current.version != version:
                raise ActivityError("活动工作检查点已经过期。", code="activity_conflict")
            record = replace(
                current,
                version=current.version + 1,
                work_json=work_json,
                updated_at=self._wall_time(),
            )
            await self._commit(record, expected_version=current.version)
            return record

    async def feedback(
        self,
        activity_id: str,
        feedback_id: str,
        kind: FeedbackKind,
        payload_json: str,
        *,
        run_generation: int,
        version: int,
        command_id: str | None = None,
    ) -> ActivityFeedback:
        """保存业务原文与可靠交接；旧反馈只留事实，不恢复或覆盖当前控制。"""
        _nonempty(feedback_id, "feedback_id")
        validate_json(payload_json)
        if kind not in ("progress", "completed", "failed", "needs_decision", "reconciled"):
            raise ActivityError("活动反馈类别无效。", code="activity_arguments")
        async with self._lock:
            previous = await self._store.get_activity_feedback(feedback_id)
            if previous is not None:
                if (
                    previous.activity_id,
                    previous.command_id,
                    previous.run_generation,
                    previous.version,
                    previous.kind,
                    previous.payload_json,
                ) != (activity_id, command_id, run_generation, version, kind, payload_json):
                    raise ActivityError("反馈身份对应不同材料。", code="activity_conflict")
                return previous
            current = await self.activity(activity_id)
            applied = (
                current.run_generation == run_generation
                and current.control_version <= version <= current.version
                and current.state not in ("ended", "completed")
                and not current.needs_reconciliation
            )
            record = replace(current, version=current.version + 1, updated_at=self._wall_time())
            if applied:
                if kind == "progress":
                    record = replace(record, progress_json=payload_json)
                elif kind == "completed":
                    record = self._timed_state(record, "completed")
                elif kind in ("failed", "needs_decision"):
                    record = self._timed_state(record, "awaiting_decision")
            notice = ActivityFeedback(
                feedback_id,
                activity_id,
                command_id,
                run_generation,
                version,
                kind,
                payload_json,
                self._wall_time(),
                applied,
            )
            await self._commit(record, expected_version=current.version, feedback=notice)
            self._apply_clock(record)
            return notice

    async def reconcile(self, activity_id: str) -> RecoveryObservation:
        """明确只读核对后保存材料；未知或仍运行时不解除恢复前提。

        此入口只有明确核对调用才构造驱动；start 的恢复装载永远不调用工厂。
        工厂契约禁止构造即执行。观察在事务外取得，返回时再次核当前版本。
        """
        self._available()
        async with self._lock:
            current = await self.activity(activity_id)
            self._available()
            if current.state in ("ended", "completed"):
                raise ActivityError("已结束活动无需恢复核对。")
            driver = self._drivers.get(activity_id)
            if driver is None:
                spec = self._definitions.get(current.definition)
                if spec is None:
                    raise ActivityError("活动定义未登记，无法核对。", code="activity_definition")
                driver = spec.factory(ActivityPort(self, activity_id))
                self._drivers[activity_id] = driver
        observation = await driver.reconcile(current)
        validate_json(observation.evidence_json)
        async with self._lock:
            self._available()
            latest = await self.activity(activity_id)
            self._available()
            if latest.version != current.version or latest.run_generation != current.run_generation:
                raise ActivityError(
                    "核对期间活动已经改变，观察不能采用。", code="activity_conflict"
                )
            stopped = observation.stopped is True and not self._in_flight(activity_id)
            record = replace(
                latest,
                version=latest.version + 1,
                needs_reconciliation=not stopped,
                updated_at=self._wall_time(),
            )
            if stopped:
                record = self._timed_state(
                    record, "interrupted" if latest.state == "interrupted" else "paused"
                )
            notice = ActivityFeedback(
                "reconcile-" + uuid4().hex,
                activity_id,
                None,
                record.run_generation,
                current.version,
                "reconciled",
                observation.evidence_json,
                self._wall_time(),
                True,
            )
            await self._commit(record, expected_version=latest.version, feedback=notice)
            self._apply_clock(record)
            return observation

    async def checkpoint(self) -> None:
        """结清本次已观察累计，并立即核预算；用于计时任务与正常关闭前检查。"""
        async with self._lock:
            for activity_id in tuple(self._clocks):
                current = await self.activity(activity_id)
                used = self._used(current)
                if used >= current.granted_seconds and current.state not in (
                    "budget_exhausted",
                    "pause_requested",
                    "end_requested",
                    "ended",
                    "completed",
                ):
                    await self._expire(current)
                    continue
                record = replace(
                    current,
                    version=current.version + 1,
                    used_seconds=used,
                    checkpoint_at=self._wall_time(),
                    updated_at=self._wall_time(),
                )
                await self._commit(record, expected_version=current.version)
                clock = self._clocks.get(activity_id)
                if clock is not None:
                    clock.checkpoint_at = self._monotonic()

    async def _expire(self, current: ActivityRecord) -> None:
        """预算耗尽先禁止步骤，再用独立控制交接停止并保存需要决策的事实。"""
        now = self._wall_time()
        command_id = "budget-" + uuid4().hex
        record = replace(
            current,
            state="budget_exhausted",
            version=current.version + 1,
            used_seconds=self._used(current),
            checkpoint_at=now,
            last_command_id=command_id,
            control_version=current.version + 1,
            updated_at=now,
        )
        command = ActivityCommand(
            command_id,
            record.activity_id,
            "pause",
            None,
            None,
            None,
            None,
            "budget",
            current.state,
            record.run_generation,
            record.version,
            "admitted",
            now,
        )
        notice = ActivityFeedback(
            "feedback-" + command_id,
            record.activity_id,
            command_id,
            record.run_generation,
            record.version,
            "needs_decision",
            '{"reason":"activity_budget_exhausted"}',
            now,
            True,
        )
        await self._commit(
            record, expected_version=current.version, command=command, feedback=notice
        )
        self._schedule_delivery(command)

    async def _timing_loop(self) -> None:
        """一个所有者只为实际 running 时段等待；暂停集合不周期唤醒。"""
        while self._clocks and not self._closing and self._failure is None:
            # 在任何数据库等待前清除上一轮信号。读取期间的关闭/控制通知
            # 必须保留，不能在 await 返回后清掉已经发生的唤醒。
            self._wake_timer.clear()
            now = self._monotonic()
            delay = self._checkpoint
            for activity_id, clock in tuple(self._clocks.items()):
                record = await self.activity(activity_id)
                delay = min(delay, max(0.0, clock.checkpoint_at + self._checkpoint - now))
                if record.state not in ("budget_exhausted", "pause_requested", "end_requested"):
                    delay = min(delay, max(0.0, record.granted_seconds - self._used(record)))
            if self._closing or self._failure is not None:
                return
            try:
                async with asyncio.timeout(delay):
                    await self._wake_timer.wait()
            except TimeoutError:
                if not self._closing:
                    await self.checkpoint()

    async def close(self) -> None:
        """立即禁止下一步，等待实际驱动、控制与已接管提交后结清运行累计。

        无法收回的真实步骤保持关闭未完成，不能提前释放业务资源。Host 的有限
        关闭期限只限制其等待；本所有者仍负责实际收尾，不把超时伪报为已关闭。
        """
        if self._closing_task is None:
            self._closing = True
            self._wake_timer.set()
            self._closing_task = asyncio.create_task(
                self._close(), name="purrcept-activities-close"
            )
            self._closing_task.add_done_callback(_observe)
        await asyncio.shield(self._closing_task)

    async def _close(self) -> None:
        """先收回计时和驱动，再提交最后累计；不在锁内等待专业收尾。"""
        errors: list[Exception] = []
        if self._timer is not None:
            try:
                await self._timer
            except Exception as error:
                errors.append(error)
        if self._requests:
            results = await asyncio.gather(
                *(owner for _, owner in tuple(self._requests.values())),
                return_exceptions=True,
            )
            errors.extend(result for result in results if isinstance(result, Exception))
        if self._retiring:
            retired = await asyncio.gather(*tuple(self._retiring.values()), return_exceptions=True)
            errors.extend(result for result in retired if isinstance(result, Exception))
        # 一项资源失败仍收回其余独立资源；失败的原关闭任务留在所有者中。
        for activity_id, driver in tuple(self._drivers.items()):
            try:
                await self._close_driver(activity_id, driver)
            except Exception as error:
                if error not in errors:
                    errors.append(error)
            else:
                self._drivers.pop(activity_id)
                self._driver_closures.pop(activity_id)
        if self._deliveries:
            results = await asyncio.gather(
                *tuple(self._deliveries.values()), return_exceptions=True
            )
            errors.extend(result for result in results if isinstance(result, Exception))
        # 新资格已经关闭；提交等待与真实在途都须归还。信号只在计数变化
        # 后设置，取消步骤等待也无需一次额外可取消的异步通知才能唤醒关闭。
        while self._steps or self._step_requests:
            self._steps_changed.clear()
            await self._steps_changed.wait()
        async with self._lock:
            if self._failure is None:
                records = await self._store.activity_records()
                for current in records:
                    interrupted = current.clock_running or current.state in (
                        "starting",
                        "resume_requested",
                        "pause_requested",
                        "end_requested",
                        "adjust_requested",
                    )
                    state = "interrupted" if interrupted else current.state
                    record = self._timed_state(
                        replace(
                            current,
                            version=current.version + 1,
                            needs_reconciliation=True,
                            control_version=current.version + 1,
                            updated_at=self._wall_time(),
                        ),
                        state,
                    )
                    await self._commit(record, expected_version=current.version)
                    self._apply_clock(record)
            elif isinstance(self._failure, Exception) and self._failure not in errors:
                errors.append(self._failure)
        if errors:
            raise ExceptionGroup("活动收尾未全部完成。", errors)
