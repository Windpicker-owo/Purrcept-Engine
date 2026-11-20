"""QQ 附件/贴纸的真实临时文件、旧 get_image 路径及取消后回执资源寿命。"""

from __future__ import annotations

import asyncio
import base64
import io
import subprocess
from pathlib import Path

import pytest
from PIL import Image
from test_qq_input_history import PNG
from test_qq_plugin import config
from test_qq_transport import Connector, Socket

from purrcept_engine import EngineHost
from purrcept_engine.plugins.qq.media import MediaOwner, QQMediaError
from purrcept_engine.plugins.qq.parameters import Send
from purrcept_engine.plugins.qq.plugin import factory_with_connector
from purrcept_engine.plugins.qq.settings import QQSettings
from purrcept_engine.values import decode_object, encode_json


async def test_attachment_order_paths_and_owned_voice_cleanup(tmp_path: Path) -> None:
    owner = MediaOwner(QQSettings(), tmp_path)
    directory = tmp_path / ".purrcept"
    directory.mkdir()
    (directory / "image.png").write_bytes(PNG)
    (directory / "file.txt").write_bytes(b"body")
    parameters = Send(
        chat="group",
        target_id="123",
        text="正文",
        at="456",
        reply="-789",
        voice=base64.b64encode(b"RIFF synthetic WAV").decode(),
        images=[".purrcept/image.png"],
        files=[".purrcept/file.txt"],
    )
    try:
        segments, temporary = await owner.io(lambda: owner.prepare(parameters))
        assert isinstance(segments, list)
        assert [part["type"] for part in segments if isinstance(part, dict)] == [
            "reply",
            "at",
            "text",
            "record",
            "text",
            "image",
            "file",
        ]
        images = [part for part in segments if isinstance(part, dict) and part["type"] == "image"]
        assert images == [
            {"type": "image", "data": {"file": "base64://" + base64.b64encode(PNG).decode()}}
        ]
        assert len(temporary) == 1 and temporary[0].read_bytes() == b"RIFF synthetic WAV"
        bad = Send(
            chat="private",
            target_id="123",
            voice=base64.b64encode(b"voice").decode(),
            files=["https://example.invalid/no"],
        )
        with pytest.raises(QQMediaError, match="本地文件"):
            await owner.io(lambda: owner.prepare(bad))
        assert len(list(owner.media_dir.glob("voice-*"))) == 1
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        with pytest.raises(QQMediaError, match="允许目录"):
            await owner.io(lambda: owner.read(str(outside)))
    finally:
        await owner.close()
    assert list(owner.media_dir.glob("voice-*")) == []
    assert (directory / "file.txt").exists()


def _uncompressed_png(edge: int) -> bytes:
    """构造指定尺寸的真实 PNG，让 Base64 体积由可预期的像素数量决定。"""
    with Image.new("RGB", (edge, edge)) as image, io.BytesIO() as buffer:
        image.save(buffer, format="PNG", compress_level=0)
        return buffer.getvalue()


@pytest.mark.parametrize("edge", [400, 512])
async def test_large_local_images_fit_one_complete_request(tmp_path: Path, edge: int) -> None:
    """默认额度下，验证单张或累计内联超限时仍按原顺序发送完整消息。"""
    directory = tmp_path / ".purrcept"
    directory.mkdir()
    first, second = directory / "第一张.png", directory / "第二张.png"
    attachment = directory / "说明.txt"
    image = await asyncio.to_thread(_uncompressed_png, edge)
    for path in (first, second):
        await asyncio.to_thread(path.write_bytes, image)
    await asyncio.to_thread(attachment.write_text, "图片说明", encoding="utf-8")

    socket = Socket()
    connector = Connector((socket,))
    host = EngineHost(
        config(tmp_path), plugin_factories={"qq-fixture": factory_with_connector(connector)}
    )
    await host.start()
    try:
        task = asyncio.create_task(
            host.invoke(
                "me.phone.qq.send",
                {
                    "chat": "group",
                    "target_id": "123",
                    "text": "两张出图供对比",
                    "images": [str(first), str(second)],
                    "files": [str(attachment)],
                },
            )
        )
        request = await asyncio.wait_for(socket.sent.get(), 5)
        assert request["action"] == "send_group_msg"
        assert len(encode_json(request)) <= QQSettings().frame_bytes
        params = request["params"]
        assert isinstance(params, dict)
        first_source = (
            "base64://" + base64.b64encode(image).decode() if edge == 400 else first.as_uri()
        )
        assert params["message"] == [
            {"type": "text", "data": {"text": "两张出图供对比"}},
            {"type": "image", "data": {"file": first_source}},
            {"type": "image", "data": {"file": second.as_uri()}},
            {"type": "file", "data": {"file": attachment.as_uri(), "name": attachment.name}},
        ]
        socket.reply(request, {"message_id": 456})
        result = await asyncio.wait_for(task, 5)
        assert result.calls[0].state == "confirmed"
        assert socket.sent.empty()
    finally:
        await host.close()


