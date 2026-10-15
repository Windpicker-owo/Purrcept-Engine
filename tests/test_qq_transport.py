"""不联网的真实 QQ 所有者验收：有限恢复、echo 关联和停接入后的资源归还。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from purrcept_engine.execution.providers import CallContext
from purrcept_engine.plugins.qq.client import QQClient
from purrcept_engine.plugins.qq.ports import (
    Connection,
    ConnectionRejected,
    ConnectionUnavailable,
    QQCallError,
    Reply,
)
from purrcept_engine.plugins.qq.settings import QQSettings
from purrcept_engine.values import JsonObject, decode_object, encode_json


class Socket:
    """仅传递显式测试帧；没有默认成功或真实 Socket。"""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self.sent: asyncio.Queue[JsonObject] = asyncio.Queue()
        self.closed = False

    async def send(self, data: str) -> None:
        """保留真实请求动作、参数和 echo 供样例明确应答。"""
        await self.sent.put(decode_object(data.encode()))

    async def receive(self) -> str:
        """逐帧读取，结束由显式异常确认。"""
        value = await self.incoming.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self) -> None:
        """幂等结束替身传输，使收包任务实际退出。"""
        if not self.closed:
            self.closed = True
            self.incoming.put_nowait(ConnectionUnavailable("样例连接结束"))

    def reply(self, request: JsonObject, data: object = None) -> None:
        """按实际请求 echo 提供标准 OneBot 信封。"""
        self.incoming.put_nowait(
            encode_json(
                {"status": "ok", "retcode": 0, "data": data, "echo": request["echo"]}
            ).decode()
        )


class Connector:
    """逐次消耗显式连接/故障样例，reverse 也只保存有限接入回调。"""

    def __init__(self, outcomes: tuple[Connection | BaseException, ...] = ()) -> None:
        self.outcomes = iter(outcomes)
        self.attempts = 0
        self.connected = asyncio.Event()
        self.accepted: Callable[[Connection], Awaitable[None]] | None = None
        self.stopped = False
        self.closed = False

    @property
    def port(self) -> int:
        """返回替身监听身份，不宣称绑定系统端口。"""
        return 8095

    async def connect(self, settings: QQSettings) -> Connection:
        """只用当前指定样例，不自动成功。"""
        del settings
        self.attempts += 1
        result = next(self.outcomes)
        if isinstance(result, BaseException):
            raise result
        self.connected.set()
        return result

    async def listen(
        self, settings: QQSettings, accepted: Callable[[Connection], Awaitable[None]]
    ) -> Connector:
        """不联网地取得接入回调。"""
        del settings
        self.accepted = accepted
        return self

    async def stop_accepting(self) -> None:
        """停新接入不关闭现有连接。"""
        self.stopped = True

    async def close(self) -> None:
        """只结束本替身监听。"""
        self.closed = True


async def ignore_event(payload: JsonObject, generation: int) -> None:
    """无业务的传输样例不提交输入。"""
    del payload, generation


def ignore_gap(payload: JsonObject, reason: str) -> None:
    """无诊断断言的样例不收集缺口。"""
    del payload, reason


async def online(*, pending_calls: int = 16) -> tuple[QQClient, Socket, Connector]:
    """创建已建立的 direct 替身，只连接一个显式 Socket。"""
    socket = Socket()
    connector = Connector((socket,))
    settings = QQSettings(mode="direct", pending_calls=pending_calls)
    client = QQClient(settings, connector, ignore_event, ignore_gap)
    await client.start()
    await client.wait_state("online")
    return client, socket, connector


def test_finite_recovery_and_explicit_restart() -> None:
    async def run() -> None:
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        connector = Connector(tuple(ConnectionUnavailable("离线") for _ in range(10)))
        client = QQClient(
            QQSettings(mode="direct"), connector, ignore_event, ignore_gap, sleep=sleep
        )
        await client.start()
        await client.wait_state("exhausted")
        assert connector.attempts == 5
        assert sleeps == [1.0, 2.0, 4.0, 8.0]
        await client.reconnect("explicit")
        await client.wait_state("exhausted")
        assert connector.attempts == 10
        assert client.snapshot()["recovery_id"] == "explicit"
        await client.close()

    asyncio.run(run())


def test_terminal_connection_error_is_not_retried() -> None:
    async def run() -> None:
        connector = Connector((ConnectionRejected("凭据拒绝"),))
        client = QQClient(QQSettings(mode="direct"), connector, ignore_event, ignore_gap)
        await client.start()
        await client.wait_state("failed")
        assert connector.attempts == 1
        await client.close()

    asyncio.run(run())


def test_pending_timeout_is_bounded_and_late_echo_survives_stop_inputs() -> None:
    async def run() -> None:
        client, socket, connector = await online(pending_calls=1)
        late: list[Reply] = []
        committed = asyncio.Event()

        async def record(reply: Reply) -> None:
            late.append(reply)
            committed.set()

        context = CallContext("call", "action", asyncio.get_running_loop().time() + 0.03)
        with pytest.raises(QQCallError, match="发送阶段") as caught:
            await client.request(
                "send_private_msg", {"user_id": 1, "message": "x"}, context=context, on_late=record
            )
        assert caught.value.phase == "unknown"
        request = await socket.sent.get()
        assert client.snapshot()["pending_calls"] == 1
        with pytest.raises(QQCallError, match="名额已满") as full:
            await client.request("get_login_info", {})
        assert full.value.phase == "not_started"
        await client.stop_inputs()
        assert connector.stopped is False  # direct 没有监听资源。
        assert not socket.closed
        socket.reply(request, {"message_id": 123})
        await asyncio.wait_for(committed.wait(), 1)
        assert len(late) == 1 and late[0].echo == request["echo"]
        assert client.snapshot()["pending_calls"] == 0
        assert socket.sent.empty()
        await client.close()
        assert socket.closed

    asyncio.run(run())


def test_cancelled_observation_keeps_real_pending_echo() -> None:
    async def run() -> None:
        client, socket, _ = await online()
        late = asyncio.Event()

        async def record(reply: Reply) -> None:
            assert reply.response["data"] == {"message_id": -45}
            late.set()

        task = asyncio.create_task(
            client.request("send_group_msg", {"group_id": 2, "message": "x"}, on_late=record)
        )
        request = await socket.sent.get()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        socket.reply(request, {"message_id": -45})
        await asyncio.wait_for(late.wait(), 1)
        assert socket.sent.empty()
        await client.close()

    asyncio.run(run())


def test_business_queue_full_does_not_block_echo() -> None:
    async def run() -> None:
        socket = Socket()
        connector = Connector((socket,))
        started, release = asyncio.Event(), asyncio.Event()
        gaps: list[str] = []

        async def event(payload: JsonObject, generation: int) -> None:
            del payload, generation
            started.set()
            await release.wait()

        client = QQClient(
            QQSettings(mode="direct", inbox_size=1),
            connector,
            event,
            lambda _p, reason: gaps.append(reason),
        )
        await client.start()
        await client.wait_state("online")
        socket.incoming.put_nowait('{"post_type":"message","message_id":1}')
        await started.wait()
        socket.incoming.put_nowait('{"post_type":"message","message_id":2}')
        socket.incoming.put_nowait('{"post_type":"message","message_id":3}')
        task = asyncio.create_task(client.request("get_login_info", {}))
        request = await socket.sent.get()
        socket.reply(request, {"user_id": 1})
        assert (await asyncio.wait_for(task, 1)).response["data"] == {"user_id": 1}
        assert gaps == ["input_queue_full"]
        stop = asyncio.create_task(client.stop_inputs())
        await asyncio.sleep(0)
        assert not stop.done()
        release.set()
        await stop
        assert gaps[-1] == "ingress_stopped"
        await client.close()

    asyncio.run(run())


def test_reverse_reconnect_queries_listener_and_generation_replacement() -> None:
    async def run() -> None:
        connector = Connector()
        client = QQClient(QQSettings(), connector, ignore_event, ignore_gap)
        await client.start()
        assert (await client.reconnect("read-only"))["state"] == "waiting"
        assert connector.accepted is not None
        accepted = connector.accepted

        async def accept(connection: Connection) -> None:
            await accepted(connection)

        first, second = Socket(), Socket()
        first_reader = asyncio.create_task(accept(first))
        await client.wait_state("online")
        request_task = asyncio.create_task(client.request("send_private_msg", {"message": "x"}))
        request = await first.sent.get()
        second_reader = asyncio.create_task(accept(second))
        with pytest.raises(QQCallError) as failure:
            await request_task
        assert failure.value.phase == "unknown"
        assert failure.value.echo == request["echo"]
        await first_reader
        assert first.closed and client.generation == 2
        await client.stop_inputs()
        assert connector.stopped and not second.closed
        await client.close()
        await second_reader
        assert connector.closed

    asyncio.run(run())


def test_internal_fault_closes_other_resources_and_keeps_original() -> None:
    async def run() -> None:
        failure = AssertionError("协议内部错误")
        socket = Socket()
        connector = Connector((socket,))

        async def event(payload: JsonObject, generation: int) -> None:
            del payload, generation
            raise failure

        client = QQClient(QQSettings(mode="direct"), connector, event, ignore_gap)
        await client.start()
        await client.wait_state("online")
        socket.incoming.put_nowait('{"post_type":"message"}')
        await client.wait_state("failed")
        with pytest.raises(AssertionError) as caught:
            await client.close()
        assert caught.value is failure and socket.closed

    asyncio.run(run())


def test_short_connections_consume_budget_and_only_stable_period_resets() -> None:
    async def run() -> None:
        now = [1000.0]
        sockets = (Socket(), Socket(), Socket())
        connector = Connector(sockets)
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        client = QQClient(
            QQSettings(mode="direct"),
            connector,
            ignore_event,
            ignore_gap,
            monotonic=lambda: now[0],
            sleep=sleep,
        )
        await client.start()
        await client.wait_state("online")
        now[0] += 30
        connector.connected.clear()
        await sockets[0].close()
        await asyncio.wait_for(connector.connected.wait(), 2)
        assert client.snapshot()["generation"] == 2
        assert client.snapshot()["attempts"] == 2 and sleeps == [1.0]
        now[0] += 61
        connector.connected.clear()
        await sockets[1].close()
        await asyncio.wait_for(connector.connected.wait(), 2)
        assert client.snapshot()["generation"] == 3
        assert client.snapshot()["attempts"] == 1 and sleeps == [1.0]
        await client.close()

    asyncio.run(run())
