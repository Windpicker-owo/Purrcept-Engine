"""独占 SQLite 线程、连接、通知和有界提交队列；不提供业务账本操作。

Store 借用 submit；事务成功后才通知观察者。关闭排空已接管任务，取消等待
不能让连接失主。业务错误保留原类型，权威失败撤销后续写入资格。
"""

import asyncio
import os
import sqlite3
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import purrcept_engine.domain.controls.models as controls_models
import purrcept_engine.domain.knowledge.models as knowledge_models
import purrcept_engine.domain.time.models as time_models
from purrcept_engine.domain.context.maintenance import (
    SummaryError,
)
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite.schema import initialize


class _QueuedJob(Protocol):
    """数据库线程执行一次交接，结果通过所属事件循环交回等待者。

    Store 创建并入队，工作线程调用 run；数据库或业务边界错误由线程调用
    fail 回传。结果 Future 只在事件循环修改。该内部契约隐藏结果类型，
    使混合事务队列不必抹除公开方法的返回类型。
    """

    logical_bytes: int
    writing: bool

    def run(self, connection: sqlite3.Connection) -> Callable[[], None]:
        """执行事务并返回结果交付动作。

        Args:
            connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。

        Returns:
            将已完成结果交回原事件循环的动作，在线程归还队列名额后调用。

        Note:
            线程先归还交接位置再公开结果。
        """
        ...

    def fail(self, error: BaseException) -> None:
        """送回原错误。

        Args:
            error: 当前流程已经取得的真实故障或失败说明。

        Note:
            内部异常不转换成容量拒收或可纠正参数错误。
        """
        ...


@dataclass(slots=True)
class _Job[Result]:
    """保留操作与等待者的精确结果类型，不将连接交给异步调用者。"""

    operation: Callable[[sqlite3.Connection], Result]
    future: asyncio.Future[Result]
    loop: asyncio.AbstractEventLoop
    logical_bytes: int
    writing: bool

    def run(self, connection: sqlite3.Connection) -> Callable[[], None]:
        """冻结事务结果，延迟公开到队列名额实际归还后。

        Args:
            connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。

        Returns:
            将已完成结果交回原事件循环的动作，在线程归还队列名额后调用。
        """
        result = self.operation(connection)

        def publish() -> None:
            """在所属事件循环公开已完成的结果，线程不直接修改 Future。"""
            self.loop.call_soon_threadsafe(self.future.set_result, result)

        return publish

    def fail(self, error: BaseException) -> None:
        """在所属循环保留错误，即使原等待者已经取消。

        Args:
            error: 当前流程已经取得的真实故障或失败说明。
        """
        self.loop.call_soon_threadsafe(self.future.set_exception, error)


def _observe_result[Result](future: asyncio.Future[Result]) -> None:
    """取回无人继续等待的异常，保留 Future 中的结果供正常等待者读取。"""
    if not future.cancelled():
        future.exception()


