"""宿主阶段、唯一启动/关闭任务以及依赖有序的实际资源归还。

关闭同步撤资格后建立并行停止所有者，借用归还后才释放依赖和目录锁。
超时或取消只结束调用者等待；故障与释放事实分别保存，不盲目重做清理。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from purrcept_engine.app.host_state import EngineState
from purrcept_engine.app.host_types import HostPhase, HostSnapshot
from purrcept_engine.errors import HostStateError, StorageError
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.values import JsonObject

_logger = logging.getLogger("purrcept_engine.app.host")


class HostLifecycle:
    """应用创建的HostLifecycle；职责和借用只属于当前 Engine 实例。"""

    def __init__(self, state: EngineState) -> None:
        """借用唯一资源台账，不取得模型、插件或存储连接。

        Args:
            state: 此宿主唯一的内部资源台账，不跨实例或插件共享。
        """
        self.state = state
        self.start_operation: Callable[[], Awaitable[None]] | None = None

    def bind_start(self, operation: Callable[[], Awaitable[None]]) -> None:
        """由 Host 在构造末尾绑定唯一装配操作，不能运行期替换。

        Args:
            operation: 同一宿主的异步生命周期入口，创建者须负责其真实收尾。

        Raises:
            HostStateError: 启动操作已经装配。
        """
        if self.start_operation is not None:
            raise HostStateError("启动操作已经装配。")
        self.start_operation = operation

    async def start(self) -> HostSnapshot:
        """取得单实例与核心存储。

        Returns:
            当前实例、生命周期阶段和未归还资源的不可变快照；closing 表示收尾仍在继续。

        Raises:
            HostStateError: 该宿主不能启动；宿主启动操作尚未装配。

        Note:
            失败清理自身资源，重复调用等待同次启动。
        """
        if self.state.phase in ("closing", "closed", "failed"):
            raise HostStateError("该宿主不能启动。")
        if self.start_operation is None:
            raise HostStateError("宿主启动操作尚未装配。")
        if self.state.start_task is None:
            self.transition("starting")
            self.state.start_task = asyncio.create_task(
                self.call_lifecycle(self.start_operation), name="engine-start"
            )

        # Lifecycle: 开始取得资源后，取消一个等待者不能丢掉资源所有者。
        await asyncio.shield(self.state.start_task)
        return self.query()

    async def maintain_diagnostics(self) -> None:
        """一个固定所有者按批次清诊断，旧正文较多时仍让出事务和关闭机会。"""
        store = self.state.store
        assert store is not None
        while not self.state.maintenance_stop.is_set():
            now = time.time()
            try:
                self.state.diagnostic_cleanup = await store.expire_diagnostics(
                    before=now - self.state.config.diagnostics.retention_days * 86400,
                    expired_at=now,
                )
                bodies = await store.expire_model_bodies(
                    before=now - self.state.config.diagnostics.retention_days * 86400,
                    now=now,
                    max_bytes=self.state.config.diagnostics.retained_bytes,
                )
                self.state.diagnostic_cleanup = {
                    **self.state.diagnostic_cleanup,
                    "bodies": bodies,
                    "has_more": bool(
                        self.state.diagnostic_cleanup.get("has_more") or bodies.get("has_more")
                    ),
                }
                await store.collect_unused_media()
            except StorageError as error:
                if error.code != "store_busy":
                    raise
                # 普通维护不挤占控制提交，也不把队列满当成权威故障。
                self.state.diagnostic_cleanup = {
                    "state": "deferred",
                    "error_type": type(error).__name__,
                }
            delay = 1.0 if self.state.diagnostic_cleanup.get("has_more") is True else 3600.0
            try:
                async with asyncio.timeout(delay):
                    await self.state.maintenance_stop.wait()
            except TimeoutError:
                continue

    def maintenance_done(self, task: asyncio.Task[None]) -> None:
        """只消费并锁存真实维护程序/权威故障。

        Args:
            task: 当前所有者创建并负责回收结果的任务。

        Note:
            明确诊断失败已在查询值中表达。
        """
        if not task.cancelled() and (error := task.exception()) is not None:
            self.state.maintenance_failure = error
            self.state.maintenance_health.fail(error)

    def query(self) -> HostSnapshot:
        """返回生命周期快照，不触发存储读取、设备或模型调用。

        Returns:
            当前实例、生命周期阶段和未归还资源的不可变快照；closing 表示收尾仍在继续。
        """
        resources: list[str] = []
        if self.state.phase in ("closing", "failed"):
            if self.state.start_task is not None and not self.state.start_task.done():
                resources.append("startup")
            if self.state.store is not None:
                resources.append("storage")
            if self.state.input_tasks:
                resources.append(f"input_handoffs:{len(self.state.input_tasks)}")
            if self.state.vad_tasks:
                resources.append(f"vad_controls:{len(self.state.vad_tasks)}")
            if self.state.maintenance_task is not None and not self.state.maintenance_task.done():
                resources.append("diagnostic_retention")
            if self.state.execution is not None:
                resources.append(f"execution:{self.state.execution.pending_resources}")
            resources.extend(f"plugin:{name}" for name in self.state.plugins)
            if self.state.activities is not None:
                resources.append(f"activities:{self.state.activities.pending_resources}")
            if self.state.subject is not None:
                resources.append(f"subject:{self.state.subject.service.pending_resources}")
                resources.append(f"background:{self.state.subject.background.pending_resources}")
            if self.state.knowledge_resources is not None:
                resources.append(f"knowledge:{self.state.knowledge_resources.pending_resources}")
            resources.extend(f"model:{name}" for name in self.state.models)
            if self.state.owns_lock:
                resources.append("data_directory_lock")
        return HostSnapshot(
            self.state.instance_id, self.state.version, self.state.phase, tuple(resources)
        )

    async def close(self) -> HostSnapshot:
        """关闭新入口并等待同一总期限。

        Returns:
            当前实例、生命周期阶段和未归还资源的不可变快照；closing 表示收尾仍在继续。

        Note:
            超时返回 closing，继续持有收尾资源。
        """
        if self.state.close_task is None:
            loop = asyncio.get_running_loop()
            self.state.close_deadline = loop.time() + self.state.config.host.close_seconds
            self.transition("closing")
            # Ordering: 慢插件停止、启动收尾或数据库等待都不能给旧 Worker
            # 留出下一项能力的窗口。这里只撤资格，已开始调用保留原回执链。
            if self.state.execution is not None:
                self.state.execution.stop_dispatch()
            self.state.close_task = asyncio.create_task(self.finish_close(), name="engine-close")

        close_task = self.state.close_task
        deadline = self.state.close_deadline
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
        """继续等待已发起的关闭。

        Args:
            wait_seconds: 本次观察关闭进度的等待秒数；None 表示等待实际结束。

        Returns:
            当前实例、生命周期阶段和未归还资源的不可变快照；closing 表示收尾仍在继续。

        Raises:
            HostStateError: 尚未发起关闭。

        Note:
            新的观察期限不重置原关闭期限或清理任务。
        """
        if self.state.close_task is None:
            raise HostStateError("尚未发起关闭。")
        async with asyncio.timeout(wait_seconds):
            await asyncio.shield(self.state.close_task)
        return self.query()

    async def finish_close(self) -> None:
        """先收回启动所有权，再排空库。

        Note:
            连接实际释放前不能解除目录锁。
        """
        try:
            if self.state.start_task is not None:
                try:
                    await self.state.start_task
                except BaseException:
                    # 启动任务已经清理它取得的资源。收尾继续，但原启动等待者
                    # 仍收到原异常；此处不重新启动，也不声称启动成功。
                    if self.state.store is not None or self.state.owns_lock:
                        raise

            await self.release_resources()
        except BaseException:
            # 故障与资源寿命分开表达。全部资源已归还也不能把原始失败报为
            # 正常 closed；重复 close/wait_closed 继续观察同一失败任务。
            self.transition("failed")
            raise
        self.transition("closed")

    async def release_resources(self) -> None:
        """按依赖收回借用并汇总原始错误，独立失败不阻断其余资源归还。

        Raises:
            BaseExceptionGroup: 宿主资源收尾失败。
            HostStateError: 资源关闭已结束，但尚未确认全部借用归还。

        Note:
            close 报错可能是已排空工作重新报告原始故障，也可能是资源释放未被
            确认。只有成功或组件明确给出的归还证据才清引用；未归还的借用者
            继续保留母 Store 和目录锁。各关闭任务由 Host 登记一次，启动失败
            后的关闭观察也不会盲目重做已经部分执行的插件清理。
        """
        errors: list[BaseException] = []
        self.state.maintenance_stop.set()
        if self.state.maintenance_task is not None:
            results = await asyncio.gather(self.state.maintenance_task, return_exceptions=True)
            errors.extend(result for result in results if isinstance(result, BaseException))
            self.state.maintenance_task = None
        for name in reversed(tuple(self.state.plugins)):
            runtime = self.state.plugins[name]
            if runtime.stop_inputs is not None:
                task = self.state.plugin_stop_tasks.get(name)
                if task is None:
                    task = asyncio.create_task(self.call_lifecycle(runtime.stop_inputs))
                    self.state.plugin_stop_tasks[name] = task
                try:
                    await asyncio.shield(task)
                except BaseException as error:
                    errors.append(error)
        if self.state.input_tasks:
            results = await asyncio.gather(*tuple(self.state.input_tasks), return_exceptions=True)
            errors.extend(result for result in results if isinstance(result, BaseException))
        for failure in (self.state.input_failure, self.state.maintenance_failure):
            if failure is not None and all(error is not failure for error in errors):
                errors.append(failure)

        # Ordering: 活动、主体及面板可能正在等待执行器的真实调用，先建立
        # 全部停止所有者，再观察执行关闭，避免收尾互相等待却尚未发出停止。
        if self.state.subject is not None and self.state.subject_close_task is None:
            self.state.subject_close_task = asyncio.create_task(self.close_subject())
        if self.state.panel is not None and self.state.panel_close_task is None:
            self.state.panel_close_task = asyncio.create_task(self.state.panel.close())
        if self.state.activities is not None and self.state.activity_close_task is None:
            # 活动停止与 Worker/Provider 收尾并行持有，防止驱动等待自己已经
            # 开始的行动时，宿主还没有向执行器发出收尾请求。
            self.state.activity_close_task = asyncio.create_task(self.state.activities.close())
        if self.state.execution is not None:
            if self.state.execution_close_task is None:
                self.state.execution_close_task = asyncio.create_task(self.state.execution.close())
            try:
                await asyncio.shield(self.state.execution_close_task)
            except BaseException as error:
                errors.append(error)
                # ActionExecutor 的实际资源计数包括收回失败的 Worker 和仍未
                # 结束的 Provider。close 已结束且计数为零时只剩原始业务故障。
                if self.state.execution.pending_resources == 0:
                    self.state.execution = None
            else:
                self.state.execution = None
        if self.state.vad_tasks:
            results = await asyncio.gather(*tuple(self.state.vad_tasks), return_exceptions=True)
            errors.extend(result for result in results if isinstance(result, BaseException))
        if self.state.activity_close_task is not None:
            try:
                await asyncio.shield(self.state.activity_close_task)
            except BaseException as error:
                errors.append(error)
                if (
                    self.state.activities is not None
                    and self.state.activities.pending_resources == 0
                ):
                    self.state.activities = None
            else:
                self.state.activities = None
        if self.state.subject_close_task is not None:
            try:
                await asyncio.shield(self.state.subject_close_task)
            except BaseException as error:
                errors.append(error)
                if self.state.subject_released:
                    self.state.subject = None
            else:
                self.state.subject = None
        if self.state.panel_close_task is not None:
            try:
                await asyncio.shield(self.state.panel_close_task)
            except BaseException as error:
                # Panel.close 只拥有已接管观察和提交，异常在全部任务结束后
                # 报告；它不拥有设备，也不会在失败后留下额外后台收尾。
                errors.append(error)
            self.state.panel = None

        # 主体借用认识服务和 SDK，执行/控制借用插件。上游未确认归还时
        # 只保留其依赖；其他已独立结束的所有者仍可继续释放。
        if self.state.subject is None and self.state.knowledge_resources is not None:
            if self.state.knowledge_close_task is None:
                self.state.knowledge_close_task = asyncio.create_task(
                    self.state.knowledge_resources.close()
                )
            try:
                await asyncio.shield(self.state.knowledge_close_task)
            except BaseException as error:
                errors.append(error)
                if self.state.knowledge_resources.resources_released:
                    self.state.knowledge_resources = None
            else:
                self.state.knowledge_resources = None
        if (
            self.state.execution is None
            and self.state.activities is None
            and self.state.subject is None
            and self.state.panel is None
        ):
            for name in reversed(tuple(self.state.plugins)):
                runtime = self.state.plugins[name]
                if runtime.close is not None:
                    task = self.state.plugin_close_tasks.get(name)
                    if task is None:
                        task = asyncio.create_task(self.call_lifecycle(runtime.close))
                        self.state.plugin_close_tasks[name] = task
                    try:
                        await asyncio.shield(task)
                    except BaseException as error:
                        errors.append(error)
                        if runtime.resources_released is None:
                            continue
                        try:
                            released = runtime.resources_released()
                        except BaseException as release_error:
                            errors.append(release_error)
                            continue
                        if released is not True:
                            continue
                del self.state.plugins[name]
        if not self.state.plugins:
            self.state.audio_output = None
        if self.state.subject is None:
            for name in reversed(tuple(self.state.models)):
                task = self.state.model_close_tasks.get(name)
                if task is None:
                    task = asyncio.create_task(self.state.models[name].close())
                    self.state.model_close_tasks[name] = task
                try:
                    await asyncio.shield(task)
                except BaseException as error:
                    errors.append(error)
                else:
                    del self.state.models[name]

        borrowers_remain = (
            bool(self.state.input_tasks or self.state.vad_tasks or self.state.plugins)
            or self.state.execution is not None
            or self.state.activities is not None
            or self.state.subject is not None
            or self.state.panel is not None
            or self.state.knowledge_resources is not None
        )
        if not borrowers_remain and self.state.store is not None:
            try:
                await self.state.store.close()
            except BaseException as error:
                errors.append(error)
            else:
                self.state.store = None
        if not borrowers_remain and self.state.store is None and not self.state.models:
            try:
                self.state.lock.release()
            except BaseException as error:
                errors.append(error)
            else:
                self.state.owns_lock = False

        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("宿主资源收尾失败。", errors)
        if borrowers_remain or self.state.store is not None or self.state.models:
            raise HostStateError("资源关闭已结束，但尚未确认全部借用归还。")

    async def call_lifecycle(self, operation: Callable[[], Awaitable[None]]) -> None:
        """将生命周期调用本身纳入唯一任务，返回 Awaitable 前的故障也保留。

        Args:
            operation: 同一宿主的异步生命周期入口，创建者须负责其真实收尾。

        Note:
            插件可交回协程或已有 Future；宿主登记后才调用它。同步构造等待对象
            失败仍属于同一关闭进度，不能跳出资源汇总或在后续观察中再次调用。
        """
        await operation()

    async def close_subject(self) -> None:
        """撤销主体等待并收回摘要，真实执行借用由并行执行收尾继续持有。

        Raises:
            BaseExceptionGroup: 主体与摘要收尾失败。
        """
        subject = self.state.subject
        assert subject is not None
        workers = tuple(
            await asyncio.gather(
                subject.service.close(), subject.background.close(), return_exceptions=True
            )
        )
        # coordinator/background 会在资源结束后重抛锁存故障，但 session.busy
        # 只表示 Core 主动等待，不能证明 SDK 包装和提交已归还。再观察两条
        # 会话的同一关闭任务取得明证，并独立收回前台、反思和睡眠摘要所有者。
        borrowers = tuple(
            await asyncio.gather(
                subject.service.session.close(),
                subject.background.session.close(),
                subject.background.behavior_session.close(),
                subject.foreground_history.close(),
                subject.sleep_history.close(),
                return_exceptions=True,
            )
        )
        original_failures = (subject.service.failure, subject.background.failure)
        self.state.subject_released = (
            all(not isinstance(result, BaseException) for result in borrowers)
            and subject.service.pending_resources == 0
            and subject.background.pending_resources == 0
            and all(
                not isinstance(result, BaseException) or result is original_failure
                for result, original_failure in zip(workers, original_failures, strict=True)
            )
        )
        errors = [result for result in (*workers, *borrowers) if isinstance(result, BaseException)]
        if errors:
            raise BaseExceptionGroup("主体与摘要收尾失败。", errors)

    def projection(self) -> JsonObject:
        """插件借用提交后的宿主状态，不取得主脑会话或可变配置。

        Returns:
            业务对象，按实际结果提供 instance_id、version、phase、pending_resources 字段。
        """
        snapshot = self.query()
        return {
            "instance_id": snapshot.instance_id,
            "version": snapshot.version,
            "phase": snapshot.phase,
            "pending_resources": list(snapshot.pending_resources),
        }

    def check_dispatch(self) -> None:
        """新派发在实际开始前核验宿主和权威资格，收尾查询不使用此门槛。

        Raises:
            StorageError: 权威存储当前不能接管新派发。
        """
        store = self.running_store()
        if self.state.input_failure is not None:
            raise self.state.input_failure
        if self.state.maintenance_failure is not None:
            raise self.state.maintenance_failure
        subject = self.state.subject
        if subject is not None and subject.service.failure is not None:
            raise subject.service.failure
        if subject is not None and subject.background.failure is not None:
            raise subject.background.failure
        if not store.authority_available:
            raise StorageError("权威存储当前不能接管新派发。") from store.authority_failure

    def check_ready_dispatch(self) -> None:
        """公开业务等待全部插件就绪；内部启动输入仍可先持久接管。

        Raises:
            HostStateError: 启动阶段尚未结束。
            StorageError: 权威存储已失去派发资格。
        """
        if self.state.startup_stage is not None:
            raise HostStateError("宿主仍在启动，请等待全部资源就绪。")
        self.check_dispatch()

    def check_owned_dispatch(self) -> None:
        """将宿主正常关闭传入工作器取消链，内部故障仍保持原始类型。

        Note:
            phase 先关闭新入口，再启动各实际收尾任务。这个短窗口里的固定工作器
            必须停止等待，不能把正常关闭当程序故障锁存；公开入口仍报 HostStateError。
        """
        if self.state.phase in ("closing", "closed"):
            raise asyncio.CancelledError
        self.check_dispatch()

    def running_store(self) -> Store:
        """新业务只在 running 阶段借用权威库。

        Returns:
            同一宿主借出的权威库，连接关闭仍归原所有者。

        Raises:
            HostStateError: 宿主当前不接管新输入。

        Note:
            收尾查询另走只读入口。
        """
        if self.state.phase != "running" or self.state.store is None:
            raise HostStateError("宿主当前不接管新输入。")
        return self.state.store

    def transition(self, phase: HostPhase) -> None:
        """生命周期版本与实际状态一起前进，查询不会制造新版本。

        Args:
            phase: 宿主实际生命周期阶段，与查询版本同时更新。
        """
        self.state.phase = phase
        self.state.version += 1
