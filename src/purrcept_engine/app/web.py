"""将当前宿主的事实/有限控制与包内页面装配为一个本机 ASGI 服务。

服务不创建第二个 Engine，不因页面刷新或断线重新启动设备。CLI 是监听器
所有者；嵌入者可以明确构造 WebServer 借用已有 Host。HTTP 写操作直接进入
持久去重的 PanelController，观察取消不撤销业务，查询返回原真实回执。
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import socket
from collections.abc import Generator
from contextlib import contextmanager
from importlib.resources import files
from typing import Protocol

import uvicorn
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from purrcept_engine.app.local_http import LocalHttpBoundary
from purrcept_engine.app.panel import ControlRequest
from purrcept_engine.domain.controls.models import ControlError, ControlRecord
from purrcept_engine.errors import HostStateError, StorageError
from purrcept_engine.plugin_system.panels import PreviewFrame
from purrcept_engine.values import ActionProtocolError, JsonObject, decode_object


class PanelHost(Protocol):
    """服务借用的有限宿主端口；不授予任意行动、数据库或模型调用。"""

    async def query_panel(self) -> JsonObject:
        """读取真实运行状态；请求不打开设备。"""
        ...

    async def submit_control(self, request: ControlRequest) -> ControlRecord:
        """持久接管明确快捷操作，观察取消不撤销已受理工作。"""
        ...

    async def query_control(self, request_id: str) -> JsonObject | None:
        """读取接管及当前真实回执，查询不会重发控制。"""
        ...

    def query_preview(self, target: str) -> PreviewFrame | None:
        """只读当前缓存像素，缺失不额外采集。"""
        ...


def create_app(host: PanelHost, *, port: int, token: str) -> ASGIApp:
    """构造有限路由与固定来源边界，不监听或启动宿主。"""

    async def session(request: Request) -> Response:
        del request
        state = await host.query_panel()
        return _json({"token": token, "instance_id": state["instance_id"]})

    async def snapshot(request: Request) -> Response:
        del request
        return _json(await host.query_panel())

    async def submit(request: Request) -> Response:
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > 16 * 1024:
                return _json({"error": "control_size", "message": "控制参数超过接管上限。"}, 413)
            payload.extend(chunk)
        try:
            # 严格 JSON 拒绝重复字段和非有限数字，Pydantic 再校验实际入口。
            value = decode_object(bytes(payload))
            command = ControlRequest.model_validate(value)
        except (ActionProtocolError, ValidationError):
            return _json({"error": "control_arguments", "message": "控制字段无效。"}, 400)
        record = await host.submit_control(command)
        return _json(record.projection(), 202)

    async def result(request: Request) -> Response:
        record = await host.query_control(request.path_params["request_id"])
        return _json({"error": "control_missing"}, 404) if record is None else _json(record)

    async def preview(request: Request) -> Response:
        frame = host.query_preview(request.path_params["target"])
        if frame is None:
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        etag = '"' + hashlib.sha256(frame.data).hexdigest() + '"'
        headers = {
            "Cache-Control": "no-store",
            "ETag": etag,
            "X-Captured-At": str(frame.captured_at),
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(frame.data, media_type=frame.media_type, headers=headers)

    async def static(request: Request) -> Response:
        name = request.path_params.get("name", "index.html")
        # 白名单既约束打包入口，也拒绝路径穿越；不把磁盘配置/用户数据映射HTTP。
        types = {
            "index.html": "text/html",
            "panel.js": "text/javascript",
            "panel.css": "text/css",
            "purrcept.css": "text/css",
        }
        media_type = types.get(name)
        if media_type is None:
            return Response(status_code=404)
        content = files("purrcept_engine.app").joinpath("static", name).read_bytes()
        return Response(
            content,
            media_type=media_type,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'self'; connect-src 'self'; "
                "img-src 'self' blob:; style-src 'self'; script-src 'self'; "
                "object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            },
        )

    async def business_error(request: Request, error: Exception) -> Response:
        del request
        if isinstance(error, ControlError):
            return _json({"error": error.code, "message": str(error)}, 409)
        if isinstance(error, HostStateError):
            return _json({"error": "host_unavailable", "message": str(error)}, 503)
        if isinstance(error, StorageError):
            return _json({"error": "authority_unavailable", "message": "权威存储不可用。"}, 503)
        raise error

    app = Starlette(
        routes=[
            Route("/api/session", session),
            Route("/api/snapshot", snapshot),
            Route("/api/controls", submit, methods=["POST"]),
            Route("/api/controls/{request_id}", result),
            Route("/api/preview/{target}", preview),
            Route("/", static),
            Route("/assets/{name}", static),
        ],
        exception_handlers={
            ControlError: business_error,
            HostStateError: business_error,
            StorageError: business_error,
        },
    )
    return LocalHttpBoundary(app, port=port, token=token)


def _json(value: JsonObject, status: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})


class _LocalServer(uvicorn.Server):
    """只借用HTTP生命周期；CLI保留信号与二次Ctrl+C的进程所有权。"""

    def __init__(self, config: uvicorn.Config) -> None:
        """在当前循环保存监听就绪信号，不用定时轮询推断启动完成。"""
        super().__init__(config)
        self.ready = asyncio.Event()

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        """在原监听器真实启动之后发布就绪，启动失败仍由serve任务传出。"""
        await super().startup(sockets=sockets)
        if self.started:
            self.ready.set()

    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        """冻结Uvicorn0.39的公开信号钩子不安装或回放调用者信号处理器。"""
        yield


class WebServer:
    """CLI/嵌入者拥有的单个监听器，借用 Host 到请求实际收回后才关闭。

    server 不接管进程信号，正常关闭由同一个 app 生命周期调用。start先绑定
    回环并取得实际端口，再构造 Host/Origin 边界；失败收回本次socket。close
    停止监听并等待原HTTP请求，业务已接管任务仍由宿主自己的所有者持有。
    """

    def __init__(self, host: PanelHost, *, port: int) -> None:
        """保存有限宿主端口，不绑定网络或修改信号处理器。"""
        self.host, self.port = host, port
        self._socket: socket.socket | None = None
        self._server: _LocalServer | None = None
        self._task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        """返回实际已启动回环地址；未启动不能宣称URL可用。"""
        if self._socket is None or self._server is None or not self._server.started:
            raise HostStateError("Web服务尚未启动。")
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        """只启动一次本机服务，不启动新的Host或设备。"""
        if self._task is not None or self._close_task is not None:
            raise HostStateError("Web服务不能重复启动。")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", self.port))
            listener.listen(64)
            listener.setblocking(False)
            self.port = listener.getsockname()[1]
            app = create_app(self.host, port=self.port, token=secrets.token_urlsafe(32))
            self._socket = listener
            self._server = _LocalServer(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=self.port,
                    proxy_headers=False,
                    server_header=False,
                    date_header=False,
                    lifespan="off",
                    log_config=None,
                    access_log=False,
                    ws="none",
                    limit_concurrency=64,
                    timeout_graceful_shutdown=0,
                )
            )
            # 关闭HTTP只结束页面观察。已接管业务由Host的shield所有者继续收尾，
            # 不给长HTTP等待另开一个关闭期限或重复派发的机会。
            self._task = asyncio.create_task(
                self._server.serve(sockets=[listener]), name="engine-web"
            )
            ready = asyncio.create_task(self._server.ready.wait())
            try:
                await asyncio.wait((ready, self._task), return_when=asyncio.FIRST_COMPLETED)
            finally:
                ready.cancel()
                await asyncio.gather(ready, return_exceptions=True)
            if self._task.done():
                await self._task
                raise HostStateError("Web服务未进入监听状态。")
        except BaseException:
            if self._task is not None:
                await self.close()
            else:
                listener.close()
            raise

    async def close(self) -> None:
        """等待同一次监听收尾，取消等待不解除HTTP请求和Host借用的所有者。"""
        if self._close_task is None:
            if self._server is not None:
                self._server.should_exit = True
            self._close_task = asyncio.create_task(self._close(), name="engine-web-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        """先收回请求任务，再释放本次取得的socket；不关闭借用Host。"""
        try:
            if self._task is not None:
                await self._task
        finally:
            if self._socket is not None:
                self._socket.close()
                self._socket = None
