"""应用内部的单实例资源台账，不向插件或 Worker 暴露。

装配写入已取得资源，生命周期负责资格与释放，输入交接维护自己的任务；
所有访问在同一宿主事件循环。这里保存部分启动的所有权，不负责启动或关闭，
也不是按名字查找服务的注册表。借入后端与宿主自建客户端分别登记。
"""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from purrcept_core.models import DecisionBackend, ModelBackend

from purrcept_engine.app.assembly import prepare_plugins
from purrcept_engine.app.config import EngineConfig, GenerationModelSettings
from purrcept_engine.app.host_types import ExecutorFactory, HostPhase
from purrcept_engine.app.knowledge_resources import KnowledgeResources
from purrcept_engine.app.panel import PanelController
from purrcept_engine.app.subject import SubjectAssembly
from purrcept_engine.domain.activities.service import ActivityService
from purrcept_engine.domain.execution.ports import ActionExecutor
from purrcept_engine.domain.execution.providers import (
    CallOutcome,
)
from purrcept_engine.domain.health import ComponentHealth
from purrcept_engine.domain.inputs.models import (
    Admission,
)
from purrcept_engine.domain.knowledge.ports import Reranker
from purrcept_engine.domain.knowledge.semantic import EmbeddingProvider
from purrcept_engine.errors import ConfigError, HostStateError
from purrcept_engine.infrastructure.locking import InstanceLock
from purrcept_engine.infrastructure.models.clients import DecisionResource, OpenAIGenerationResource
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system.audio_port import AudioOutputPort
from purrcept_engine.plugin_system.contracts import PluginFactory, PluginRuntime
from purrcept_engine.values import JsonObject


class EngineState:
    """Host 创建的唯一可变台账，生命周期不得在借用归还前清除资源引用。"""

    def __init__(
        self,
        config: EngineConfig,
        *,
        plugin_factories: Mapping[str, PluginFactory] | None = None,
        executor_factory: ExecutorFactory | None = None,
        console_output: Callable[[str], None] = print,
        generation_backends: Mapping[str, ModelBackend] | None = None,
        decision_backends: Mapping[str, DecisionBackend] | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        reranker: Reranker | None = None,
        on_data_dir_acquired: Callable[[Path], None] | None = None,
    ) -> None:
        """保存配置并核验纯声明。

        Args:
            config: 已校验的宿主配置声明，不在构造时连接外部服务。
            plugin_factories: 显式插件工厂表；None 使用内置登记，不扫描外部包。
            executor_factory: 可替换隔离执行器的工厂；None 使用默认执行器。
            console_output: 显式控制台能力使用的文字输出回调。
            generation_backends: 按配置名注入的生成后端，由嵌入调用者负责最终关闭。
            decision_backends: 按配置名注入的判断后端，由嵌入调用者负责最终关闭。
            embedding_provider: 嵌入调用者注入的向量 Provider；None 按显式配置装配。
            reranker: 可选重排器，只能对给定候选排序。 None 表示不重排。
            on_data_dir_acquired: 取得目录锁后同步调用的应用诊断入口；None 不调用。

        Raises:
            HostStateError: 创建宿主前必须显式解析数据目录。
            ConfigError: 配置字段、资源声明或目录绑定不满足本入口的装配要求。

        Note:
            具体资源与可替换执行器仅在 Host 的 start 装配。

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
        self.config = config
        self.prepared = prepare_plugins(
            config.plugins,
            base_dir=config.plugin_base_dir,
            factories={} if plugin_factories is None else plugin_factories,
            console_output=console_output if config.console else None,
        )
        self.executor_factory = executor_factory
        self.generation_backends = MappingProxyType(dict(generation_backends or {}))
        self.decision_backends = MappingProxyType(dict(decision_backends or {}))
        if embedding_provider is not None and config.knowledge.embedding is None:
            raise ConfigError("knowledge.embedding: 借入向量后端仍须显式声明配置。")
        self.embedding_provider, self.reranker = embedding_provider, reranker
        for name in self.generation_backends:
            if not isinstance(config.models.get(name), GenerationModelSettings):
                raise ConfigError("生成后端必须绑定已声明的生成配置名。")
        for name in self.decision_backends:
            settings = config.models.get(name)
            if settings is None or isinstance(settings, GenerationModelSettings):
                raise ConfigError("判断后端必须绑定已声明的判断配置名。")
        self.on_data_dir_acquired = on_data_dir_acquired
        self.startup_stage: str | None = None
        self.startup_failed_stage: str | None = None
        self.startup_timings: dict[str, float] = {}
        self.startup_health = ComponentHealth("startup")
        self.input_health = ComponentHealth("input_handoff")
        self.maintenance_health = ComponentHealth("maintenance")
        self.instance_id = str(uuid4())
        self.phase: HostPhase = "new"
        self.version = 0
        self.lock = InstanceLock(config.host.data_dir)
        self.store: Store | None = None
        self.models: dict[str, OpenAIGenerationResource | DecisionResource] = {}
        self.plugins: dict[str, PluginRuntime] = {}
        self.audio_output: AudioOutputPort | None = None
        self.input_tasks: set[asyncio.Task[Admission]] = set()
        self.vad_tasks: set[asyncio.Task[CallOutcome]] = set()
        self.input_failure: BaseException | None = None
        self.maintenance_stop = asyncio.Event()
        self.maintenance_task: asyncio.Task[None] | None = None
        self.maintenance_failure: BaseException | None = None
        self.diagnostic_cleanup: JsonObject | None = None
        self.execution: ActionExecutor | None = None
        self.activities: ActivityService | None = None
        self.subject: SubjectAssembly | None = None
        self.knowledge_resources: KnowledgeResources | None = None
        self.panel: PanelController | None = None
        self.panel_close_task: asyncio.Task[None] | None = None
        self.subject_close_task: asyncio.Task[None] | None = None
        self.subject_released = False
        self.work_changed = asyncio.Event()
        self.activity_close_task: asyncio.Task[None] | None = None
        self.execution_close_task: asyncio.Task[None] | None = None
        self.knowledge_close_task: asyncio.Task[None] | None = None
        self.plugin_stop_tasks: dict[str, asyncio.Task[None]] = {}
        self.plugin_close_tasks: dict[str, asyncio.Task[None]] = {}
        self.model_close_tasks: dict[str, asyncio.Task[None]] = {}
        self.start_task: asyncio.Task[None] | None = None
        self.close_task: asyncio.Task[None] | None = None
        self.close_deadline: float | None = None
        self.owns_lock = False
