"""真实 Windows 匿名管道和 Host 验证标准输入 EOF、完整行与取消寿命。"""

import asyncio
import os
from collections import deque
from pathlib import Path

import pytest
from purrcept_core.models import Message, ModelResponse
from test_host_subject import Backend, config, wait_processed

from purrcept_engine import Admission, EngineHost, InputSubmission, StorageError
from purrcept_engine.app.stdin import ConsoleIngress, WindowsLineReader


@pytest.mark.asyncio
async def test_native_pipe_keeps_full_unicode_lines_and_eof_partial() -> None:
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "r", encoding="utf-8") as stream:
        reader = WindowsLineReader(stream, max_bytes=100)
        pending = asyncio.create_task(reader.read_line())
        try:
            await asyncio.sleep(0.03)
            assert not pending.done()
            os.write(write_fd, "完整原文\r\n未补换行的最后一行".encode())
            assert await asyncio.wait_for(pending, 2) == "完整原文"
        finally:
            os.close(write_fd)
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        assert await reader.read_line() == "未补换行的最后一行"
        assert await reader.read_line() is None
    assert stream.closed


@pytest.mark.asyncio
async def test_cancel_wait_does_not_close_borrowed_pipe_or_leave_a_reader() -> None:
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "r", encoding="utf-8") as stream:
        reader = WindowsLineReader(stream, max_bytes=100)
        waiting = asyncio.create_task(reader.read_line())
        try:
            await asyncio.sleep(0.03)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiting, 2)
            os.write(write_fd, b"next\n")
            assert await reader.read_line() == "next" and not stream.closed
        finally:
            os.close(write_fd)


@pytest.mark.asyncio
async def test_overlong_line_is_explicit_without_partial_admission(tmp_path: Path) -> None:
    source = tmp_path / "input.txt"
    source.write_text("完整但超额\n", encoding="utf-8")
    with source.open(encoding="utf-8") as stream:
        reader = WindowsLineReader(stream, max_bytes=3)
        with pytest.raises(StorageError) as error:
            await reader.read_line()
        assert error.value.code == "input_capacity"


class Reader:
    """可重放的一行与 EOF，结束读取不暗中关闭 Host。"""

    def __init__(self) -> None:
        self.lines: deque[str | None] = deque(("完整输入", None))
        self.eof = asyncio.Event()

    async def read_line(self) -> str | None:
        """返回脚本的一行，EOF 只用于测试等待接入确实结束。"""
        line = self.lines.popleft()
        if line is None:
            self.eof.set()
        return line


@pytest.mark.asyncio
async def test_console_eof_keeps_real_subject_running_and_final_text_internal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = Backend((ModelResponse(Message.assistant("内部结束")),))
    output: list[str] = []
    host = EngineHost(
        config(tmp_path), generation_backends={"main": backend}, console_output=output.append
    )
    await host.start()
    admitted: list[str] = []
    original = host.submit

    async def capture(submission: InputSubmission) -> Admission:
        """记录真实接管身份供查询，不代替 Host 的输入事务。"""
        result = await original(submission)
        admitted.append(result.input_id)
        return result

    monkeypatch.setattr(host, "submit", capture)
    reader = Reader()
    ingress = ConsoleIngress(host, reader)
    failure = asyncio.create_task(ingress.wait_failed())
    try:
        ingress.start()
        await asyncio.wait_for(reader.eof.wait(), 2)
        assert len(admitted) == 1 and host.query().phase == "running" and not failure.done()
        await wait_processed(host, admitted[0])
        assert output == [] and len(backend.requests) == 1
        assert "完整输入" in backend.requests[0].messages[0].text
    finally:
        failure.cancel()
        await asyncio.gather(failure, return_exceptions=True)
        await ingress.close()
        await host.close()
