"""将当前宿主的事实、控制与真后台管理能力装配为一个本机 ASGI 服务。

服务不创建第二个 Engine，不因页面刷新或断线重新启动设备。CLI 是监听器
所有者；嵌入者可以明确构造 WebServer 借用已有 Host。HTTP 写操作直接进入
LocalHttpBoundary 保护下的持久去重控制与管理接口。
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import socket
import time
from collections.abc import Generator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
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
from purrcept_engine.plugin_system.panels import PairingOperation, PreviewFrame
from purrcept_engine.values import ActionProtocolError, JsonObject, decode_object

_MODEL_USAGE_WINDOWS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "all": None}


class PanelHost(Protocol):
    """服务借用的有限宿主端口；不授予任意 Python 代码执行。"""

    def query_health(self) -> JsonObject:
        """读取无需权威库的启动和组件健康。"""
        ...

    async def phone_pairing(
        self,
        target: str,
        *,
        operation: PairingOperation = "read",
        expected_device_id: str | None = None,
    ) -> JsonObject:
        """本机配对专用端口，查询不续期，明确写请求才开启短时邀请。"""
        ...

    async def query_panel(self) -> JsonObject:
        """读取真实运行状态，请求不打开设备。"""
        ...

    async def query_model_usage(self, *, since: float) -> JsonObject:
        """读取指定 Unix 时间戳以来的模型调用与 token 统计。"""
        ...

    async def query_reviews(self, before: str | None = None, *, kind: str = "action") -> JsonObject:
        """读取有限历史行动和下一页游标，不确认业务待办。"""
        ...

    async def query_review(self, action_id: str) -> JsonObject | None:
        """读取所选行动的完整审查材料及历史评价。"""
        ...

    async def query_review_media(self, action_id: str, media_id: str) -> tuple[bytes, str] | None:
        """只读审查刺激关联的原始媒体，不授予任意文件访问。"""
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


def create_app(
    host: PanelHost,
    *,
    port: int,
    token: str,
    config_path: Path | None = None,
    owner_data_dir: Path | None = None,
) -> ASGIApp:
    """构造有限路由与固定来源边界，不监听或启动宿主。"""

    async def health(request: Request) -> Response:
        del request
        return _json(host.query_health())

    async def session(request: Request) -> Response:
        del request
        state = host.query_health()
        return _json({"token": token, "instance_id": state["instance_id"]})

    async def snapshot(request: Request) -> Response:
        del request
        return _json(await host.query_panel())

    async def model_usage(request: Request) -> Response:
        """将相对时间范围转换为宿主账本的查询起点。"""
        period = request.query_params.get("period", "7d")
        if period not in _MODEL_USAGE_WINDOWS:
            return _json(
                {"error": "invalid_period", "message": "模型用量统计的时间范围无效。"}, 400
            )
        window = _MODEL_USAGE_WINDOWS[period]
        since = 0.0 if window is None else time.time() - window
        return _json(await host.query_model_usage(since=since))

    async def submit(request: Request) -> Response:
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > 16 * 1024:
                return _json({"error": "control_size", "message": "控制参数超过接管上限。"}, 413)
            payload.extend(chunk)
        try:
            value = decode_object(bytes(payload))
            command = ControlRequest.model_validate(value)
        except (ActionProtocolError, ValidationError):
            return _json({"error": "control_arguments", "message": "控制字段无效。"}, 400)
        record = await host.submit_control(command)
        return _json(record.projection(), 202)

    async def result(request: Request) -> Response:
        record = await host.query_control(request.path_params["request_id"])
        return _json({"error": "control_missing"}, 404) if record is None else _json(record)

    async def reviews(request: Request) -> Response:
        return _json(
            await host.query_reviews(
                request.query_params.get("before"), kind=request.query_params.get("kind", "action")
            )
        )

    async def review(request: Request) -> Response:
        record = await host.query_review(request.path_params["action_id"])
        return _json({"error": "review_missing"}, 404) if record is None else _json(record)

    async def review_media(request: Request) -> Response:
        value = await host.query_review_media(
            request.path_params["action_id"], request.path_params["media_id"]
        )
        if value is None:
            return Response(status_code=404)
        content, kind = value
        native = kind in {
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
            "audio/wav",
            "audio/x-wav",
            "audio/mpeg",
            "audio/ogg",
            "audio/webm",
            "audio/mp4",
        }
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if not native:
            headers["Content-Disposition"] = 'attachment; filename="stimulus.bin"'
        return Response(
            content, media_type=kind if native else "application/octet-stream", headers=headers
        )

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
                "img-src 'self' blob:; media-src 'self' blob:; style-src 'self'; "
                "script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
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

    from purrcept_engine.app.admin_api import admin_routes
    from purrcept_engine.app.phone_web import phone_routes

    app = Starlette(
        routes=[
            *phone_routes(host, config_path, owner_data_dir),
            *admin_routes(
                host,
                token=token,
                config_path=config_path,
                owner_data_dir=owner_data_dir,
            ),
            Route("/api/health", health),
            Route("/api/session", session),
            Route("/api/snapshot", snapshot),
            Route("/api/model-usage", model_usage),
            Route("/api/controls", submit, methods=["POST"]),
            Route("/api/controls/{request_id}", result),
            Route("/api/reviews", reviews),
            Route("/api/reviews/{action_id}", review),
            Route("/api/reviews/{action_id}/media/{media_id}", review_media),
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
        super().__init__(config)
        self.ready = asyncio.Event()

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            self.ready.set()

    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class WebServer:
    """CLI/嵌入者拥有的单个监听器，借用 Host 到请求实际收回后才关闭。"""

    def __init__(
        self,
        host: PanelHost,
        *,
        port: int,
        config_path: Path | None = None,
        owner_data_dir: Path | None = None,
    ) -> None:
        self.host, self.port = host, port
        self.config_path, self.owner_data_dir = config_path, owner_data_dir
        self._socket: socket.socket | None = None
        self._server: _LocalServer | None = None
        self._task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        if self._socket is None or self._server is None or not self._server.started:
            raise HostStateError("Web服务尚未启动。")
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        if self._task is not None or self._close_task is not None:
            raise HostStateError("Web服务不能重复启动。")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", self.port))
            listener.listen(64)
            listener.setblocking(False)
            self.port = listener.getsockname()[1]
            app = create_app(
                self.host,
                port=self.port,
                token=secrets.token_urlsafe(32),
                config_path=self.config_path,
                owner_data_dir=self.owner_data_dir,
            )
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
        if self._close_task is None:
            if self._server is not None:
                self._server.should_exit = True
            self._close_task = asyncio.create_task(self._close(), name="engine-web-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        try:
            if self._task is not None:
                await self._task
        finally:
            if self._socket is not None:
                self._socket.close()
                self._socket = None
