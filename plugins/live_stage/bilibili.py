"""B站网页弹幕的只读传输：匿名临时会话、有限解压、心跳与可取消重连。

协议字段参考 blivedm 作者源码；不读取浏览器 Cookie、不登录、不发送弹幕。
鉴权拒绝明确停止；网络断开有限退避，重连不把旧消息作为新输入重放。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import time
import zlib
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.typing import Origin

from purrcept_engine.values import JsonObject, JsonValue, decode_object

MAX_PACKET = 2 * 1024 * 1024
HEADER = struct.Struct(">IHHII")
USER_AGENT = "Mozilla/5.0 PurrceptLiveStage/1.0"
MIXIN = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
)


class PlatformDenied(ValueError):
    """平台明确拒绝匿名接入，须由用户决定授权方式；不自动换身份。"""


class ProtocolError(ValueError):
    """收到不符合预期的有界平台数据。"""


@dataclass(frozen=True, slots=True)
class Danmaku:
    """展示与正式输入共同使用的原文；native_id 缺失时不承诺重放去重。"""

    sender: str
    uid: str
    text: str
    timestamp: float
    native_id: str | None = None


def packet(operation: int, payload: bytes) -> bytes:
    """编码一项B站协议帧。

    Args:
        operation: 协议操作号；7为鉴权，2为心跳。
        payload: 已编码正文，不能包含持久凭据。

    Returns:
        固定16字节头和正文组成的帧。
    """
    return HEADER.pack(16 + len(payload), 16, 1, operation, 1) + payload


def frames(data: bytes, depth: int = 0) -> list[tuple[int, bytes]]:
    """解析完整帧及zlib嵌套帧，拒绝截断、过深和解压炸弹。

    Args:
        data: 一次WebSocket收到的二进制消息。
        depth: 内部递归层数，调用者使用默认值。

    Returns:
        原顺序的操作号与正文。

    Raises:
        ProtocolError: 长度、压缩版本或嵌套深度不合法。
    """
    if len(data) > MAX_PACKET or depth > 2:
        raise ProtocolError("弹幕帧超限。")
    result: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 16:
            raise ProtocolError("弹幕帧头不完整。")
        size, header_size, version, operation, _ = HEADER.unpack_from(data, offset)
        if header_size < 16 or size < header_size or offset + size > len(data):
            raise ProtocolError("弹幕帧长度无效。")
        body = data[offset + header_size : offset + size]
        offset += size
        if version == 2:
            decoder = zlib.decompressobj()
            try:
                plain = decoder.decompress(body, MAX_PACKET + 1)
            except zlib.error as error:
                raise ProtocolError("弹幕压缩数据无效。") from error
            if len(plain) > MAX_PACKET or decoder.unconsumed_tail or not decoder.eof:
                raise ProtocolError("弹幕解压超限或不完整。")
            result.extend(frames(plain, depth + 1))
        elif version in (0, 1):
            result.append((operation, body))
        else:
            raise ProtocolError("平台未采用请求的zlib协议版本。")
        if len(result) > 2048 or sum(len(payload) for _, payload in result) > MAX_PACKET:
            raise ProtocolError("弹幕帧数量超限。")
    return result


def parse_message(body: bytes) -> Danmaku | None:
    """只提取真实 DANMU_MSG 原文，忽略礼物、系统通知和其他事件。

    Args:
        body: 解包后的JSON对象正文。

    Returns:
        有效普通弹幕，其他命令返回 None。

    Raises:
        ProtocolError: 普通弹幕缺失必要字段或正文超限。
    """
    value = decode_object(body)
    command = value.get("cmd")
    if not isinstance(command, str) or command.partition(":")[0] != "DANMU_MSG":
        return None
    info = value.get("info")
    if not isinstance(info, list) or len(info) < 3:
        raise ProtocolError("普通弹幕缺少info。")
    meta, text, sender = info[:3]
    if (
        not isinstance(text, str)
        or not text
        or len(text) > 4000
        or not isinstance(sender, list)
        or len(sender) < 2
        or not isinstance(sender[1], str)
        or not sender[1]
        or len(sender[1]) > 128
        or not isinstance(sender[0], (str, int))
        or isinstance(sender[0], bool)
        or not isinstance(meta, list)
        or len(meta) < 5
        or not isinstance(meta[4], (int, float))
        or isinstance(meta[4], bool)
    ):
        raise ProtocolError("普通弹幕字段无效。")
    timestamp = float(meta[4])
    if timestamp > 100_000_000_000:
        timestamp /= 1000
    if not 0 < timestamp < 32_503_680_000:
        raise ProtocolError("普通弹幕时间无效。")
    native = value.get("msg_id")
    native_id = (
        str(native)
        if isinstance(native, (str, int)) and not isinstance(native, bool) and str(native)
        else None
    )
    return Danmaku(sender[1], str(sender[0]), text, timestamp, native_id)


def _object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        raise ProtocolError("平台响应对象无效。")
    return value


class BilibiliSource:
    """拥有一个匿名接入连接，停止时关闭HTTP、WebSocket和心跳任务。"""

    def __init__(self, room_id: int, status: Callable[[str], None]) -> None:
        """保存公开房间号与无秘密状态回调；构造不联网。"""
        self.room_id, self.status = room_id, status

    async def _json(
        self, client: httpx.AsyncClient, url: str, params: dict[str, str]
    ) -> JsonObject:
        response = await client.get(url, params=params)
        if response.status_code in (401, 403, 412, 429):
            raise PlatformDenied("平台拒绝匿名接入或要求稍后重试。")
        response.raise_for_status()
        if len(response.content) > MAX_PACKET:
            raise ProtocolError("平台响应超限。")
        return decode_object(response.content)

    async def _connection(self, client: httpx.AsyncClient) -> tuple[str, JsonObject]:
        """每次连接重新获取临时握手材料，不落盘、不回显令牌。"""
        info = await self._json(
            client,
            "https://api.live.bilibili.com/room/v1/Room/get_info",
            {"room_id": str(self.room_id)},
        )
        if info.get("code") != 0:
            raise PlatformDenied("平台未允许读取该直播间。")
        room = _object(info.get("data")).get("room_id")
        if type(room) is not int or room <= 0:
            raise ProtocolError("平台未返回有效房间号。")
        navigation = await self._json(client, "https://api.bilibili.com/x/web-interface/nav", {})
        images = _object(_object(navigation.get("data")).get("wbi_img"))
        parts: list[str] = []
        for key in ("img_url", "sub_url"):
            url = images.get(key)
            if not isinstance(url, str):
                raise ProtocolError("平台未提供签名材料。")
            parts.append(urlsplit(url).path.rsplit("/", 1)[-1].split(".")[0])
        seed = "".join(parts)
        if len(seed) < 64:
            raise ProtocolError("平台签名材料不完整。")
        mix = "".join(seed[index] for index in MIXIN)
        params = {"id": str(room), "type": "0", "wts": str(int(time.time()))}
        signature = urlencode(sorted(params.items())) + mix
        params["w_rid"] = hashlib.md5(signature.encode()).hexdigest()
        config = await self._json(
            client, "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo", params
        )
        if config.get("code") != 0:
            raise PlatformDenied("平台未允许弹幕握手；未尝试登录或换身份。")
        data = _object(config.get("data"))
        hosts, token = data.get("host_list"), data.get("token")
        if not isinstance(hosts, list) or not hosts or not isinstance(token, str):
            raise ProtocolError("弹幕连接信息无效。")
        server = _object(hosts[0])
        host, port = server.get("host"), server.get("wss_port")
        if (
            not isinstance(host, str)
            or not host.endswith(".chat.bilibili.com")
            or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for char in host)
            or type(port) is not int
            or not 1 <= port <= 65535
        ):
            raise ProtocolError("弹幕服务器地址不在B站协议范围。")
        auth: JsonObject = {
            "uid": 0,
            "roomid": room,
            "protover": 2,
            "platform": "web",
            "type": 2,
            "key": token,
        }
        return f"wss://{host}:{port}/sub", auth

    async def messages(self) -> AsyncGenerator[Danmaku, None]:
        """连接并产生原文消息，断线退避恢复，拒绝匿名访问则停止。

        Yields:
            从当前连接收到的真实普通弹幕。

        Raises:
            PlatformDenied: 平台明确拒绝匿名方式，需要用户选择下一步。
            ProtocolError: 收到不支持的协议，不能伪装已连接。

        Note:
            调用者负责取消生成器。HTTP不读取环境代理或身份凭据，不持久化Cookie。
        """
        retry = 0
        async with httpx.AsyncClient(
            timeout=10,
            trust_env=False,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Referer": "https://live.bilibili.com/"},
        ) as client:
            while True:
                self.status("connecting" if retry == 0 else "reconnecting")
                try:
                    url, auth = await self._connection(client)
                    async with connect(
                        url,
                        proxy=None,
                        max_size=MAX_PACKET,
                        max_queue=16,
                        open_timeout=10,
                        close_timeout=3,
                        origin=Origin("https://live.bilibili.com"),
                        user_agent_header=USER_AGENT,
                        # B站使用op2/op3心跳；RFC Ping未获Pong会误触发库的40秒断线。
                        ping_interval=None,
                    ) as ws:
                        await ws.send(packet(7, json.dumps(auth).encode()))
                        authenticated = False
                        next_heartbeat = time.monotonic()
                        last_pong = time.monotonic()
                        while True:
                            now = time.monotonic()
                            if now >= next_heartbeat:
                                await ws.send(packet(2, b"[object Object]"))
                                next_heartbeat = now + 30
                            if now - last_pong > 75:
                                raise TimeoutError("弹幕心跳超时。")
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=1)
                            except TimeoutError:
                                continue
                            if not isinstance(raw, bytes):
                                raise ProtocolError("预期二进制弹幕帧。")
                            for operation, body in frames(raw):
                                if operation == 8:
                                    if decode_object(body).get("code") != 0:
                                        raise PlatformDenied("平台拒绝弹幕连接。")
                                    authenticated, retry = True, 0
                                    self.status("connected")
                                elif operation == 3:
                                    last_pong = time.monotonic()
                                elif operation == 5 and authenticated:
                                    message = parse_message(body)
                                    if message is not None:
                                        yield message
                except (
                    httpx.TransportError,
                    httpx.HTTPStatusError,
                    ConnectionClosed,
                    InvalidHandshake,
                    OSError,
                    TimeoutError,
                ):
                    retry += 1
                    self.status("reconnecting")
                    await asyncio.sleep(min(30, 2 ** min(retry - 1, 5)))