async def test_overflowing_base64_is_staged_and_failed_preparation_cleans_up(
    tmp_path: Path,
) -> None:
    """暂存原图只缩小信封，后续准备失败仅清理本次新文件。"""
    owner = MediaOwner(QQSettings(frame_bytes=512), tmp_path)
    image = await asyncio.to_thread(_uncompressed_png, 32)
    raw = "base64://" + base64.b64encode(image).decode()
    send = Send(chat="private", target_id="123", images=[raw])
    try:
        segments, temporary = await owner.io(lambda: owner.prepare(send))
        assert len(temporary) == 1
        path = temporary[0]
        assert await asyncio.to_thread(path.read_bytes) == image
        assert segments == [{"type": "image", "data": {"file": path.as_uri()}}]

        invalid = Send(chat="private", target_id="123", images=[raw], text="大" * 200)
        with pytest.raises(QQMediaError, match="超过请求帧容量"):
            await owner.io(lambda: owner.prepare(invalid))
        assert list(owner.media_dir.glob("image-*")) == [path]
        await owner.io(lambda: owner.release(temporary))
        assert not path.exists()
    finally:
        await owner.close()


@pytest.mark.parametrize("adjustment", [0, -1])
async def test_attachment_budget_includes_complete_utf8_envelope(
    tmp_path: Path, adjustment: int
) -> None:
    """小图内联可比长路径更短，完整信封刚好用满额度时仍可派发。"""
    directory = tmp_path / ".purrcept"
    directory.mkdir()
    image = directory / ("图" * 40 + ".png")
    await asyncio.to_thread(image.write_bytes, PNG)
    source = "base64://" + base64.b64encode(PNG).decode()
    assert len(source) < len(image.as_uri())
    segments = [
        {"type": "text", "data": {"text": "正文"}},
        {"type": "image", "data": {"file": source}},
    ]
    frame_bytes = len(
        encode_json(
            {
                "action": "send_private_msg",
                "params": {"user_id": 123, "message": segments},
                "echo": "a" * 32,
            }
        )
    )
    owner = MediaOwner(QQSettings(frame_bytes=frame_bytes + adjustment), tmp_path)
    parameters = Send(chat="private", target_id="123", text="正文", images=[str(image)])
    try:
        if adjustment == 0:
            assert await owner.io(lambda: owner.prepare(parameters)) == (segments, ())
        else:
            with pytest.raises(QQMediaError, match=f"需要 {frame_bytes} 字节"):
                await owner.io(lambda: owner.prepare(parameters))
    finally:
        await owner.close()


async def test_external_junction_cannot_supply_attachment(tmp_path: Path) -> None:
    owner = MediaOwner(QQSettings(), tmp_path)
    allowed = tmp_path / ".purrcept"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (outside / "secret.png").write_bytes(PNG)
    linked = allowed / "link"
    result = await asyncio.to_thread(
        subprocess.run,
        ["cmd.exe", "/c", "mklink", "/J", str(linked), str(outside)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    try:
        with pytest.raises(QQMediaError, match="symlink"):
            await owner.io(lambda: owner.image_source(".purrcept/link/secret.png"))
    finally:
        linked.rmdir()
        await owner.close()


@pytest.mark.parametrize("chat", ("private", "group"))
async def test_sticker_uses_real_get_image_and_survives_restart(tmp_path: Path, chat: str) -> None:
    socket = Socket()
    connector = Connector((socket,))
    host = EngineHost(
        config(tmp_path), plugin_factories={"qq-fixture": factory_with_connector(connector)}
    )
    await host.start()
    await connector.connected.wait()
    try:
        task = asyncio.create_task(
            host.invoke("me.phone.qq.save_sticker", {"tag": "开心", "message_id": "-123"})
        )
        request = await socket.sent.get()
        assert request["action"] == "get_msg" and request["params"] == {"message_id": -123}
        socket.reply(
            request,
            {
                "message": [
                    {"type": "image", "data": {"file": "opaque.image", "file_id": "native-file"}}
                ]
            },
        )
        request = await socket.sent.get()
        assert request["action"] == "get_image" and request["params"] == {
            "file": "opaque.image",
            "file_id": "native-file",
        }
        socket.reply(request, {"base64": "base64://" + base64.b64encode(PNG).decode()})
        action = await task
        assert action.calls[0].state == "confirmed" and action.calls[0].result_json is not None
        saved = decode_object(action.calls[0].result_json.encode())
        assert saved["tag"] == "开心"
        assert isinstance(saved["media_id"], str)
        assert "path" not in saved
    finally:
        await host.close()
    second_socket = Socket()
    connector = Connector((second_socket,))
    host = EngineHost(
        config(tmp_path), plugin_factories={"qq-fixture": factory_with_connector(connector)}
    )
    await host.start()
    await connector.connected.wait()
    try:
        listing = await host.invoke("me.stickers.list", {})
        assert listing.calls[0].result_json is not None
        values = decode_object(listing.calls[0].result_json.encode())["stickers"]
        assert isinstance(values, list) and len(values) == 1
        task = asyncio.create_task(
            host.invoke(
                "me.phone.qq.send_sticker", {"chat": chat, "target_id": "123", "tag": "开心"}
            )
        )
        request = await second_socket.sent.get()
        assert request["action"] == f"send_{chat}_msg"
        params = request["params"]
        assert isinstance(params, dict)
        assert params["group_id" if chat == "group" else "user_id"] == 123
        message = params["message"]
        assert isinstance(message, list) and isinstance(message[0], dict)
        assert message[0]["data"] == {
            "file": "base64://" + base64.b64encode(PNG).decode(),
            "sub_type": 1,
            "summary": "[开心]",
        }
        second_socket.reply(request, {"message_id": -456})
        assert (await task).calls[0].state == "confirmed"
    finally:
        await host.close()
