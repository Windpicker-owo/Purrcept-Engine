"""本机HTTP来源、有限路由与真实监听生命周期，不打开设备或外部端点。"""

from __future__ import annotations

import asyncio
import signal
import time

import httpx
import pytest

from purrcept_engine.app.panel import ControlRequest
from purrcept_engine.app.web import WebServer, create_app
from purrcept_engine.domain.controls.models import ControlRecord
from purrcept_engine.plugin_system.panels import PreviewFrame
from purrcept_engine.values import JsonObject, encode_json


class Host:
    """只证明HTTP接缝，真实业务去重与副作用另由test_panel的Host/Store验证。"""

    def __init__(self) -> None:
        self.requests: list[ControlRequest] = []

    async def query_panel(self) -> JsonObject:
        return {"instance_id": "current", "phase": "running"}

    async def submit_control(self, request: ControlRequest) -> ControlRecord:
        self.requests.append(request)
        return ControlRecord(
            request.request_id,
            request.instance_id,
            encode_json({"operation": request.operation}).decode(),
            "admitted",
            time.time(),
            None,
            None,
            None,
            None,
            None,
        )

    async def query_control(self, request_id: str) -> JsonObject | None:
        return {"request_id": request_id, "state": "admitted"} if request_id == "one" else None

    async def query_reviews(self, before: str | None = None) -> JsonObject:
        return {"items": [], "next_cursor": before}

    async def query_review(self, action_id: str) -> JsonObject | None:
        return {"action_id": action_id, "stimuli": []} if action_id == "one" else None

    async def query_review_media(self, action_id: str, media_id: str) -> tuple[bytes, str] | None:
        if action_id == "one" and media_id == "image":
            return b"original-image", "image/png"
        if action_id == "one" and media_id == "unsafe":
            return b"<script>untrusted</script>", "text/html"
        return None

    def query_preview(self, target: str) -> PreviewFrame | None:
        return PreviewFrame(b"cached-image", "image/jpeg", 1.0) if target == "computer" else None


@pytest.fixture(autouse=True)
def local_asgi_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """在防外网夹具之上只允许明确传入的ASGI传输，不放行真实HTTP客户端。"""
    transports: dict[httpx.AsyncClient, httpx.ASGITransport] = {}

    def initialize(client: httpx.AsyncClient, **options: object) -> httpx.ASGITransport:
        transport = options.get("transport")
        assert isinstance(transport, httpx.ASGITransport)
        transports[client] = transport
        return transport

    def selected(client: httpx.AsyncClient, url: httpx.URL) -> httpx.ASGITransport:
        del url
        return transports[client]

    monkeypatch.setattr(httpx.AsyncClient, "_init_transport", initialize)
    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", selected)


def command() -> dict[str, object]:
    return {
        "request_id": "one",
        "instance_id": "current",
        "target": "computer",
        "operation": "connect",
        "expected_revision": "1",
        "arguments": {},
    }


async def test_read_query_and_preview_have_finite_current_facts() -> None:
    host = Host()
    transport = httpx.ASGITransport(
        create_app(host, port=8765, token="token"), client=("127.0.0.1", 123)
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        session = await client.get("/api/session")
        assert session.json() == {"token": "token", "instance_id": "current"}
        assert session.headers["cache-control"] == "no-store"
        assert (await client.get("/api/snapshot")).json()["phase"] == "running"
        image = await client.get("/api/preview/computer")
        assert image.content == b"cached-image" and image.headers["x-captured-at"] == "1.0"
        assert (
            await client.get(
                "/api/preview/computer", headers={"If-None-Match": image.headers["etag"]}
            )
        ).status_code == 304
        assert (await client.get("/api/preview/missing")).status_code == 204
        assert (await client.get("/api/controls/missing")).status_code == 404
        assert (await client.get("/api/reviews?before=older")).json()["next_cursor"] == "older"
        assert (await client.get("/api/reviews/one")).json()["action_id"] == "one"
        assert (await client.get("/api/reviews/missing")).status_code == 404
        assert (await client.get("/api/reviews/one/media/image")).content == b"original-image"
        assert (await client.get("/api/reviews/one/media/missing")).status_code == 404
        unsafe = await client.get("/api/reviews/one/media/unsafe")
        assert unsafe.headers["content-type"] == "application/octet-stream"
        assert unsafe.headers["content-disposition"].startswith("attachment")
        assert host.requests == []


@pytest.mark.parametrize(
    "headers,remote",
    [
        ({"Host": "evil.invalid:8765"}, "127.0.0.1"),
        ({"Host": "127.0.0.1:8766"}, "127.0.0.1"),
        ({"Origin": "http://evil.invalid"}, "127.0.0.1"),
        ({"Sec-Fetch-Site": "cross-site"}, "127.0.0.1"),
        ({"X-Forwarded-For": "127.0.0.1"}, "192.0.2.1"),
    ],
)
async def test_read_and_write_reject_wrong_host_origin_or_real_client(
    headers: dict[str, str], remote: str
) -> None:
    host = Host()
    transport = httpx.ASGITransport(
        create_app(host, port=8765, token="token"), client=(remote, 123)
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        assert (await client.get("/api/session", headers=headers)).status_code == 403
        assert (
            await client.post(
                "/api/controls", json=command(), headers=headers | {"X-Purrcept-Token": "token"}
            )
        ).status_code == 403
        assert host.requests == []


async def test_write_needs_token_and_closed_shape_and_does_not_offer_python_or_chat() -> None:
    host = Host()
    transport = httpx.ASGITransport(
        create_app(host, port=8765, token="token"), client=("127.0.0.1", 123)
    )
    headers = {"X-Purrcept-Token": "token", "Origin": "http://127.0.0.1:8765"}
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        assert (await client.post("/api/controls", json=command())).status_code == 403
        assert (
            await client.post(
                "/api/controls", json=command() | {"source": "return 1"}, headers=headers
            )
        ).status_code == 400
        assert (
            await client.post(
                "/api/controls", content=b'{"request_id":"one","request_id":"two"}', headers=headers
            )
        ).status_code == 400
        assert (
            await client.post("/api/controls", content=b"x" * 16385, headers=headers)
        ).status_code == 413
        assert (
            await client.post("/api/execute", json={"source": "return 1"}, headers=headers)
        ).status_code == 404
        assert (
            await client.post("/api/chat", json={"text": "hi"}, headers=headers)
        ).status_code == 404
        response = await client.post("/api/controls", json=command(), headers=headers)
        assert response.status_code == 202 and response.json()["state"] == "admitted"
        assert len(host.requests) == 1
        assert (await client.get("/api/controls/one")).json()["state"] == "admitted"


async def test_real_loopback_listener_keeps_signal_owner_and_closes_port() -> None:
    """真实TCP只连本次回环端口，服务不装配设备/模型，也不接管CLI信号。"""
    before = signal.getsignal(signal.SIGINT)
    server = WebServer(Host(), port=0)
    await server.start()
    try:
        assert signal.getsignal(signal.SIGINT) == before
        assert server.url.startswith("http://127.0.0.1:")
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        try:
            writer.write(
                (
                    "GET /api/snapshot HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{server.port}\r\nConnection: close\r\n\r\n"
                ).encode()
            )
            await writer.drain()
            result = await asyncio.wait_for(reader.read(), 5)
            assert result.startswith(b"HTTP/1.1 200") and b'"phase":"running"' in result
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await asyncio.wait_for(server.close(), 5)
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", server.port)
    assert signal.getsignal(signal.SIGINT) == before
