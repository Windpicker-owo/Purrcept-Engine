"""真实 Host/Worker/Store 与离线 OneBot 帧验证 QQ 目录、回执和旧公开参数。

传输只使用 test_qq_transport 显式 Socket 样例，不访问账号、网络或真实服务。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_qq_transport import Connector, Socket

from purrcept_engine import EngineConfig, EngineHost
from purrcept_engine.domain.execution.catalog import Catalog, ObjectDeclaration
from purrcept_engine.domain.execution.models import ActionRecord
from purrcept_engine.plugins.qq import factory
from purrcept_engine.plugins.qq.plugin import factory_with_connector
from purrcept_engine.values import JsonObject, JsonValue, decode_object, encode_json


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
                    "capacity_rule": "reference-text-only-v1",
                }
            },
            "subject": {"foreground": "local", "reflection": "local", "summary": "local"},
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
        "stickers",
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
    reference = value["raw_response"]
    assert isinstance(reference, dict)
    with sqlite3.connect(rig.directory / "engine.sqlite3") as database:
        row = database.execute(
            "SELECT payload_json FROM knowledge_evidence WHERE evidence_id=?",
            (reference["evidence_ref"],),
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
    )
    for name, arguments in cases:
        action = await rig.host.invoke("me.phone.qq." + name, arguments)
        assert action.calls[0].state == "failed"
        assert action.calls[0].error_type == "CapabilityInputError"
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
    assert isinstance(decode_object(call.result_json.encode())["raw_response"], dict)


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
        assert isinstance(
            decode_object(current.calls[0].result_json.encode())["raw_response"], dict
        )
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
        assert isinstance(value["raw_response"], dict)
        if state == "failed":
            assert value["retcode"] == -10000 and action.calls[0].error == "风控"
    assert rig.socket.sent.empty()
