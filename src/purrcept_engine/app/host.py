"""宿主的资源所有权和嵌入生命周期。

EngineHost 串起已校验配置、目录锁、权威存储与后续主体装配。关闭只有一个
收尾任务：调用者超时或取消只结束自己的等待，仍在使用的库和目录锁继续归
宿主所有。这里不拥有模型工具循环，也不把输入接管等同于主体已处理。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from purrcept_engine.errors import HostStateError
from purrcept_engine.storage import Admission, InputRecord, InputSubmission, Store

from .config import EngineConfig
from .locking import InstanceLock

_logger = logging.getLogger(__name__)
type HostPhase = Literal["new", "starting", "running", "closing", "closed", "failed"]


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

    def __init__(self, config: EngineConfig) -> None:
        """保存不可变、已解析的配置；构造不打开库、设备或模型。"""
        if not config.host.data_dir.is_absolute():
            raise HostStateError("创建宿主前必须显式解析数据目录。")
        self._config = config
        self._instance_id = str(uuid4())
        self._phase: HostPhase = "new"
        self._version = 0
        self._lock = InstanceLock(config.host.data_dir)
        self._store: Store | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._close_deadline: float | None = None
        self._owns_lock = False

    @property
    def config(self) -> EngineConfig:
        """返回本实例启动快照；运行期间不能替换它来改变资源和关闭边界。"""
        return self._config

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
        except BaseException:
            # 获取库失败时 Store.open 负责其线程/连接，宿主只释放自己的锁。
            # 终止信号仍传播，不能被翻译成外围离线或正常就绪。
            self._lock.release()
            self._owns_lock = False
            if self._phase != "closing":
                self._transition("failed")
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

    def query(self) -> HostSnapshot:
        """返回生命周期快照，不触发存储读取、设备或模型调用。"""
        resources: list[str] = []
        if self._phase == "closing":
            if self._start_task is not None and not self._start_task.done():
                resources.append("startup")
            if self._store is not None:
                resources.append("storage")
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

        if self._store is not None:
            await self._store.close()
            self._store = None
        self._lock.release()
        self._owns_lock = False
        self._transition("closed")

    def _running_store(self) -> Store:
        if self._phase != "running" or self._store is None:
            raise HostStateError("宿主当前不接管新输入。")
        return self._store

    def _transition(self, phase: HostPhase) -> None:
        """生命周期版本与实际状态一起前进，查询不会制造新版本。"""
        self._phase = phase
        self._version += 1
