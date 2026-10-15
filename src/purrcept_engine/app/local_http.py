"""本机 HTTP 的来源边界，主面板与独立调音室共用同一校验机制。

监听地址并不足以约束浏览器来源。这里只接收真实回环客户端、当前端口的
Host 和同源页面；写入还需从同源会话接口取得的随机令牌。没有人员角色、
代理头信任或额外审批流程，业务版本仍由宿主具体入口再次核验。
"""

from __future__ import annotations

import ipaddress
import secrets
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class LocalHttpBoundary:
    """服务所有者创建的固定端口/会话边界，页面刷新不取得设备资源。

    同一实例复用 token；服务重启产生新值。反向代理和局域网不属于首版范围，
    因而只验证 TCP 客户端与原始 Host，不采信 Forwarded/X-Forwarded-*。
    """

    def __init__(self, app: ASGIApp, *, port: int, token: str) -> None:
        """保存实际已绑定端口及所有者生成的随机 ASCII 令牌。

        Args:
            app: 已装配的 ASGI 应用。
            port: 已绑定的本机 HTTP 端口。
            token: 当前本机控制实例的随机 ASCII 凭据。
        """
        self.app, self.token = app, token
        self.hosts = frozenset((f"127.0.0.1:{port}", f"localhost:{port}"))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """先核对来源，再放行有限路由。

        Args:
            scope: ASGI 提供的当前连接和请求元数据。
            receive: ASGI 提供的当前请求接收回调。
            send: ASGI 提供的当前请求发送回调。

        Note:
            拒绝不执行对应业务。
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        client = request.client
        host_values = request.headers.getlist("host")
        allowed = (
            client is not None
            and _loopback(client.host)
            and len(host_values) == 1
            and host_values[0].lower() in self.hosts
            and request.url.scheme == "http"
        )
        if not allowed:
            await _denied("当前服务只接受本机同端口访问。")(scope, receive, send)
            return

        origin = request.headers.get("origin")
        if origin is not None and not _same_origin(origin, host_values[0]):
            await _denied("请求来源不匹配，请从本机页面操作。")(scope, receive, send)
            return
        if request.headers.get("sec-fetch-site") == "cross-site":
            await _denied("跨站请求不被接管。")(scope, receive, send)
            return
        if request.method not in ("GET", "HEAD"):
            supplied = request.headers.get("x-purrcept-token", "")
            if not secrets.compare_digest(supplied.encode("utf-8"), self.token.encode("ascii")):
                await _denied("页面会话已失效，请刷新。")(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _same_origin(value: str, host: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "http"
            and parsed.netloc.lower() == host.lower()
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def _denied(message: str) -> JSONResponse:
    return JSONResponse({"error": "local_origin", "message": message}, status_code=403)
