"""以临时 Notebook、真实 Worker 和离线 OneBot/HTTP 验证 QQ 附件保存。

原文件字节只在宿主传输和磁盘中流动，模型仅取得链接和文件回执。下载限额、
不可覆盖、目录边界与资源关闭均走生产入口，不连接真实账号或下载用户文件。
"""

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
from test_qq_plugin import config as qq_config
from test_qq_transport import Connector, Socket

from purrcept_engine import EngineHost, HostStateError
from purrcept_engine.app.config import PluginSettings
from purrcept_engine.app.evidence import EvidenceAccess
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugins.notebook import Notebook, NotebookError
from purrcept_engine.plugins.qq.plugin import factory_with_connector
from purrcept_engine.values import decode_object


def _http(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    transport = httpx.MockTransport(handler)

    def select(client: httpx.AsyncClient, url: httpx.URL) -> httpx.MockTransport:
        return transport

    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", select)


@pytest.fixture
async def notebook(tmp_path: Path) -> AsyncIterator[Notebook]:
    store = await Store.open(tmp_path / "state.sqlite3")
    notes = Notebook(
        tmp_path / "notes",
        EvidenceAccess(store.knowledge, 8000),
        instance_id="notes",
        seed_examples=False,
        file_max_bytes=1024,
    )
    await notes.start()
    try:
        yield notes
    finally:
        await notes.close()
        await store.close()


async def test_binary_attachment_is_original_bytes_visible_and_never_a_skill(
    notebook: Notebook,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"%PDF-1.7\n\x00\xff original attachment"
    _http(monkeypatch, lambda request: httpx.Response(200, content=data))
    result = await notebook.save_file("https://files.example/report", "attachments/qq/report.pdf")
    assert (tmp_path / "notes/attachments/qq/report.pdf").read_bytes() == data
    assert result["bytes"] == len(data) and result["created"] is True
    info = await notebook.file_info("attachments/qq/report.pdf")
    assert info["version"] == result["version"] and "content" not in info
    tree = await notebook.tree()
    assert "report.pdf" in str(tree)
    data = b"---\nname: external\ndescription: never auto install\n---\ntext"
    await notebook.save_file("https://files.example/skill", "attachments/skill.md")
    assert (await notebook.prepare_request())["skills"] == []
    with pytest.raises(NotebookError, match="存在"):
        await notebook.save_file("https://files.example/report", "attachments/qq/report.pdf")
    assert (tmp_path / "notes/attachments/qq/report.pdf").read_bytes().startswith(b"%PDF")


@pytest.mark.parametrize(
    "path", ["../escape.zip", "C:/escape.zip", "attachments/../../escape", "skill.md"]
)
async def test_attachment_rejects_paths_before_http(
    notebook: Notebook,
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        raise AssertionError("路径拒绝前不能请求网络")

    _http(monkeypatch, reject)
    with pytest.raises(NotebookError):
        await notebook.save_file("https://files.example/report", path)


async def test_binary_attachment_move_and_delete_require_actual_version(
    notebook: Notebook,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _http(monkeypatch, lambda request: httpx.Response(200, content=b"zip\x00\xff"))
    saved = await notebook.save_file("https://files.example/file", "attachments/a.zip")
    version = saved["version"]
    assert isinstance(version, str)
    with pytest.raises(NotebookError):
        await notebook.delete("attachments/a.zip", expected_version="wrong")
    await notebook.move("attachments/a.zip", "attachments/b.zip", expected_version=version)
    await notebook.delete("attachments/b.zip", expected_version=version)
    with pytest.raises(NotebookError):
        await notebook.file_info("attachments/b.zip")


@pytest.mark.parametrize(
    "failure", ["declared_large", "stream_large", "truncated", "http", "redirect", "scheme"]
)
async def test_failed_download_never_publishes_partial_file(
    notebook: Notebook,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if failure == "declared_large":
            return httpx.Response(200, headers={"content-length": "2048"}, content=b"small")
        if failure == "stream_large":
            return httpx.Response(200, stream=httpx.ByteStream(b"x" * 1025))
        if failure == "truncated":
            return httpx.Response(200, headers={"content-length": "99"}, content=b"short")
        if failure == "http":
            return httpx.Response(403)
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "/again"})
        return httpx.Response(302, headers={"location": "file:///C:/secret"})

    _http(monkeypatch, respond)
    with pytest.raises(NotebookError):
        await notebook.save_file("https://files.example/file", "attachments/failed.zip")
    assert len(requests) == (4 if failure == "redirect" else 1)
    assert not (tmp_path / "notes/attachments").exists()


async def test_relative_redirect_and_local_onebot_download_are_supported(
    notebook: Notebook,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/first":
            return httpx.Response(302, headers={"location": "/file"})
        return httpx.Response(200, content=b"original")

    _http(monkeypatch, respond)
    result = await notebook.save_file("http://127.0.0.1:3000/first", "attachments/local.bin")
    assert result["bytes"] == 8
    assert requested == ["http://127.0.0.1:3000/first", "http://127.0.0.1:3000/file"]


async def test_download_deadline_closes_partial_stream_without_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released = asyncio.Event()

    class StalledStream(httpx.AsyncByteStream):
        """首块后永久等待，实际整体期限必须关闭响应。"""

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"partial"
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            released.set()

    _http(monkeypatch, lambda request: httpx.Response(200, stream=StalledStream()))
    store = await Store.open(tmp_path / "timeout.sqlite3")
    notes = Notebook(
        tmp_path / "timeout-notes",
        EvidenceAccess(store.knowledge, 8000),
        instance_id="timeout",
        seed_examples=False,
        file_timeout_seconds=0.02,
    )
    await notes.start()
    try:
        with pytest.raises(NotebookError, match="超时"):
            await asyncio.wait_for(
                notes.save_file("https://files.example/file", "attachments/f.bin"), 3
            )
        assert released.is_set() and list((tmp_path / "timeout-notes").iterdir()) == []
    finally:
        await notes.close()
        await store.close()


async def test_file_appearing_during_download_is_not_overwritten(
    notebook: Notebook,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "notes/attachments/race.bin"

    def respond(request: httpx.Request) -> httpx.Response:
        target.parent.mkdir()
        target.write_bytes(b"external")
        return httpx.Response(200, content=b"downloaded")

    _http(monkeypatch, respond)
    with pytest.raises(NotebookError, match="存在"):
        await notebook.save_file("https://files.example/file", "attachments/race.bin")
    assert target.read_bytes() == b"external"
    assert list(target.parent.glob(".purrcept-*")) == []


async def test_cancel_download_closes_stream_and_close_waits_for_owner(
    notebook: Notebook,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, released = asyncio.Event(), asyncio.Event()

    class SlowStream(httpx.AsyncByteStream):
        """只在取消时退出的离线传输，用于观察实际资源关闭。"""

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"partial"
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            released.set()

    _http(monkeypatch, lambda request: httpx.Response(200, stream=SlowStream()))
    saving = asyncio.create_task(
        notebook.save_file("https://files.example/slow", "attachments/slow.bin")
    )
    await asyncio.wait_for(entered.wait(), 3)
    closing = asyncio.create_task(notebook.close())
    await asyncio.sleep(0)
    assert not closing.done()
    saving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await saving
    await asyncio.wait_for(closing, 3)
    assert released.is_set() and not (tmp_path / "notes/attachments").exists()
    with pytest.raises(HostStateError):
        await notebook.file_info("attachments/slow.bin")


async def test_attachment_junction_cannot_publish_into_skill_directory(
    notebook: Notebook,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skills = tmp_path / "notes/skills"
    skills.mkdir()
    junction = tmp_path / "notes/attachments"
    await asyncio.to_thread(
        subprocess.run,
        ["cmd", "/c", "mklink", "/J", str(junction), str(skills)],
        check=True,
        capture_output=True,
    )
    try:
        _http(monkeypatch, lambda request: httpx.Response(200, content=b"foreign"))
        with pytest.raises(NotebookError):
            await notebook.save_file("https://files.example/skill", "attachments/skill.md")
        assert list(skills.iterdir()) == []
    finally:
        os.rmdir(junction)


async def test_real_worker_saves_qq_file_then_reopens_same_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整行动串联两个插件；原字节留在宿主，回执保存到真实 SQLite。"""
    original = b"PK\x00\xff original QQ file"
    _http(monkeypatch, lambda request: httpx.Response(200, content=original))
    configuration = qq_config(tmp_path)
    configuration = configuration.model_copy(
        update={
            "plugins": (
                *configuration.plugins,
                PluginSettings.model_validate(
                    {
                        "instance_id": "notebook",
                        "entry": "notebook",
                        "config": {"notebook_dir": "notes", "seed_examples": False},
                    }
                ),
            )
        }
    )
    socket = Socket()
    connector = Connector((socket,))
    host = EngineHost(
        configuration, plugin_factories={"qq-fixture": factory_with_connector(connector)}
    )
    await host.start()
    try:
        await asyncio.wait_for(connector.connected.wait(), 3)
        task = asyncio.create_task(
            host.execute(
                "from world import me\n"
                "link = await me.phone.qq.file_url(chat='private', target_id='123', "
                "file_id='native-file')\n"
                "return await me.notebook.save_file(url=link.url, path='attachments/qq/report.zip')"
            )
        )
        request = await asyncio.wait_for(socket.sent.get(), 5)
        assert request["action"] == "get_private_file_url"
        assert request["params"] == {"file_id": "native-file"}
        socket.reply(request, {"url": "https://files.example/qq.zip"})
        action = await asyncio.wait_for(task, 5)
        assert action.state == "completed", action
        assert [call.path for call in action.calls] == [
            "me.phone.qq.file_url",
            "me.notebook.save_file",
        ]
        assert all(call.state == "confirmed" for call in action.calls)
        assert action.result_json is not None
        saved = decode_object(action.result_json.encode())
        assert (tmp_path / "notes/attachments/qq/report.zip").read_bytes() == original
        action_id = action.action_id
    finally:
        await host.close()
    reopened = EngineHost(
        configuration,
        plugin_factories={"qq-fixture": factory_with_connector(Connector((Socket(),)))},
    )
    await reopened.start()
    try:
        info = await reopened.invoke("me.notebook.file_info", {"path": "attachments/qq/report.zip"})
        assert info.calls[0].state == "confirmed" and info.calls[0].result_json is not None
        assert decode_object(info.calls[0].result_json.encode())["version"] == saved["version"]
        assert await reopened.query_action(action_id) == action
    finally:
        await reopened.close()
