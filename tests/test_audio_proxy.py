"""用本机 HTTP 代理验证语音路由，不访问 Fish Audio 或产生付费合成。

恢复 HTTPX 原生路由后，目标仍限定为本机不可用端口；只有显式代理才能
完成代理场景。直连场景同时证明环境代理不会意外接管插件的网络连接。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from purrcept_engine.plugins.audio.models import AudioSettings, RecordParameters, SpeechParameters
from purrcept_engine.plugins.audio.tts import FishSource


class LocalHTTPRoutes(httpx.AsyncClient):
    """在离线拦截安装前保存子类可用的 HTTPX 原生路由接缝。

    本类不创建客户端；仅在下方本机服务场景恢复这些方法，避免用
    MockTransport 绕过生产代理选择而得到假阳性。
    """

    initialize_transport = httpx.AsyncClient._init_transport
    initialize_proxy_transport = httpx.AsyncClient._init_proxy_transport
    select_transport = httpx.AsyncClient._transport_for_url


@pytest.mark.parametrize(
    "proxy",
    [
        "",
        "127.0.0.1:10808",
        "socks5://127.0.0.1:10808",
        "http://user:password@localhost:10808",
        "http://localhost:10808/path",
        "http://localhost:10808?secret=value",
        "http://localhost:10808#fragment",
        "http://localhost:invalid",
        "http://localhost:65536",
    ],
)
def test_audio_proxy_rejects_invalid_declarations(proxy: str) -> None:
    """不支持的路由在声明阶段失败，不留给首次合成才发现。"""
    with pytest.raises(ValidationError):
        AudioSettings(proxy=proxy)


@pytest.mark.parametrize("proxy", [None, "http://127.0.0.1:10808", "https://localhost:443/"])
def test_audio_proxy_accepts_explicit_http_routes(proxy: str | None) -> None:
    """未配置与标准 HTTP(S) 代理均可纯校验，不打开网络。"""
    assert AudioSettings(proxy=proxy).proxy == proxy


@pytest.mark.parametrize("use_proxy", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
async def test_audio_requests_share_explicit_route(
    use_proxy: bool, streaming: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目录、音色与两类合成都走声明路由，环境值不会改变选择。"""
    requests: list[str] = []

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """代理仅返回合成材料，每次响应关闭连接以简化测试资源归还。"""
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            first, *headers = head.split("\r\n")
            requests.append(first)
            for header in headers:
                if header.lower().startswith("content-length:"):
                    await reader.readexactly(int(header.split(":", 1)[1]))

            content_type = "application/json"
            if first.startswith("POST"):
                if streaming:
                    content_type = "text/event-stream"
                    body = (
                        b'event: audio\ndata: {"audioBase64":"YXVkaW8="}\n\n'
                        b"event: completed\ndata: {}\n\n"
                    )
                else:
                    content_type, body = "audio/mpeg", b"audio"
            elif "capabilities" in first:
                body = json.dumps(
                    {
                        "models": [
                            {
                                "id": "model",
                                "available": True,
                                "supportedOperations": {
                                    "synchronous": True,
                                    "alignedStreaming": True,
                                },
                                "outputFormats": ["mp3"],
                                "maxTextLength": 10000,
                                "controls": {
                                    "speed": {"min": 0.5, "max": 2},
                                    "volume": {"min": -20, "max": 20},
                                },
                            }
                        ]
                    }
                ).encode()
            else:
                body = b'{"voice":{"modelIds":["model"]}}'

            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                ).encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        local_url = f"http://127.0.0.1:{port}"
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(name, "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", "*")
        monkeypatch.setattr(
            httpx.AsyncClient, "_init_transport", LocalHTTPRoutes.initialize_transport
        )
        monkeypatch.setattr(
            httpx.AsyncClient, "_init_proxy_transport", LocalHTTPRoutes.initialize_proxy_transport
        )
        monkeypatch.setattr(
            httpx.AsyncClient, "_transport_for_url", LocalHTTPRoutes.select_transport
        )
        settings = AudioSettings(
            api_base="http://127.0.0.1:1" if use_proxy else local_url,
            proxy=local_url if use_proxy else None,
            api_key=SecretStr("test-only"),
            model="model",
            voice_id="voice",
            timeout_seconds=2.0,
        )
        source = FishSource(settings)
        try:
            if streaming:
                parts = [part async for part in source.stream(SpeechParameters(text="测试"), "id")]
                assert b"".join(parts) == b"audio"
            else:
                audio = await source.synthesize(RecordParameters(text="测试"), "id")
                assert audio.data == b"audio"
        finally:
            await source.close()

    prefix = "http://127.0.0.1:1" if use_proxy else ""
    assert requests == [
        f"GET {prefix}/speech/tts/capabilities HTTP/1.1",
        f"GET {prefix}/voices/voice HTTP/1.1",
        f"POST {prefix}/speech/tts{'/aligned' if streaming else ''} HTTP/1.1",
    ]
