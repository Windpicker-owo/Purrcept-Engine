"""沿既有 Python websockets asyncio API 实现 OneBot 的实际传输。

QQ 工厂声明不导入本模块；仅启用后的 build 选择此适配器。direct 一次 await
建连，不使用 SDK 的无限恢复迭代器；恢复次数归 client。reverse Bearer 只校验
传输接入，不建立发言人权限。停止监听保留既有连接，最终 close 再收回连接。
"""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import Awaitable, Callable
from typing import cast
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.client import connect
from websockets.asyncio.connection import Connection as WebSocketConnection
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from purrcept_engine.plugins.qq.ports import (
    Connection,
    ConnectionRejected,
    ConnectionUnavailable,
    Listener,
)
from purrcept_engine.plugins.qq.settings import QQSettings


class _Connection:
    """借用一个实际 SDK 连接，仅翻译可识别的外部传输错误。"""

    def __init__(self, connection: WebSocketConnection) -> None:
        """借用已完成握手的 SDK 对象，不重新连接。

        Args:
            connection: 当前所有者实际取得的协议连接。
        """
        self.connection = connection

    async def send(self, data: str) -> None:
        """一次完整 SDK 帧发送，断线保留为发送阶段未知。

        Args:
            data: 当前边界接收的原始协议或媒体内容。

        Raises:
            ConnectionUnavailable: QQ WebSocket 在发送阶段断开。
        """
        try:
            await self.connection.send(data)
        except ConnectionClosed as error:
            raise ConnectionUnavailable("QQ WebSocket 在发送阶段断开。") from error

    async def receive(self) -> str | bytes:
        """识别明确协议/凭据关闭，其余网络结束交有限连接恢复。

        Returns:
            当前原生连接实际交付的文本或二进制消息。

        Raises:
            ConnectionRejected: 参数、材料或当前状态不满足此入口的约束。
            ConnectionUnavailable: QQ WebSocket 已断开。
        """
        try:
            return await self.connection.recv()
        except ConnectionClosed as error:
            if error.rcvd is not None and error.rcvd.code in {1002, 1007, 1008, 1009, 4401}:
                raise ConnectionRejected(
                    f"QQ WebSocket 协议关闭，code={error.rcvd.code}。"
                ) from error
            raise ConnectionUnavailable("QQ WebSocket 已断开。") from error

    async def close(self) -> None:
        """等待 SDK 真正完成关闭，不以取消读取表示连接已归还。"""
        await self.connection.close()


class _Listener:
    """SDK close(False) 只执行一次，最终须显式关闭它仍持有的实际连接。"""

    def __init__(self, server: Server) -> None:
        """保存由 SDK 已取得的真实监听。

        Args:
            server: 当前所有者创建的 WebSocket 服务端资源。
        """
        self.server = server

    @property
    def port(self) -> int:
        """只从实际绑定 socket 查询端口，不返回配置值伪造监听成功。

        Raises:
            RuntimeError: QQ reverse 监听缺少实际绑定套接字；QQ reverse 监听地址形状异常。
        """
        sockets = tuple(self.server.sockets or ())
        if not sockets:
            raise RuntimeError("QQ reverse 监听缺少实际绑定套接字。")
        address: object = sockets[0].getsockname()
        if not isinstance(address, tuple):
            raise RuntimeError("QQ reverse 监听地址形状异常。")
        parts = cast(tuple[object, ...], address)
        if len(parts) < 2 or not isinstance(parts[1], int):
            raise RuntimeError("QQ reverse 监听地址形状异常。")
        return parts[1]

    async def stop_accepting(self) -> None:
        """停止新握手，已开始调用仍可使用当前连接收 echo。"""
        self.server.close(close_connections=False)

    async def close(self) -> None:
        """显式关闭剩余连接后等待服务器的真实结束。"""
        self.server.close(close_connections=False)
        if self.server.connections:
            await asyncio.gather(*(connection.close() for connection in self.server.connections))
        await self.server.wait_closed()


class WebSocketConnector:
    """QQ build 创建的冻结 SDK 适配器，只沿同一套 SnowLuma OneBot WebSocket 契约。"""

    async def connect(self, settings: QQSettings) -> Connection:
        """连接配置端点，显式停用环境代理。

        Args:
            settings: 当前组件已校验的配置或纯声明。

        Returns:
            当前尝试实际取得的原生连接，由创建它的所有者最终关闭。

        Raises:
            ConnectionUnavailable: QQ direct 网络或服务暂时不可用；参数、
                材料或当前状态不满足此入口的约束。
            ConnectionRejected: 参数、材料或当前状态不满足此入口的约束；QQ direct WebSocket
                协议握手无效。

        Note:
            协议与凭据错误不被包装成临时离线。
        """
        host = settings.host
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        token = settings.access_token.get_secret_value()
        headers = {} if not token else {"Authorization": "Bearer " + token}
        try:
            socket = await connect(
                f"ws://{host}:{settings.port}",
                additional_headers=headers,
                proxy=None,
                open_timeout=settings.connect_timeout_seconds,
                max_size=settings.frame_bytes,
                max_queue=settings.inbox_size,
                close_timeout=settings.connect_timeout_seconds,
            )
        except (OSError, TimeoutError, EOFError) as error:
            raise ConnectionUnavailable("QQ direct 网络或服务暂时不可用。") from error
        except InvalidStatus as error:
            code = error.response.status_code
            if code in {500, 502, 503, 504}:
                raise ConnectionUnavailable(f"QQ direct 服务暂时不可用，HTTP {code}。") from error
            raise ConnectionRejected(f"QQ direct 握手被拒绝，HTTP {code}。") from error
        except InvalidHandshake as error:
            raise ConnectionRejected("QQ direct WebSocket 协议握手无效。") from error
        return _Connection(socket)

    async def listen(
        self, settings: QQSettings, accepted: Callable[[Connection], Awaitable[None]]
    ) -> Listener:
        """reverse 只建立监听。

        Args:
            settings: 当前组件已校验的配置或纯声明。
            accepted: 接受新连接时移交实际连接所有权的异步函数。

        Returns:
            当前所有者创建的反向连接监听资源。

        Note:
            成功监听不能声明外部 QQ 已经接入。
        """
        token = settings.access_token.get_secret_value()

        async def received(socket: ServerConnection) -> None:
            """校验反向连接的实际请求，再交给当前连接所有者。"""
            request = socket.request
            if request is None:
                raise RuntimeError("QQ reverse SDK 缺少完成握手的 request。")
            header = request.headers.get("Authorization", "")
            supplied = parse_qs(urlsplit(request.path).query).get("access_token", [])
            authorized = (
                not token
                or hmac.compare_digest(header, "Bearer " + token)
                or (len(supplied) == 1 and hmac.compare_digest(supplied[0], token))
            )
            if not authorized:
                await socket.close(code=4401, reason="unauthorized")
                return
            await accepted(_Connection(socket))

        server = await serve(
            received,
            settings.host,
            settings.port,
            open_timeout=settings.connect_timeout_seconds,
            close_timeout=settings.connect_timeout_seconds,
            max_size=settings.frame_bytes,
            max_queue=settings.inbox_size,
        )
        return _Listener(server)
