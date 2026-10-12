"""在专属 SQLite 线程上保存输入事实、待办和去重媒体。

``Store.open`` 创建单连接所有者；宿主须先持有数据目录实例锁。接管事务同时
检查额度并保存原文、媒体引用和待办。普通请求与处置提交使用独立有界队列，
处置可以先于尚未开始的普通事务，不能抢占已开始的 SQLite 事务。

等待取消不撤销已入队操作；原生来源可通过重投取得第一次接管身份。关闭只
执行一次，停止新的写入后排空已受理操作；连接收回前仍可接管有限只读查询。
权威故障阻止后续写入，保留原库和可读取事实；本模块不调度模型或插件，
不以空库或内存状态替代提交失败。
"""

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from purrcept_engine.errors import StorageError

from .models import Admission, InputRecord, InputSource, InputSubmission, MediaReference

_FORMAT_VERSION = 1
_APPLICATION_ID = 0x50555245
_INPUT_COLUMNS = (
    "input_id, adapter_id, account_id, conversation_id, event_kind, native_event_id, "
    "sender_id, display_name, text, occurred_at, received_at, logical_bytes"
)
_SCHEMA = (
    "CREATE TABLE media (media_id TEXT PRIMARY KEY, content BLOB NOT NULL)",
    "CREATE TABLE inputs ("
    "input_id TEXT PRIMARY KEY, adapter_id TEXT NOT NULL, account_id TEXT NOT NULL, "
    "conversation_id TEXT NOT NULL, event_kind TEXT NOT NULL, native_event_id TEXT, "
    "sender_id TEXT, display_name TEXT, text TEXT NOT NULL, occurred_at TEXT NOT NULL, "
    "received_at TEXT NOT NULL, logical_bytes INTEGER NOT NULL CHECK(logical_bytes >= 0), "
    "dedup_key TEXT UNIQUE, fingerprint TEXT NOT NULL)",
    "CREATE TABLE input_media ("
    "input_id TEXT NOT NULL REFERENCES inputs(input_id), ordinal INTEGER NOT NULL, "
    "media_id TEXT NOT NULL REFERENCES media(media_id), media_type TEXT NOT NULL, "
    "name TEXT, PRIMARY KEY(input_id, ordinal))",
    "CREATE TABLE pending_inputs ("
    "sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
    "input_id TEXT UNIQUE NOT NULL REFERENCES inputs(input_id))",
)


class _QueuedJob(Protocol):
    """数据库线程执行一次交接，结果通过所属事件循环交回等待者。

    Store 创建并入队，工作线程调用 run；数据库或业务边界错误由线程调用
    fail 回传。结果 Future 只在事件循环修改。该内部契约隐藏结果类型，
    使混合事务队列不必抹除公开方法的返回类型。
    """

    logical_bytes: int

    def run(self, connection: sqlite3.Connection) -> None:
        """执行已接管操作，将成功结果送回事件循环；错误留给线程边界分类。"""
        ...

    def fail(self, error: BaseException) -> None:
        """送回原错误；内部异常不转换成容量拒收或可纠正参数错误。"""
        ...


@dataclass(slots=True)
class _Job[Result]:
    """保留操作与等待者的精确结果类型，不将连接交给异步调用者。"""

    operation: Callable[[sqlite3.Connection], Result]
    future: asyncio.Future[Result]
    loop: asyncio.AbstractEventLoop
    logical_bytes: int

    def run(self, connection: sqlite3.Connection) -> None:
        """取得结果后在所属循环完成 Future，保持跨线程所有权。"""
        result = self.operation(connection)
        self.loop.call_soon_threadsafe(self.future.set_result, result)

    def fail(self, error: BaseException) -> None:
        """在所属循环保留错误，即使原等待者已经取消。"""
        self.loop.call_soon_threadsafe(self.future.set_exception, error)


def _observe_result[Result](future: asyncio.Future[Result]) -> None:
    """取回无人继续等待的异常，保留 Future 中的结果供正常等待者读取。"""
    if not future.cancelled():
        future.exception()


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise StorageError("权威记录中的字符串字段无效。", code="invalid_record")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StorageError("权威记录中的非负整数字段无效。", code="invalid_record")
    return value


