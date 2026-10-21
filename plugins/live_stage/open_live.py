"""B站官方项目接入：单一场次、签名HTTP、双心跳和只读事件；不拥有账号登录。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from purrcept_engine.values import (
    ActionProtocolError,
    JsonObject,
    JsonValue,
    decode_object,
)

from .bilibili import MAX_PACKET, Danmaku, ProtocolError, frames, packet
from .credentials import OpenLiveCredentials
from .events import LiveEvent, parse_interaction

API_ROOT = "https://live-open.biliapi.com"
PROJECT_HEARTBEAT_SECONDS = 20.0
WS_HEARTBEAT_SECONDS = 30.0
WS_PONG_TIMEOUT_SECONDS = 75.0
RECEIVE_POLL_SECONDS = 1.0
RETRY_BASE_SECONDS = 1.0


class OpenLiveFailure(RuntimeError):
    """公开固定阶段及数字错误码，不保存平台原文或请求对象。"""

    def __init__(self, status: str, code: int | None = None) -> None:
        self.status, self.code = status, code
        super().__init__(status if code is None else f"{status}:{code}")


def signed_request(
    credentials: OpenLiveCredentials, body: JsonObject, *, timestamp: int, nonce: str
) -> tuple[bytes, dict[str, str]]:
    """生成官方HMAC请求，签名与实际发送正文使用完全相同的字节。

    Args:
        credentials: 仅在本机参与计算的项目凭据。
        body: 对应官方接口的JSON正文。
        timestamp: 当前Unix秒数。
        nonce: 每次请求新的随机数，测试可传固定值。

    Returns:
        UTF-8正文及HTTP头；二者含认证材料，调用者不得记录。
    """
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    headers = {
        "x-bili-accesskeyid": credentials.access_key_id,
        "x-bili-content-md5": hashlib.md5(encoded).hexdigest(),
        "x-bili-signature-method": "HMAC-SHA256",
        "x-bili-signature-nonce": nonce,
        "x-bili-signature-version": "1.0",
        "x-bili-timestamp": str(timestamp),
    }
    canonical = "\n".join(f"{key}:{headers[key]}" for key in sorted(headers))
    headers["Authorization"] = hmac.new(
        credentials.access_key_secret.encode(), canonical.encode(), hashlib.sha256
    ).hexdigest()
    headers.update({"Content-Type": "application/json", "Accept": "application/json"})
    return encoded, headers


def _object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        raise ProtocolError("官方响应对象无效。")
    return value


def _text(value: JsonValue, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ProtocolError("官方响应文本字段无效。")
    return value


def parse_open_danmaku(body: bytes, room_id: int) -> Danmaku | None:
    """只将指定房间的官方普通文字弹幕转换为既有输入值。

    Args:
        body: 已解包的官方JSON事件。
        room_id: 用户固定授权接收的房间。

    Returns:
        带原生消息ID及open_id命名空间的弹幕；其他事件/表情返回None。

    Raises:
        ProtocolError: 房间不符或普通弹幕缺少合法字段。
    """
    value = decode_object(body)
    if value.get("cmd") != "LIVE_OPEN_PLATFORM_DM":
        return None
    data = _object(value.get("data"))
    if type(data.get("room_id")) is not int or data["room_id"] != room_id:
        raise ProtocolError("官方弹幕房间不匹配。")
    if data.get("dm_type", 0) != 0:
        return None
    timestamp = data.get("timestamp")
    if type(timestamp) is not int or not 0 < timestamp < 32_503_680_000:
        raise ProtocolError("官方弹幕时间无效。")
    return Danmaku(
        _text(data.get("uname"), 128),
        "open_id:" + _text(data.get("open_id"), 256),
        _text(data.get("msg"), 4000),
        float(timestamp),
        "open_live:" + _text(data.get("msg_id"), 256),
    )


@dataclass(frozen=True, slots=True)
class _Session:
    game_id: str = field(repr=False)
    auth_body: str = field(repr=False)
    links: tuple[str, ...]


class OpenLiveSource:
    """独占一个官方项目场次；WS重连复用场次，关闭负责end与资源归还。"""

    def __init__(
        self,
        room_id: int,
        app_id: int,
        credentials: Callable[[], OpenLiveCredentials],
        status: Callable[[str], None],
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """保存显式依赖；构造不取密钥、不联网。

        Args:
            room_id: 必须与start返回结果相符的房间。
            app_id: 用户已有的官方项目ID。
            credentials: 仅在messages消费开始时读取选定项目的回调。
            status: 只接收无秘密状态码的投影回调。
            http_transport: 离线HTTP替身；正常运行使用默认传输。
        """
        self.room_id, self.app_id = room_id, app_id
        self._credentials, self.status = credentials, status
        self._transport = http_transport
        self._game_id: str | None = None
        self._active = False

    async def _api(
        self,
        client: httpx.AsyncClient,
        credentials: OpenLiveCredentials,
        action: str,
        body: JsonObject,
    ) -> JsonObject:
        """只调用三个固定官方端点；不自动重放结果未知的start。"""
        if action not in {"start", "heartbeat", "end"}:
            raise ProtocolError("官方操作不在许可范围。")
        encoded, headers = signed_request(
            credentials, body, timestamp=int(time.time()), nonce=uuid4().hex
        )
        try:
            async with client.stream(
                "POST", f"{API_ROOT}/v2/app/{action}", content=encoded, headers=headers
            ) as response:
                if response.status_code != 200:
                    stage = "start_outcome_unknown" if action == "start" else "api_http_error"
                    if response.status_code in (401, 403, 412, 429):
                        stage = "authorization_required"
                    raise OpenLiveFailure(stage, response.status_code)
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_PACKET:
                        raise ProtocolError("官方响应超限。")
                    chunks.append(chunk)
        except asyncio.CancelledError:
            if action == "start":
                self.status("start_outcome_unknown")
            raise
        except httpx.TransportError:
            raise OpenLiveFailure(
                "start_outcome_unknown" if action == "start" else "api_transport_error"
            ) from None
        try:
            data = decode_object(b"".join(chunks))
        except ActionProtocolError:
            raise ProtocolError("官方响应不是合法JSON对象。") from None
        code = data.get("code")
        if type(code) is not int:
            raise ProtocolError("官方响应缺少错误码。")
        if code != 0:
            if action == "end" and code in (7000, 7003):
                return {}
            raise OpenLiveFailure("session_expired" if code == 7003 else "api_rejected", code)
        return data

    async def _start(self, client: httpx.AsyncClient, credentials: OpenLiveCredentials) -> _Session:
        reply = await self._api(
            client,
            credentials,
            "start",
            {"app_id": self.app_id, "code": credentials.anchor_code},
        )
        data = _object(reply.get("data"))
        game = _text(_object(data.get("game_info")).get("game_id"), 512)
        # 尽早登记已创建场次；后续房间/WS校验失败也必须尝试end。
        self._game_id = game
        anchor = _object(data.get("anchor_info"))
        if type(anchor.get("room_id")) is not int or anchor["room_id"] != self.room_id:
            raise OpenLiveFailure("room_mismatch")
        websocket = _object(data.get("websocket_info"))
        auth = _text(websocket.get("auth_body"), 16384)
        auth_value = decode_object(auth.encode())
        version = auth_value.get("protover", 0)
        if type(version) is not int or version not in (0, 1, 2):
            raise ProtocolError("官方场次要求尚不支持的压缩版本。")
        links = websocket.get("wss_link")
        if not isinstance(links, list) or not 1 <= len(links) <= 16:
            raise ProtocolError("官方WS地址列表无效。")
        valid: list[str] = []
        for item in links:
            link = _text(item, 2048)
            try:
                url = urlsplit(link)
                port = url.port
            except ValueError:
                raise ProtocolError("官方WS地址格式无效。") from None
            if (
                url.scheme != "wss"
                or not url.hostname
                or not url.hostname.endswith(".chat.bilibili.com")
                or url.username is not None
                or url.password is not None
                or port not in (None, 443)
                or url.path != "/sub"
                or url.query
                or url.fragment
            ):
                raise ProtocolError("官方WS地址不在允许范围。")
            valid.append(link)
        return _Session(game, auth, tuple(valid))

    async def _heartbeat(
        self, client: httpx.AsyncClient, credentials: OpenLiveCredentials, game_id: str
    ) -> None:
        while True:
            await asyncio.sleep(PROJECT_HEARTBEAT_SECONDS)
            await self._api(client, credentials, "heartbeat", {"game_id": game_id})

    async def messages(self) -> AsyncGenerator[LiveEvent, None]:
        """创建一个场次并产生实际弹幕；鉴权错误停止，不切换匿名源。

        Yields:
            已校验、与固定房间一致的普通弹幕。

        Raises:
            CredentialError: 明确选择的项目凭据缺失或无效。
            OpenLiveFailure: 官方拒绝、心跳失效或start结果未知。
            ProtocolError: 官方协议/房间校验失败。

        Note:
            关闭生成器会取消心跳、关闭WS并尝试end；end失败通过
            session_end_uncertain状态保留，不掩盖原始错误。Python字符串无法保证内存清零。
        """
        if self._active:
            raise OpenLiveFailure("source_already_active")
        self._active = True
        heartbeat: asyncio.Task[None] | None = None
        try:
            credentials = self._credentials()
            async with httpx.AsyncClient(
                timeout=10,
                trust_env=False,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                try:
                    self.status("starting")
                    session = await self._start(client, credentials)
                    heartbeat = asyncio.create_task(
                        self._heartbeat(client, credentials, session.game_id),
                        name="open-live-project-heartbeat",
                    )
                    retry = 0
                    while True:
                        if heartbeat.done():
                            heartbeat.result()
                        self.status("connecting" if retry == 0 else "reconnecting")
                        try:
                            async with connect(
                                session.links[retry % len(session.links)],
                                proxy=None,
                                ping_interval=None,
                                max_size=MAX_PACKET,
                                max_queue=16,
                                open_timeout=10,
                                close_timeout=3,
                                logger=logging.Logger("live_stage.open_live.transport", level=100),
                            ) as ws:
                                await ws.send(packet(7, session.auth_body.encode()))
                                authenticated = False
                                next_beat = last_pong = time.monotonic()
                                auth_deadline = last_pong + 10
                                while True:
                                    if heartbeat.done():
                                        heartbeat.result()
                                    now = time.monotonic()
                                    if not authenticated and now >= auth_deadline:
                                        raise OpenLiveFailure("ws_auth_timeout")
                                    if authenticated and now >= next_beat:
                                        await ws.send(packet(2, b"{}"))
                                        next_beat = now + WS_HEARTBEAT_SECONDS
                                    if now - last_pong > WS_PONG_TIMEOUT_SECONDS:
                                        raise TimeoutError
                                    try:
                                        raw = await asyncio.wait_for(
                                            ws.recv(), RECEIVE_POLL_SECONDS
                                        )
                                    except TimeoutError:
                                        continue
                                    if not isinstance(raw, bytes):
                                        raise ProtocolError("官方WS预期二进制帧。")
                                    for operation, body in frames(raw):
                                        if operation == 8:
                                            code = decode_object(body).get("code")
                                            if type(code) is not int or code != 0:
                                                raise OpenLiveFailure(
                                                    "authorization_required",
                                                    code if type(code) is int else None,
                                                )
                                            authenticated = True
                                            self.status("connected")
                                        elif operation == 3:
                                            last_pong = time.monotonic()
                                        elif operation == 5:
                                            if not authenticated:
                                                raise ProtocolError("官方WS鉴权前发送业务帧。")
                                            event = decode_object(body)
                                            if (
                                                event.get("cmd")
                                                == "LIVE_OPEN_PLATFORM_INTERACTION_END"
                                            ):
                                                if (
                                                    _object(event.get("data")).get("game_id")
                                                    == session.game_id
                                                ):
                                                    raise OpenLiveFailure("session_expired")
                                            message: LiveEvent | None = parse_open_danmaku(
                                                body, self.room_id
                                            )
                                            if message is None:
                                                message = parse_interaction(body, self.room_id)
                                            if message is not None:
                                                yield message
                        except (
                            ConnectionClosed,
                            InvalidHandshake,
                            OSError,
                            TimeoutError,
                        ) as error:
                            if isinstance(error, InvalidStatus) and error.response.status_code in (
                                401,
                                403,
                                412,
                                429,
                            ):
                                raise OpenLiveFailure(
                                    "authorization_required", error.response.status_code
                                ) from None
                            retry += 1
                            self.status("reconnecting")
                            try:
                                await asyncio.wait_for(
                                    asyncio.shield(heartbeat),
                                    min(
                                        30.0,
                                        RETRY_BASE_SECONDS * 2 ** min(retry - 1, 5),
                                    ),
                                )
                            except TimeoutError:
                                pass
                finally:
                    if heartbeat is not None:
                        heartbeat.cancel()
                        await asyncio.gather(heartbeat, return_exceptions=True)
                    if self._game_id is not None:
                        try:
                            await self._api(
                                client,
                                credentials,
                                "end",
                                {"app_id": self.app_id, "game_id": self._game_id},
                            )
                        except (OpenLiveFailure, ProtocolError):
                            self.status("session_end_uncertain")
                        finally:
                            self._game_id = None
        finally:
            self._active = False
