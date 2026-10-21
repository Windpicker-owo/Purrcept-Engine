"""官方来源的离线签名、HTTP替身、真实loopback WS和保险库替身验收。"""

from __future__ import annotations

import asyncio
import ctypes
import json
import tkinter as tk
import zlib
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from tkinter import ttk
from typing import cast

import httpx
import pytest
from pydantic import ValidationError
from test_live_stage import Inputs, http_snapshot
from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from plugins.live_stage import credential_ui, open_live
from plugins.live_stage.bilibili import HEADER, ProtocolError, frames, packet
from plugins.live_stage.credentials import (
    CredentialError,
    OpenLiveCredentials,
    WinCredentialRecord,
    WindowsCredentialStore,
    credential_target,
)
from plugins.live_stage.open_live import (
    OpenLiveFailure,
    OpenLiveSource,
    parse_open_danmaku,
    signed_request,
)
from plugins.live_stage.runtime import LiveStage, Settings, declare
from purrcept_engine.plugin_system import PluginPorts
from purrcept_engine.values import JsonObject, decode_object

APP = 1783128932975
ROOM = 1850493825
FAKE = OpenLiveCredentials("fake-id", "fake-secret", "fake-anchor")


def event(text: str = "离线替身弹幕") -> bytes:
    return json.dumps(
        {
            "cmd": "LIVE_OPEN_PLATFORM_DM",
            "data": {
                "room_id": ROOM,
                "uname": "测试观众",
                "open_id": "open-test",
                "msg": text,
                "timestamp": 1700000000,
                "msg_id": "message-1",
                "dm_type": 0,
            },
        }
    ).encode()


def start_data(room: int = ROOM, link: str = "wss://test.chat.bilibili.com/sub") -> JsonObject:
    return {
        "code": 0,
        "data": {
            "game_info": {"game_id": "fake-game"},
            "anchor_info": {"room_id": room},
            "websocket_info": {
                "auth_body": '{"protover":2,"key":"temporary-test"}',
                "wss_link": [link],
            },
        },
    }


class API:
    def __init__(self, room: int = ROOM) -> None:
        self.calls: list[str] = []
        self.nonces: list[str] = []
        self.room = room
        self.start_code = 0
        self.heartbeat_code = 0
        self.end_code = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "live-open.biliapi.com"
        assert request.method == "POST"
        action = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(action)
        self.nonces.append(request.headers["x-bili-signature-nonce"])
        assert "fake-secret" not in str(request.headers)
        body = decode_object(request.content)
        if action == "start":
            assert body == {"app_id": APP, "code": "fake-anchor"}
            return httpx.Response(
                200,
                json=start_data(self.room)
                if self.start_code == 0
                else {"code": self.start_code, "message": "fake-secret"},
            )
        assert body["game_id"] == "fake-game"
        return httpx.Response(
            200,
            json={
                "code": self.heartbeat_code if action == "heartbeat" else self.end_code,
                "message": "fake-secret",
            },
        )