def _datetime(value: object) -> datetime:
    """拒绝无法重建的持久化时间，不以当前时间补齐坏记录。"""
    try:
        result = datetime.fromisoformat(_text(value))
    except ValueError as error:
        raise StorageError("权威记录中的时间无效。", code="invalid_record") from error
    if result.utcoffset() is None:
        raise StorageError("权威记录中的时间缺少时区。", code="invalid_record")
    return result


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _positive(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} 必须是正整数。")


class Store:
    """拥有专属连接与工作线程的权威存储服务。

    宿主在取得实例锁后创建并在所有借用者停止后关闭。公开方法只在创建它的
    事件循环使用；数据库连接从不离开专属线程。调用者可并发提交，但饱和时
    明确返回 ``store_busy``，不在内部创建无限等待任务。
    """

    def __init__(self, path: Path, max_inputs: int, max_input_bytes: int) -> None:
        """准备未打开实例；公开创建通过 open 取得线程初始化结果。

        路径在交接前固定到当前工作目录，实际文件与 SQLite I/O 全部在线程上。
        """
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
        self._closing = False
        self._connection_closing = False
        self._failure: StorageError | None = None
        self._thread = threading.Thread(target=self._run, name="purrcept-storage", daemon=False)

    @classmethod
    async def open(
        cls, path: Path, *, max_inputs: int = 1024, max_input_bytes: int = 256 * 1024 * 1024
    ) -> "Store":
        """打开已知 Engine 格式的权威库，不转换未知数据库。

        空路径首次建立格式，已有库须通过格式和完整性核验。额度约束未处置
        输入；历史记录与同库 BLOB 不因处置而删除。打开失败释放已取得资源；
        打开等待取消也会请求线程关闭，不留下无所有者连接。
        """
        if not isinstance(path, Path):
            raise ValueError("path 必须是 Path。")
        _positive(max_inputs, "max_inputs")
        _positive(max_input_bytes, "max_input_bytes")
        store = cls(path, max_inputs, max_input_bytes)
        store._thread.start()
        try:
            await asyncio.shield(store._ready)
        except asyncio.CancelledError:
            store._begin_close()
            await asyncio.shield(store._closed)
            raise
        except BaseException:
            # 生命周期：失败只有在专属连接实际收回后才能交给目录锁所有者。
            # 这里仅等待清理，原错误仍原样传播。
            await asyncio.shield(store._closed)
            raise
        return store

    async def admit(self, submission: InputSubmission) -> Admission:
        """原子接管输入或返回同一原生事实的第一次确认。

        来源冲突与额度不足均不写入部分原文或媒体。接收时间变化可以重放；
        来源发生事实、正文、发生时间或媒体变化必须使用新的事件身份。
        已排队操作不因等待取消回滚，接入方可用原生身份重投查询真实结果。
        """
        if not isinstance(submission, InputSubmission):
            raise ValueError("submission 必须是 InputSubmission。")
        try:
            text_bytes = len(submission.text.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise StorageError("输入正文不能表示为 UTF-8。", code="invalid_input") from error
        logical_bytes = text_bytes + sum(len(asset.content) for asset in submission.media)
        if logical_bytes > self._max_input_bytes:
            raise StorageError("单条输入超过逻辑负载额度。", code="input_capacity")
        return await self._submit(
            lambda connection: self._admit(connection, submission, logical_bytes),
            logical_bytes=logical_bytes,
            writing=True,
        )

    async def get_input(self, input_id: str) -> InputRecord | None:
        """读取接管事实及当前处置状态，查询不会改变待办。"""
        self._validate_id(input_id)
        return await self._submit(lambda connection: self._get_input(connection, input_id))

    async def pending(self, *, limit: int = 8) -> tuple[InputRecord, ...]:
        """按接管顺序取得一小批待办，不创建逐条等待任务或确认已处理。

        单批不会超过配置的接管条数上限；默认最多读取八条。
        """
        _positive(limit, "limit")
        bounded_limit = min(limit, self._max_inputs)
        return await self._submit(lambda connection: self._pending(connection, bounded_limit))

    async def complete_input(self, input_id: str) -> None:
        """明确提交输入已处置，释放积压额度并保留原文与媒体。

        注意或前台所有者在完成处置后调用；重复提交同一身份没有附加效果。
        该控制提交不占普通排队位置，也不把释放额度等同于对外回复。
        """
        self._validate_id(input_id)
        await self._submit(
            lambda connection: self._complete_input(connection, input_id),
            control=True,
            writing=True,
        )

    async def get_media(self, media_id: str) -> bytes | None:
        """读取正式媒体并核对内容摘要，不以损坏字节替代已接管证据。"""
        self._validate_id(media_id)
        return await self._submit(lambda connection: self._get_media(connection, media_id))

    async def close(self) -> None:
        """禁止新的写入并等待同一排空与关闭过程。

        取消本次等待不会取消线程收尾；再次调用加入原关闭过程，不再次关闭
        连接。宿主负责整体关闭期限，本入口等待实际完成才返回。
        """
        self._check_loop()
        self._begin_close()
        await asyncio.shield(self._closed)

    def _check_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Store 只能在创建它的事件循环上使用。")

    @staticmethod
    def _validate_id(value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError("记录身份必须是非空字符串。")

    async def _submit[Result](
        self,
        operation: Callable[[sqlite3.Connection], Result],
        *,
        logical_bytes: int = 0,
        control: bool = False,
        writing: bool = False,
    ) -> Result:
        """在取得有限交接位置后提交；shield 保护的是实际提交所有权。"""
        self._check_loop()
        future: asyncio.Future[Result] = self._loop.create_future()
        future.add_done_callback(_observe_result)
        job = _Job(operation, future, self._loop, logical_bytes)
        with self._condition:
            if self._connection_closing or (self._closing and writing):
                raise StorageError("存储已开始关闭。", code="store_closed")
            if writing and self._failure is not None:
                raise StorageError("权威存储已故障，拒绝新的写入。") from self._failure

            # 队列条数来自接管上限，载荷同时受逻辑字节额度约束。控制只传身份，
            # 有独立的同等条数位置，不借普通输入容量完成处置。
            if control:
                if self._control_count >= self._max_inputs:
                    raise StorageError("存储控制提交已满。", code="store_busy")
                self._control_count += 1
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
            try:
                job.run(connection)
            except StorageError as error:
                if error.code in {"invalid_record", "unsupported_format"}:
                    self._latch_failure(error)
                job.fail(error)
            except sqlite3.Error as error:
                failure = self._storage_failure(error)
                self._latch_failure(failure)
                job.fail(failure)
            except BaseException as error:
                job.fail(error)
                self._abort_pending(error)
                return
            finally:
                with self._condition:
                    if control:
                        self._control_count -= 1
                    else:
                        self._ordinary_count -= 1
                        self._ordinary_bytes -= job.logical_bytes

    def _abort_pending(self, error: BaseException) -> None:
        """程序异常停止线程派发，并把所有已接管等待者交回同一个真实故障。"""
        with self._condition:
            self._closing = True
            self._connection_closing = True
            pending = (*self._control, *self._ordinary)
            self._control_count -= len(self._control)
            self._ordinary_count -= len(self._ordinary)
            self._ordinary_bytes -= sum(queued.logical_bytes for queued in self._ordinary)
            self._control.clear()
            self._ordinary.clear()
            # 当前任务仍由 _drain.finally 归还，只扣尚未开始的队列位置。
            for queued in pending:
                queued.fail(error)

    def _latch_failure(self, failure: StorageError) -> None:
        """权威故障保留为拒写原因，不自动重建连接或替换空数据库。"""
        with self._condition:
            if self._failure is None:
                self._failure = failure

    @staticmethod
    def _storage_failure(error: BaseException) -> StorageError:
        """翻译真实 I/O 与 SQLite 错误并保留原因，沿用已分类的存储错误。"""
        if isinstance(error, StorageError):
            return error
        result = StorageError("权威 SQLite 操作失败，原数据库已保留。")
        result.__cause__ = error
        return result

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        """只初始化空数据库；未知格式与完整性失败必须交给调用者处置。"""
        connection.execute("PRAGMA foreign_keys = ON")
        version_row: tuple[object, ...] = connection.execute("PRAGMA user_version").fetchone()
        identity_row: tuple[object, ...] = connection.execute("PRAGMA application_id").fetchone()
        version = _integer(version_row[0])
        identity = _integer(identity_row[0])
        tables: list[tuple[object, ...]] = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if version == 0 and identity == 0 and not tables:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {_FORMAT_VERSION}")
        elif version != _FORMAT_VERSION or identity != _APPLICATION_ID:
            raise StorageError("数据库不是支持的 Engine 格式。", code="unsupported_format")

        check: tuple[object, ...] = connection.execute("PRAGMA quick_check").fetchone()
        if check != ("ok",):
            raise StorageError("权威数据库完整性检查失败。", code="invalid_record")
        broken_reference: tuple[object, ...] | None = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchone()
        if broken_reference is not None:
            raise StorageError("权威数据库包含失效引用。", code="invalid_record")
        # 明确读取必需列，在装配时暴露缺表/缺列，避免首次输入才触发格式失败。
        connection.execute(f"SELECT {_INPUT_COLUMNS}, dedup_key, fingerprint FROM inputs LIMIT 0")
        connection.execute("SELECT media_id, content FROM media LIMIT 0")
        connection.execute(
            "SELECT input_id, ordinal, media_id, media_type, name FROM input_media LIMIT 0"
        )
        connection.execute("SELECT sequence, input_id FROM pending_inputs LIMIT 0")

    def _admit(
        self, connection: sqlite3.Connection, submission: InputSubmission, logical_bytes: int
    ) -> Admission:
        """去重、额度和所有输入事实在同一写事务中判断与提交。"""
        with self._condition:
            if self._failure is not None:
                raise StorageError("权威存储已故障，拒绝新的写入。") from self._failure
        source = submission.source
        media_ids = tuple(hashlib.sha256(asset.content).hexdigest() for asset in submission.media)
        source_facts = (
            source.adapter_id,
            source.account_id,
            source.conversation_id,
            source.event_kind,
            source.native_event_id,
            source.sender_id,
            source.display_name,
        )
        dedup_key = None
        if source.native_event_id is not None:
            dedup_key = json.dumps(source_facts[:5], separators=(",", ":"))
        # 接收时间属于这次传输；发生事实与媒体顺序才决定原生身份是否冲突。
        facts = (
            source_facts,
            submission.text,
            _timestamp(submission.occurred_at),
            tuple(
                (media_id, asset.media_type, asset.name)
                for media_id, asset in zip(media_ids, submission.media, strict=True)
            ),
        )
        fingerprint = hashlib.sha256(
            json.dumps(facts, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        connection.execute("BEGIN IMMEDIATE")
        try:
            if dedup_key is not None:
                existing: tuple[object, ...] | None = connection.execute(
                    "SELECT input_id, fingerprint FROM inputs WHERE dedup_key = ?", (dedup_key,)
                ).fetchone()
                if existing is not None:
                    if _text(existing[1]) != fingerprint:
                        raise StorageError("原生输入身份与已接管事实冲突。", code="source_conflict")
                    connection.commit()
                    return Admission(_text(existing[0]), duplicate=True)

            usage: tuple[object, ...] = connection.execute(
                "SELECT count(*), coalesce(sum(i.logical_bytes), 0) "
                "FROM pending_inputs p JOIN inputs i ON i.input_id = p.input_id"
            ).fetchone()
            if (
                _integer(usage[0]) >= self._max_inputs
                or _integer(usage[1]) + logical_bytes > self._max_input_bytes
            ):
                raise StorageError("未处置输入的接管额度已满。", code="input_capacity")

            input_id = uuid4().hex
            connection.execute(
                "INSERT INTO inputs (" + _INPUT_COLUMNS + ", dedup_key, fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    input_id,
                    *source_facts,
                    submission.text,
                    _timestamp(submission.occurred_at),
                    _timestamp(submission.received_at),
                    logical_bytes,
                    dedup_key,
                    fingerprint,
                ),
            )
            for ordinal, (media_id, asset) in enumerate(
                zip(media_ids, submission.media, strict=True)
            ):
                connection.execute(
                    "INSERT OR IGNORE INTO media (media_id, content) VALUES (?, ?)",
                    (media_id, asset.content),
                )
                connection.execute(
                    "INSERT INTO input_media (input_id, ordinal, media_id, media_type, name) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (input_id, ordinal, media_id, asset.media_type, asset.name),
                )
            connection.execute("INSERT INTO pending_inputs (input_id) VALUES (?)", (input_id,))
            connection.commit()
        except (sqlite3.Error, StorageError):
            connection.rollback()
            raise
        except UnicodeEncodeError as error:
            # Python 字符串可以包含孤立代理项，SQLite UTF-8 边界无法保存它们。
            # 这是未接管输入的错误，不应杀死线程或污染权威故障状态。
            connection.rollback()
            raise StorageError(
                "输入来源或媒体信息不能表示为 UTF-8。", code="invalid_input"
            ) from error
        return Admission(input_id, duplicate=False)

    def _get_input(self, connection: sqlite3.Connection, input_id: str) -> InputRecord | None:
        """由唯一待办关系推导状态，不维护第二份可写状态字段。"""
        row: tuple[object, ...] | None = connection.execute(
            f"SELECT {_INPUT_COLUMNS} FROM inputs WHERE input_id = ?", (input_id,)
        ).fetchone()
        if row is None:
            return None
        media_rows: list[tuple[object, ...]] = connection.execute(
            "SELECT r.media_id, r.media_type, length(m.content), r.name FROM input_media r "
            "LEFT JOIN media m ON m.media_id = r.media_id WHERE r.input_id = ? ORDER BY r.ordinal",
            (input_id,),
        ).fetchall()
        pending: tuple[object, ...] | None = connection.execute(
            "SELECT 1 FROM pending_inputs WHERE input_id = ?", (input_id,)
        ).fetchone()
        try:
            source = InputSource(
                _text(row[1]),
                _text(row[2]),
                _text(row[3]),
                _text(row[4]),
                _optional_text(row[5]),
                _optional_text(row[6]),
                _optional_text(row[7]),
            )
        except ValueError as error:
            raise StorageError("权威记录中的输入来源无效。", code="invalid_record") from error
        media = tuple(
            MediaReference(
                _text(item[0]), _text(item[1]), _integer(item[2]), _optional_text(item[3])
            )
            for item in media_rows
        )
        return InputRecord(
            _text(row[0]),
            source,
            _text(row[8]),
            _datetime(row[9]),
            _datetime(row[10]),
            media,
            "pending" if pending is not None else "processed",
            _integer(row[11]),
        )

    def _pending(self, connection: sqlite3.Connection, limit: int) -> tuple[InputRecord, ...]:
        """数据库保留所有身份，只加载调用者明确请求的有限批次。"""
        rows: list[tuple[object, ...]] = connection.execute(
            "SELECT input_id FROM pending_inputs ORDER BY sequence LIMIT ?", (limit,)
        ).fetchall()
        records: list[InputRecord] = []
        for row in rows:
            record = self._get_input(connection, _text(row[0]))
            if record is None:
                raise StorageError("待办引用不存在的输入。", code="invalid_record")
            records.append(record)
        return tuple(records)

    def _complete_input(self, connection: sqlite3.Connection, input_id: str) -> None:
        """显式处置只移除积压关系；原文和媒体继续作为权威历史保留。"""
        with self._condition:
            if self._failure is not None:
                raise StorageError("权威存储已故障，拒绝新的写入。") from self._failure
        with connection:
            existing: tuple[object, ...] | None = connection.execute(
                "SELECT 1 FROM inputs WHERE input_id = ?", (input_id,)
            ).fetchone()
            if existing is None:
                raise StorageError("待处置输入不存在。", code="input_missing")
            connection.execute("DELETE FROM pending_inputs WHERE input_id = ?", (input_id,))

    @staticmethod
    def _get_media(connection: sqlite3.Connection, media_id: str) -> bytes | None:
        """内容寻址只在摘要仍匹配时成立，坏 BLOB 必须作为权威故障暴露。"""
        row: tuple[object, ...] | None = connection.execute(
            "SELECT content FROM media WHERE media_id = ?", (media_id,)
        ).fetchone()
        if row is None:
            return None
        content = row[0]
        if not isinstance(content, bytes) or hashlib.sha256(content).hexdigest() != media_id:
            raise StorageError("正式媒体内容与摘要不符。", code="invalid_record")
        return content
