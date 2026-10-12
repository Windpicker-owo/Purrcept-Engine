"""宿主的资源所有权和嵌入生命周期。

EngineHost 串起已校验配置、目录锁、权威存储与后续主体装配。关闭只有一个
收尾任务：调用者超时或取消只结束自己的等待，仍在使用的库和目录锁继续归
宿主所有。这里不拥有模型工具循环，也不把输入接管等同于主体已处理。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import time
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
from purrcept_engine.execution.providers import CallContext, CallOutcome, CapabilityImplementation
from purrcept_engine.plugins.audio.ports import AudioOutputPort
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
from purrcept_engine.subject.knowledge import Reranker
from purrcept_engine.subject.semantic import EmbeddingProvider

from .activity import activity_capability
from .assembly import prepare_plugins
from .config import EngineConfig, ExecutionSettings, GenerationModelSettings
from .evidence import EvidenceAccess
from .knowledge_resources import KnowledgeResources
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
        embedding_provider: EmbeddingProvider | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        """保存配置并核验纯声明；具体资源与可替换执行器仅在 start 装配。

        plugin_factories 显式登记可信工厂，不触发自动扫描。executor_factory
        可替换平台执行器，但仍借用有限 ExecutionJournal 并满足 ActionExecutor
        的资格、真实效果与资源寿命契约，不能降级成宿主 exec。
        """
        if not config.host.data_dir.is_absolute():
            raise HostStateError("创建宿主前必须显式解析数据目录。")
        if config.web.enabled:
            for module in ("starlette", "uvicorn"):
                if importlib.util.find_spec(module) is None:
                    raise ConfigError(f"web.enabled: 缺少依赖 {module}，请安装web extra。")
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
        if embedding_provider is not None and config.knowledge.embedding is None:
            raise ConfigError("knowledge.embedding: 借入向量后端仍须显式声明配置。")
        self._embedding_provider, self._reranker = embedding_provider, reranker
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
        self._audio_output: AudioOutputPort | None = None
        self._input_tasks: set[asyncio.Task[Admission]] = set()
        self._vad_tasks: set[asyncio.Task[CallOutcome]] = set()
        self._input_failure: BaseException | None = None
        self._maintenance_stop = asyncio.Event()
        self._maintenance_task: asyncio.Task[None] | None = None
        self._maintenance_failure: BaseException | None = None
        self._diagnostic_cleanup: JsonObject | None = None
        self._execution: ActionExecutor | None = None
        self._activities: ActivityService | None = None
        self._subject: SubjectAssembly | None = None
        self._knowledge_resources: KnowledgeResources | None = None
        self._panel: PanelController | None = None
        self._panel_close_task: asyncio.Task[None] | None = None
        self._subject_close_task: asyncio.Task[None] | None = None
        self._subject_released = False
        self._work_changed = asyncio.Event()
        self._activity_close_task: asyncio.Task[None] | None = None
        self._execution_close_task: asyncio.Task[None] | None = None
        self._knowledge_close_task: asyncio.Task[None] | None = None
        self._plugin_stop_tasks: dict[str, asyncio.Task[None]] = {}
        self._plugin_close_tasks: dict[str, asyncio.Task[None]] = {}
        self._model_close_tasks: dict[str, asyncio.Task[None]] = {}
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
            foreground_model = self.config.models[self.config.subject.foreground]
            native_audio_enabled = (
                isinstance(foreground_model, GenerationModelSettings)
                and foreground_model.supports_audio
            )
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
                    audio=self._store.audio,
                    native_audio_enabled=native_audio_enabled,
                )
                runtime = await blueprint.build(ports)
                self._plugins[instance_id] = runtime
            self._bind_audio_body()
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
            self._knowledge_resources = KnowledgeResources(
                self._store.knowledge,
                self._store,
                self.config.knowledge.embedding,
                page_chars=self.config.context.page_chars,
                can_start=self._check_owned_dispatch,
                embedding=self._embedding_provider,
                reranker=self._reranker,
            )
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
                knowledge=self._knowledge_resources.knowledge,
            )

            # Ordering: 目录、实现和输入端口就绪后才放行外部 start；同步输入
            # 可以在插件启动中接管。关闭已到达时不再启动任何接入。
            if self._phase == "starting":
                self._transition("running")
                await self._knowledge_resources.start()
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

        if self._phase == "running":
            self._maintenance_task = asyncio.create_task(
                self._maintain_diagnostics(), name="engine-diagnostic-retention"
            )
            self._maintenance_task.add_done_callback(self._maintenance_done)
        _logger.info("宿主核心存储已就绪。", extra={"host_instance": self._instance_id})

    async def _maintain_diagnostics(self) -> None:
        """一个固定所有者按批次清诊断，旧正文较多时仍让出事务和关闭机会。"""
        store = self._store
        assert store is not None
        while not self._maintenance_stop.is_set():
            now = time.time()
            try:
                self._diagnostic_cleanup = await store.expire_diagnostics(
                    before=now - self.config.diagnostics.retention_days * 86400,
                    expired_at=now,
                )
            except StorageError as error:
                if error.code != "store_busy":
                    raise
                # 普通维护不挤占控制提交，也不把队列满当成权威故障。
                self._diagnostic_cleanup = {"state": "deferred", "error_type": type(error).__name__}
            delay = 1.0 if self._diagnostic_cleanup.get("has_more") is True else 3600.0
            try:
                async with asyncio.timeout(delay):
                    await self._maintenance_stop.wait()
            except TimeoutError:
                continue

    def _maintenance_done(self, task: asyncio.Task[None]) -> None:
        """只消费并锁存真实维护程序/权威故障；明确诊断失败已在查询值中表达。"""
        if not task.cancelled() and (error := task.exception()) is not None:
            self._maintenance_failure = error

    async def submit(self, submission: InputSubmission) -> Admission:
        """接管原文与可信信号；取消等待不撤销提交后必要的抢占和停音交接。

        宿主在首次等待前持有完整接管所有者，不只持有SQLite事务。否则来源
        等待取消可能留下已接管VAD，却丢失停音通知。返回后仍须查询真实处理。
        """
        store = self._running_store()
        self._check_dispatch()
        subject = self._subject
        observed = 0 if subject is None else subject.coordinator.generation
        if len(self._input_tasks) >= self.config.execution.max_pending_inputs:
            raise StorageError("输入接管所有者已满。", code="input_capacity")
        task = asyncio.create_task(self._admit_input(store, submission, observed))
        self._input_tasks.add(task)
        task.add_done_callback(self._input_done)
        return await asyncio.shield(task)

    async def _admit_input(
        self, store: Store, submission: InputSubmission, observed: int
    ) -> Admission:
        """正式提交成功后只交接一次可信信号，停止观察不会让已接管任务失主。"""
        admission = await store.admit(submission)
        subject = self._subject
        if subject is not None and not admission.duplicate:
            if submission.context.speech_started and self._audio_output is not None:
                identity = "vad-audio-" + uuid4().hex
                context = CallContext(
                    identity,
                    "vad-" + uuid4().hex,
                    asyncio.get_running_loop().time() + self.config.host.close_seconds,
                    admission.input_id,
                )
                # 音频控制在独立通道同步撤候播资格。它不排在普通Provider名额
                # 后，也不等声音实际退出才向主体交接原代次抢占。
                stopped = asyncio.create_task(self._audio_output.interrupt(context))
                self._vad_tasks.add(stopped)
                stopped.add_done_callback(self._vad_done)
            # 抢占绑定接管前观察的旧代次；等待数据库期间新轮次不受迟到信号影响。
            await subject.coordinator.input_admitted(
                admission.input_id,
                observed_generation=observed,
            )
        return admission

    def _input_done(self, task: asyncio.Task[Admission]) -> None:
        """收回接管所有者；明确容量拒收不关闭健康宿主，内部错误仍锁存。"""
        self._input_tasks.discard(task)
        if task.cancelled() or (error := task.exception()) is None:
            return
        if isinstance(error, StorageError) and error.code in {
            "input_capacity",
            "invalid_input",
            "source_conflict",
            "store_busy",
        }:
            return
        self._input_failure = self._input_failure or error

    def _vad_done(self, task: asyncio.Task[CallOutcome]) -> None:
        """停音观察结束不重发；AudioLedger仍保存受理、未知及后续真实确认。"""
        self._vad_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self._input_failure = self._input_failure or error

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
            "input_handoff_failure": None
            if self._input_failure is None
            else type(self._input_failure).__name__,
            "devices": panel.devices(),
            "foreground": None if subject is None else subject.coordinator.session.snapshot(),
            "reflection": None if subject is None else subject.background.session.snapshot(),
            "knowledge": None
            if self._knowledge_resources is None
            else {
                "semantic_enabled": self.config.knowledge.embedding is not None,
                "pending_resources": self._knowledge_resources.pending_resources,
                "index_failure": None
                if self._knowledge_resources.failure is None
                else type(self._knowledge_resources.failure).__name__,
            },
            "pending_limits": {
                "count": self.config.execution.max_pending_inputs,
                "logical_bytes": self.config.execution.pending_input_bytes,
            },
            "diagnostics": self._diagnostic_cleanup,
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
            if self._input_tasks:
                resources.append(f"input_handoffs:{len(self._input_tasks)}")
            if self._vad_tasks:
                resources.append(f"vad_controls:{len(self._vad_tasks)}")
            if self._maintenance_task is not None and not self._maintenance_task.done():
                resources.append("diagnostic_retention")
            if self._execution is not None:
                resources.append(f"execution:{self._execution.pending_resources}")
            resources.extend(f"plugin:{name}" for name in self._plugins)
            if self._activities is not None:
                resources.append(f"activities:{self._activities.pending_resources}")
            if self._subject is not None:
                resources.append(f"subject:{self._subject.coordinator.pending_resources}")
                resources.append(f"background:{self._subject.background.pending_resources}")
            if self._knowledge_resources is not None:
                resources.append(f"knowledge:{self._knowledge_resources.pending_resources}")
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
        try:
            if self._start_task is not None:
                try:
                    await self._start_task
                except BaseException:
                    # 启动任务已经清理它取得的资源。收尾继续，但原启动等待者
                    # 仍收到原异常；此处不重新启动，也不声称启动成功。
                    if self._store is not None or self._owns_lock:
                        raise

            await self._release_resources()
        except BaseException:
            # 故障与资源寿命分开表达。全部资源已归还也不能把原始失败报为
            # 正常 closed；重复 close/wait_closed 继续观察同一失败任务。
            self._transition("failed")
            raise
        self._transition("closed")

    async def _release_resources(self) -> None:
        """按依赖收回借用并汇总原始错误，独立失败不阻断其余资源归还。

        close 报错可能是已排空工作重新报告原始故障，也可能是资源释放未被
        确认。只有成功或组件明确给出的归还证据才清引用；未归还的借用者
        继续保留母 Store 和目录锁。各关闭任务由 Host 登记一次，启动失败
        后的关闭观察也不会盲目重做已经部分执行的插件清理。
        """
        errors: list[BaseException] = []
        self._maintenance_stop.set()
        if self._maintenance_task is not None:
            results = await asyncio.gather(self._maintenance_task, return_exceptions=True)
            errors.extend(result for result in results if isinstance(result, BaseException))
            self._maintenance_task = None
        for name in reversed(tuple(self._plugins)):
            runtime = self._plugins[name]
            if runtime.stop_inputs is not None:
                task = self._plugin_stop_tasks.get(name)
                if task is None:
                    task = asyncio.create_task(self._call_lifecycle(runtime.stop_inputs))
                    self._plugin_stop_tasks[name] = task
                try:
                    await asyncio.shield(task)
                except BaseException as error:
                    errors.append(error)
        if self._input_tasks:
            results = await asyncio.gather(*tuple(self._input_tasks), return_exceptions=True)
            errors.extend(result for result in results if isinstance(result, BaseException))
        for failure in (self._input_failure, self._maintenance_failure):
            if failure is not None and all(error is not failure for error in errors):
                errors.append(failure)

        # Ordering: 活动、主体及面板可能正在等待执行器的真实调用，先建立
        # 全部停止所有者，再观察执行关闭，避免收尾互相等待却尚未发出停止。
        if self._subject is not None and self._subject_close_task is None:
            self._subject_close_task = asyncio.create_task(self._close_subject())
        if self._panel is not None and self._panel_close_task is None:
            self._panel_close_task = asyncio.create_task(self._panel.close())
        if self._activities is not None and self._activity_close_task is None:
            # 活动停止与 Worker/Provider 收尾并行持有，防止驱动等待自己已经
            # 开始的行动时，宿主还没有向执行器发出收尾请求。
            self._activity_close_task = asyncio.create_task(self._activities.close())
        if self._execution is not None:
            if self._execution_close_task is None:
                self._execution_close_task = asyncio.create_task(self._execution.close())
            try:
                await asyncio.shield(self._execution_close_task)
            except BaseException as error:
                errors.append(error)
                # ActionExecutor 的实际资源计数包括收回失败的 Worker 和仍未
                # 结束的 Provider。close 已结束且计数为零时只剩原始业务故障。
                if self._execution.pending_resources == 0:
                    self._execution = None
            else:
                self._execution = None
        if self._vad_tasks:
            results = await asyncio.gather(*tuple(self._vad_tasks), return_exceptions=True)
            errors.extend(result for result in results if isinstance(result, BaseException))
        if self._activity_close_task is not None:
            try:
                await asyncio.shield(self._activity_close_task)
            except BaseException as error:
                errors.append(error)
                if self._activities is not None and self._activities.pending_resources == 0:
                    self._activities = None
            else:
                self._activities = None
        if self._subject_close_task is not None:
            try:
                await asyncio.shield(self._subject_close_task)
            except BaseException as error:
                errors.append(error)
                if self._subject_released:
                    self._subject = None
            else:
                self._subject = None
        if self._panel_close_task is not None:
            try:
                await asyncio.shield(self._panel_close_task)
            except BaseException as error:
                # Panel.close 只拥有已接管观察和提交，异常在全部任务结束后
                # 报告；它不拥有设备，也不会在失败后留下额外后台收尾。
                errors.append(error)
            self._panel = None

        # 主体借用认识服务和 SDK，执行/控制借用插件。上游未确认归还时
        # 只保留其依赖；其他已独立结束的所有者仍可继续释放。
        if self._subject is None and self._knowledge_resources is not None:
            if self._knowledge_close_task is None:
                self._knowledge_close_task = asyncio.create_task(self._knowledge_resources.close())
            try:
                await asyncio.shield(self._knowledge_close_task)
            except BaseException as error:
                errors.append(error)
                if self._knowledge_resources.resources_released:
                    self._knowledge_resources = None
            else:
                self._knowledge_resources = None
        if (
            self._execution is None
            and self._activities is None
            and self._subject is None
            and self._panel is None
        ):
            for name in reversed(tuple(self._plugins)):
                runtime = self._plugins[name]
                if runtime.close is not None:
                    task = self._plugin_close_tasks.get(name)
                    if task is None:
                        task = asyncio.create_task(self._call_lifecycle(runtime.close))
                        self._plugin_close_tasks[name] = task
                    try:
                        await asyncio.shield(task)
                    except BaseException as error:
                        errors.append(error)
                        continue
                del self._plugins[name]
        if not self._plugins:
            self._audio_output = None
        if self._subject is None:
            for name in reversed(tuple(self._models)):
                task = self._model_close_tasks.get(name)
                if task is None:
                    task = asyncio.create_task(self._models[name].close())
                    self._model_close_tasks[name] = task
                try:
                    await asyncio.shield(task)
                except BaseException as error:
                    errors.append(error)
                else:
                    del self._models[name]

        borrowers_remain = (
            bool(self._input_tasks or self._vad_tasks or self._plugins)
            or self._execution is not None
            or self._activities is not None
            or self._subject is not None
            or self._panel is not None
            or self._knowledge_resources is not None
        )
        if not borrowers_remain and self._store is not None:
            try:
                await self._store.close()
            except BaseException as error:
                errors.append(error)
            else:
                self._store = None
        if not borrowers_remain and self._store is None and not self._models:
            try:
                self._lock.release()
            except BaseException as error:
                errors.append(error)
            else:
                self._owns_lock = False

        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("宿主资源收尾失败。", errors)
        if borrowers_remain or self._store is not None or self._models:
            raise HostStateError("资源关闭已结束，但尚未确认全部借用归还。")

    async def _call_lifecycle(self, operation: Callable[[], Awaitable[None]]) -> None:
        """将生命周期调用本身纳入唯一任务，返回 Awaitable 前的故障也保留。

        插件可交回协程或已有 Future；宿主登记后才调用它。同步构造等待对象
        失败仍属于同一关闭进度，不能跳出资源汇总或在后续观察中再次调用。
        """
        await operation()

    async def _close_subject(self) -> None:
        """撤销主体等待并收回摘要，真实执行借用由并行执行收尾继续持有。"""
        subject = self._subject
        assert subject is not None
        workers = tuple(
            await asyncio.gather(
                subject.coordinator.close(), subject.background.close(), return_exceptions=True
            )
        )
        # coordinator/background 会在资源结束后重抛锁存故障，但 session.busy
        # 只表示 Core 主动等待，不能证明 SDK 包装和提交已归还。再观察两条
        # 会话的同一关闭任务取得明证，并独立收回两个摘要所有者。
        borrowers = tuple(
            await asyncio.gather(
                subject.coordinator.session.close(),
                subject.background.session.close(),
                subject.foreground_history.close(),
                subject.reflection_history.close(),
                return_exceptions=True,
            )
        )
        original_failures = (subject.coordinator.failure, subject.background.failure)
        self._subject_released = (
            all(not isinstance(result, BaseException) for result in borrowers)
            and subject.coordinator.pending_resources == 0
            and subject.background.pending_resources == 0
            and all(
                not isinstance(result, BaseException) or result is original_failure
                for result, original_failure in zip(workers, original_failures, strict=True)
            )
        )
        errors = [result for result in (*workers, *borrowers) if isinstance(result, BaseException)]
        if errors:
            raise BaseExceptionGroup("主体与摘要收尾失败。", errors)

    def _bind_audio_body(self) -> None:
        """全部资源构造后一次关联实际播放事实，配置顺序不决定身体是否可用。"""
        outputs = tuple(
            runtime.audio_output
            for runtime in self._plugins.values()
            if runtime.audio_output is not None
        )
        bodies = tuple(
            runtime.body for runtime in self._plugins.values() if runtime.body is not None
        )
        if len(outputs) > 1 or len(bodies) > 1:
            raise ConfigError("同一主体只能明确装配一个现场输出与一个身体。")
        if outputs:
            self._audio_output = outputs[0]
            self._audio_output.bind_body(bodies[0] if bodies else None)

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
        if self._input_failure is not None:
            raise self._input_failure
        if self._maintenance_failure is not None:
            raise self._maintenance_failure
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
