"""Phone 独立 HTTPS/WSS 监听器，借用同一个插件 Runtime。

配对邀请由监听器拥有，仅向本机 WebUI 专用端口或启动时的本地导出交付，
不进入日志或模型材料。每条认证连接拥有唯一发送锁，聊天事件按持久游标补送；设备命令通过同一连接发送但不进入补送日志。
监听器的启动和关闭由 PluginRuntime 生命周期持有，不创建第二个 Engine。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import secrets
import socket as sockets
import ssl
import time
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING
from uuid import uuid4

import uvicorn
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from purrcept_engine.errors import HostStateError, StorageError
from purrcept_engine.plugins.phone.protocol import (
    ChatSubmission,
    Cursor,
    DeviceResult,
    DeviceState,
    Frame,
    Pairing,
)
from purrcept_engine.plugins.phone.runtime import PhoneRuntime
from purrcept_engine.values import JsonObject, decode_object, encode_json

if TYPE_CHECKING:
    from collections.abc import Generator

    from purrcept_engine.plugins.phone.plugin import PhoneSettings


def token_hash(token: str) -> str:
    """生成仅供设备认证的摘要，原始 token 不进入数据库。"""
    return hashlib.sha256(token.encode()).hexdigest()


class SocketPeer:
    """单条认证 WebSocket 的发送所有者，跨业务发送共用一个异步锁。"""

    def __init__(self, socket: WebSocket, device_id: str) -> None:
        """为当前连接分配短期身份，不建立设备操控资格。"""
        self.socket, self.device_id = socket, device_id
        self.connection_id = uuid4().hex
        self._send_lock = asyncio.Lock()

    async def send(self, kind: str, payload: JsonObject, reply_to: str | None = None) -> None:
        """保持帧原子顺序，网络失败交回调用方。"""
        frame = Frame(
            type=kind,
            id=uuid4().hex,
            reply_to=reply_to,
            connection_id=self.connection_id,
            payload=payload,
        )
        async with self._send_lock:
            if self.socket.application_state != WebSocketState.CONNECTED:
                raise ConnectionError("手机连接已经结束。")
            try:
                await self.socket.send_text(frame.model_dump_json())
            except WebSocketDisconnect as error:
                raise ConnectionError("手机连接已断开。") from error


class PhoneServer:
    """插件持有的监听器与一次性配对窗口，不拥有业务数据库。"""

    def __init__(
        self, runtime: PhoneRuntime, settings: PhoneSettings, *, invitation: str | None = None
    ) -> None:
        """构造未监听路由；邀请缺省随机，也可由应用明确注入，不能进入日志。"""
        self.runtime, self.settings = runtime, settings
        self._invitation = secrets.token_urlsafe(32) if invitation is None else invitation
        self._invitation_deadline = time.monotonic() + 120 if settings.pairing_enabled else 0
        self._expires_at = time.time() + 120 if settings.pairing_enabled else 0
        self._ready = False
        self._pair_attempts = 0
        self._pair_lock = asyncio.Lock()
        self._pairing_epoch = 0
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._listener: sockets.socket | None = None
        self._owns_invitation = False
        self.app = Starlette(
            routes=[
                Route("/phone/v1/pair", self.pair, methods=["POST"]),
                Route("/phone/v1/state", self.state),
                Route("/phone/v1/chat", self.history),
                Route("/phone/v1/media", self.upload, methods=["POST"]),
                Route("/phone/v1/media/{media_id}", self.media),
                WebSocketRoute("/phone/v1/channel", self.channel),
            ]
        )

    async def _device(self, authorization: str) -> str | None:
        if not authorization.startswith("Bearer ") or len(authorization) > 256:
            return None
        return await self.runtime.store.authenticate(token_hash(authorization[7:]))

    async def pair(self, request: Request) -> JSONResponse:
        """消费短时邀请；单设备限制和凭据写入由同库事务核验。"""
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 4096:
                return JSONResponse({"error": "frame_too_large"}, 413)
        try:
            value = Pairing.model_validate(decode_object(bytes(data)))
        except (ValueError, ValidationError):
            return JSONResponse({"error": "invalid_pairing"}, 400)
        async with self._pair_lock:
            self._pair_attempts += 1
            allowed = (
                not self.runtime.stopping
                and self._pair_attempts <= 10
                and time.monotonic() < self._invitation_deadline
                and secrets.compare_digest(value.invitation, self._invitation)
            )
            if not allowed:
                return JSONResponse({"error": "invitation_expired"}, 403)
            device_id, token = uuid4().hex, secrets.token_urlsafe(48)
            try:
                await self.runtime.store.pair(device_id, value.name, token_hash(token))
            except StorageError as error:
                if error.code != "phone_already_paired":
                    raise
                return JSONResponse({"error": error.code}, 409)
            self._invitation_deadline = 0
            if self._owns_invitation:
                self.settings.invitation_file.unlink(missing_ok=True)
                self._owns_invitation = False
            return JSONResponse(
                {"v": 1, "device_id": device_id, "token": token},
                headers={"Cache-Control": "no-store"},
            )

    async def state(self, request: Request) -> JSONResponse:
        """认证后读取当前手机状态，查询不会截图或建立操作会话。"""
        device_id = await self._device(request.headers.get("authorization", ""))
        if device_id is None:
            return JSONResponse({"error": "unauthorized"}, 401)
        return JSONResponse(self.runtime.snapshot(), headers={"Cache-Control": "no-store"})

    async def upload(self, request: Request) -> JSONResponse:
        """接管有界完整图片，不接受客户端路径或远程 URL。"""
        device = await self._device(request.headers.get("authorization", ""))
        if device is None:
            return JSONResponse({"error": "unauthorized"}, 401)
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 8 * 1024 * 1024:
                return JSONResponse({"error": "media_too_large"}, 413)
        try:
            with Image.open(io.BytesIO(content)) as image:
                if image.width * image.height > 32_000_000 or image.format not in ("JPEG", "PNG"):
                    return JSONResponse({"error": "unsupported_media"}, 415)
                media_type = "image/jpeg" if image.format == "JPEG" else "image/png"
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            return JSONResponse({"error": "invalid_media"}, 415)
        media_id = await self.runtime.store.put_media(device, bytes(content), media_type)
        return JSONResponse({"media_id": media_id, "media_type": media_type})

    async def media(self, request: Request) -> Response:
        """认证设备只能读取自己的附件。"""
        device = await self._device(request.headers.get("authorization", ""))
        if device is None:
            return JSONResponse({"error": "unauthorized"}, 401)
        asset = await self.runtime.store.media(device, request.path_params["media_id"])
        if asset is None:
            return JSONResponse({"error": "media_missing"}, 404)
        return Response(
            asset.content, media_type=asset.media_type, headers={"Cache-Control": "no-store"}
        )

    async def history(self, request: Request) -> JSONResponse:
        """按本设备游标读取有界历史，不授予其他设备的记录。"""
        device_id = await self._device(request.headers.get("authorization", ""))
        if device_id is None:
            return JSONResponse({"error": "unauthorized"}, 401)
        try:
            after = int(request.query_params.get("after", "0"))
            if after < 0:
                raise ValueError("游标不能为负。")
        except ValueError:
            return JSONResponse({"error": "invalid_cursor"}, 400)
        events = await self.runtime.store.events(device_id, after)
        return JSONResponse({"events": events}, headers={"Cache-Control": "no-store"})

    async def channel(self, socket: WebSocket) -> None:
        """认证及 hello 完成后并行补送聊天和接收回包，退出必收回发送任务。"""
        epoch = self._pairing_epoch
        device_id = await self._device(socket.headers.get("authorization", ""))
        if device_id is None or epoch != self._pairing_epoch:
            await socket.close(code=4401)
            return
        await socket.accept()
        if epoch != self._pairing_epoch:
            await socket.close(code=4401)
            return
        peer = SocketPeer(socket, device_id)
        sender: asyncio.Task[None] | None = None
        try:
            self.runtime.attach(peer)
            async with asyncio.timeout(10):
                hello = await self._receive(socket)
            if hello.type != "hello":
                await socket.close(code=4400)
                return
            cursor = Cursor.model_validate(hello.payload).through
            while await self.runtime.store.recover_inputs(device_id) == 100:
                pass
            # 服务端也验证恢复游标；不能让客户端静默跳过未取得的未来事件。
            await self.runtime.store.acknowledge(device_id, cursor)
            await peer.send(
                "welcome",
                {"device_id": device_id, "server_time": time.time(), "features": ["engine_state"]},
                hello.id,
            )
            sender = asyncio.create_task(self.runtime.deliver(peer, cursor), name="phone-delivery")
            for command in await self.runtime.store.commands():
                intent = command["intent"]
                if isinstance(intent, dict) and intent.get("device_id") == device_id:
                    await peer.send("command.query", {"command_id": command["command_id"]})
            while True:
                # 接收与发送任一失败都结束连接，不把发送故障留到下次用户输入才发现。
                receiver = asyncio.create_task(self._receive(socket))
                try:
                    done, _ = await asyncio.wait(
                        (receiver, sender), return_when=asyncio.FIRST_COMPLETED
                    )
                    if sender in done:
                        await sender
                    frame = await receiver
                finally:
                    if not receiver.done():
                        receiver.cancel()
                    with suppress(asyncio.CancelledError):
                        await receiver
                if frame.connection_id != peer.connection_id:
                    raise ValueError("旧连接帧不能复用。")
                try:
                    await self._dispatch(peer, frame)
                except StorageError as error:
                    if error.code in ("invalid_record", "unsupported_format"):
                        raise
                    await peer.send("error", {"code": error.code}, frame.id)
        except (ValueError, ValidationError, HostStateError, TimeoutError):
            if socket.application_state == WebSocketState.CONNECTED:
                await socket.close(code=4400)
        except (WebSocketDisconnect, ConnectionError):
            pass
        finally:
            self.runtime.detach(peer)
            if sender is not None:
                sender.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect, ConnectionError):
                    await sender

    async def _receive(self, socket: WebSocket) -> Frame:
        raw = await socket.receive_text()
        if len(raw.encode()) > 65536:
            raise ValueError("手机帧超过上限。")
        return Frame.model_validate(decode_object(raw.encode()))

    async def _dispatch(self, peer: SocketPeer, frame: Frame) -> None:
        """只允许固定手机协议操作，客户端不具有任意 Engine 控制入口。"""
        if self.runtime.peer is not peer:
            raise HostStateError("旧手机连接已撤销。")
        if frame.type == "chat.submit":
            result = await self.runtime.submit_chat(
                peer.device_id, ChatSubmission.model_validate(frame.payload)
            )
            await peer.send("chat.accepted", result, frame.id)
        elif frame.type == "sync.ack":
            await self.runtime.store.acknowledge(
                peer.device_id, Cursor.model_validate(frame.payload).through
            )
        elif frame.type == "device.state":
            state = DeviceState.model_validate(frame.payload)
            self.runtime.set_device_state(peer, decode_object(state.model_dump_json().encode()))
        elif frame.type == "device.result":
            result = DeviceResult.model_validate(frame.payload)
            await self.runtime.receive_result(
                peer, decode_object(result.model_dump_json(exclude_none=True).encode())
            )
        else:
            await peer.send("error", {"code": "unsupported_type"}, frame.id)

    async def start(self) -> None:
        """取得 TLS 监听器，实际启动成功才写本地配对邀请。"""

        class Server(uvicorn.Server):
            @contextmanager
            def capture_signals(self) -> Generator[None]:
                yield

        config = uvicorn.Config(
            self.app,
            host=self.settings.listen_host,
            port=self.settings.port,
            ssl_certfile=str(self.settings.certificate),
            ssl_keyfile=str(self.settings.private_key),
            access_log=False,
            log_config=None,
            ws_max_size=65536,
            timeout_graceful_shutdown=5,
        )
        self._server = Server(config)
        # 应用先取得实际端口，冲突以 OSError 返回宿主；不能让 uvicorn 的
        # 默认绑定分支调用 sys.exit() 终止嵌入进程。
        family = sockets.AF_INET6 if ":" in self.settings.listen_host else sockets.AF_INET
        listener = sockets.socket(family, sockets.SOCK_STREAM)
        try:
            listener.bind((self.settings.listen_host, self.settings.port))
            listener.listen(32)
            listener.setblocking(False)
        except OSError:
            listener.close()
            raise
        self._listener = listener
        self._task = asyncio.create_task(
            self._server.serve(sockets=[listener]), name="phone-server"
        )
        async with asyncio.timeout(10):
            while not self._server.started:
                if self._task.done():
                    await self._task
                    raise HostStateError("手机监听器未启动。")
                await asyncio.sleep(0.01)
        self._ready = True
        if self.settings.pairing_enabled and not await self.runtime.store.device():
            # 启动保留构造时明确注入的邀请；只有 WebUI 的刷新才轮换身份。
            self._invitation_deadline = time.monotonic() + 120
            self._expires_at = time.time() + 120
            value = await self.pairing()
            # 启动自动导出只服务命令行接入，WebUI 刷新不依赖该文件的读写。
            path = self.settings.invitation_file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                encode_json({key: item for key, item in value.items() if key != "status"})
            )
            path.chmod(0o600)
            self._owns_invitation = True

    async def pairing(self) -> JsonObject:
        """读取当前配对事实，普通页面查询不延长或重新生成邀请。"""
        device = await self.runtime.store.device()
        if device:
            return {
                "status": "paired",
                "name": device.get("name", "手机"),
                "device_id": device["device_id"],
            }
        if not self._ready or self.runtime.stopping:
            return {"status": "unavailable"}
        if self._invitation_deadline == 0:
            return {"status": "closed"}
        if time.monotonic() >= self._invitation_deadline:
            return {"status": "expired"}
        certificate = ssl.PEM_cert_to_DER_cert(
            self.settings.certificate.read_text(encoding="ascii")
        )
        return {
            "status": "ready",
            "v": 1,
            "url": self.settings.public_url,
            "certificate_sha256": hashlib.sha256(certificate).hexdigest(),
            "invitation": self._invitation,
            "expires_at": self._expires_at,
        }

    async def open_pairing(self) -> JsonObject:
        """本机用户明确开启 120 秒窗口，和手机消费共用锁，旧二维码立即失效。

        Returns:
            当前窗口或已经配对的实际设备；不撤销已有配对和操控授权。

        Raises:
            HostStateError: 监听器未就绪或插件正在关闭。
        """
        async with self._pair_lock:
            if not self._ready or self.runtime.stopping:
                raise HostStateError("Phone 监听器尚未就绪。")
            if await self.runtime.store.device():
                return await self.pairing()
            self._open_window()
            return await self.pairing()

    async def replace_pairing(self, expected_device_id: str) -> JsonObject:
        """本机用户确认后撤销旧手机并生成新的扫码窗口。

        Args:
            expected_device_id: 本机用户确认时看到的旧手机身份，不能撤销已被替换的配对。

        Returns:
            已完成凭据撤销后的新邀请。聊天、媒体和行动记录仍保留在 Engine。

        Raises:
            HostStateError: 监听器未就绪或插件正在关闭。
            StorageError: 撤销未能提交；不生成新邀请或声称已成功重新配对。

        Note:
            已派发效果不能被撤销；在途观察转为 unknown，不能向新手机重放。
        """
        async with self._pair_lock:
            if not self._ready or self.runtime.stopping:
                raise HostStateError("Phone 监听器尚未就绪。")
            device = await self.runtime.store.device()
            if not device or device.get("device_id") != expected_device_id:
                raise HostStateError("当前配对已改变，请重新读取后再确认。")
            # 先撤销连接借用再等待落盘，避免等待期间继续派发新操作。
            # epoch 同时拒绝在认证/accept 等待中跨过撤销边界的旧连接。
            self._pairing_epoch += 1
            self._invitation_deadline = 0
            peer = self.runtime.peer
            if peer is not None:
                self.runtime.detach(peer)
            try:
                await self.runtime.store.revoke()
            finally:
                # 即使落盘失败，也收回已经 detach 的网络借用；失败不会生成新邀请。
                if (
                    isinstance(peer, SocketPeer)
                    and peer.socket.application_state == WebSocketState.CONNECTED
                ):
                    try:
                        await peer.socket.close(code=4401)
                    except (WebSocketDisconnect, ConnectionError):
                        # 网络已断开无需再关闭；是否撤销凭据仍由上面的账本事务决定。
                        pass
            self._open_window()
            return await self.pairing()

    def _open_window(self) -> None:
        """配对锁内轮换一次性身份、期限与尝试次数，不修改长期设备凭据。"""
        self._invitation = secrets.token_urlsafe(32)
        self._invitation_deadline = time.monotonic() + 120
        self._expires_at = time.time() + 120
        self._pair_attempts = 0

    async def close(self) -> None:
        """结束设备连接和监听任务，再删除本次邀请。"""
        self._ready = False
        self._invitation_deadline = 0
        peer = self.runtime.peer
        if (
            isinstance(peer, SocketPeer)
            and peer.socket.application_state == WebSocketState.CONNECTED
        ):
            await peer.socket.close(code=1001)
        if self._server is not None:
            self._server.should_exit = True
        try:
            if self._task is not None:
                await self._task
        finally:
            if self._listener is not None:
                self._listener.close()
                self._listener = None
            if self._owns_invitation:
                self.settings.invitation_file.unlink(missing_ok=True)
                self._owns_invitation = False
