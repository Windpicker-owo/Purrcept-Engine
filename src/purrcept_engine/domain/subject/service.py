"""主体的统一输入编排入口，连接注意、前台、延期与独立后台。

同一实例持有唯一前台会话、共享资格和失败事实；AttentionService、
ForegroundService 与 DeferredInputService 各自推进一条固定流程。应用在启动前
绑定 BackgroundService，反馈与反思仍拥有独立任务和会话，Host 分别等待它们
实际关闭。输入选择以事务中的版本为准，迟到判断不能覆盖已拉取输入。
本模块不拥有客户端、插件或数据库连接，也不复制 Core 的模型工具循环。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from purrcept_core.models import (
    SystemInstruction,
)

from purrcept_engine.domain.execution.catalog import Catalog
from purrcept_engine.domain.inputs.models import InputRecord
from purrcept_engine.domain.knowledge.ports import KnowledgeStore
from purrcept_engine.domain.knowledge.service import KnowledgeService
from purrcept_engine.domain.model_requests.service import (
    ModelDecisionBackend,
    ModelGenerationBackend,
)
from purrcept_engine.domain.subject.attention import AttentionModel, NativeMedia
from purrcept_engine.domain.subject.attention_service import AttentionService
from purrcept_engine.domain.subject.deferred_service import DeferredInputService
from purrcept_engine.domain.subject.foreground_service import ForegroundService
from purrcept_engine.domain.subject.models import (
    InputProcessing,
)
from purrcept_engine.domain.subject.ports import ActionCallback, CoordinatorStore
from purrcept_engine.domain.subject.session import (
    ContinuousSession,
    PrepareHistory,
    RefreshState,
)
from purrcept_engine.domain.subject.settings import CoordinatorSettings, SubjectModel
from purrcept_engine.errors import HostStateError, StorageError
from purrcept_engine.values import JsonObject

if TYPE_CHECKING:
    from purrcept_engine.domain.subject.background import BackgroundService


class SubjectService:
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
        """借用显式资源和纯声明，构造不联网、不推进输入或开启任务。

        Args:
            store: 应用借出的领域存储端口，实际连接和关闭归资源所有者。
            knowledge: 原文展开与认识召回服务，修订仍经过反思批次契约。
            knowledge_store: 同库认识端口，模型或插件不取得通用 SQL 权限。
            catalog: 全部已启用插件共同生成的冻结目录。
            subject: 前台使用的模型、容量与预算声明。
            attention: 可选的注意判断模型声明；None 使用明确的无模型处置。
            settings: 当前组件已校验的配置或纯声明。
            instructions: 稳定 system 指令序列，实时状态不得混入缓存前缀。
            execute: 当前主体执行受限行动的有限入口。
            media: 已接管媒体的引用集合，展开时必须取得原始 BLOB。
            refresh: 每次逻辑请求前刷新已提交动态尾部的异步入口。
            attention_context: 每次注意判断前取得真实动态状态的异步函数。
            wakeup_committed: 计划提交后唤醒调度器的回调，不直接执行计划。
            can_start: 实际派发前重新检查的资格回调，等待后仍须复核。
            prepare_history: 会话空闲时维护已提交历史的入口；None 不维护。
        """
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
        self.session = ContinuousSession(
            store,
            "foreground",
            can_start=self.check,
            compiler=subject.compiler,
        )
        self.attention_owner = uuid4().hex
        self.tasks: set[asyncio.Task[None]] = set()
        self.commits: set[asyncio.Future[object]] = set()
        self.attention_backends: set[ModelDecisionBackend | ModelGenerationBackend] = set()
        self.started = False
        self.closing = False
        self.failure_error: BaseException | None = None
        self.close_task: asyncio.Task[None] | None = None

        self.background: BackgroundService | None = None
        self.attention_service = AttentionService(self)
        self.foreground_service = ForegroundService(self)
        self.deferred_service = DeferredInputService(self)

    def bind_background(self, background: BackgroundService) -> None:
        """装配时绑定独立反馈/反思所有者。

        Args:
            background: 同一主体的独立反馈和反思所有者。

        Raises:
            HostStateError: 主体后台只能在启动前绑定一次。

        Note:
            Host 仍分别等待两者实际关闭。
        """
        if self.started or self.background is not None:
            raise HostStateError("主体后台只能在启动前绑定一次。")
        self.background = background

    @property
    def generation(self) -> int:
        """提供接管前观察的生成身份，紧急通知只能抢占相同旧代次。"""
        return self.session.generation

    @property
    def failure(self) -> BaseException | None:
        """返回停止派发的原始故障，不将内部错误转为正常离线。"""
        return self.failure_error

    @property
    def pending_resources(self) -> int:
        """报告工作器、提交、未结束 Core 轮次及实际注意后端借用。"""
        return (
            len(self.tasks)
            + len(self.commits)
            + int(self.session.busy)
            + sum(backend.pending_attempts for backend in self.attention_backends)
        )

    async def start(self) -> None:
        """启动主体的注意、前台与延期固定工作器。

        Raises:
            HostStateError: 主体工作器已经启动。

        Note:
            启动固定工作器；重新 start 不重放任务或建立平行会话。
        """
        if self.started:
            raise HostStateError("主体工作器已经启动。")
        self.check()
        self.started = True
        for worker in (self.attention_service, self.foreground_service, self.deferred_service):
            task = worker.start()
            self.tasks.add(task)
            task.add_done_callback(self.worker_done)

        if self.background is not None:
            await self.background.start()

    async def input_admitted(self, input_id: str, *, observed_generation: int) -> bool:
        """在真实接管后核对可信抢占，正文关键词或缺失通知不产生控制。

        Args:
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。
            observed_generation: 接管或判断开始时观察到的前台生成代次。

        Raises:
            StorageError: 可信接管通知引用不存在的输入。

        Note:
            app 在接管前读取 generation，再在成功提交后调用。输入若已经进入
            前台处理或代次已经变化，不抢占新生成；重启后不猜测旧观察身份。
            QQ 直接 @ 已取得最低 WAKE，仍有效的旧代次可登记一次上下文升级，
            由相同注意工作器有限判断。音频停播由音频所有者另行受理；返回值
            只表示本通知是否已经直接抢占，不预报后续模型判断的结果。
        """
        self.check()
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
            await self.commit(
                self.store.register_minimum_wake_assessment(
                    processing,
                    owner_id=self.attention_owner,
                    observed_generation=observed_generation,
                )
            )
        return False

    def check(self) -> None:
        """在新工作和实际模型边界同步核验本所有者及宿主的派发资格。

        Raises:
            HostStateError: 主体已开始关闭。
        """
        if self.failure_error is not None:
            raise self.failure_error
        if self.closing:
            raise HostStateError("主体已开始关闭。")
        self.can_start()

    def worker_done(self, task: asyncio.Task[None]) -> None:
        """固定工作器的原始故障锁存，不能留下其他工作器继续派发。

        Args:
            task: 当前所有者创建并负责回收结果的任务。
        """
        self.tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self.failure_error = self.failure_error or error
            self.session.interrupt(self.session.generation)
            for other in self.tasks:
                if not other.cancelling():
                    other.cancel()

    async def wait(self, revision: int, *, seconds: float | None = None) -> None:
        """先读序号再查询后等待，保留发生在查询期间的实际写入通知。

        Args:
            revision: 已观察的状态修订号。
            seconds: 本次等待的秒数；None 表示等待显式唤醒。
        """
        if seconds is None:
            await self.store.wait_for_change(revision)
        elif seconds > 0:
            try:
                async with asyncio.timeout(seconds):
                    await self.store.wait_for_change(revision)
            except TimeoutError:
                pass

    async def processing(self, records: tuple[InputRecord, ...]) -> tuple[InputProcessing, ...]:
        """读取本批版本供处置 CAS，查询本身不授予输入的处理资格。

        Args:
            records: 按接管顺序取得的完整输入记录。

        Returns:
            条目集合：输入实际处理阶段、版本和关联轮次。

        Raises:
            StorageError: 原文缺少处理身份。
        """
        result: list[InputProcessing] = []
        for record in records:
            item = await self.store.input_processing(record.input_id)
            if item is None:
                raise StorageError("原文缺少处理身份。", code="invalid_record")
            result.append(item)
        return tuple(result)

    async def wait_foreground(self, revision: int) -> None:
        """等会话释放或可信控制通知。

        Args:
            revision: 已观察的状态修订号。

        Note:
            观察者取消不能传播到主脑任务。

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

    async def commit[Result](self, operation: Awaitable[Result]) -> Result:
        """已受理调度写入由本所有者保留至真实提交，第二次取消也不遗失责任。

        Args:
            operation: 当前所有者负责推进并回收结果的操作。

        Returns:
            同步操作完成后的真实返回值，异常保持原类型。
        """
        task = asyncio.ensure_future(operation)
        owned = cast(asyncio.Future[object], task)
        self.commits.add(owned)
        task.add_done_callback(self.commit_done)
        return await asyncio.shield(task)

    def commit_done[Result](self, task: asyncio.Future[Result]) -> None:
        """取回已取消观察留下的提交错误，后续派发继续受同一失败锁存约束。

        Args:
            task: 当前所有者创建并负责回收结果的任务。
        """
        self.commits.discard(cast(asyncio.Future[object], task))
        if not task.cancelled() and (error := task.exception()) is not None:
            self.failure_error = self.failure_error or error

    async def close(self) -> None:
        """只建立一次实际收尾，取消等待不能使模型借用或写入失去所有者。"""
        if self.close_task is None:
            self.closing = True
            tasks = tuple(self.tasks)
            # Ordering: 撤销派发资格与取消工作器必须在同一同步段完成。已经
            # ready 的 Store 等待可能先于新收尾任务恢复，不能因此把正常停止
            # 锁存为资格故障；已有取消也不能再次打断工作器的资源清理。
            for task in tasks:
                if not task.cancelling():
                    task.cancel()
            self.close_task = asyncio.create_task(self.finish_close(tasks), name="subject-close")
        await asyncio.shield(self.close_task)

    async def finish_close(self, tasks: tuple[asyncio.Task[None], ...]) -> None:
        """等待已经撤销的工作器和 Core，再收回权威提交及实际注意借用。

        Args:
            tasks: 当前所有者创建并需统一汇总的固定工作任务。

        Raises:
            BaseExceptionGroup: 主体资源收尾失败。
        """
        errors: list[BaseException] = []
        try:
            await self.session.close()
        except BaseException as error:
            # 收尾是所有权边界；一个资源的失败不能跳过其他已接管提交或借用。
            errors.append(error)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for backend in tuple(self.attention_backends):
            try:
                await backend.close()
            except BaseException as error:
                errors.append(error)
            else:
                self.attention_backends.discard(backend)
        while self.commits:
            await asyncio.gather(*tuple(self.commits), return_exceptions=True)
        if self.failure_error is not None and all(
            error is not self.failure_error for error in errors
        ):
            errors.append(self.failure_error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("主体资源收尾失败。", errors)
