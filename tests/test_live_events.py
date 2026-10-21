"""礼物/SC端到端离线回归：不连接真实平台、账号或付费入口。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from test_live_open import API, APP, FAKE, ROOM, fast_clocks, mock_http
from test_live_stage import Inputs, http_snapshot
from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection, serve

from plugins.live_stage import open_live
from plugins.live_stage.bilibili import Danmaku, ProtocolError, packet
from plugins.live_stage.events import (
    Gift,
    SuperChat,
    SuperChatDelete,
    gift_label,
    interaction_data,
    parse_interaction,
)
from plugins.live_stage.open_live import OpenLiveSource
from plugins.live_stage.runtime import LiveStage, Settings
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system import PluginPorts

NOW = 1_800_000_000


def payload(kind: str, **changes: object) -> bytes:
    data: dict[str, object] = {
        "room_id": ROOM,
        "msg_id": "native-1",
        "uname": "测试观众",
        "open_id": "test-user",
        "timestamp": NOW,
        "gift_name": "测试礼物",
        "gift_num": 3,
        "paid": True,
        "price": 1250,
        "r_price": 900,
        "message_id": 42,
        "message": "<script>危险</script>",
        "rmb": 30,
        "start_time": NOW - 1,
        "end_time": NOW + 10,
        "message_ids": [42],
    }
    data.update(changes)
    return json.dumps({"cmd": "LIVE_OPEN_PLATFORM_" + kind, "data": data}).encode()


def gift() -> Gift:
    value = parse_interaction(payload("SEND_GIFT"), ROOM)
    assert isinstance(value, Gift)
    return value


def sc() -> SuperChat:
    value = parse_interaction(payload("SUPER_CHAT"), ROOM)
    assert isinstance(value, SuperChat)
    return value


def owner_at(now: list[float], inputs: Inputs) -> LiveStage:
    return LiveStage(
        "stage",
        Settings(source="open_live", room_id=ROOM, open_live_app_id=APP, port=0),
        PluginPorts(inputs, lambda: {"phase": "running"}),
        clock=lambda: now[0],
    )


def test_amounts_and_free_gifts_keep_units() -> None:
    value = gift()
    assert gift_label(value) == "测试礼物 ×3 · 付费礼物 · 标价¥1.25/件"
    assert "¥" not in gift_label(replace(value, paid=False))
    assert "免费" in gift_label(replace(value, paid=False))
    assert interaction_data(value)["platform_r_price"] == 900
    assert interaction_data(replace(value, paid=False))["price_unit"] == "银瓜子，非人民币"
    assert sc().rmb == 30
    assert sc().native_id == "open_live:sc:42"
    # 同一SC即使换投递msg_id也不变成新的付费留言。
    other = parse_interaction(payload("SUPER_CHAT", msg_id="replayed-envelope"), ROOM)
    assert isinstance(other, SuperChat) and other.native_id == sc().native_id


@pytest.mark.parametrize(
    "kind,changes",
    [
        ("SEND_GIFT", {"paid": 1}),
        ("SEND_GIFT", {"gift_num": 0}),
        ("SEND_GIFT", {"price": -1}),
        ("SEND_GIFT", {"r_price": "1"}),
        ("SEND_GIFT", {"room_id": ROOM + 1}),
        ("SEND_GIFT", {"msg_id": ""}),
        ("SUPER_CHAT", {"rmb": True}),
        ("SUPER_CHAT", {"message_id": 0}),
        ("SUPER_CHAT", {"end_time": NOW - 5}),
        ("SUPER_CHAT_DEL", {"message_ids": [True]}),
        ("SUPER_CHAT_DEL", {"message_ids": []}),
    ],
)
def test_malformed_interactions_fail_closed(kind: str, changes: dict[str, object]) -> None:
    with pytest.raises(ProtocolError):
        parse_interaction(payload(kind, **changes), ROOM)


async def test_gift_sc_dedup_deletion_and_ordinary_danmaku() -> None:
    inputs, now = Inputs(), [float(NOW)]
    owner = owner_at(now, inputs)
    await owner.receive(gift())
    await owner.receive(gift())
    await owner.receive(sc())
    await owner.receive(sc())
    assert len(inputs.submissions) == 2
    assert [s.source.event_kind for s in inputs.submissions] == ["gift", "super_chat"]
    assert all(not s.context.minimum_wake for s in inputs.submissions)
    assert json.loads(inputs.submissions[1].text)["text"] == sc().text
    current = owner.snapshot()["super_chats"]
    assert isinstance(current, list) and len(current) == 1
    deletion = SuperChatDelete("delete-1", (42,))
    await owner.receive(deletion)
    await owner.receive(deletion)
    await owner.receive(SuperChatDelete("delete-2", (42,)))
    await owner.receive(sc())
    assert owner.snapshot()["super_chats"] == []
    assert len(inputs.submissions) == 3
    assert inputs.submissions[-1].source.event_kind == "super_chat_delete"
    assert "危险" not in inputs.submissions[-1].text
    await owner.receive(Danmaku("D", "u", "正常弹幕", NOW, "dm-1"))
    assert inputs.submissions[-1].source.event_kind == "danmaku"
    assert len(owner.messages) == 2


async def test_delete_before_arrival_and_expiry_never_replay() -> None:
    inputs, now = Inputs(), [float(NOW)]
    owner = owner_at(now, inputs)
    await owner.receive(SuperChatDelete("early-delete", (42,)))
    await owner.receive(sc())
    assert not inputs.submissions and not owner.super_chats
    fresh = replace(sc(), message_id=43, native_id="open_live:sc:43")
    await owner.receive(fresh)
    assert len(inputs.submissions) == 1
    now[0] += 11
    assert owner.snapshot()["super_chats"] == []
    await owner.receive(fresh)
    await owner.receive(replace(sc(), message_id=44, native_id="open_live:sc:44"))
    assert len(inputs.submissions) == 1
    assert owner.event_counts["expired"] == 2
    assert owner.event_counts["deleted_before_arrival"] == 1


async def test_future_sc_and_snapshot_limit_do_not_evict_tombstones() -> None:
    inputs, now = Inputs(), [float(NOW)]
    owner = owner_at(now, inputs)
    future = replace(sc(), start_time=NOW + 3)
    await owner.receive(future)
    assert owner.snapshot()["super_chats"] == []
    now[0] += 4
    current = owner.snapshot()["super_chats"]
    assert isinstance(current, list) and len(current) == 1
    owner.interaction_seen.update(str(n) for n in range(20_000))
    with pytest.raises(ProtocolError):
        await owner.receive(gift())


async def test_interactions_over_reconnecting_official_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_clocks(monkeypatch)
    api = API()
    mock_http(monkeypatch, api)
    connections = 0
    ready, release = asyncio.Event(), asyncio.Event()

    async def handler(ws: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        await ws.recv()
        await ws.send(packet(8, b'{"code":0}'))
        await ws.recv()
        # 第二连接重放礼物/SC，再撤回；不允许重复输入或SC复活。
        await ws.send(packet(5, payload("SEND_GIFT")) + packet(5, payload("SUPER_CHAT")))
        if connections == 1:
            await ws.close()
            return
        await ws.send(packet(5, payload("SUPER_CHAT_DEL")))
        await ws.send(packet(5, payload("SUPER_CHAT")))
        ready.set()
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
            clock=lambda: float(NOW),
        )
        await owner.start()
        try:
            await asyncio.wait_for(ready.wait(), 3)
            await asyncio.sleep(0.05)
            _, snapshot = await http_snapshot(owner.bound_port)
            assert snapshot["super_chats"] == []
            assert len(inputs.submissions) == 3
            assert [s.source.event_kind for s in inputs.submissions] == [
                "gift",
                "super_chat",
                "super_chat_delete",
            ]
            assert owner.event_counts["duplicate"] == 3
        finally:
            release.set()
            await owner.close()
    assert connections == 2
    assert api.calls.count("start") == api.calls.count("end") == 1


async def test_real_temporary_store_retains_native_interaction_identity(
    tmp_path: Path,
) -> None:
    inputs = Inputs()
    owner = owner_at([float(NOW)], inputs)
    await owner.receive(gift())
    await owner.receive(sc())
    await owner.receive(SuperChatDelete("store-delete", (42,)))
    store = await Store.open(tmp_path / "events.sqlite3")
    try:
        for submission in inputs.submissions:
            first = await store.admit(submission)
            again = await store.admit(submission)
            assert first.input_id == again.input_id and again.duplicate
            record = await store.get_native_input(submission.source)
            assert record is not None and record.source.event_kind == submission.source.event_kind
    finally:
        await store.close()


async def test_deleted_sc_survives_plugin_restart(tmp_path: Path) -> None:
    path = tmp_path / "deleted.json"
    inputs = Inputs()
    settings = Settings(source="open_live", room_id=ROOM, open_live_app_id=APP, port=0)
    ports = PluginPorts(inputs, lambda: {"phase": "running"})
    first = LiveStage("stage", settings, ports, clock=lambda: float(NOW), state_path=path)
    await first.receive(sc())
    await first.receive(SuperChatDelete("persisted-delete", (42,)))
    second = LiveStage("stage", settings, ports, clock=lambda: float(NOW), state_path=path)
    await second.receive(sc())
    await second.receive(SuperChatDelete("replayed-delete", (42,)))
    assert not second.super_chats and len(inputs.submissions) == 2
    assert json.loads(path.read_text())["message_ids"] == [42]
    assert "危险" not in path.read_text()
    path.write_text('{"version":1,"message_ids":[true]}')
    with pytest.raises(ProtocolError):
        LiveStage("stage", settings, ports, state_path=path)
