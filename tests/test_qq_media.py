"""QQ 附件/贴纸的真实临时文件、旧 get_image 路径及取消后回执资源寿命。"""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import threading
from pathlib import Path

import pytest
from test_qq_input_history import PNG
from test_qq_plugin import config
from test_qq_transport import Connector, Socket

from purrcept_engine import EngineHost
from purrcept_engine.execution.protocol import decode_object
from purrcept_engine.plugins.qq.media import MediaOwner, QQMediaError
from purrcept_engine.plugins.qq.parameters import Send
from purrcept_engine.plugins.qq.plugin import factory_with_connector
from purrcept_engine.plugins.qq.settings import QQSettings
from purrcept_engine.plugins.qq.stickers import StickerLibrary


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


async def test_sticker_uses_real_get_image_and_survives_restart(tmp_path: Path) -> None:
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
        assert isinstance(saved["path"], str)
        assert await asyncio.to_thread(Path(saved["path"]).read_bytes) == PNG
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
        listing = await host.invoke("me.phone.qq.stickers", {})
        assert listing.calls[0].result_json is not None
        values = decode_object(listing.calls[0].result_json.encode())["stickers"]
        assert isinstance(values, list) and len(values) == 1
        task = asyncio.create_task(
            host.invoke(
                "me.phone.qq.send_sticker", {"chat": "private", "target_id": "123", "tag": "开心"}
            )
        )
        request = await second_socket.sent.get()
        assert request["action"] == "send_private_msg"
        params = request["params"]
        assert isinstance(params, dict)
        message = params["message"]
        assert isinstance(message, list) and isinstance(message[0], dict)
        assert message[0]["data"] == {
            "file": Path(saved["path"]).as_uri(),
            "subtype": 1,
            "summary": "[开心]",
        }
        second_socket.reply(request, {"message_id": -456})
        assert (await task).calls[0].state == "confirmed"
    finally:
        await host.close()


async def test_cancelled_sticker_waiter_retains_actual_write_and_late_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = Socket()
    connector = Connector((socket,))
    host = EngineHost(
        config(tmp_path, call_timeout=0.05),
        plugin_factories={"qq-fixture": factory_with_connector(connector)},
    )
    entered, release = threading.Event(), threading.Event()
    original = os.fsync

    def delayed(fd: int) -> None:
        entered.set()
        assert release.wait(5)
        original(fd)

    monkeypatch.setattr(os, "fsync", delayed)
    await host.start()
    await connector.connected.wait()
    try:
        task = asyncio.create_task(
            host.invoke("me.phone.qq.save_sticker", {"tag": "迟到", "message_id": "-123"})
        )
        request = await socket.sent.get()
        socket.reply(request, {"base64": "base64://" + base64.b64encode(PNG).decode()})
        assert await asyncio.to_thread(entered.wait, 2)
        initial = await task
        assert initial.calls[0].state == "unknown"
        release.set()
        async with asyncio.timeout(5):
            while True:
                result = await host.query_action(initial.action_id)
                if result is not None and result.calls[0].state == "confirmed":
                    break
                await asyncio.sleep(0.005)
        assert result.state == initial.state and result.calls[0].history[0].state == "unknown"
        assert result.calls[0].result_json is not None
        assert decode_object(result.calls[0].result_json.encode())["tag"] == "迟到"
    finally:
        release.set()
        await host.close()
    assert len(StickerLibrary(tmp_path / ".purrcept/qq-stickers").list()) == 1
