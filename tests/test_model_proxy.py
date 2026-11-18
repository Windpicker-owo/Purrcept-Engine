"""用本机 HTTP 代理替身验证真实 SDK 的路由，包括 Gemini 的缓存前置请求。

模型地址使用本机不可用端口；请求只有经过显式代理才能成功。
替身记录代理收到的完整目标地址，不替换 HTTPX 传输或 SDK 编译流程。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from purrcept_core.models import (
    CacheMode,
    DecisionRequest,
    Message,
    ModelRequest,
    PredicateQuestion,
    PromptCachePolicy,
    TextBlock,
)

from purrcept_engine.app.config import DecisionModelSettings, GenerationModelSettings
from purrcept_engine.infrastructure.models.clients import DecisionResource, GenerationResource


class LocalHTTPRoutes(httpx.AsyncClient):
    """在离线拦截安装前保留原生路由，仅供下方本机代理验证恢复使用。"""

    initialize_transport = httpx.AsyncClient._init_transport
    initialize_proxy_transport = httpx.AsyncClient._init_proxy_transport
    select_transport = httpx.AsyncClient._transport_for_url


@pytest.mark.parametrize(
    ("provider", "cache_mode"),
    [
        ("openai", CacheMode.AUTO),
        ("gemini", CacheMode.AUTO),
        ("gemini", CacheMode.DISABLED),
        ("gemini", CacheMode.EXPLICIT),
        ("typesafe", CacheMode.AUTO),
    ],
)
async def test_explicit_proxy_carries_sdk_generation_cache_and_decision(
    provider: str, cache_mode: CacheMode, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缓存列表、创建及流式生成都必须走模型代理；判断路径遵循同一配置。"""
    received: list[tuple[str, str]] = []
    errors: list[Exception] = []

    # 本机代理验证恢复真实路由；冲突环境同时证明显式配置不会被环境覆盖。
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setattr(httpx.AsyncClient, "_init_transport", LocalHTTPRoutes.initialize_transport)
    monkeypatch.setattr(
        httpx.AsyncClient, "_init_proxy_transport", LocalHTTPRoutes.initialize_proxy_transport
    )
    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", LocalHTTPRoutes.select_transport)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """接收代理格式的绝对 URL，每个响应关闭连接以简化替身的连接生命周期。"""
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            method, url, _ = head.splitlines()[0].split(" ")
            received.append((method, url))
            for header in head.splitlines()[1:]:
                name, _, value = header.partition(":")
                if name.lower() == "content-length":
                    await reader.readexactly(int(value.strip()))
            if method == "GET":
                body: object = {"cachedContents": []}
            elif "cachedContents" in url:
                body = {"name": "cachedContents/synthetic-proxy-cache"}
            elif provider == "typesafe":
                body = {
                    "model": "jev-1.13.0",
                    "answers": {"attend": {"type": "noul", "noul": 0.8}},
                }
            elif provider == "gemini":
                body = {
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": "ok"}]},
                            "finishReason": "STOP",
                            "index": 0,
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 12,
                        "candidatesTokenCount": 1,
                        "totalTokenCount": 13,
                    },
                    "modelVersion": "gemini-4-hydrogen",
                }
            else:
                body = {
                    "id": "synthetic-proxy",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "synthetic",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            streaming = provider != "typesafe" and method == "POST" and "cachedContents" not in url
            content = json.dumps(body)
            if streaming:
                content = f"data: {content}\n\ndata: [DONE]\n\n"
            data = content.encode("utf-8")
            content_type = "text/event-stream" if streaming else "application/json"
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n"
                f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode("ascii")
                + data
            )
            await writer.drain()
        except Exception as error:
            errors.append(error)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    proxy = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    async with server:
        if provider == "typesafe":
            decision = DecisionResource.create(
                DecisionModelSettings.model_validate(
                    {
                        "kind": "decision",
                        "model": "typesafe/jev-1.13.0",
                        "api_base": "http://127.0.0.1:1",
                        "api_key": "synthetic-key",
                        "proxy": proxy,
                        "input_limit": 8192,
                    }
                )
            )
            try:
                await decision.decide(
                    DecisionRequest(
                        (TextBlock("hello"),),
                        (PredicateQuestion("attend", "Should we attend?"),),
                    ),
                    model="typesafe/jev-1.13.0",
                )
            finally:
                await decision.close()
        else:
            model = "gemini/gemini-4-hydrogen" if provider == "gemini" else "openai/synthetic"
            generation = GenerationResource.create(
                GenerationModelSettings.model_validate(
                    {
                        "model": model,
                        "api_base": "http://127.0.0.1:1",
                        "api_key": "synthetic-key",
                        "proxy": proxy,
                    }
                )
            )
            try:
                result = await generation.generate(
                    ModelRequest(
                        (Message.user("cached text " * 3000),),
                        cache=PromptCachePolicy(mode=cache_mode),
                    ),
                    model=model,
                )
                assert result.message.text == "ok"
            finally:
                await generation.close()
    assert not errors
    assert received and all(url.startswith("http://127.0.0.1:1/") for _, url in received)
    if provider == "gemini" and cache_mode is CacheMode.EXPLICIT:
        assert [method for method, _ in received] == ["GET", "POST", "POST"]
        assert "cachedContents" in received[0][1] and "cachedContents" in received[1][1]
        assert "streamGenerateContent" in received[2][1]
    else:
        assert len(received) == 1
