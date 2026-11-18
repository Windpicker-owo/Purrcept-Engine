"""本机HTTP来源、有限路由、真后台管理接口与真实监听生命周期测试。"""

from __future__ import annotations

import asyncio
import signal
import time
from pathlib import Path

import httpx
import pytest

from purrcept_engine.app import cli
from purrcept_engine.app.panel import ControlRequest
from purrcept_engine.app.web import WebServer, create_app
from purrcept_engine.domain.controls.models import ControlRecord
from purrcept_engine.plugin_system.panels import PairingOperation, PreviewFrame
from purrcept_engine.values import JsonObject, encode_json


class Host:
    """只证明HTTP接缝，真实业务去重与副作用另由test_panel的Host/Store验证。"""

    def __init__(self) -> None:
        self.requests: list[ControlRequest] = []
        self.chats: list[tuple[str, str]] = []

    def query_health(self) -> JsonObject:
        return {
            "instance_id": "current",
            "phase": "starting",
            "can_process": False,
            "startup_stage": "sqlite",
            "components": [],
        }

    async def phone_pairing(
        self,
        target: str,
        *,
        operation: PairingOperation = "read",
        expected_device_id: str | None = None,
    ) -> JsonObject:
        del target, operation, expected_device_id
        return {"status": "closed"}

    async def query_panel(self) -> JsonObject:
        return {"instance_id": "current", "phase": "running"}

    async def query_chat(self, *, limit: int = 40) -> JsonObject:
        return {
            "inputs": [
                {
                    "input_id": f"in-{idx}",
                    "adapter_id": "console",
                    "conversation_id": "web-admin",
                    "display_name": sender,
                    "text": text,
                    "occurred_at": "2026-11-15T12:00:00Z",
                    "received_at": "2026-11-15T12:00:00Z",
                    "stage": "processed",
                    "reason": None,
                    "turn_id": None,
                }
                for idx, (sender, text) in enumerate(self.chats[:limit])
            ],
            "actions": [],
            "console_enabled": True,
            "character_name": "杰瑞喵",
            "foreground_busy": False,
            "can_process": True,
        }

    async def submit_chat(self, text: str, *, sender_name: str = "Web 管理员") -> JsonObject:
        self.chats.append((sender_name, text))
        return {"input_id": f"in-{len(self.chats)}", "duplicate": False}

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

    async def query_reviews(self, before: str | None = None, *, kind: str = "action") -> JsonObject:
        del kind
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


async def test_write_needs_token_and_closed_shape_and_blocks_raw_python_exec() -> None:
    host = Host()
    transport = httpx.ASGITransport(
        create_app(host, port=8765, token="token"), client=("127.0.0.1", 123)
    )
    headers = {"X-Purrcept-Token": "token", "Origin": "http://127.0.0.1:8765"}
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        assert (await client.post("/api/controls", json=command())).status_code == 403
        assert (await client.post("/api/chat", json={"text": "hi"})).status_code == 403
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
            await client.post("/api/chat", json={"text": "   "}, headers=headers)
        ).status_code == 400
        chat_post = await client.post(
            "/api/chat", json={"text": "你好，杰瑞喵！", "sender_name": "拾风"}, headers=headers
        )
        assert chat_post.status_code == 202 and chat_post.json()["input_id"] == "in-1"
        chat_get = await client.get("/api/chat")
        assert chat_get.status_code == 200
        assert chat_get.json()["inputs"][0]["text"] == "你好，杰瑞喵！"

        response = await client.post("/api/controls", json=command(), headers=headers)
        assert response.status_code == 202 and response.json()["state"] == "admitted"
        assert len(host.requests) == 1
        assert (await client.get("/api/controls/one")).json()["state"] == "admitted"