class SqliteExecutor:
    """由 Store 创建的唯一连接所有者，全部异步入口只在创建循环调用。"""

    def __init__(self, path: Path, max_inputs: int, max_input_bytes: int) -> None:
        """准备未启动的提交器。

        Args:
            path: 此操作定位的明确路径。
            max_inputs: 有界提交队列允许接管的条目上限。
            max_input_bytes: 有界提交队列允许接管的逻辑字节上限。

        Note:
            Store 随后调用 start 等待线程初始化结果。

            路径在交接前固定到当前工作目录，实际文件与 SQLite I/O 全部在线程上。
        """
        self._initialize = initialize
        self._path = Path(os.path.abspath(path))
        self._max_inputs = max_inputs
        self._max_input_bytes = max_input_bytes
        self._loop = asyncio.get_running_loop()
        self._ready: asyncio.Future[None] = self._loop.create_future()
        self._closed: asyncio.Future[None] = self._loop.create_future()
        self._ready.add_done_callback(_observe_result)
        self._closed.add_done_callback(_observe_result)
        self._condition = threading.Condition()
        self._ordinary: deque[_QueuedJob] = deque()
        self._control: deque[_QueuedJob] = deque()
        self._ordinary_count = 0
        self._ordinary_bytes = 0
        self._control_count = 0
        self._control_bytes = 0
        self._closing = False
        self._connection_closing = False
        self._failure: StorageError | None = None
        self._thread = threading.Thread(target=self._run, name="purrcept-storage", daemon=False)
        self._change_revision = 0
        self._changed = asyncio.Event()

    async def start(self) -> None:
        """启动专属连接。

        Note:
            等待取消仍收回真实连接，失败不遗留线程。
        """
        self._thread.start()
        try:
            await asyncio.shield(self._ready)
        except asyncio.CancelledError:
            self._begin_close()
            await asyncio.shield(self._closed)
            raise
        except BaseException:
            await asyncio.shield(self._closed)
            raise

    @property
    def change_revision(self) -> int:
        """返回本进程成功写入通知代次，业务事实仍须从权威记录读取。"""
        return self._change_revision

    async def wait_for_change(self, after_revision: int) -> int:
        """等待新提交或关闭唤醒。

        Args:
            after_revision: 查询待办前取得的通知序号，避免查询与等待之间漏唤醒。

        Note:
            通知丢失不能替代持久待办查询。

            工作器先记录代次再查可靠待办，查询期间发生的写入会立即结束本次
            等待。固定工作器可借用此等待，不创建每行订阅或无界消息总线。
        """
        while self._change_revision <= after_revision and not self._closing:
            self._changed.clear()
            if self._change_revision > after_revision:
                break
            await self._changed.wait()
        return self._change_revision

    def _notify_change(self) -> None:
        """数据库线程仅排入本循环通知，事实及其可靠待办已经提交。"""
        self._change_revision += 1
        self._changed.set()

    @property
    def authority_available(self) -> bool:
        """返回派发前可同步核验的权威资格，不以查询成功替代写入健康。

        Note:
            故障与关闭门槛由同一线程条件锁读取；调用者在 await 后仍须再次核验，
            该快照不预留提交容量，也不是外部效果已经发生的证明。
        """
        with self._condition:
            return self._failure is None and not self._closing and not self._connection_closing

    async def close(self) -> None:
        """禁止新的写入并等待同一排空与关闭过程。

        Note:
            取消本次等待不会取消线程收尾；再次调用加入原关闭过程，不再次关闭
            连接。宿主负责整体关闭期限，本入口等待实际完成才返回。
        """
        self._check_loop()
        self._begin_close()
        await asyncio.shield(self._closed)

    def _check_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Store 只能在创建它的事件循环上使用。")

    async def submit[Result](
        self,
        operation: Callable[[sqlite3.Connection], Result],
        *,
        logical_bytes: int = 0,
        control: bool = False,
        writing: bool = False,
    ) -> Result:
        """向专属数据库线程提交有界同步操作，并等待真实结果。

        Args:
            operation: 在专属数据库线程执行的同步短事务，返回值交回原事件循环。
            logical_bytes: 本次操作占用的逻辑载荷字节数，用于接管前背压。 默认 0。
            control: 是否使用独立控制名额，避免普通输入阻塞真实回执收尾。 默认 False。
            writing: 是否属于写操作，以便关闭或权威故障时撤销派发资格。 默认 False。

        Returns:
            同步操作完成后的真实返回值，异常保持原类型。

        Raises:
            StorageError: 权威记录缺失、版本不一致、载荷无效或提交条件不成立。

        Note:
            在取得有限交接位置后提交；shield 保护的是实际提交所有权。
        """
        self._check_loop()
        future: asyncio.Future[Result] = self._loop.create_future()
        future.add_done_callback(_observe_result)
        job = _Job(operation, future, self._loop, logical_bytes, writing)
        with self._condition:
            if self._connection_closing or (self._closing and writing):
                raise StorageError("存储已开始关闭。", code="store_closed")
            if writing and self._failure is not None:
                raise StorageError("权威存储已故障，拒绝新的写入。") from self._failure

            # 控制有独立的条数和字节位置，真实回执不借普通输入容量收尾；
            # 参数与结果也须计量，不能在控制路径绕过有界载荷。
            if control:
                if (
                    self._control_count >= self._max_inputs
                    or self._control_bytes + logical_bytes > self._max_input_bytes
                ):
                    raise StorageError("存储控制提交已满。", code="store_busy")
                self._control_count += 1
                self._control_bytes += logical_bytes
                self._control.append(job)
            else:
                if (
                    self._ordinary_count >= self._max_inputs
                    or self._ordinary_bytes + logical_bytes > self._max_input_bytes
                ):
                    raise StorageError("存储普通提交已满。", code="store_busy")
                self._ordinary_count += 1
                self._ordinary_bytes += logical_bytes
                self._ordinary.append(job)
            self._condition.notify()
        return await asyncio.shield(future)

    def _begin_close(self) -> None:
        """关闭资格由同一条件锁串行确认，排队中的提交仍由线程持有。"""
        with self._condition:
            self._closing = True
            self._condition.notify()
        self._loop.call_soon_threadsafe(self._notify_change)

    def _run(self) -> None:
        """专属线程拥有从打开到关闭的全部连接访问与事务执行。"""
        connection: sqlite3.Connection | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._path)
            self._initialize(connection)
        except (OSError, sqlite3.Error, StorageError) as error:
            failure = self._storage_failure(error)
            self._loop.call_soon_threadsafe(self._ready.set_exception, failure)
        except BaseException as error:
            # 跨线程交接必须完成 Future。保留程序异常本身，不让线程退出后
            # 启动等待者永久悬挂，也不把它归入可恢复的 SQLite 错误。
            self._loop.call_soon_threadsafe(self._ready.set_exception, error)
        else:
            self._loop.call_soon_threadsafe(self._ready.set_result, None)
            self._drain(connection)
        finally:
            with self._condition:
                self._connection_closing = True
            try:
                if connection is not None:
                    connection.close()
            except sqlite3.Error as error:
                self._loop.call_soon_threadsafe(
                    self._closed.set_exception, self._storage_failure(error)
                )
            else:
                self._loop.call_soon_threadsafe(self._closed.set_result, None)

    def _drain(self, connection: sqlite3.Connection) -> None:
        """控制优先于未开始事务；已开始事务完成后才归还交接位置。"""
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: bool(self._control or self._ordinary) or self._closing
                )
                if not self._control and not self._ordinary:
                    # 读取与连接关闭使用同一门槛；此后不再接管排队，否则新查询
                    # 可能落在已排空队列之后，永久等待无人执行的任务。
                    self._connection_closing = True
                    return
                control = bool(self._control)
                job = self._control.popleft() if control else self._ordinary.popleft()
            fatal: BaseException | None = None
            error_to_send: BaseException | None = None
            publish: Callable[[], None] | None = None
            changes_before = connection.total_changes
            try:
                # 权威故障也撤销尚未开始的已排队写入资格，防止前一提交失败后
                # 后续派发意图仍被接管并成为能力启动许可；只读事实继续可查询。
                with self._condition:
                    if job.writing and self._failure is not None:
                        raise StorageError("权威存储已故障，拒绝排队写入。") from self._failure
                publish = job.run(connection)
            except StorageError as error:
                if error.code in {"invalid_record", "unsupported_format"}:
                    self._latch_failure(error)
                error_to_send = error
            except knowledge_models.KnowledgeInputError as error:
                # 来源/批次/版本冲突可在原预算内纠正，不是数据库或程序故障。
                # 该显式业务分类仍原样交回，其他未识别异常继续停止派发。
                error_to_send = error
            except (SummaryError, time_models.TimeError, controls_models.ControlError) as error:
                error_to_send = error
            except sqlite3.Error as error:
                failure = self._storage_failure(error)
                self._latch_failure(failure)
                error_to_send = failure
            except BaseException as error:
                fatal = error
                error_to_send = error
            finally:
                with self._condition:
                    if control:
                        self._control_count -= 1
                        self._control_bytes -= job.logical_bytes
                    else:
                        self._ordinary_count -= 1
                        self._ordinary_bytes -= job.logical_bytes
            # Ordering: Future 一旦完成，调用方可立即提交下一条操作。名额须
            # 已归还，否则 max_inputs=1 时成功 admit 后的 pending 会随机拒收。
            if error_to_send is not None:
                job.fail(error_to_send)
            else:
                assert publish is not None
                if job.writing and connection.total_changes > changes_before:
                    self._loop.call_soon_threadsafe(self._notify_change)
                publish()
            if fatal is not None:
                self._abort_pending(fatal)
                return

    def _abort_pending(self, error: BaseException) -> None:
        """程序异常停止线程派发，并把所有已接管等待者交回同一个真实故障。"""
        with self._condition:
            self._closing = True
            self._connection_closing = True
            pending = (*self._control, *self._ordinary)
            self._control_count -= len(self._control)
            self._control_bytes -= sum(queued.logical_bytes for queued in self._control)
            self._ordinary_count -= len(self._ordinary)
            self._ordinary_bytes -= sum(queued.logical_bytes for queued in self._ordinary)
            self._control.clear()
            self._ordinary.clear()
            self._loop.call_soon_threadsafe(self._notify_change)
            # 当前任务已归还，只扣尚未开始的队列位置。
            for queued in pending:
                queued.fail(error)

    def _latch_failure(self, failure: StorageError) -> None:
        """权威故障保留为拒写原因，不自动重建连接或替换空数据库。"""
        with self._condition:
            if self._failure is None:
                self._failure = failure
                self._loop.call_soon_threadsafe(self._notify_change)

    @staticmethod
    def _storage_failure(error: BaseException) -> StorageError:
        """翻译真实 I/O 与 SQLite 错误并保留原因，沿用已分类的存储错误。"""
        if isinstance(error, StorageError):
            return error
        result = StorageError("权威 SQLite 操作失败，原数据库已保留。")
        result.__cause__ = error
        return result

    def check_writing(self) -> None:
        """事务入口复查已锁存的权威故障，不撤销关闭前接管的正常提交。

        Raises:
            StorageError: 权威存储已故障，拒绝新的写入。
        """
        with self._condition:
            if self._failure is not None:
                raise StorageError("权威存储已故障，拒绝新的写入。") from self._failure
