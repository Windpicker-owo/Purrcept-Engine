"""按声明取得资源并连接宿主依赖；不拥有资源的最终关闭。

每次取得立即写入同一 EngineState，部分启动失败交给 HostLifecycle 收尾。
插件接入放行前必须完成目录、执行器和主体装配；关闭到达后不再开放接入。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from purrcept_core.models import DecisionBackend, ModelBackend

from purrcept_engine.app.activity import activity_capability
from purrcept_engine.app.config import GenerationModelSettings
from purrcept_engine.app.evidence import EvidenceAccess
from purrcept_engine.app.host_state import EngineState
from purrcept_engine.app.knowledge_resources import KnowledgeResources
from purrcept_engine.app.lifecycle import HostLifecycle
from purrcept_engine.app.panel import PanelController
from purrcept_engine.app.receipts import LateCallResults
from purrcept_engine.app.subject import build_subject
from purrcept_engine.domain.activities.service import ActivityService
from purrcept_engine.domain.execution.models import ActionRecord
from purrcept_engine.domain.execution.ports import ActionExecutor
from purrcept_engine.errors import ConfigError, HostStateError
from purrcept_engine.infrastructure.models.clients import DecisionResource, OpenAIGenerationResource
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system.captions import SpeechCaption
from purrcept_engine.plugin_system.contracts import PluginPorts
from purrcept_engine.values import JsonObject

if TYPE_CHECKING:
    from purrcept_engine.app.host import EngineHost

_logger = logging.getLogger("purrcept_engine.app.host")


class EngineAssembly:
    """按依赖构建一个宿主的资源与领域服务。

    Note:
        EngineHost 创建此协作者。取得资源后立即记入同一 EngineState，部分启动
        失败的收尾仍归 HostLifecycle；外部接入最后放行，不跨宿主共享资源。
    """

    def __init__(self, state: EngineState, host: EngineHost, lifecycle: HostLifecycle) -> None:
        """绑定同一宿主的装配目标、资源台账和生命周期所有者。

        Args:
            state: 此宿主唯一的内部资源台账，不跨实例或插件共享。
            host: 当前应用实例的宿主入口。
            lifecycle: 同一宿主的阶段和资源归还所有者。

        Note:
            固定应用实例借用；资源创建、观察和归还不跨宿主共享。
        """
        self.state = state
        self.lifecycle = lifecycle
        self.host = host

    async def start(self) -> None:
        """按依赖取得资源并完成装配，最后开放插件接入。

        Raises:
            BaseExceptionGroup: 宿主启动及收尾失败。

        Note:
            先锁目录再开库；启动期间若收到关闭，不重新开放业务入口。
        """
        try:
            self.state.lock.acquire()
            self.state.owns_lock = True
            self.state.store = await Store.open(
                self.state.config.host.data_dir / "engine.sqlite3",
                max_inputs=self.state.config.execution.max_pending_inputs,
                max_input_bytes=self.state.config.execution.pending_input_bytes,
            )
            model_names = {
                self.state.config.subject.foreground,
                self.state.config.subject.reflection,
                self.state.config.subject.summary,
            }
            if self.state.config.subject.attention is not None:
                model_names.add(self.state.config.subject.attention)
            generation: dict[str, ModelBackend] = {}
            decisions: dict[str, DecisionBackend] = {}
            for name in sorted(model_names):
                settings = self.state.config.models[name]
                if isinstance(settings, GenerationModelSettings):
                    backend = self.state.generation_backends.get(name)
                    if backend is None:
                        resource = OpenAIGenerationResource.create(settings)
                        # 资源取得后立即归属宿主；显式借用的嵌入后端归调用者关闭。
                        self.state.models[name] = resource
                        backend = resource.backend
                    generation[name] = backend
                else:
                    decision = self.state.decision_backends.get(name)
                    if decision is None:
                        resource = DecisionResource.create(settings)
                        self.state.models[name] = resource
                        decision = resource.backend
                    decisions[name] = decision
            capability_paths = {
                binding.declaration.capability_id: binding.path
                for binding in self.state.prepared.catalog.bindings.values()
            }
            foreground_model = self.state.config.models[self.state.config.subject.foreground]
            # 原声准入只取决于前台声明的传输能力；容量由统一估算器在
            # 每次请求派发前检查，不再依赖模型或媒体专用规则注册。
            native_audio_enabled = (
                isinstance(foreground_model, GenerationModelSettings)
                and "audio" in foreground_model.supports_contents
            )
            for instance_id, blueprint in self.state.prepared.blueprints:
                paths = frozenset(
                    capability_paths[capability.capability_id]
                    for capability in blueprint.capabilities
                )
                ports = PluginPorts(
                    self.host,
                    self.lifecycle.projection,
                    EvidenceAccess(
                        self.state.store.knowledge, self.state.config.context.page_chars
                    ),
                    self.state.store.time,
                    LateCallResults(self.state.store, paths),
                    audio=self.state.store.audio,
                    native_audio_enabled=native_audio_enabled,
                    native_inputs=self.host,
                    captions=self.caption_snapshot,
                )
                runtime = await blueprint.build(ports)
                self.state.plugins[instance_id] = runtime
            self.bind_audio_body()
            definitions = tuple(
                definition
                for _, blueprint in self.state.prepared.blueprints
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
                return await self.host.invoke(
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

            self.state.activities = ActivityService(
                self.state.store,
                definitions,
                max_active=self.state.config.activities.max_active,
                max_steps=self.state.config.execution.max_workers
                * self.state.config.execution.max_calls,
                initial_seconds=self.state.config.activities.initial_seconds,
                max_remaining_seconds=self.state.config.activities.max_remaining_seconds,
                checkpoint_seconds=self.state.config.activities.checkpoint_seconds,
                can_start=self.lifecycle.check_owned_dispatch,
                invoke=invoke_activity,
                wait_invocation_released=wait_activity_release,
            )
            await self.state.activities.start()
            implementations = (
                activity_capability(self.state.activities),
                *tuple(
                    capability
                    for runtime in self.state.plugins.values()
                    for capability in runtime.capabilities
                ),
            )
            if self.state.executor_factory is None:
                from purrcept_engine.infrastructure.execution.service import ExecutionService

                self.state.execution = ExecutionService(
                    self.state.prepared.catalog,
                    implementations,
                    self.state.store,
                    self.state.config.execution,
                    scratch_root=self.state.config.host.data_dir / "scratch",
                )
            else:
                self.state.execution = self.state.executor_factory(
                    self.state.prepared.catalog,
                    implementations,
                    self.state.store,
                    self.state.config.execution,
                    self.state.config.host.data_dir / "scratch",
                )
            execution_for_activity = self.state.execution
            self.state.knowledge_resources = KnowledgeResources(
                self.state.store.knowledge,
                self.state.store,
                self.state.config.knowledge.embedding,
                page_chars=self.state.config.context.page_chars,
                can_start=self.lifecycle.check_owned_dispatch,
                embedding=self.state.embedding_provider,
                reranker=self.state.reranker,
            )
            self.state.subject = await build_subject(
                self.state.config,
                self.state.store,
                self.state.prepared.catalog,
                generation,
                decisions,
                execute=self.host.execute,
                plugins=self.state.plugins,
                can_start=self.lifecycle.check_owned_dispatch,
                wakeup_committed=self.state.work_changed.set,
                knowledge=self.state.knowledge_resources.knowledge,
            )
            self.state.panel = PanelController(
                self.state.instance_id,
                self.state.prepared.catalog,
                self.state.plugins,
                self.state.store.controls,
                self.host.invoke,
                self.state.activities,
                self.state.subject.service.proactive,
                self.state.store.human_feedback,
            )

            # Ordering: 目录、实现和输入端口就绪后才放行外部 start；同步输入
            # 可以在插件启动中接管。关闭已到达时不再启动任何接入。
            if self.state.phase == "starting":
                self.lifecycle.transition("running")
                await self.state.knowledge_resources.start()
                await self.state.subject.service.start()
            for runtime in self.state.plugins.values():
                if self.state.phase == "running" and runtime.start is not None:
                    await runtime.start()
        except BaseException as startup_error:
            # 部分启动失败沿同一资源逆序清理。清理失败保留所有权，并与原始
            # 启动错误一起报告，不能解锁后另起宿主覆盖未释放的资源。
            if self.state.phase != "closing":
                self.lifecycle.transition("failed")
            try:
                await self.lifecycle.release_resources()
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(
                    "宿主启动及收尾失败。", [startup_error, cleanup_error]
                ) from None
            raise

        if self.state.phase == "running":
            self.state.maintenance_task = asyncio.create_task(
                self.lifecycle.maintain_diagnostics(), name="engine-diagnostic-retention"
            )
            self.state.maintenance_task.add_done_callback(self.lifecycle.maintenance_done)
        _logger.info("宿主核心存储已就绪。", extra={"host_instance": self.state.instance_id})

    def bind_audio_body(self) -> None:
        """全部资源构造后一次关联实际播放事实，配置顺序不决定身体是否可用。

        Raises:
            ConfigError: 同一主体只能明确装配一个现场输出与一个身体。
        """
        outputs = tuple(
            runtime.audio_output
            for runtime in self.state.plugins.values()
            if runtime.audio_output is not None
        )
        bodies = tuple(
            runtime.body for runtime in self.state.plugins.values() if runtime.body is not None
        )
        if len(outputs) > 1 or len(bodies) > 1:
            raise ConfigError("同一主体只能明确装配一个现场输出与一个身体。")
        if outputs:
            self.state.audio_output = outputs[0]
            self.state.audio_output.bind_body(bodies[0] if bodies else None)

    def caption_snapshot(self) -> tuple[SpeechCaption, ...]:
        """读取声音所有者明确发布的当前台词，不借出其他插件资源。

        Returns:
            当前实际播放的不可变台词集合；没有播放时为空。

        Note:
            与插件同一宿主事件循环调用。显示轮询不执行能力或访问模型历史。
        """
        return tuple(
            caption
            for runtime in self.state.plugins.values()
            if runtime.caption is not None and (caption := runtime.caption()) is not None
        )
