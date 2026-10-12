"""宿主的资源所有权和嵌入生命周期。

EngineHost 串起已校验配置、目录锁、权威存储与后续主体装配。关闭只有一个
收尾任务：调用者超时或取消只结束自己的等待，仍在使用的库和目录锁继续归
宿主所有。这里不拥有模型工具循环，也不把输入接管等同于主体已处理。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal
from uuid import uuid4

from purrcept_core.models import DecisionBackend, ModelBackend

from purrcept_engine.context.capacity import CapacityRule
from purrcept_engine.errors import ConfigError, HostStateError, StorageError
from purrcept_engine.execution.catalog import Catalog
from purrcept_engine.execution.ports import ActionExecutor, ExecutionJournal
from purrcept_engine.execution.protocol import JsonObject
from purrcept_engine.execution.providers import CapabilityImplementation
from purrcept_engine.plugins.base import PluginFactory, PluginPorts, PluginRuntime
from purrcept_engine.plugins.panels import PreviewFrame
from purrcept_engine.storage import (
    ActionRecord,
    Admission,
    InputRecord,
    InputSubmission,
)
from purrcept_engine.storage.activities import ActivityCommand, ActivityRecord, CommandKind
from purrcept_engine.storage.controls import ControlRecord
from purrcept_engine.storage.model_requests import RequestRecord
from purrcept_engine.storage.projections import action_projection
from purrcept_engine.storage.store import Store
from purrcept_engine.storage.subject import InputProcessing
from purrcept_engine.subject.activities import ActivityService

from .activity import activity_capability
from .assembly import prepare_plugins
from .config import EngineConfig, ExecutionSettings, GenerationModelSettings
from .evidence import EvidenceAccess
from .locking import InstanceLock
from .models import OpenAIGenerationResource, TypeSafeDecisionResource
from .panel import ControlRequest, PanelController
from .receipts import LateCallResults
from .subject import SubjectAssembly, build_subject

_logger = logging.getLogger(__name__)
type HostPhase = Literal["new", "starting", "running", "closing", "closed", "failed"]
type ExecutorFactory = Callable[
    [Catalog, tuple[CapabilityImplementation, ...], ExecutionJournal, ExecutionSettings, Path],
    ActionExecutor,
]


@dataclass(frozen=True, slots=True)
class HostSnapshot:
    """一次宿主事实投影；版本只在生命周期变化后增加，查询不启动资源。"""

    instance_id: str
    version: int
    phase: HostPhase
    pending_resources: tuple[str, ...]


class EngineHost:
    """由 CLI 或嵌入调用者创建并独占的宿主，所有方法在同一事件循环使用。

    start 和 close 都合并重复调用；关闭后不能重新启动同一个实例。submit
    只确认权威接管，query_input 读取其实际状态。输入等待超时不回滚接管。
    进程退出属于 CLI，库入口始终保留调用者对进程的处置权。
    """

    def __init__(
        self,
        config: EngineConfig,
        *,
        plugin_factories: Mapping[str, PluginFactory] | None = None,
        executor_factory: ExecutorFactory | None = None,
        console_output: Callable[[str], None] = print,
        generation_backends: Mapping[str, ModelBackend] | None = None,
        decision_backends: Mapping[str, DecisionBackend] | None = None,
        capacity_rules: Mapping[str, CapacityRule] | None = None,
    ) -> None:
        """保存配置并核验纯声明；具体资源与可替换执行器仅在 start 装配。

        plugin_factories 显式登记可信工厂，不触发自动扫描。executor_factory
        可替换平台执行器，但仍借用有限 ExecutionJournal 并满足 ActionExecutor
        的资格、真实效果与资源寿命契约，不能降级成宿主 exec。
        """
        if not config.host.data_dir.is_absolute():
            raise HostStateError("创建宿主前必须显式解析数据目录。")
        self._config = config
        self._prepared = prepare_plugins(
            config.plugins,
            base_dir=config.plugin_base_dir,
            factories={} if plugin_factories is None else plugin_factories,
            console_output=console_output if config.console else None,
        )
        self._executor_factory = executor_factory
        self._generation_backends = MappingProxyType(dict(generation_backends or {}))
        self._decision_backends = MappingProxyType(dict(decision_backends or {}))
        self._capacity_rules = MappingProxyType(dict(capacity_rules or {}))
        for name in self._generation_backends:
            if not isinstance(config.models.get(name), GenerationModelSettings):
                raise ConfigError("生成后端必须绑定已声明的生成配置名。")
        for name in self._decision_backends:
            settings = config.models.get(name)
            if settings is None or isinstance(settings, GenerationModelSettings):
                raise ConfigError("判断后端必须绑定已声明的判断配置名。")
        self._instance_id = str(uuid4())
        self._phase: HostPhase = "new"
        self._version = 0
        self._lock = InstanceLock(config.host.data_dir)
        self._store: Store | None = None
        self._models: dict[str, OpenAIGenerationResource | TypeSafeDecisionResource] = {}
        self._plugins: dict[str, PluginRuntime] = {}
        self._execution: ActionExecutor | None = None
        self._activities: ActivityService | None = None
        self._subject: SubjectAssembly | None = None
        self._panel: PanelController | None = None
        self._panel_close_task: asyncio.Task[None] | None = None
        self._subject_close_task: asyncio.Task[None] | None = None
        self._work_changed = asyncio.Event()
        self._activity_close_task: asyncio.Task[None] | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._close_deadline: float | None = None
        self._owns_lock = False

    @property
    def config(self) -> EngineConfig:
        """返回本实例启动快照；运行期间不能替换它来改变资源和关闭边界。"""
        return self._config

    @property
    def catalog(self) -> Catalog:
        """返回同一冻结能力目录；查询不开始插件、网络或 Worker。"""
        return self._prepared.catalog

    async def start(self) -> HostSnapshot:
        """取得单实例与核心存储；失败清理自身资源，重复调用等待同次启动。"""
        if self._phase in ("closing", "closed", "failed"):
            raise HostStateError("该宿主不能启动。")
        if self._start_task is None:
            self._transition("starting")
            self._start_task = asyncio.create_task(self._start(), name="engine-start")

        # Lifecycle: 开始取得资源后，取消一个等待者不能丢掉资源所有者。
        await asyncio.shield(self._start_task)
        return self.query()

    async def _start(self) -> None:
        """先锁目录再开库；启动期间若收到关闭，不重新开放业务入口。"""
        try:
            self._lock.acquire()
            self._owns_lock = True
            self._store = await Store.open(
                self.config.host.data_dir / "engine.sqlite3",
                max_inputs=self.config.execution.max_pending_inputs,
                max_input_bytes=self.config.execution.pending_input_bytes,
            )
            model_names = {
                self.config.subject.foreground,
                self.config.subject.reflection,
                self.config.subject.summary,
            }
            if self.config.subject.attention is not None:
                model_names.add(self.config.subject.attention)
            generation: dict[str, ModelBackend] = {}
            decisions: dict[str, DecisionBackend] = {}
            for name in sorted(model_names):
                settings = self.config.models[name]
                if isinstance(settings, GenerationModelSettings):
                    backend = self._generation_backends.get(name)
                    if backend is None:
                        resource = OpenAIGenerationResource.create(settings)
                        # 资源取得后立即归属宿主；显式借用的嵌入后端归调用者关闭。
                        self._models[name] = resource
                        backend = resource.backend
                    generation[name] = backend
                else:
                    decision = self._decision_backends.get(name)
                    if decision is None:
                        resource = TypeSafeDecisionResource.create(settings)
                        self._models[name] = resource
                        decision = resource.backend
                    decisions[name] = decision
            capability_paths = {
                binding.declaration.capability_id: binding.path
                for binding in self.catalog.bindings.values()
            }
            for instance_id, blueprint in self._prepared.blueprints:
                paths = frozenset(
                    capability_paths[capability.capability_id]
                    for capability in blueprint.capabilities
                )
                ports = PluginPorts(
                    self,
                    self._projection,
                    EvidenceAccess(self._store.knowledge, self.config.context.page_chars),
                    self._store.time,
                    LateCallResults(self._store, paths),
                )
                runtime = await blueprint.build(ports)
                self._plugins[instance_id] = runtime
            definitions = tuple(
                definition
                for _, blueprint in self._prepared.blueprints
                for definition in blueprint.activities
            )
            execution_for_activity: ActionExecutor | None = None

            async def invoke_activity(
                path: str,
                arguments: JsonObject,
                *,
                input_id: str,
                before_start: Callable[[], Awaitable[float | None]],
                on_admitted: Callable[[str], None],
                timeout_seconds: float | None,
            ) -> ActionRecord:
                return await self.invoke(
                    path,
                    arguments,
                    input_id=input_id,
                    before_start=before_start,
                    on_admitted=on_admitted,
                    timeout_seconds=timeout_seconds,
                )

            async def wait_activity_release(action_id: str) -> None:
                # Lifecycle: 保留装配时的执行器借用，Host 字段在实际执行关闭后
                # 可先归零；活动仍须确认这一调用的真实资源归还，不能查询坏库推断。
                if execution_for_activity is None:
                    raise HostStateError("活动执行端口尚未装配。")
                await execution_for_activity.wait_invocation_released(action_id)

            self._activities = ActivityService(
                self._store,
                definitions,
                max_active=self.config.activities.max_active,
                max_steps=self.config.execution.max_workers * self.config.execution.max_calls,
                initial_seconds=self.config.activities.initial_seconds,
                max_remaining_seconds=self.config.activities.max_remaining_seconds,
                checkpoint_seconds=self.config.activities.checkpoint_seconds,
                can_start=self._check_owned_dispatch,
                invoke=invoke_activity,
                wait_invocation_released=wait_activity_release,
            )
            await self._activities.start()
            implementations = (
                activity_capability(self._activities),
                *tuple(
                    capability
                    for runtime in self._plugins.values()
                    for capability in runtime.capabilities
                ),
            )
            if self._executor_factory is None:
                from purrcept_engine.execution.service import ExecutionService

                self._execution = ExecutionService(
                    self.catalog,
                    implementations,
                    self._store,
                    self.config.execution,
                    scratch_root=self.config.host.data_dir / "scratch",
                )
            else:
                self._execution = self._executor_factory(
                    self.catalog,
                    implementations,
                    self._store,
                    self.config.execution,
                    self.config.host.data_dir / "scratch",
                )
            execution_for_activity = self._execution
            self._panel = PanelController(
                self._instance_id,
                self.catalog,
                self._plugins,
                self._store.controls,
                self.invoke,
                self._activities,
            )
            self._subject = build_subject(
                self.config,
                self._store,
                self.catalog,
                generation,
                decisions,
                execute=self.execute,
                plugins=self._plugins,
                can_start=self._check_owned_dispatch,
                rules=self._capacity_rules,
                wakeup_committed=self._work_changed.set,
            )

            # Ordering: 目录、实现和输入端口就绪后才放行外部 start；同步输入
            # 可以在插件启动中接管。关闭已到达时不再启动任何接入。
            if self._phase == "starting":
                self._transition("running")
                await self._subject.coordinator.start()
                await self._subject.background.start()
            for runtime in self._plugins.values():
                if self._phase == "running" and runtime.start is not None:
                    await runtime.start()
        except BaseException as startup_error:
            # 部分启动失败沿同一资源逆序清理。清理失败保留所有权，并与原始
            # 启动错误一起报告，不能解锁后另起宿主覆盖未释放的资源。
            if self._phase != "closing":
                self._transition("failed")
            try:
                await self._release_resources()
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(
                    "宿主启动及收尾失败。", [startup_error, cleanup_error]
                ) from None
            raise

        if self._phase == "starting":
            self._transition("running")
        _logger.info("宿主核心存储已就绪。", extra={"host_instance": self._instance_id})

    async def submit(self, submission: InputSubmission) -> Admission:
        """原文、媒体与待处理状态同事务接管；返回后仍须查询真实处理结果。"""
        store = self._running_store()
        self._check_dispatch()
        subject = self._subject
        observed = 0 if subject is None else subject.coordinator.generation
        admission = await store.admit(submission)
        if subject is not None and not admission.duplicate:
            # 抢占绑定接管前观察的旧代次；等待数据库期间新轮次不受迟到信号影响。
            await subject.coordinator.input_admitted(
                admission.input_id,
                observed_generation=observed,
            )
        return admission

    async def query_input(self, input_id: str) -> InputRecord | None:
        """查询已接管输入；关闭收尾期间仍可使用尚未释放的存储。"""
        store = self._store
        if store is None or self._phase == "closed":
            raise HostStateError("宿主没有可查询的权威存储。")
        return await store.get_input(input_id)

    async def query_processing(self, input_id: str) -> InputProcessing | None:
        """读取宿主实际输入阶段，取得、接管及处理结束分别表达。"""
        store = self._store
        if store is None:
            raise HostStateError("宿主没有可查询的权威存储。")
        return await store.input_processing(input_id)

    async def execute(
        self,
        source: str,
        *,
        input_id: str | None = None,
        can_start: Callable[[], bool] | None = None,
    ) -> ActionRecord:
        """程序式执行明确行动，接管后的观察取消不撤销真实效果。

        主脑 play_action 使用同一入口和冻结目录，不另建平行执行路径。
        input_id 表示因果来源，不授予能力权限；can_start 撤销未派发生成。
        """
        self._check_dispatch()
        execution = self._execution
        if execution is None:
            raise HostStateError("执行服务尚未装配。")

        def eligible() -> bool:
            if self._phase in ("closing", "closed"):
                return False
            self._check_dispatch()
            return can_start is None or can_start()

        return await execution.execute(source, input_id=input_id, can_start=eligible)

    async def query_action(self, action_id: str) -> ActionRecord | None:
        """读取包括迟到调用结论的权威事实，关闭收尾期间仍可查询。"""
        store = self._store
        if store is None:
            raise HostStateError("宿主没有可查询的权威存储。")
        return await store.get_action(action_id)

    async def invoke(
        self,
        path: str,
        arguments: JsonObject,
        *,
        input_id: str | None = None,
        can_start: Callable[[], bool] | None = None,
        timeout_seconds: float | None = None,
        before_start: Callable[[], Awaitable[float | None]] | None = None,
        on_admitted: Callable[[str], None] | None = None,
    ) -> ActionRecord:
        """宿主明确调用单能力，共用目录、并发和真实回执，不先生成 Python。

        before_start 为专业活动的异步资格核验，只能拒绝或缩短原期限。
        on_admitted 交出真实关联身份，观察取消/未知后资源仍由执行器持有。
        接管身份不证明已发生效果；停止与关闭等待另有实际资源归还语义。
        """
        self._check_dispatch()
        execution = self._execution
        if execution is None:
            raise HostStateError("执行服务尚未装配。")

        def eligible() -> bool:
            if self._phase in ("closing", "closed"):
                return False
            self._check_dispatch()
            return can_start is None or can_start()

        return await execution.invoke(
            path,
            arguments,
            input_id=input_id,
            can_start=eligible,
            timeout_seconds=timeout_seconds,
            before_start=before_start,
            on_admitted=on_admitted,
        )

    async def control_activity(
        self,
        command: CommandKind,
        *,
        activity_id: str | None = None,
        definition: str | None = None,
        goal: str | None = None,
        additional_seconds: float | None = None,
        reason: str | None = None,
    ) -> ActivityCommand:
        """独立受理明确活动控制，不等待普通行动名额，也不假报实际完成。"""
        self._running_store()
        service = self._activities
        if service is None:
            raise HostStateError("活动服务尚未装配。")
        return await service.command(
            uuid4().hex,
            command,
            activity_id=activity_id,
            definition=definition,
            goal=goal,
            additional_seconds=additional_seconds,
            reason=reason,
        )

    async def query_activity(self, activity_id: str) -> ActivityRecord | None:
        """读取真实控制、预算和核对状态，不自动启动或恢复驱动。"""
        store = self._store
        if store is None:
            raise HostStateError("宿主没有可查询的权威存储。")
        return await store.get_activity(activity_id)

    async def query_request(self, request_id: str) -> RequestRecord | None:
        """读取可还原的模型材料及真实尝试，不发起新的模型调用。"""
        store = self._store
        if store is None:
            raise HostStateError("宿主没有可查询的权威存储。")
        return await store.get_request(request_id)

    async def query_panel(self) -> JsonObject:
        """读取有限真实概况与设备缓存状态；多页面查询不启动模型或设备。"""
        store, panel = self._store, self._panel
        if store is None or panel is None:
            raise HostStateError("宿主面板尚未就绪或已收回。")
        facts = await store.runtime_projection()
        snapshot = self.query()
        subject = self._subject
        return {
            **facts,
            "instance_id": snapshot.instance_id,
            "host_version": snapshot.version,
            "phase": snapshot.phase,
            "pending_resources": list(snapshot.pending_resources),
            "authority_available": store.authority_available,
            "devices": panel.devices(),
            "foreground": None if subject is None else subject.coordinator.session.snapshot(),
            "reflection": None if subject is None else subject.background.session.snapshot(),
            "pending_limits": {
                "count": self.config.execution.max_pending_inputs,
                "logical_bytes": self.config.execution.pending_input_bytes,
            },
        }

    async def submit_control(self, request: ControlRequest) -> ControlRecord:
        """提交面板允许的显式控制，共用原能力与活动状态，不经过模型。"""
        self._check_dispatch()
        if self._panel is None:
            raise HostStateError("面板控制尚未装配。")
        return await self._panel.submit(request)

    async def query_control(self, request_id: str) -> JsonObject | None:
        """查询接管记录及当前真实业务回执，未知/迟到确认仍关联原身份。"""
        store = self._store
        if store is None:
            raise HostStateError("宿主没有可查询的权威存储。")
        record = await store.controls.get(request_id)
        if record is None:
            return None
        result = record.projection()
        if record.action_id is not None:
            action = await store.get_action(record.action_id)
            result["action"] = None if action is None else action_projection(action)
        if record.command_id is not None:
            command = await store.get_activity_command(record.command_id)
            result["activity_command"] = (
                None
                if command is None
                else {
                    "command_id": command.command_id,
                    "activity_id": command.activity_id,
                    "status": command.status,
                    "actual_state": command.actual_state,
                    "applied": command.applied,
                    "error": command.error,
                }
            )
        return result

    def query_preview(self, target: str) -> PreviewFrame | None:
        """只读已取得画面；查询不额外截图或延长设备会话。"""
        if self._panel is None:
            raise HostStateError("面板尚未就绪。")
        return self._panel.preview(target)

    def query(self) -> HostSnapshot:
        """返回生命周期快照，不触发存储读取、设备或模型调用。"""
        resources: list[str] = []
        if self._phase in ("closing", "failed"):
            if self._start_task is not None and not self._start_task.done():
                resources.append("startup")
            if self._store is not None:
                resources.append("storage")
            if self._execution is not None:
                resources.append(f"execution:{self._execution.pending_resources}")
            resources.extend(f"plugin:{name}" for name in self._plugins)
            if self._activities is not None:
                resources.append(f"activities:{self._activities.pending_resources}")
            if self._subject is not None:
                resources.append(f"subject:{self._subject.coordinator.pending_resources}")
                resources.append(f"background:{self._subject.background.pending_resources}")
            resources.extend(f"model:{name}" for name in self._models)
            if self._owns_lock:
                resources.append("data_directory_lock")
        return HostSnapshot(self._instance_id, self._version, self._phase, tuple(resources))

    async def close(self) -> HostSnapshot:
        """关闭新入口并等待同一总期限；超时返回 closing，继续持有收尾资源。"""
        if self._close_task is None:
            loop = asyncio.get_running_loop()
            self._close_deadline = loop.time() + self.config.host.close_seconds
            self._transition("closing")
            self._close_task = asyncio.create_task(self._close(), name="engine-close")

        close_task = self._close_task
        deadline = self._close_deadline
        assert deadline is not None
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        if close_task.done():
            await asyncio.shield(close_task)
        elif remaining > 0:
            try:
                async with asyncio.timeout(remaining):
                    await asyncio.shield(close_task)
            except TimeoutError:
                if close_task.done():
                    # 不把收尾自身抛出的 TimeoutError 冒充等待期限到达。
                    await close_task
        return self.query()

    async def wait_closed(self, *, wait_seconds: float | None = None) -> HostSnapshot:
        """继续等待已发起的关闭；新的观察期限不重置原关闭期限或清理任务。"""
        if self._close_task is None:
            raise HostStateError("尚未发起关闭。")
        async with asyncio.timeout(wait_seconds):
            await asyncio.shield(self._close_task)
        return self.query()

    async def _close(self) -> None:
        """先收回启动所有权，再排空库；连接实际释放前不能解除目录锁。"""
        if self._start_task is not None:
            try:
                await self._start_task
            except BaseException:
                # 启动任务已经清理它取得的资源。收尾继续，但原启动等待者
                # 仍收到原异常；此处不重新启动，也不声称启动成功。
                if self._store is not None or self._owns_lock:
                    raise

        await self._release_resources()
        self._transition("closed")

    async def _release_resources(self) -> None:
        """停止接入、等待执行借用，再逆序释放插件和核心；失败保留所有者。"""
        for runtime in reversed(tuple(self._plugins.values())):
            if runtime.stop_inputs is not None:
                await runtime.stop_inputs()
        if self._subject is not None and self._subject_close_task is None:
            self._subject_close_task = asyncio.create_task(self._close_subject())
        if self._panel is not None and self._panel_close_task is None:
            self._panel_close_task = asyncio.create_task(self._panel.close())
        if self._activities is not None and self._activity_close_task is None:
            # 活动停止与 Worker/Provider 收尾并行持有，防止驱动等待自己已经
            # 开始的行动时，宿主还没有向执行器发出收尾请求。
            self._activity_close_task = asyncio.create_task(self._activities.close())
        if self._execution is not None:
            await self._execution.close()
            self._execution = None
        if self._activity_close_task is not None:
            await asyncio.shield(self._activity_close_task)
            self._activities = None
        if self._subject_close_task is not None:
            await asyncio.shield(self._subject_close_task)
            self._subject = None
        if self._panel_close_task is not None:
            await asyncio.shield(self._panel_close_task)
            self._panel = None
        for name in reversed(tuple(self._plugins)):
            runtime = self._plugins[name]
            if runtime.close is not None:
                await runtime.close()
            del self._plugins[name]
        for name in reversed(tuple(self._models)):
            await self._models[name].close()
            del self._models[name]
        if self._store is not None:
            await self._store.close()
            self._store = None
        self._lock.release()
        self._owns_lock = False

    async def _close_subject(self) -> None:
        """撤销主体等待并收回摘要，真实执行借用由并行执行收尾继续持有。"""
        subject = self._subject
        assert subject is not None
        results = list(
            await asyncio.gather(
                subject.coordinator.close(), subject.background.close(), return_exceptions=True
            )
        )
        results.extend(
            await asyncio.gather(
                subject.foreground_history.close(),
                subject.reflection_history.close(),
                return_exceptions=True,
            )
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise BaseExceptionGroup("主体与摘要收尾失败。", errors)

    def _projection(self) -> JsonObject:
        """插件借用提交后的宿主状态，不取得主脑会话或可变配置。"""
        snapshot = self.query()
        return {
            "instance_id": snapshot.instance_id,
            "version": snapshot.version,
            "phase": snapshot.phase,
            "pending_resources": list(snapshot.pending_resources),
        }

    def _check_dispatch(self) -> None:
        """新派发在实际开始前核验宿主和权威资格，收尾查询不使用此门槛。"""
        store = self._running_store()
        subject = self._subject
        if subject is not None and subject.coordinator.failure is not None:
            raise subject.coordinator.failure
        if subject is not None and subject.background.failure is not None:
            raise subject.background.failure
        if not store.authority_available:
            raise StorageError("权威存储当前不能接管新派发。")

    def _check_owned_dispatch(self) -> None:
        """将宿主正常关闭传入工作器取消链，内部故障仍保持原始类型。

        phase 先关闭新入口，再启动各实际收尾任务。这个短窗口里的固定工作器
        必须停止等待，不能把正常关闭当程序故障锁存；公开入口仍报 HostStateError。
        """
        if self._phase in ("closing", "closed"):
            raise asyncio.CancelledError
        self._check_dispatch()

    def _running_store(self) -> Store:
        if self._phase != "running" or self._store is None:
            raise HostStateError("宿主当前不接管新输入。")
        return self._store

    def _transition(self, phase: HostPhase) -> None:
        """生命周期版本与实际状态一起前进，查询不会制造新版本。"""
        self._phase = phase
        self._version += 1
