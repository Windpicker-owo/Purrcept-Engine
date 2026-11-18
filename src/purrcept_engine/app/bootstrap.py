"""按启动阶段获取宿主资源并完成依赖装配；关闭与异常回收由 HostLifecycle 统一负责。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from purrcept_core.models import DecisionBackend, ModelBackend

from purrcept_engine.app.activity import activity_capability
from purrcept_engine.app.config import GenerationModelSettings
from purrcept_engine.app.evidence import EvidenceAccess
from purrcept_engine.app.host_state import EngineState
from purrcept_engine.app.lifecycle import HostLifecycle
from purrcept_engine.app.notebook_resources import NotebookAccess, PluginModel
from purrcept_engine.app.panel import PanelController
from purrcept_engine.app.receipts import LateCallResults
from purrcept_engine.app.subject import build_subject
from purrcept_engine.domain.activities.service import ActivityService
from purrcept_engine.domain.evidence.service import EvidenceService
from purrcept_engine.domain.execution.models import ActionRecord
from purrcept_engine.domain.execution.ports import ActionExecutor
from purrcept_engine.errors import ConfigError, HostStateError
from purrcept_engine.infrastructure.models.clients import (
    DecisionResource,
    GenerationResource,
)
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system.captions import SpeechCaption
from purrcept_engine.plugin_system.contracts import PluginPorts
from purrcept_engine.values import JsonObject

if TYPE_CHECKING:
    from purrcept_engine.app.host import EngineHost

_logger = logging.getLogger("purrcept_engine.app.host")


class EngineAssembly:
    """按五阶段依赖顺序构建一个宿主实例的存储、模型、插件、执行器与主体服务。"""

    def __init__(self, state: EngineState, host: EngineHost, lifecycle: HostLifecycle) -> None:
        self.state = state
        self.lifecycle = lifecycle
        self.host = host

    async def start(self) -> None:
        """按阶段完成资源装配，最后放行插件接入与主体循环。"""
        try:
            store = await self._open_store()
            generation, decisions = self._open_models()
            await self._build_plugins(store, generation)
            await self._assemble_execution_and_subject(store, generation, decisions)
            await self._activate_plugins_and_subject()
            self.stage(None)
        except BaseException as startup_error:
            if not isinstance(startup_error, asyncio.CancelledError):
                self.state.startup_failed_stage = self.state.startup_stage
                self.state.startup_health.fail(startup_error)
            self.stage(None)
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

    async def _open_store(self) -> Store:
        """阶段 1：获取数据目录独占锁并打开权威 SQLite 数据库。"""
        self.state.lock.acquire()
        self.state.owns_lock = True
        if self.state.on_data_dir_acquired is not None:
            self.state.on_data_dir_acquired(self.state.config.host.data_dir)
        self.stage("sqlite")
        store = await Store.open(
            self.state.config.host.data_dir / "engine.sqlite3",
            max_inputs=self.state.config.execution.max_pending_inputs,
            max_input_bytes=self.state.config.execution.pending_input_bytes,
            unknown_call_protection_seconds=(
                self.state.config.context.unknown_call_protection_seconds
            ),
        )
        self.state.store = store
        return store

    def _open_models(self) -> tuple[dict[str, ModelBackend], dict[str, DecisionBackend]]:
        """阶段 2：按主体与已启用插件声明创建生成模型和判断模型客户端。"""
        self.stage("models")
        model_names = {
            self.state.config.subject.foreground,
            self.state.config.subject.reflection,
        }
        for _, blueprint in self.state.prepared.blueprints:
            model_names.update(requirement.model for requirement in blueprint.model_requirements)
        if self.state.config.subject.attention is not None:
            model_names.add(self.state.config.subject.attention)

        generation: dict[str, ModelBackend] = {}
        decisions: dict[str, DecisionBackend] = {}
        for name in sorted(model_names):
            settings = self.state.config.models[name]
            if isinstance(settings, GenerationModelSettings):
                backend = self.state.generation_backends.get(name)
                if backend is None:
                    resource = GenerationResource.create(settings)
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
        return generation, decisions

    async def _build_plugins(self, store: Store, generation: dict[str, ModelBackend]) -> None:
        """阶段 3：为每个已启用插件构造隔离的 PluginPorts 并实例化运行时。"""
        capability_paths = {
            binding.declaration.capability_id: binding.path
            for binding in self.state.prepared.catalog.bindings.values()
        }
        foreground_model = self.state.config.models[self.state.config.subject.foreground]
        native_audio_enabled = (
            isinstance(foreground_model, GenerationModelSettings)
            and "audio" in foreground_model.supports_contents
        )
        for instance_id, blueprint in self.state.prepared.blueprints:
            paths = frozenset(
                capability_paths[capability.capability_id] for capability in blueprint.capabilities
            )
            notebook_access = None
            if blueprint.notebook is not None:
                notebook_access = NotebookAccess(
                    store.notebook(instance_id),
                    store.evidence,
                    blueprint.notebook.embedding,
                    embedding=self.state.plugin_embeddings.get(instance_id),
                    reranker=self.state.plugin_rerankers.get(instance_id),
                )
                self.state.notebook_resources[instance_id] = notebook_access
            background_models: dict[str, PluginModel] = {}
            for requirement in blueprint.model_requirements:
                declaration = self.state.config.models[requirement.model]
                if not isinstance(declaration, GenerationModelSettings):
                    raise TypeError("插件后台模型不是生成模型。")
                background_models[requirement.alias] = PluginModel(
                    store,
                    generation[requirement.model],
                    declaration,
                    self.state.config,
                    f"plugin:{instance_id}:{requirement.alias}",
                    self.lifecycle.check_owned_dispatch,
                    requirement.retry,
                )
            ports = PluginPorts(
                self.host,
                self.lifecycle.projection,
                EvidenceAccess(store.evidence, self.state.config.context.page_chars),
                store.time,
                LateCallResults(store, paths),
                audio=store.audio,
                native_audio_enabled=native_audio_enabled,
                native_inputs=self.host,
                captions=self.caption_snapshot,
                notebook=notebook_access,
                background_models=background_models,
                phone=store.phone(instance_id),
                subject_name=self.state.config.subject.character.name,
                stickers=store.stickers,
            )
            self.stage(f"plugin_build:{instance_id}")
            self.state.plugins[instance_id] = await blueprint.build(ports)

    async def _assemble_execution_and_subject(
        self,
        store: Store,
        generation: dict[str, ModelBackend],
        decisions: dict[str, DecisionBackend],
    ) -> None:
        """阶段 4：装配活动服务、行动隔离执行器、主体服务与控制面板控制器。"""
        self.stage("execution")
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
            if execution_for_activity is None:
                raise HostStateError("活动执行端口尚未装配。")
            await execution_for_activity.wait_invocation_released(action_id)

        self.state.activities = ActivityService(
            store,
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
                store,
                self.state.config.execution,
                scratch_root=self.state.config.host.data_dir / "scratch",
            )
        else:
            self.state.execution = self.state.executor_factory(
                self.state.prepared.catalog,
                implementations,
                store,
                self.state.config.execution,
                self.state.config.host.data_dir / "scratch",
            )
        execution_for_activity = self.state.execution

        self.stage("subject_restore")
        self.state.subject = await build_subject(
            self.state.config,
            store,
            self.state.prepared.catalog,
            generation,
            decisions,
            execute=self.host.execute,
            plugins=self.state.plugins,
            can_start=self.lifecycle.check_owned_dispatch,
            wakeup_committed=self.state.work_changed.set,
            evidence=EvidenceService(
                store.evidence, page_chars=self.state.config.context.page_chars
            ),
        )
        self.state.panel = PanelController(
            self.state.instance_id,
            self.state.prepared.catalog,
            self.state.plugins,
            store.controls,
            self.host.invoke,
            self.state.activities,
            self.state.subject.service.proactive,
            store.human_feedback,
        )

    async def _activate_plugins_and_subject(self) -> None:
        """阶段 5：切换到 running，依次放行插件外部连接、插件后台任务与主体主循环。"""
        if self.state.phase == "starting":
            self.lifecycle.transition("running")
        for instance_id, runtime in self.state.plugins.items():
            if self.state.phase == "running" and runtime.start is not None:
                self.stage(f"plugin_start:{instance_id}")
                await runtime.start()
        if self.state.phase == "running" and self.state.subject is not None:
            for runtime in self.state.plugins.values():
                if runtime.start_background is not None:
                    await runtime.start_background()
            self.stage("subject_start")
            await self.state.subject.service.start()

    def stage(self, name: str | None) -> None:
        """记录上一个启动阶段的耗时并切换当前阶段标识。"""
        now = time.perf_counter()
        previous = self.state.startup_stage
        if previous is not None:
            elapsed = (now - self._stage_started) * 1000
            self.state.startup_timings[previous] = elapsed
            _logger.info("启动阶段已结束。", extra={"stage": previous, "duration_ms": elapsed})
        self.state.startup_stage = name
        self._stage_started = now

    def bind_audio_body(self) -> None:
        """将现场语音输出插件与唯一 Live2D 身体实例绑定，驱动实时口型同步。"""
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
        """读取当前正在播放的语音字幕快照。"""
        return tuple(
            caption
            for runtime in self.state.plugins.values()
            if runtime.caption is not None and (caption := runtime.caption()) is not None
        )