@pytest.mark.parametrize("clear_api_base", [False, True])
async def test_true_admin_config_notebooks_and_studio_endpoints(
    tmp_path: Path, clear_api_base: bool
) -> None:
    template = Path(cli.__file__).with_name("default.toml")
    config_path = tmp_path / "purrcept.toml"
    raw_text = template.read_text(encoding="utf-8").replace(
        'api_key = ""', 'api_key = "sk-super-secret-value-999"', 1
    )
    config_path.write_text(raw_text, encoding="utf-8")
    data_dir = (tmp_path / ".purrcept-engine").resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    host = Host()
    transport = httpx.ASGITransport(
        create_app(
            host,
            port=8765,
            token="token",
            config_path=config_path,
            owner_data_dir=data_dir,
        ),
        client=("127.0.0.1", 123),
    )
    headers = {"X-Purrcept-Token": "token", "Origin": "http://127.0.0.1:8765"}
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        cfg_resp = await client.get("/api/config")
        assert cfg_resp.status_code == 200
        cfg = cfg_resp.json()
        assert cfg["available"] is True
        assert "sk-super-secret-value-999" not in cfg["raw_toml"]
        assert "********" in cfg["raw_toml"]

        visual_save = await client.post(
            "/api/config",
            headers=headers,
            json={
                "mode": "visual",
                "revision": cfg["revision"],
                "general": {
                    "data_dir": ".purrcept-engine",
                    "console": True,
                    "web_enabled": True,
                    "web_port": 8765,
                    "log_level": "DEBUG",
                },
                "subject": {
                    **cfg["subject"],
                    "character": {
                        **cfg["subject"]["character"],
                        "name": "杰瑞喵",
                    },
                },
                "models": [
                    {
                        "name": m["name"],
                        "kind": m["kind"],
                        "model": m["model"],
                        "api_base": "" if clear_api_base else m["api_base"],
                        "api_key": "",
                        "keep_existing_key": True,
                        "context_window": m["context_window"],
                        "max_output_tokens": m["max_output_tokens"],
                        "input_limit": m["input_limit"],
                    }
                    for m in cfg["models"]
                ],
                "plugins": [
                    {
                        "instance_id": "notebook",
                        "entry": "notebook",
                        "enabled": True,
                        "config": {"notebook_dir": "notes"},
                    }
                ],
            },
        )
        assert visual_save.status_code == 200 and visual_save.json()["saved"] is True
        disk_after = config_path.read_text(encoding="utf-8")
        assert "sk-super-secret-value-999" in disk_after
        assert 'name = "杰瑞喵"' in disk_after
        assert 'level = "DEBUG"' in disk_after
        if clear_api_base:
            saved_config = await client.get("/api/config")
            assert all(model["api_base"] == "" for model in saved_config.json()["models"])

        note_save = await client.post(
            "/api/notebooks/file",
            headers=headers,
            json={
                "instance": "notebook",
                "path": "journal/2026-11-15.md",
                "content": "# 今日随笔\n今天完成了真后台重构！\n",
                "revision": "missing",
            },
        )
        assert note_save.status_code == 200
        note_rev = note_save.json()["revision"]

        nb_list = await client.get("/api/notebooks")
        assert nb_list.status_code == 200
        assert nb_list.json()["instances"][0]["files"][0]["path"] == "journal/2026-11-15.md"

        note_read = await client.get(
            "/api/notebooks/file?instance=notebook&path=journal/2026-11-15.md"
        )
        assert note_read.status_code == 200 and note_read.json()["revision"] == note_rev

        traversal = await client.get("/api/notebooks/file?instance=notebook&path=../purrcept.toml")
        assert traversal.status_code == 400

        studio_prof = await client.get("/api/studio/profile")
        assert studio_prof.status_code == 200 and studio_prof.json()["revision"] == "missing"
        studio_save = await client.post(
            "/api/studio/profile",
            headers=headers,
            json={
                "revision": "missing",
                "profile": {
                    "version": 1,
                    "enabled": True,
                    "pitch_semitones": 1.5,
                    "highpass_hz": 80.0,
                    "low_db": 0.0,
                    "mid_db": 1.0,
                    "mid_hz": 1800.0,
                    "mid_q": 0.8,
                    "high_db": 0.5,
                    "gain_db": 0.0,
                    "limiter": True,
                },
            },
        )
        assert studio_save.status_code == 200
        assert (tmp_path / ".purrcept-engine" / "voice-profile.json").is_file()


async def test_real_loopback_listener_keeps_signal_owner_and_closes_port() -> None:
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