def mock_http(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    transport = httpx.MockTransport(handler)

    def select(client: httpx.AsyncClient, url: httpx.URL) -> httpx.MockTransport:
        del client, url
        return transport

    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", select)


def fast_clocks(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PROJECT_HEARTBEAT_SECONDS",
        "WS_HEARTBEAT_SECONDS",
        "RECEIVE_POLL_SECONDS",
        "RETRY_BASE_SECONDS",
    ):
        monkeypatch.setattr(open_live, name, 0.02)


def test_signature_fixed_cross_language_vector() -> None:
    body, headers = signed_request(
        FAKE, {"app_id": APP, "code": "fake-anchor"}, timestamp=1700000000, nonce="fixed-nonce"
    )
    assert body == b'{"app_id":1783128932975,"code":"fake-anchor"}'
    # 此固定值独立由Node crypto实现计算，不从被测实现反生成。
    assert (
        headers["Authorization"]
        == "7ea8efa42e10b6813a6e18b6c5f43d36630e8e27f75fc31073f3154388cdcc99"
    )
    assert "fake-secret" not in repr(FAKE)
    assert credential_target(APP).endswith(str(APP))


def test_official_event_boundary() -> None:
    value = parse_open_danmaku(event(), ROOM)
    assert value is not None and value.uid == "open_id:open-test"
    assert value.native_id == "open_live:message-1"
    with pytest.raises(ProtocolError):
        parse_open_danmaku(event(), 1)
    assert parse_open_danmaku(b'{"cmd":"LIVE_OPEN_PLATFORM_SEND_GIFT"}', ROOM) is None
    payload = decode_object(event())
    data = payload["data"]
    assert isinstance(data, dict)
    data["dm_type"] = 1
    assert parse_open_danmaku(json.dumps(payload).encode(), ROOM) is None
    data["dm_type"] = 0
    data["open_id"] = ""
    with pytest.raises(ProtocolError):
        parse_open_danmaku(json.dumps(payload).encode(), ROOM)


async def test_declaration_and_disabled_mode_never_read_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def forbidden(self: WindowsCredentialStore, app_id: int) -> OpenLiveCredentials:
        raise AssertionError("configuration must not read credentials")

    monkeypatch.setattr(WindowsCredentialStore, "read", forbidden)
    settings = Settings(source="open_live", room_id=ROOM, open_live_app_id=APP, port=0)
    blueprint = declare("stage", settings, tmp_path)
    assert blueprint.capabilities == ()
    owner = LiveStage(
        "stage", Settings(port=0), PluginPorts(Inputs(), lambda: {"phase": "running"})
    )
    await owner.start()
    await owner.close()
    assert "fake" not in settings.model_dump_json()
    for raw in (
        {"source": "open_live"},
        {"source": "anonymous", "open_live_app_id": APP},
        {"access_key_secret": "fake-secret"},
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate(raw)


async def test_missing_credentials_never_start_or_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(self: WindowsCredentialStore, app_id: int) -> OpenLiveCredentials:
        assert app_id == APP
        raise CredentialError("credential_missing")

    monkeypatch.setattr(WindowsCredentialStore, "read", missing)
    owner = LiveStage(
        "stage",
        Settings(source="open_live", room_id=ROOM, open_live_app_id=APP, port=0),
        PluginPorts(Inputs(), lambda: {"phase": "running"}),
    )
    await owner.start()
    try:
        assert owner.reader is not None
        await owner.reader
        assert owner.snapshot()["bilibili"] == "credential_required"
    finally:
        await owner.close()


@pytest.mark.parametrize("case", ["denied", "wrong_room", "redirect", "unknown_start", "unsafe_ws"])
async def test_start_rejection_is_sanitized_and_not_retried(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        calls.append(action)
        if action == "end":
            return httpx.Response(200, json={"code": 0})
        if case == "denied":
            return httpx.Response(200, json={"code": 7007, "message": "fake-secret"})
        if case == "wrong_room":
            return httpx.Response(200, json=start_data(1))
        if case == "unsafe_ws":
            return httpx.Response(200, json=start_data(link="wss://attacker.invalid/sub"))
        if case == "redirect":
            return httpx.Response(302, headers={"Location": "https://attacker.invalid/fake-secret"})
        raise httpx.ReadTimeout("fake-secret", request=request)

    mock_http(monkeypatch, respond)
    source = OpenLiveSource(
        ROOM, APP, lambda: FAKE, lambda _: None, http_transport=httpx.MockTransport(respond)
    )
    with pytest.raises((OpenLiveFailure, ProtocolError)) as caught:
        await anext(source.messages())
    assert "fake-secret" not in str(caught.value)
    assert calls == (["start", "end"] if case in ("wrong_room", "unsafe_ws") else ["start"])


async def test_reconnect_same_session_and_real_stage_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    fast_clocks(monkeypatch)
    api = API()
    mock_http(monkeypatch, api)
    connections = 0
    received = asyncio.Event()
    release = asyncio.Event()

    async def handler(ws: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        raw = await ws.recv()
        assert isinstance(raw, bytes)
        assert frames(raw) == [(7, b'{"protover":2,"key":"temporary-test"}')]
        await ws.send(packet(8, b'{"code":0}'))
        heartbeat = await ws.recv()
        assert isinstance(heartbeat, bytes) and frames(heartbeat) == [(2, b"{}")]
        batch = packet(5, event()) + packet(5, event())
        compressed = zlib.compress(batch)
        await ws.send(HEADER.pack(len(compressed) + 16, 16, 2, 5, 1) + compressed)
        if connections == 1:
            await ws.close()
            return
        received.set()
        await release.wait()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = int(next(iter(server.sockets)).getsockname()[1])

        def local_connect(uri: str, **kwargs: object) -> connect:
            assert uri == "wss://test.chat.bilibili.com/sub"
            assert kwargs["ping_interval"] is None
            return connect(f"ws://127.0.0.1:{port}", proxy=None, ping_interval=None)

        monkeypatch.setattr(open_live, "connect", local_connect)
        inputs = Inputs()
        owner = LiveStage(
            "stage",
            Settings(source="open_live", room_id=ROOM, open_live_app_id=APP, port=0),
            PluginPorts(inputs, lambda: {"phase": "running"}),
            source_factory=lambda room, status: OpenLiveSource(
                room, APP, lambda: FAKE, status, http_transport=httpx.MockTransport(api)
            ),
        )
        await owner.start()
        try:
            await asyncio.wait_for(received.wait(), 3)
            await asyncio.sleep(0.05)
            _, snapshot = await http_snapshot(owner.bound_port)
            assert len(inputs.submissions) == 1 and len(owner.messages) == 1
            assert snapshot["bilibili"] == "connected"
            assert "fake-secret" not in json.dumps(snapshot)
            assert not inputs.submissions[0].context.minimum_wake
        finally:
            await owner.close()
            release.set()
    assert api.calls.count("start") == api.calls.count("end") == 1
    assert api.calls.count("heartbeat") >= 1 and connections == 2
    assert len(api.nonces) == len(set(api.nonces))


@pytest.mark.parametrize(
    "failure", ["project_heartbeat", "server_end", "ws_denied", "http_denied", "cancel"]
)
async def test_failure_or_cancellation_ends_session(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    fast_clocks(monkeypatch)
    api = API()
    mock_http(monkeypatch, api)
    ready = asyncio.Event()

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(packet(8, b'{"code":-101}' if failure == "ws_denied" else b'{"code":0}'))
        ready.set()
        if failure == "server_end":
            await ws.send(
                packet(
                    5,
                    b'{"cmd":"LIVE_OPEN_PLATFORM_INTERACTION_END","data":{"game_id":"fake-game"}}',
                )
            )
        await ws.wait_closed()

    def process_request(ws: ServerConnection, request: Request) -> Response | None:
        del request
        if failure == "http_denied":
            ready.set()
            return ws.respond(HTTPStatus.UNAUTHORIZED, "fake-secret")
        return None

    async with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
        port = int(next(iter(server.sockets)).getsockname()[1])

        def local_connect(uri: str, **kwargs: object) -> connect:
            del uri, kwargs
            return connect(f"ws://127.0.0.1:{port}", proxy=None, ping_interval=None)

        monkeypatch.setattr(open_live, "connect", local_connect)
        source = OpenLiveSource(
            ROOM, APP, lambda: FAKE, lambda _: None, http_transport=httpx.MockTransport(api)
        )
        stream = source.messages()
        task = asyncio.create_task(anext(stream))
        await asyncio.wait_for(ready.wait(), 2)
        if failure == "project_heartbeat":
            api.heartbeat_code = 7003
        if failure == "cancel":
            task.cancel()
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else OpenLiveFailure):
            await asyncio.wait_for(task, 2)
        await stream.aclose()
    assert api.calls.count("start") == api.calls.count("end") == 1


def test_windows_store_write_delete_mock_only(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[bytes] = []
    targets: list[str] = []

    class FakeLibrary:
        def CredWriteW(self, record: object, flags: int) -> int:
            del flags
            item = ctypes.cast(
                cast(ctypes.c_void_p, record), ctypes.POINTER(WinCredentialRecord)
            ).contents
            targets.append(str(item.TargetName))
            captured.append(ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize))
            assert item.Type == 1 and item.Persist == 2
            return 1

        def CredDeleteW(self, target: str, kind: int, flags: int) -> int:
            assert kind == 1 and flags == 0
            targets.append(target)
            return 1

    library = FakeLibrary()
    monkeypatch.setattr(
        WindowsCredentialStore, "_library", staticmethod(lambda: cast(ctypes.WinDLL, library))
    )
    store = WindowsCredentialStore()
    store.write(APP, FAKE)
    store.delete(APP)
    assert targets == [credential_target(APP)] * 2
    assert decode_object(captured[0])["access_key_secret"] == "fake-secret"


def test_windows_store_read_mock_only_and_buffer_release(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = (
        b'{"access_key_id":"fake-id","access_key_secret":"fake-secret","anchor_code":"fake-anchor"}'
    )
    blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    record = WinCredentialRecord()
    record.CredentialBlobSize, record.CredentialBlob = len(raw), blob
    freed: list[bool] = []

    class FakeLibrary:
        def CredReadW(self, target: str, kind: int, flags: int, output: object) -> int:
            assert target == credential_target(APP) and kind == 1 and flags == 0
            result = ctypes.cast(
                cast(ctypes.c_void_p, output), ctypes.POINTER(ctypes.POINTER(WinCredentialRecord))
            )
            result[0] = ctypes.pointer(record)
            return 1

        def CredFree(self, value: object) -> None:
            del value
            freed.append(bytes(blob) == bytes(len(raw)))

    monkeypatch.setattr(
        WindowsCredentialStore, "_library", staticmethod(lambda: cast(ctypes.WinDLL, FakeLibrary()))
    )
    value = WindowsCredentialStore().read(APP)
    assert value == FAKE and freed == [True]
    assert "fake-secret" not in repr(value)


async def test_end_failure_remains_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    api = API(room=1)
    api.end_code = 9999
    mock_http(monkeypatch, api)
    statuses: list[str] = []
    source = OpenLiveSource(
        ROOM, APP, lambda: FAKE, statuses.append, http_transport=httpx.MockTransport(api)
    )
    with pytest.raises(OpenLiveFailure, match="room_mismatch"):
        await anext(source.messages())
    assert statuses[-1] == "session_end_uncertain"
    assert api.calls == ["start", "end"]


def test_hidden_input_window_uses_only_fake_store(monkeypatch: pytest.MonkeyPatch) -> None:
    root = tk.Tk()
    root.withdraw()
    writes: list[OpenLiveCredentials] = []
    errors: list[str] = []

    class FakeStore:
        def write(self, app_id: int, value: OpenLiveCredentials) -> None:
            assert app_id == APP
            writes.append(value)

    monkeypatch.setattr(credential_ui.tk, "Tk", lambda: root)
    monkeypatch.setattr(credential_ui, "WindowsCredentialStore", FakeStore)

    def fake_notice(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(credential_ui.messagebox, "showinfo", fake_notice)

    def submit() -> None:
        try:
            controls = root.winfo_children()[0].winfo_children()
            entries = [item for item in controls if isinstance(item, ttk.Entry)]
            assert len(entries) == 3
            for entry, value in zip(
                entries, ("fake-id", "fake-secret", "fake-anchor"), strict=True
            ):
                assert entry.cget("show") == "●"
                entry.insert(0, value)
            button = next(
                item
                for item in controls
                if isinstance(item, ttk.Button) and item.cget("text") == "保存凭据"
            )
            button.invoke()
            assert writes == [FAKE]
            assert all(entry.get() == "" for entry in entries)
            assert button.instate(["disabled"])
        except AssertionError:
            errors.append("hidden_ui_assertion_failed")
        finally:
            root.destroy()

    root.after(20, submit)
    credential_ui.main()
    assert not errors and writes == [FAKE]
