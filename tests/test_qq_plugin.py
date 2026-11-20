"""真实 Host/Worker/Store 与离线 OneBot 帧验证 QQ 目录、回执和旧公开参数。

传输只使用 test_qq_transport 显式 Socket 样例，不访问账号、网络或真实服务。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from test_qq_transport import Connector, Socket

from purrcept_engine import EngineConfig, EngineHost
from purrcept_engine.domain.execution.catalog import Catalog, ObjectDeclaration
from purrcept_engine.domain.execution.models import ActionRecord
from purrcept_engine.plugins.qq import factory
from purrcept_engine.plugins.qq.plugin import factory_with_connector
from purrcept_engine.values import JsonObject, JsonValue, decode_object, encode_json

_BVID = "BV1xx411c7m9"
_BILI_URL = f"https://www.bilibili.com/video/{_BVID}/"
_ARK: JsonObject = {
    "app": "com.tencent.miniapp_01",
    "view": "view_8C8E89B49BE609866298ADDFF2DBABA4",
    "meta": {"detail_1": {"title": "测试视频", "qqdocurl": _BILI_URL}},
    "config": {"token": "synthetic-signature"},
}


def config(directory: Path, *, call_timeout: float = 15.0, page_chars: int = 8000) -> EngineConfig:
    """模型资源不触发请求；QQ 明确由离线工厂接入实际 Host。"""
    return EngineConfig.validate(
        {
            "host": {"data_dir": directory},
            "console": False,
            "context": {"page_chars": page_chars},
            "models": {
                "local": {
                    "kind": "generation",
                    "model": "openai/offline",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "context_window": 100000,
                    "max_output_tokens": 1000,
                }
            },
            "subject": {"foreground": "local", "reflection": "local"},
            "plugins": [
                {
                    "instance_id": "qq",
                    "entry": "qq-fixture",
                    "config": {
                        "mode": "direct",
                        "call_timeout_seconds": call_timeout,
                        "typing_delay_max_seconds": 0.0,
                    },
                }
            ],
        },
        base_dir=directory,
    )


@dataclass(frozen=True)
class Rig:
    host: EngineHost
    socket: Socket
    directory: Path

    async def call(
        self, name: str, arguments: JsonObject, data: JsonValue
    ) -> tuple[ActionRecord, JsonObject, JsonObject]:
        """实际派发后才提供指定回应，不假造 execution 或持久回执。"""
        task = asyncio.create_task(self.host.invoke("me.phone.qq." + name, arguments))
        request = await asyncio.wait_for(self.socket.sent.get(), 5)
        self.socket.reply(request, data)
        action = await asyncio.wait_for(task, 5)
        assert action.calls[0].state == "confirmed", action
        assert action.calls[0].result_json is not None
        value = decode_object(action.calls[0].result_json.encode())
        return action, request, value


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    socket = Socket()
    connector = Connector((socket,))
    host = EngineHost(
        config(tmp_path), plugin_factories={"qq-fixture": factory_with_connector(connector)}
    )
    await host.start()
    await asyncio.wait_for(connector.connected.wait(), 5)
    try:
        yield Rig(host, socket, tmp_path)
    finally:
        await host.close()


def test_pure_factory_declares_qq_paths_and_strict_params(tmp_path: Path) -> None:
    blueprint = factory.prepare("qq", {}, tmp_path)
    catalog = Catalog(
        (
            ObjectDeclaration("subject", "me", "主体"),
            ObjectDeclaration("phone", "phone", "应用", "subject"),
            *blueprint.objects,
        ),
        blueprint.capabilities,
    )
    names = {
        "send",
        "send_bilibili",
        "read",
        "friends",
        "groups",
        "members",
        "login",
        "recall",
        "mute",
        "unmute",
        "poke",
        "react",
        "forward",
        "compose_forward",
        "save_sticker",
        "send_sticker",
        "qzone_post",
        "qzone_list",
        "qzone_feeds",
        "qzone_comment",
        "qzone_like",
        "qzone_unlike",
        "qzone_delete",
        "qzone_set_right",
        "qzone_ban",
        "reconnect",
        "file_url",
        "avatar",
    }
    assert set(catalog.bindings) == {"me.phone.qq." + name for name in names}
    assert blueprint.required_modules == ("websockets",)
    assert not (tmp_path / ".purrcept").exists()
    assert catalog.resolve("me.phone.qq.reconnect").declaration.control
    for raw in ({"owner_id": "x"}, {"group_list": [True]}, {"host": "http://unsafe"}):
        with pytest.raises(ValidationError):
            factory.prepare("qq", raw, tmp_path)


@pytest.mark.parametrize(
    "name,arguments,action,params,data",
    [
        (
            "send",
            {"chat": "private", "target_id": "123", "text": "正文"},
            "send_private_msg",
            {"user_id": 123, "message": "正文"},
            {"message_id": -123},
        ),
        ("friends", {}, "get_friend_list", {}, [{"user_id": 123, "nickname": "例"}]),
        ("groups", {}, "get_group_list", {}, [{"group_id": 123, "group_name": "群"}]),
        (
            "members",
            {"group_id": "123"},
            "get_group_member_list",
            {"group_id": 123},
            [{"user_id": 456}],
        ),
        ("login", {}, "get_login_info", {}, {"user_id": 123, "nickname": "自己"}),
        ("recall", {"message_id": "-123"}, "delete_msg", {"message_id": -123}, None),
        (
            "mute",
            {"group_id": "123", "user_id": "456", "duration_seconds": 9999999},
            "set_group_ban",
            {"group_id": 123, "user_id": 456, "duration": 2592000},
            None,
        ),
        (
            "unmute",
            {"group_id": "123", "user_id": "456"},
            "set_group_ban",
            {"group_id": 123, "user_id": 456, "duration": 0},
            None,
        ),
        (
            "poke",
            {"chat": "group", "target_id": "123", "user_id": "456"},
            "send_poke",
            {"group_id": 123, "user_id": 456},
            None,
        ),
        (
            "react",
            {"message_id": "-123", "emoji_id": "66", "add": False},
            "set_msg_emoji_like",
            {"message_id": -123, "emoji_id": "66", "set": False},
            None,
        ),
        (
            "forward",
            {"chat": "private", "target_id": "123", "message_id": "-456"},
            "forward_friend_single_msg",
            {"user_id": 123, "message_id": -456},
            {"message_id": 789},
        ),
        (
            "compose_forward",
            {
                "chat": "group",
                "target_id": "123",
                "nodes": [{"name": "例", "uin": "456", "content": "正文"}],
                "source": "出处",
            },
            "send_group_forward_msg",
            {
                "group_id": 123,
                "messages": [
                    {"type": "node", "data": {"name": "例", "uin": "456", "content": "正文"}}
                ],
                "source": "出处",
            },
            {"message_id": 789, "res_id": "merged"},
        ),
        (
            "qzone_post",
            {"content": "原文", "ugc_right": 16, "target_uins": ["123"]},
            "send_qzone_msg",
            {"content": "原文", "ugc_right": 16, "target_uins": [123]},
            None,
        ),
        (
            "qzone_list",
            {"target_uin": "123", "pos": 2, "num": 200},
            "get_qzone_msg_list",
            {"target_uin": 123, "pos": 2, "num": 100},
            {
                "total": 5,
                "msglist": [
                    {
                        "tid": "abc",
                        "content": "完整原文",
                        "time": 1700000000,
                        "images": ["https://example.invalid/image"],
                    }
                ],
            },
        ),
        (
            "qzone_feeds",
            {"count": 100},
            "get_qzone_feeds",
            {"page_num": 1, "count": 50},
            {
                "feeds": [
                    {
                        "uin": 123,
                        "nickname": "例",
                        "time": 1700000000,
                        "appid": 311,
                        "key": "abc",
                        "html": "<p>正文</p>",
                    }
                ],
                "has_more": True,
            },
        ),
        (
            "qzone_comment",
            {"tid": "abc", "content": "评论", "target_uin": "123"},
            "comment_qzone",
            {"tid": "abc", "content": "评论", "target_uin": 123},
            {"comment_id": "c1"},
        ),
        (
            "qzone_like",
            {"tid": "abc", "target_uin": "123", "abstime": 1700000000},
            "like_qzone",
            {"tid": "abc", "target_uin": 123, "abstime": 1700000000},
            None,
        ),
        ("qzone_unlike", {"tid": "abc"}, "unlike_qzone", {"tid": "abc"}, None),
        ("qzone_delete", {"tid": "abc"}, "delete_qzone_msg", {"tid": "abc"}, None),
        (
            "qzone_set_right",
            {"tid": "abc", "ugc_right": 64},
            "set_qzone_msg_right",
            {"tid": "abc", "ugc_right": 64},
            None,
        ),
        (
            "qzone_ban",
            {"user_id": "123", "enable": False},
            "set_qzone_ban",
            {"user_id": 123, "enable": False},
            None,
        ),
    ],
)
async def test_actual_provider_uses_one_frozen_action_and_params(
    rig: Rig, name: str, arguments: JsonObject, action: str, params: JsonObject, data: JsonValue
) -> None:
    _, request, value = await rig.call(name, arguments, data)
    assert request["action"] == action and request["params"] == params
    assert rig.socket.sent.empty()
    reference = value["raw_ref"]
    assert isinstance(reference, str)
    with sqlite3.connect(rig.directory / "engine.sqlite3") as database:
        row = database.execute(
            "SELECT payload_json FROM knowledge_evidence WHERE evidence_id=?",
            (reference,),
        ).fetchone()
    assert row is not None
    saved = decode_object(str(row[0]).encode())
    assert decode_object(str(saved["text"]).encode())["data"] == data
    if name in {"qzone_post", "qzone_set_right"}:
        unknown = value["unknown_fields"]
        assert isinstance(unknown, list)
        assert value["ugc_right"] is None and "ugc_right" in unknown
    if name == "qzone_feeds":
        assert value["level"] == "overview" and value["has_more"] is True


async def test_invalid_params_and_unsupported_deep_feed_do_not_send(rig: Rig) -> None:
    cases: tuple[tuple[str, JsonObject], ...] = (
        ("qzone_feeds", {"page_num": 2}),
        ("send", {"chat": "private", "target_id": "123", "text": "x", "at": "456"}),
        ("read", {"cursor": "x", "chat": "private", "target_id": "123"}),
        ("qzone_post", {"content": "x", "ugc_right": 16}),
        ("file_url", {"chat": "private", "target_id": "123", "file_id": "f", "busid": 1}),
        ("avatar", {"user_id": "123&extra=1"}),
        ("avatar", {"user_id": 123}),
    )
    for name, arguments in cases:
        action = await rig.host.invoke("me.phone.qq." + name, arguments)
        assert action.calls[0].state == "failed"
        assert action.calls[0].error_type == "CapabilityInputError"
    assert rig.socket.sent.empty()


async def test_avatar_returns_public_url_without_onebot_request(rig: Rig) -> None:
    action = await rig.host.invoke("me.phone.qq.avatar", {"user_id": "123456"})
    call = action.calls[0]
    assert call.state == "confirmed"
    assert call.result_json is not None
    assert decode_object(call.result_json.encode()) == {
        "user_id": "123456",
        "url": "https://q1.qlogo.cn/g?b=qq&nk=123456&s=640",
    }
    assert rig.socket.sent.empty()


async def test_group_file_url_preserves_file_identity_and_busid(rig: Rig) -> None:
    _, request, value = await rig.call(
        "file_url",
        {"chat": "group", "target_id": "123", "file_id": "group-file", "busid": 102},
        {"url": "https://files.example/group.pdf"},
    )
    assert request["action"] == "get_group_file_url"
    assert request["params"] == {"group_id": 123, "file_id": "group-file", "busid": 102}
    assert value["url"] == "https://files.example/group.pdf"


@pytest.mark.parametrize(
    "data", [{}, {"url": "file:///C:/local.pdf"}, {"url": "https://user:secret@example.com/file"}]
)
async def test_file_url_requires_real_http_link_and_keeps_raw_response(
    rig: Rig, data: JsonObject
) -> None:
    task = asyncio.create_task(
        rig.host.invoke(
            "me.phone.qq.file_url",
            {"chat": "private", "target_id": "123", "file_id": "file"},
        )
    )
    request = await asyncio.wait_for(rig.socket.sent.get(), 3)
    rig.socket.reply(request, data)
    action = await task
    call = action.calls[0]
    assert call.state == "unknown" and call.error_type == "ConnectionRejected"
    assert call.result_json is not None
    assert isinstance(decode_object(call.result_json.encode())["raw_ref"], str)


async def test_real_worker_uses_qq_capability_and_platform_identity(rig: Rig) -> None:
    task = asyncio.create_task(
        rig.host.execute(
            "from world import me\n"
            "result = await me.phone.qq.send(chat='group', target_id='123', "
            "text='实际替身发送', reply='-456', at='789')\n"
            "return result.message_id"
        )
    )
    request = await asyncio.wait_for(rig.socket.sent.get(), 5)
    assert request["action"] == "send_group_msg"
    params = request["params"]
    assert isinstance(params, dict)
    assert params["message"] == [
        {"type": "reply", "data": {"id": "-456"}},
        {"type": "at", "data": {"qq": "789"}},
        {"type": "text", "data": {"text": " 实际替身发送"}},
    ]
    rig.socket.reply(request, {"message_id": -999})
    action = await task
    assert action.state == "completed" and action.result_json == '"-999"'
    assert action.calls[0].state == "confirmed" and rig.socket.sent.empty()


@pytest.fixture
def bili_http(monkeypatch: pytest.MonkeyPatch) -> list[httpx.URL]:
    """只允许访问已知 B 站短链和公开视频详情，记录每次真实 HTTP 请求。"""
    seen: list[httpx.URL] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        assert request.method == "GET" and "Cookie" not in request.headers
        if request.url.host == "b23.tv":
            if request.url.path == "/Other":
                return httpx.Response(302, headers={"Location": "https://untrusted.example/video"})
            assert request.url.path == "/Short"
            return httpx.Response(302, headers={"Location": _BILI_URL + "?p=2&t=12&spm_id_from=x"})
        assert (
            request.url.host == "api.bilibili.com" and request.url.path == "/x/web-interface/view"
        )
        if request.url.params["bvid"] != _BVID:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "bvid": _BVID,
                    "title": "测试视频",
                    "desc": "测试简介",
                    "pic": "http://i0.hdslb.com/bfs/archive/example.jpg",
                },
            },
        )

    transport = httpx.MockTransport(handle)

    def route(client: httpx.AsyncClient, url: httpx.URL) -> httpx.MockTransport:
        del client, url
        return transport

    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", route)
    return seen


async def test_worker_sends_signed_bilibili_card_for_short_link(
    rig: Rig, bili_http: list[httpx.URL]
) -> None:
    """通过真实能力目录解析短链，用签名原文构造 OneBot JSON 段。"""
    task = asyncio.create_task(
        rig.host.execute(
            "from world import me\n"
            "return await me.phone.qq.send_bilibili("
            "chat='group', target_id='123', video='https://b23.tv/Short')"
        )
    )
    sign = await asyncio.wait_for(rig.socket.sent.get(), 5)
    assert sign["action"] == "get_mini_app_ark"
    link = _BILI_URL + "?p=2&t=12"
    assert sign["params"] == {
        "type": "bili",
        "title": "测试视频",
        "desc": "测试简介",
        "picUrl": "https://i0.hdslb.com/bfs/archive/example.jpg",
        "jumpUrl": link,
        "webUrl": link,
    }
    rig.socket.reply(sign, {"data": _ARK})
    send = await asyncio.wait_for(rig.socket.sent.get(), 5)
    assert send["action"] == "send_group_msg"
    assert send["params"] == {
        "group_id": 123,
        "message": [{"type": "json", "data": {"data": encode_json(_ARK).decode()}}],
    }
    rig.socket.reply(send, {"message_id": 543})
    result = await task
    assert result.state == "completed" and result.result_json is not None
    value = decode_object(result.result_json.encode())
    assert value["format"] == "bilibili_card" and value["message_id"] == "543"
    assert value["url"] == link and isinstance(value["ark_ref"], str)
    assert len(bili_http) == 2 and rig.socket.sent.empty()


@pytest.mark.parametrize("failure", ["metadata", "signing", "malformed_ark", "card_send"])
async def test_bilibili_falls_back_to_one_plain_link_on_definite_failure(
    rig: Rig, bili_http: list[httpx.URL], failure: str
) -> None:
    """前置失败或明确发送拒绝时，用同一目标发送一条普通链接。"""
    video = "BV1xx411c7m8" if failure == "metadata" else _BVID
    link = f"https://www.bilibili.com/video/{video}/"
    task = asyncio.create_task(
        rig.host.invoke(
            "me.phone.qq.send_bilibili", {"chat": "private", "target_id": "123", "video": video}
        )
    )
    request = await asyncio.wait_for(rig.socket.sent.get(), 5)
    if failure != "metadata":
        assert request["action"] == "get_mini_app_ark"
        if failure == "malformed_ark":
            rig.socket.reply(request, {"data": {"ark": "Invalid JSON"}})
        else:
            if failure == "card_send":
                # 文档也允许嵌套字符串，发送时保持其确切内容。
                signed = encode_json(_ARK).decode() + "\n"
                rig.socket.reply(request, {"data": {"ark": signed}})
                request = await asyncio.wait_for(rig.socket.sent.get(), 5)
                assert request["action"] == "send_private_msg"
                assert request["params"] == {
                    "user_id": 123,
                    "message": [{"type": "json", "data": {"data": signed}}],
                }
            rig.socket.incoming.put_nowait(
                encode_json(
                    {
                        "status": "failed",
                        "retcode": 1200,
                        "data": None,
                        "echo": request["echo"],
                        "message": "不支持该操作",
                    }
                ).decode()
            )
        request = await asyncio.wait_for(rig.socket.sent.get(), 5)
    assert request["action"] == "send_private_msg"
    assert request["params"] == {
        "user_id": 123,
        "message": [{"type": "text", "data": {"text": link}}],
    }
    rig.socket.reply(request, {"message_id": 44})
    result = await task
    assert result.calls[0].state == "confirmed" and result.calls[0].result_json is not None
    value = decode_object(result.calls[0].result_json.encode())
    assert value["format"] == "link" and value["message_id"] == "44"
    fallback = value["fallback"]
    expected = {"metadata": "video_metadata", "card_send": "card_send"}.get(failure, "ark_signing")
    assert isinstance(fallback, dict) and fallback["stage"] == expected
    assert len(bili_http) == 1 and rig.socket.sent.empty()


@pytest.mark.parametrize("stage", ["signing", "card_send"])
async def test_bilibili_timeout_fallback_distinguishes_signing_and_writing(
    tmp_path: Path, bili_http: list[httpx.URL], stage: str
) -> None:
    """签名超时可降级，写入超时保留迟到确认，不能派发第二条消息。"""
    socket = Socket()
    connector = Connector((socket,))
    host = EngineHost(
        config(tmp_path, call_timeout=0.4),
        plugin_factories={"qq-fixture": factory_with_connector(connector)},
    )
    await host.start()
    await connector.connected.wait()
    try:
        task = asyncio.create_task(
            host.invoke(
                "me.phone.qq.send_bilibili",
                {"chat": "private", "target_id": "123", "video": _BVID},
            )
        )
        sign = await asyncio.wait_for(socket.sent.get(), 5)
        assert sign["action"] == "get_mini_app_ark"
        if stage == "card_send":
            socket.reply(sign, {"data": _ARK})
        send = await asyncio.wait_for(socket.sent.get(), 5)
        assert send["action"] == "send_private_msg"
        if stage == "signing":
            assert send["params"] == {
                "user_id": 123,
                "message": [{"type": "text", "data": {"text": _BILI_URL}}],
            }
            socket.reply(send, {"message_id": 33})
            assert (await task).calls[0].state == "confirmed"
        else:
            initial = await task
            assert initial.calls[0].state == "unknown" and socket.sent.empty()
            socket.reply(send, {"message_id": 99})
            async with asyncio.timeout(5):
                while True:
                    current = await host.query_action(initial.action_id)
                    if current is not None and current.calls[0].state == "confirmed":
                        break
                    await asyncio.sleep(0.005)
            raw = current.calls[0].result_json
            assert raw is not None and decode_object(raw.encode())["format"] == "bilibili_card"
        assert len(bili_http) == 1 and socket.sent.empty()
    finally:
        await host.close()


async def test_bilibili_rejects_short_link_to_unapproved_host(
    rig: Rig, bili_http: list[httpx.URL]
) -> None:
    """跳转到站外时不继续联网，也不将已知站外目标作为链接发送。"""
    result = await rig.host.invoke(
        "me.phone.qq.send_bilibili",
        {"chat": "private", "target_id": "123", "video": "https://b23.tv/Other"},
    )
    assert result.calls[0].state == "failed"
    assert len(bili_http) == 1 and rig.socket.sent.empty()


async def test_late_file_url_uses_same_protocol_validation(tmp_path: Path) -> None:
    """过期查询的迟到成功包也不能把本地路径声明成可下载链接。"""
    socket = Socket()
    connector = Connector((socket,))
    host = EngineHost(
        config(tmp_path, call_timeout=0.03),
        plugin_factories={"qq-fixture": factory_with_connector(connector)},
    )
    await host.start()
    try:
        await connector.connected.wait()
        task = asyncio.create_task(
            host.invoke(
                "me.phone.qq.file_url",
                {
                    "chat": "private",
                    "target_id": "123",
                    "file_id": "file",
                },
            )
        )
        request = await socket.sent.get()
        initial = await task
        assert initial.calls[0].state == "unknown"
        socket.reply(request, {"url": "file:///C:/remote/file.zip"})
        async with asyncio.timeout(3):
            while True:
                current = await host.query_action(initial.action_id)
                if current is not None and current.calls[0].error_type == "ConnectionRejected":
                    break
                await asyncio.sleep(0.005)
        assert current.calls[0].state == "unknown"
        assert current.calls[0].result_json is not None
        assert isinstance(decode_object(current.calls[0].result_json.encode())["raw_ref"], str)
    finally:
        await host.close()


async def test_late_echo_appends_real_call_version_without_rewriting_action(tmp_path: Path) -> None:
    socket = Socket()
    connector = Connector((socket,))
    host = EngineHost(
        config(tmp_path, call_timeout=0.03),
        plugin_factories={"qq-fixture": factory_with_connector(connector)},
    )
    await host.start()
    await connector.connected.wait()
    try:
        task = asyncio.create_task(
            host.invoke(
                "me.phone.qq.send",
                {"chat": "private", "target_id": "123", "text": "一次"},
                input_id="source",
            )
        )
        request = await socket.sent.get()
        initial = await task
        assert initial.calls[0].state == "unknown"
        socket.reply(request, {"message_id": -22})
        async with asyncio.timeout(5):
            while True:
                actual = await host.query_action(initial.action_id)
                if actual is not None and actual.calls[0].state == "confirmed":
                    break
                await asyncio.sleep(0.005)
        assert actual.state == initial.state
        versions = actual.calls[0].history
        assert versions[0].state == "unknown" and versions[-1].state == "confirmed"
        assert all(version.state == "unknown" for version in versions[:-1])
        assert actual.calls[0].result_json is not None
        assert decode_object(actual.calls[0].result_json.encode())["message_id"] == "-22"
        assert socket.sent.empty()
    finally:
        await host.close()


async def test_forward_id_converts_actual_received_nodes_once(rig: Rig) -> None:
    task = asyncio.create_task(
        rig.host.invoke(
            "me.phone.qq.forward",
            {"chat": "group", "target_id": "123", "forward_id": "native-forward"},
        )
    )
    first = await rig.socket.sent.get()
    assert first["action"] == "get_forward_msg" and first["params"] == {"id": "native-forward"}
    rig.socket.reply(
        first,
        {"messages": [{"sender": {"nickname": "来源", "user_id": 456}, "content": "完整正文"}]},
    )
    second = await rig.socket.sent.get()
    assert second["action"] == "send_group_forward_msg"
    assert second["params"] == {
        "group_id": 123,
        "messages": [
            {"type": "node", "data": {"name": "来源", "uin": "456", "content": "完整正文"}}
        ],
    }
    rig.socket.reply(second, {"message_id": -66})
    action = await task
    assert action.calls[0].result_json is not None
    value = decode_object(action.calls[0].result_json.encode())
    assert value["mode"] == "direct" and value["node_count"] == 1
    assert rig.socket.sent.empty()


async def test_invalid_envelope_is_unknown_and_retcode_rejection_keeps_raw(rig: Rig) -> None:
    cases: tuple[tuple[JsonObject, str], ...] = (
        ({"data": None}, "unknown"),
        ({"status": "ok", "retcode": False, "data": None}, "unknown"),
        ({"status": "failed", "retcode": -10000, "data": None, "message": "风控"}, "failed"),
    )
    for response, state in cases:
        task = asyncio.create_task(rig.host.invoke("me.phone.qq.qzone_delete", {"tid": "abc"}))
        request = await rig.socket.sent.get()
        rig.socket.incoming.put_nowait(encode_json({**response, "echo": request["echo"]}).decode())
        action = await task
        assert action.calls[0].state == state
        assert action.calls[0].result_json is not None
        value = decode_object(action.calls[0].result_json.encode())
        assert isinstance(value["raw_ref"], str)
        if state == "failed":
            assert value["retcode"] == -10000 and action.calls[0].error == "风控"
    assert rig.socket.sent.empty()
