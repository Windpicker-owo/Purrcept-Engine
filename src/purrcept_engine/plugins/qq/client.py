"""QQ 连接代次、有限恢复、独立 echo 接收与有界业务消费的唯一所有者。

direct 恢复仅重复建连，不重发 API；reverse 维持监听等待外部接入。业务消费
可以等待宿主提交或查询转发，但收包泵不等待业务队列空间，echo 不会被输入
积压堵住。RPC 超时保留有限 echo 关联，迟到实际应答另交真实回执端口。

stop_inputs 停止新业务与恢复/握手，既有收包和迟到回执仍存活；close 在执行器
收尾后关闭传输并等待后台提交。内部故障保持原型，不伪装成可以自动恢复的离线。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from purrcept_engine.domain.execution.providers import CallContext
from purrcept_engine.plugins.qq.ports import (
    Connection,
    ConnectionRejected,
    ConnectionUnavailable,
    Connector,
    Listener,
    QQCallError,
    Reply,
)
from purrcept_engine.plugins.qq.settings import QQSettings
from purrcept_engine.values import ActionProtocolError, JsonObject, decode_object, encode_json

EventHandler = Callable[[JsonObject, int], Awaitable[None]]
GapHandler = Callable[[JsonObject, str], None]
LateHandler = Callable[[Reply], Awaitable[None]]
State = Literal[
    "new", "waiting", "recovering", "online", "exhausted", "failed", "stopping", "closed"
]


@dataclass(slots=True)
class _Pending:
    action: str
    generation: int
    echo: str
    future: asyncio.Future[Reply]
    late_handler: LateHandler | None
    sending: bool = False
    timed_out: bool = False


def _observe[Value](future: asyncio.Future[Value]) -> None:
    if not future.cancelled():
        future.exception()


class QQClient:
    """build 创建、Runtime start/stop_inputs/close 调用的有限连接所有者。

    一次恢复仅有一个 Task，短连接仍消耗旧尝试。pending 包含等待应答及已报
    unknown 的有限 echo；后者仍占用同一配置额度，直到真回包或连接代次结束。
    不因发送者过滤丢 echo，也不在断线后清空已排队业务事实冒充可靠接收。
    """

    def __init__(
        self,
        settings: QQSettings,
        connector: Connector,
        event_handler: EventHandler,
        gap_handler: GapHandler,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """只绑定配置与有限回调，不连接网络或建立后台任务。"""
        self.settings = settings
        self._connector = connector
        self._event_handler = event_handler
        self._gap_handler = gap_handler
        self._monotonic = monotonic
        self._sleep = sleep
        self._state: State = "new"
        self._reason: str | None = None
        self._failure: BaseException | None = None
        self._generation = 0
        self._attempts = 0
        self._recovery_id: str | None = None
        self._listener: Listener | None = None
        self._connection: Connection | None = None
        self._receiver: asyncio.Task[None] | None = None
        self._recovery: asyncio.Task[None] | None = None
        self._consumer: asyncio.Task[None] | None = None
        self._processing: asyncio.Task[None] | None = None
        self._events: asyncio.Queue[tuple[JsonObject, int]] = asyncio.Queue(settings.inbox_size)
        self._pending: dict[str, _Pending] = {}
        self._late: set[asyncio.Task[None]] = set()
        self._readers: set[asyncio.Task[None]] = set()
        self._changed = asyncio.Event()
        self._adopting = False
        self._stopping = False
        self._closing: asyncio.Task[None] | None = None

    @property
    def generation(self) -> int:
        """返回实际接入代次，不用重连请求身份冒充已经建立新连接。"""
        return self._generation

    def snapshot(self) -> JsonObject:
        """只读取本所有者事实，不试探账号或远端服务。"""
        return {
            "mode": self.settings.mode,
            "state": self._state,
            "generation": self._generation,
            "attempts": self._attempts,
            "recovery_id": self._recovery_id,
            "bound_port": None if self._listener is None else self._listener.port,
            "reason": self._reason,
            "pending_calls": len(self._pending),
            "pending_late_results": len(self._late),
            "queued_events": self._events.qsize(),
            "implementation": "SnowLuma",
            "implementation_version": None,
            "verification": "离线动作契约样例；真实部署版本及效果待验",
        }

    async def wait_state(self, state: State, *, timeout_seconds: float = 10.0) -> None:
        """本地所有者测试/观察的有界等待，不发起连接或增加恢复预算。"""
        async with asyncio.timeout(timeout_seconds):
            while self._state != state:
                self._changed.clear()
                await self._changed.wait()

    def panel_revision(self) -> str:
        """面板只绑定真实连接状态/代次，诊断计数和时间不令点击资格失效。"""
        return self._state + ":" + str(self._generation)

    async def _set_state(self, state: State, reason: str | None = None) -> None:
        self._state, self._reason = state, reason
        self._changed.set()

    async def start(self) -> None:
        """reverse 取得真实监听；direct 只启动已配置的有限恢复所有者。"""
        if self._state != "new" or self._stopping:
            raise RuntimeError("QQ 连接所有者不能重复启动。")
        self._consumer = asyncio.create_task(self._consume(), name="qq-input-consumer")
        self._consumer.add_done_callback(self._task_failed)
        if self.settings.mode == "reverse":
            try:
                self._listener = await self._connector.listen(self.settings, self._accepted)
            except BaseException:
                # 监听失败是启动故障；已经创建的业务消费者先实际收回再交出错误。
                self._consumer.cancel()
                await asyncio.gather(self._consumer, return_exceptions=True)
                raise
            await self._set_state("waiting")
        else:
            await self.reconnect("startup-" + uuid4().hex)

    async def reconnect(self, recovery_id: str) -> JsonObject:
        """受理一次有限恢复；已有恢复/在线时只查询，不并行重开或重发消息。"""
        if self._failure is not None:
            raise self._failure
        if self._stopping:
            raise RuntimeError("QQ 正在关闭接入，不能恢复。")
        if self.settings.mode == "reverse":
            if self._listener is None:
                self._listener = await self._connector.listen(self.settings, self._accepted)
            await self._set_state("online" if self._connection is not None else "waiting")
            return self.snapshot()
        if self._connection is not None or (
            self._recovery is not None and not self._recovery.done()
        ):
            return self.snapshot()
        self._attempts = 0
        self._recovery_id = recovery_id
        await self._set_state("recovering")
        self._recovery = asyncio.create_task(self._restore(), name="qq-direct-recovery")
        self._recovery.add_done_callback(self._task_failed)
        return self.snapshot()

    def _task_failed(self, task: asyncio.Task[None]) -> None:
        """内部错误撤销接入和调用资格，保持原型以供实际 close/调用报告。"""
        if not task.cancelled() and (error := task.exception()) is not None:
            self._failure = error
            self._state, self._reason = "failed", f"{type(error).__name__}: {error}"
            self._stopping = True
            self._changed.set()

    async def _restore(self) -> None:
        """只恢复传输；稳定期才能重置次数，Socket 回包所有者独立于本循环取消。"""
        while not self._stopping:
            if self._attempts >= self.settings.connect_attempts:
                await self._set_state("exhausted", self._reason)
                return
            if self._attempts:
                await self._sleep(self.settings.backoff_seconds[self._attempts - 1])
                if self._stopping:
                    return
            self._attempts += 1
            await self._set_state("recovering", self._reason)
            try:
                async with asyncio.timeout(self.settings.connect_timeout_seconds):
                    connection = await self._connector.connect(self.settings)
            except (ConnectionUnavailable, TimeoutError) as error:
                self._reason = str(error)
                continue
            except ConnectionRejected as error:
                await self._set_state("failed", str(error))
                return
            began = self._monotonic()
            receiver = await self._adopt(connection)
            if receiver is None:
                return
            try:
                await asyncio.shield(receiver)
            except ConnectionUnavailable as error:
                self._reason = str(error)
            except ConnectionRejected as error:
                await self._set_state("failed", str(error))
                return
            if self._monotonic() - began >= self.settings.stable_seconds:
                self._attempts = 0

    async def _accepted(self, connection: Connection) -> None:
        """外部接入可替换旧代次；交接忙时明确拒绝额外连接而不排无界等待者。"""
        receiver = await self._adopt(connection)
        if receiver is None:
            return
        try:
            await asyncio.shield(receiver)
        except (ConnectionUnavailable, ConnectionRejected) as error:
            if not self._stopping and self._connection is None:
                await self._set_state("waiting", str(error))

    async def _adopt(self, connection: Connection) -> asyncio.Task[None] | None:
        if self._stopping or self._adopting:
            await connection.close()
            return None
        self._adopting = True
        adopted = False
        try:
            previous = self._connection
            if previous is not None:
                self._fail_generation(self._generation, "QQ 连接被新代次替换。")
                await previous.close()
            if self._stopping:
                return None
            self._generation += 1
            generation = self._generation
            self._connection = connection
            adopted = True
            receiver = asyncio.create_task(
                self._receive(connection, generation), name=f"qq-receive:{generation}"
            )
            self._receiver = receiver
            self._readers.add(receiver)
            receiver.add_done_callback(self._reader_done)
            await self._set_state("online")
            return receiver
        finally:
            self._adopting = False
            if not adopted:
                await connection.close()

    def _reader_done(self, task: asyncio.Task[None]) -> None:
        self._readers.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            if not isinstance(error, (ConnectionUnavailable, ConnectionRejected)):
                self._task_failed(task)

    async def _receive(self, connection: Connection, generation: int) -> None:
        """业务入队从不等待空间；回包必须在同一真实代次和 echo 上关联。"""
        try:
            while True:
                frame = await connection.receive()
                try:
                    data = frame.encode("utf-8") if isinstance(frame, str) else frame
                    if len(data) > self.settings.frame_bytes:
                        raise ConnectionRejected("QQ 帧超过配置字节上限。")
                    payload = decode_object(data)
                except (ActionProtocolError, UnicodeError) as error:
                    raise ConnectionRejected("QQ 收到非法 JSON/UTF-8 帧。") from error
                echo = payload.get("echo")
                if payload.get("post_type") is None and isinstance(echo, str):
                    pending = self._pending.get(echo)
                    if pending is not None and pending.generation == generation:
                        reply = Reply(pending.action, generation, echo, payload, time.time())
                        if not pending.future.done():
                            pending.future.set_result(reply)
                        if pending.timed_out:
                            self._pending.pop(echo)
                            if pending.late_handler is not None:
                                self._own_late(pending.late_handler(reply))
                    continue
                if self._stopping:
                    self._gap_handler(payload, "ingress_stopped")
                    continue
                try:
                    self._events.put_nowait((payload, generation))
                except asyncio.QueueFull:
                    self._gap_handler(payload, "input_queue_full")
        finally:
            if self._connection is connection:
                self._connection = None
                self._fail_generation(generation, "QQ 连接代次已结束，旧请求未获确认。")
                if not self._stopping:
                    await self._set_state(
                        "waiting" if self.settings.mode == "reverse" else "recovering"
                    )
            await connection.close()

    def _own_late(self, operation: Awaitable[None]) -> None:
        """一项超时 echo 最多建立一个迟到提交，数量受原 pending_calls 界约束。"""

        async def commit() -> None:
            await operation

        task = asyncio.create_task(commit(), name="qq-late-result")
        self._late.add(task)

        def done(owner: asyncio.Task[None]) -> None:
            self._late.discard(owner)
            self._task_failed(owner)

        task.add_done_callback(done)

    def own_file_result(self, operation: Callable[[], Awaitable[None]]) -> None:
        """本地贴纸写入与 echo 共用有限后台名额，调用等待取消不丢真实结果。

        在创建写任务前取得名额；所有者等到文件实际完成和必要回执提交后才释放。
        stop_inputs 不取消它，close 与 echo 迟到提交一起等待。
        """
        if self._failure is not None:
            raise self._failure
        if len(self._pending) + len(self._late) >= self.settings.pending_calls:
            raise QQCallError(
                "QQ 在途文件/协议结果名额已满。",
                action="local_sticker_store",
                phase="not_started",
                generation=self._generation,
            )
        self._own_late(operation())

    async def _consume(self) -> None:
        """唯一业务消费者借给单个实际处理 Task，停接入不撤销已接管事务等待。"""
        while True:
            payload, generation = await self._events.get()
            try:
                if self._stopping:
                    self._gap_handler(payload, "ingress_stopped")
                else:
                    processing = asyncio.create_task(
                        self._deliver_event(payload, generation), name="qq-input-admission"
                    )
                    self._processing = processing
                    processing.add_done_callback(_observe)
                    await asyncio.shield(processing)
            finally:
                if self._processing is not None and self._processing.done():
                    self._processing = None
                self._events.task_done()

    async def _deliver_event(self, payload: JsonObject, generation: int) -> None:
        """把扩展端口的 Awaitable 借给一个独立任务，消费取消不撤销宿主提交。"""
        await self._event_handler(payload, generation)

    def _fail_generation(self, generation: int, message: str) -> None:
        """旧代次不能借用新连接的 echo；断线没有收到平台回应，不补造迟到成功。"""
        for echo, pending in tuple(self._pending.items()):
            if pending.generation != generation:
                continue
            self._pending.pop(echo)
            if not pending.future.done():
                pending.future.set_exception(
                    QQCallError(
                        message,
                        action=pending.action,
                        phase="unknown" if pending.sending else "not_started",
                        generation=generation,
                        echo=echo,
                    )
                )

    async def request(
        self,
        action: str,
        params: JsonObject,
        *,
        context: CallContext | None = None,
        generation: int | None = None,
        on_late: LateHandler | None = None,
    ) -> Reply:
        """一次 SDK 请求，不重试。超时后的 echo 仍有限保留供实际迟到回执关联。"""
        if self._failure is not None:
            raise self._failure
        connection = self._connection
        actual_generation = self._generation
        if connection is None or (generation is not None and generation != actual_generation):
            raise QQCallError(
                "QQ 目标连接代次不可用。",
                action=action,
                phase="not_started",
                generation=actual_generation,
            )
        if len(self._pending) + len(self._late) >= self.settings.pending_calls:
            raise QQCallError(
                "QQ RPC/迟到回执名额已满。",
                action=action,
                phase="not_started",
                generation=actual_generation,
            )
        now = asyncio.get_running_loop().time()
        deadline = now + self.settings.call_timeout_seconds
        if context is not None:
            deadline = min(deadline, context.deadline)
        if now >= deadline:
            raise QQCallError(
                "QQ 调用在发送前期限耗尽。",
                action=action,
                phase="not_started",
                generation=actual_generation,
            )
        echo = uuid4().hex
        future: asyncio.Future[Reply] = asyncio.get_running_loop().create_future()
        future.add_done_callback(_observe)
        pending = _Pending(action, actual_generation, echo, future, on_late)
        request = encode_json({"action": action, "params": params, "echo": echo}).decode("utf-8")
        if len(request.encode("utf-8")) > self.settings.frame_bytes:
            raise QQCallError(
                "QQ 请求帧超过配置字节上限，尚未发送。",
                action=action,
                phase="not_started",
                generation=actual_generation,
            )
        self._pending[echo] = pending
        timer = asyncio.timeout_at(deadline)
        try:
            async with timer:
                pending.sending = True
                await connection.send(request)
                reply = await asyncio.shield(future)
            self._pending.pop(echo, None)
            return reply
        except TimeoutError as error:
            if not timer.expired():
                self._pending.pop(echo, None)
                raise
            pending.timed_out = True
            # 超时与收包同一事件循环仍可竞争；已到达完整回应不能被当作丢失。
            if future.done() and not future.cancelled() and future.exception() is None:
                self._pending.pop(echo, None)
                return future.result()
            if on_late is None:
                self._pending.pop(echo, None)
            raise QQCallError(
                "QQ 请求已进入发送阶段但未在期限内确认。",
                action=action,
                phase="unknown",
                generation=actual_generation,
                echo=echo,
            ) from error
        except (ConnectionUnavailable, ConnectionRejected) as error:
            self._pending.pop(echo, None)
            raise QQCallError(
                str(error), action=action, phase="unknown", generation=actual_generation, echo=echo
            ) from error
        except QQCallError:
            self._pending.pop(echo, None)
            raise
        except asyncio.CancelledError:
            pending.timed_out = True
            if on_late is None:
                self._pending.pop(echo, None)
            elif future.done() and not future.cancelled() and future.exception() is None:
                self._pending.pop(echo, None)
                self._own_late(on_late(future.result()))
            raise
        except BaseException:
            self._pending.pop(echo, None)
            raise

    async def stop_inputs(self) -> None:
        """停止新业务、建连与监听；不提前关闭既有 echo 收包通路。"""
        self._stopping = True
        await self._set_state("stopping")
        if self._listener is not None:
            await self._listener.stop_accepting()
        if self._recovery is not None and not self._recovery.done():
            self._recovery.cancel()
            await asyncio.gather(self._recovery, return_exceptions=True)
        if self._consumer is not None and not self._consumer.done():
            self._consumer.cancel()
            await asyncio.gather(self._consumer, return_exceptions=True)
        if self._processing is not None:
            await asyncio.shield(self._processing)
            self._processing = None
        while not self._events.empty():
            payload, _ = self._events.get_nowait()
            self._gap_handler(payload, "ingress_stopped")
            self._events.task_done()

    async def close(self) -> None:
        """资源关闭只执行一次；回包/迟到提交实际结束前宿主不能先关闭 Store。"""
        if self._closing is None:
            self._closing = asyncio.create_task(self._close(), name="qq-close")
            self._closing.add_done_callback(_observe)
        await asyncio.shield(self._closing)

    async def _close(self) -> None:
        """独立资源都尝试收回；一个提交故障不能把其余 Socket 留在后台。"""
        errors: list[BaseException] = []
        try:
            await self.stop_inputs()
        except BaseException as error:
            errors.append(error)
        for resource in (self._connection, self._listener):
            if resource is not None:
                try:
                    await resource.close()
                except BaseException as error:
                    errors.append(error)
        if self._readers:
            results = await asyncio.gather(*tuple(self._readers), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                    result, (ConnectionUnavailable, ConnectionRejected)
                ):
                    errors.append(result)
        if self._late:
            results = await asyncio.gather(*tuple(self._late), return_exceptions=True)
            errors.extend(result for result in results if isinstance(result, BaseException))
        if self._failure is not None and self._failure not in errors:
            errors.append(self._failure)
        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise BaseExceptionGroup("QQ 独立资源收尾失败。", errors)
        await self._set_state("closed")
