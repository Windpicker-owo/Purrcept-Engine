"""宿主的资源所有权和嵌入生命周期。

EngineHost 串起已校验配置、目录锁、权威存储与后续主体装配。关闭只有一个
收尾任务：调用者超时或取消只结束自己的等待，仍在使用的库和目录锁继续归
宿主所有。这里不拥有模型工具循环，也不把输入接管等同于主体已处理。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from purrcept_engine.errors import HostStateError
from purrcept_engine.execution.catalog import Catalog
from purrcept_engine.execution.ports import ActionExecutor, ExecutionJournal
from purrcept_engine.execution.protocol import JsonObject
from purrcept_engine.execution.providers import CapabilityImplementation
from purrcept_engine.plugins.base import PluginFactory, PluginPorts, PluginRuntime
from purrcept_engine.storage import ActionRecord, Admission, InputRecord, InputSubmission, Store

from .assembly import prepare_plugins
from .config import EngineConfig, ExecutionSettings, GenerationModelSettings
from .locking import InstanceLock
from .models import OpenAIGenerationResource, TypeSafeDecisionResource

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
        self._instance_id = str(uuid4())
        self._phase: HostPhase = "new"
        self._version = 0
        self._lock = InstanceLock(config.host.data_dir)
        self._store: Store | None = None
        self._models: dict[str, OpenAIGenerationResource | TypeSafeDecisionResource] = {}
        self._plugins: dict[str, PluginRuntime] = {}
        self._execution: ActionExecutor | None = None
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
            for name in sorted(model_names):
                settings = self.config.models[name]
                resource = (
                    OpenAIGenerationResource.create(settings)
                    if isinstance(settings, GenerationModelSettings)
                    else TypeSafeDecisionResource.create(settings)
                )
                # 资源取得后立即归属宿主，不等后续主体装配成功才登记。
                self._models[name] = resource
            ports = PluginPorts(self, self._projection)
            for instance_id, blueprint in self._prepared.blueprints:
                runtime = await blueprint.build(ports)
                self._plugins[instance_id] = runtime
            implementations = tuple(
                capability
                for runtime in self._plugins.values()
                for capability in runtime.capabilities
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

            # Ordering: 目录、实现和输入端口就绪后才放行外部 start；同步输入
            # 可以在插件启动中接管。关闭已到达时不再启动任何接入。
            if self._phase == "starting":
                self._transition("running")
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
        return await store.admit(submission)

    async def query_input(self, input_id: str) -> InputRecord | None:
        """查询已接管输入；关闭收尾期间仍可使用尚未释放的存储。"""
        store = self._store
        if store is None or self._phase == "closed":
            raise HostStateError("宿主没有可查询的权威存储。")
        return await store.get_input(input_id)

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
        self._running_store()
        execution = self._execution
        if execution is None:
            raise HostStateError("执行服务尚未装配。")
        return await execution.execute(source, input_id=input_id, can_start=can_start)

    async def query_action(self, action_id: str) -> ActionRecord | None:
        """读取包括迟到调用结论的权威事实，关闭收尾期间仍可查询。"""
        store = self._store
        if store is None:
            raise HostStateError("宿主没有可查询的权威存储。")
        return await store.get_action(action_id)

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
        if self._execution is not None:
            await self._execution.close()
            self._execution = None
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

    def _projection(self) -> JsonObject:
        """插件借用提交后的宿主状态，不取得主脑会话或可变配置。"""
        snapshot = self.query()
        return {
            "instance_id": snapshot.instance_id,
            "version": snapshot.version,
            "phase": snapshot.phase,
            "pending_resources": list(snapshot.pending_resources),
        }

    def _running_store(self) -> Store:
        if self._phase != "running" or self._store is None:
            raise HostStateError("宿主当前不接管新输入。")
        return self._store

    def _transition(self, phase: HostPhase) -> None:
        """生命周期版本与实际状态一起前进，查询不会制造新版本。"""
        self._phase = phase
        self._version += 1
