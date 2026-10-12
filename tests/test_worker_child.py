"""通过真实 Windows 子进程和有界帧验证可信 Worker，不启用真实服务。

宿主替身明确消费调用帧并返回结果，测试不在 Worker 注入真实 Python 对象。
管道接收任务有独立所有者；失败清理先终止进程使实际读取结束，再关闭 FD。
正常终止帧之后先关闭宿主写端，Worker 才能收回接收线程并实际退出。
"""

from __future__ import annotations

import asyncio
import hashlib
import msvcrt
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from purrcept_engine.execution.protocol import (
    ActionProtocolError,
    FrameChannel,
    JsonObject,
    JsonValue,
    encode_json,
)
from purrcept_engine.execution.windows import WindowsProcess

_FRAME_BYTES = 2 * 1024 * 1024
_ACTION_ID = "fixture-action"
_CATALOG_VERSION = "fixture-catalog"
_LIMITS = {
    "max_calls": 64,
    "source_bytes": 64 * 1024,
    "output_bytes": 64 * 1024,
    "result_bytes": 1024 * 1024,
    "call_arguments_bytes": 1024 * 1024,
    "call_result_bytes": 1024 * 1024,
}
_BINDINGS: list[JsonValue] = [
    {
        "path": "me.fixture.echo",
        "capability_id": "fixture.echo",
        "target_id": "fixture-target",
    }
]


@dataclass(slots=True)
class _Child:
    """隔离目录、进程和管道的测试所有者，不把取消等待当作资源已结束。"""

    process: WindowsProcess
    channel: FrameChannel
    directory: Path
    reader: asyncio.Task[JsonObject] | None = None

    @classmethod
    def start(cls, directory: Path, *, memory_bytes: int = 512 * 1024 * 1024) -> _Child:
        directory.mkdir(parents=True, exist_ok=True)
        reading, to_worker = os.pipe()
        from_worker, writing = os.pipe()
        child_read = msvcrt.get_osfhandle(reading)
        child_write = msvcrt.get_osfhandle(writing)
        os.set_inheritable(reading, True)
        os.set_inheritable(writing, True)
        executable = Path(sys.base_prefix) / "python.exe"
        worker_path = Path(__file__).parents[1] / "src/purrcept_engine/execution/worker.py"
        try:
            process = WindowsProcess.launch(
                (
                    str(executable),
                    "-I",
                    str(worker_path),
                    str(child_read),
                    str(child_write),
                    str(_FRAME_BYTES),
                ),
                memory_bytes=memory_bytes,
                inherited_handles=(child_read, child_write),
            )
        except BaseException:
            for fd in (reading, to_worker, from_worker, writing):
                os.close(fd)
            raise
        os.close(reading)
        os.close(writing)
        return cls(process, FrameChannel(from_worker, to_worker, _FRAME_BYTES), directory)

    async def receive(self) -> JsonObject:
        if self.reader is not None:
            raise RuntimeError("测试通道已经有实际读取者。")
        self.reader = asyncio.create_task(asyncio.to_thread(self.channel.read))
        try:
            async with asyncio.timeout(5):
                return await asyncio.shield(self.reader)
        finally:
            if self.reader.done():
                self.reader = None

    def send_bootstrap(
        self,
        source: str,
        *,
        limits: dict[str, int] | None = None,
        bindings: list[JsonValue] | None = None,
        digest: str | None = None,
    ) -> None:
        """文本分块可包含 UTF-8 多字节内容，摘要始终按实际编码字节核验。"""
        encoded = encode_json({"bindings": _BINDINGS if bindings is None else bindings})
        text = encoded.decode("utf-8")
        chunks = [text[: len(text) // 2], text[len(text) // 2 :]]
        self.channel.send(
            {
                "kind": "bootstrap",
                "action_id": _ACTION_ID,
                "catalog_version": _CATALOG_VERSION,
                "source": source,
                "directory": str(self.directory),
                "limits": _LIMITS if limits is None else limits,
                "binding_bytes": len(encoded),
                "binding_digest": hashlib.sha256(encoded).hexdigest() if digest is None else digest,
                "binding_chunks": len(chunks),
            }
        )
        for index, data in enumerate(chunks):
            self.channel.send(
                {
                    "kind": "binding_chunk",
                    "action_id": _ACTION_ID,
                    "index": index,
                    "data": data,
                }
            )

    async def execute(self, source: str, *, limits: dict[str, int] | None = None) -> None:
        self.send_bootstrap(source, limits=limits)
        assert await self.receive() == {
            "kind": "ready",
            "action_id": _ACTION_ID,
            "catalog_version": _CATALOG_VERSION,
        }
        self.channel.send({"kind": "execute", "action_id": _ACTION_ID})

    def reply(self, sequence: int, value: object) -> None:
        self.channel.send(
            {
                "kind": "call_result",
                "action_id": _ACTION_ID,
                "sequence": sequence,
                "ok": True,
                "value": value,
            }
        )

    async def close(self) -> None:
        """线程仍在读取时先收回进程，正常终止后则通过 EOF 收回 Worker 线程。"""
        if self.reader is not None:
            if self.process.poll() is None:
                self.process.terminate(74)
            await asyncio.wait_for(self.process.wait(), timeout=5)
            try:
                await self.reader
            except (EOFError, ActionProtocolError):
                pass
            self.reader = None
        self.channel.close()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=5)
        except TimeoutError:
            self.process.terminate(74)
            await asyncio.wait_for(self.process.wait(), timeout=5)
            raise
        finally:
            self.process.close()


async def test_handshake_precedes_execution_and_returns_small_json(tmp_path: Path) -> None:
    """完整目录到齐才 ready，显式 execute 才返回结果；SDK 没有参与启动。"""
    child = _Child.start(tmp_path)
    try:
        child.send_bootstrap("import math\nreturn math.sqrt(81)")
        ready = await child.receive()
        assert ready["kind"] == "ready"
        assert child.process.poll() is None
        child.channel.send({"kind": "execute", "action_id": _ACTION_ID})
        assert await child.receive() == {
            "kind": "completed",
            "action_id": _ACTION_ID,
            "value": 9.0,
            "stdout": "",
        }
    finally:
        await child.close()
    assert child.process.poll() == 0


async def test_real_proxy_calls_keep_parameters_and_result_fields(tmp_path: Path) -> None:
    """关键词与单映射调用使用冻结路径，返回值提供只读数据点访问。"""
    child = _Child.start(tmp_path)
    try:
        await child.execute(
            "from world import me, capability\n"
            "first = await me.fixture.echo({'value': '00123'})\n"
            "second = await capability('fixture.echo', target='fixture-target', "
            "value=first.value)\n"
            "return second.value"
        )
        for sequence in (1, 2):
            call = await child.receive()
            assert call == {
                "kind": "call",
                "action_id": _ACTION_ID,
                "sequence": sequence,
                "path": "me.fixture.echo",
                "arguments": {"value": "00123"},
            }
            child.reply(sequence, {"value": "00123"})
        assert (await child.receive())["value"] == "00123"
    finally:
        await child.close()


async def test_gather_correlates_reversed_response_order(tmp_path: Path) -> None:
    """一个接收线程正确采用乱序响应，结果仍保持 gather 的提交顺序。"""
    child = _Child.start(tmp_path)
    try:
        await child.execute(
            "import asyncio\nfrom world import me\n"
            "values = await asyncio.gather(me.fixture.echo(value=1), me.fixture.echo(value=2))\n"
            "return [item.value for item in values]"
        )
        first, second = await child.receive(), await child.receive()
        assert first["sequence"] == 1 and second["sequence"] == 2
        child.reply(2, {"value": "second"})
        child.reply(1, {"value": "first"})
        terminal = await child.receive()
        assert terminal["kind"] == "completed"
        assert terminal["value"] == ["first", "second"]
    finally:
        await child.close()


async def test_cancelled_local_wait_accepts_late_reply_without_retry(tmp_path: Path) -> None:
    """脚本等待超时不重发能力，合法迟到响应也不破坏下一项等待。"""
    child = _Child.start(tmp_path)
    try:
        await child.execute(
            "import asyncio\nfrom world import me\n"
            "try:\n    await asyncio.wait_for(me.fixture.echo(value=1), 0.02)\n"
            "except TimeoutError:\n    pass\n"
            "value = await me.fixture.echo(value=2)\nreturn value"
        )
        first, second = await child.receive(), await child.receive()
        assert first["sequence"] == 1 and second["sequence"] == 2
        assert first["arguments"] == {"value": 1}
        assert second["arguments"] == {"value": 2}
        child.reply(1, "late")
        child.reply(2, "current")
        terminal = await child.receive()
        assert terminal["kind"] == "completed"
        assert terminal["value"] == "current"
    finally:
        await child.close()


async def test_duplicate_consumed_reply_terminates_current_action(tmp_path: Path) -> None:
    """序号即使曾经合法也只能交付一次，不被当作无关旧帧忽略。"""
    child = _Child.start(tmp_path)
    try:
        await child.execute(
            "from world import me\nawait me.fixture.echo(value=1)\n"
            "await me.fixture.echo(value=2)\nreturn None"
        )
        assert (await child.receive())["sequence"] == 1
        child.reply(1, None)
        assert (await child.receive())["sequence"] == 2
        child.reply(1, "duplicate")
        assert await asyncio.wait_for(child.process.wait(), timeout=5) == 72
        with pytest.raises(EOFError):
            await child.receive()
    finally:
        await child.close()


async def test_host_failure_is_a_catchable_data_error(tmp_path: Path) -> None:
    """普通能力拒绝保留远端类别，区别于必须终止 Worker 的协议故障。"""
    child = _Child.start(tmp_path)
    try:
        await child.execute(
            "from world import me, RemoteCapabilityError\n"
            "try:\n    await me.fixture.echo(value=1)\n"
            "except RemoteCapabilityError as error:\n    return error.error_type\n"
            "return 'unexpected-success'"
        )
        assert (await child.receive())["sequence"] == 1
        child.channel.send(
            {
                "kind": "call_result",
                "action_id": _ACTION_ID,
                "sequence": 1,
                "ok": False,
                "error_type": "UnavailableError",
                "error": "本地替身明确离线。",
            }
        )
        terminal = await child.receive()
        assert terminal["kind"] == "completed"
        assert terminal["value"] == "UnavailableError"
    finally:
        await child.close()


@pytest.mark.parametrize(
    "source",
    [
        "import os\nreturn None",
        "import json.codecs\nreturn None",
        "import json\nreturn json.codecs",
        "return (1).__class__",
        'return "{0.__class__}".format(1)',
    ],
)
async def test_worker_rejects_imports_and_object_reflection(source: str, tmp_path: Path) -> None:
    """不依赖宿主预检，真实 Worker 仍拒绝导入或访问真实对象图。"""
    child = _Child.start(tmp_path)
    try:
        await child.execute(source)
        terminal = await child.receive()
        assert terminal["kind"] == "failed"
        assert terminal["action_id"] == _ACTION_ID
    finally:
        await child.close()


@pytest.mark.parametrize("failure", ["identity", "sequence", "kind"])
async def test_invalid_reply_terminates_before_script_error_handler(
    failure: str, tmp_path: Path
) -> None:
    """非法响应不能被 except Exception 吞掉后启动另一项能力。"""
    child = _Child.start(tmp_path)
    try:
        await child.execute(
            "from world import me\n"
            "try:\n    await me.fixture.echo(value='before')\n"
            "except Exception:\n    await me.fixture.echo(value='after')\n"
            "return None"
        )
        assert (await child.receive())["sequence"] == 1
        reply: JsonObject = {
            "kind": "call_result",
            "action_id": _ACTION_ID,
            "sequence": 1,
            "ok": True,
            "value": None,
        }
        if failure == "identity":
            reply["action_id"] = "different-action"
        elif failure == "sequence":
            reply["sequence"] = 2
        else:
            reply["kind"] = "execute"
        child.channel.send(reply)
        assert await asyncio.wait_for(child.process.wait(), timeout=5) == 72
        with pytest.raises(EOFError):
            await child.receive()
    finally:
        await child.close()


async def test_incomplete_catalog_is_rejected_before_ready(tmp_path: Path) -> None:
    """分块长度相同但摘要不同仍不采用目录或执行源码。"""
    child = _Child.start(tmp_path)
    try:
        child.send_bootstrap("return 9", digest="0" * 64)
        assert await asyncio.wait_for(child.process.wait(), timeout=5) == 72
        with pytest.raises(EOFError):
            await child.receive()
    finally:
        await child.close()


async def test_real_memory_limit_cannot_be_caught_before_another_call(tmp_path: Path) -> None:
    """64 MiB 真实 Job 拒绝 128 MiB 分配，可信异常监控先于脚本捕获终止。"""
    child = _Child.start(tmp_path, memory_bytes=64 * 1024 * 1024)
    try:
        await child.execute(
            "from world import me\n"
            "try:\n    value = 'x' * (128 * 1024 * 1024)\n"
            "except Exception:\n    await me.fixture.echo(value='after-memory-failure')\n"
            "return 'continued'"
        )
        assert await asyncio.wait_for(child.process.wait(), timeout=5) == 71
        with pytest.raises(EOFError):
            await child.receive()
    finally:
        await child.close()


@pytest.mark.parametrize(
    "source",
    [
        "print('é' * 40000)\nreturn None",
        "try:\n    print('é' * 40000)\nexcept RuntimeError:\n    pass\nreturn 'caught'",
    ],
)
async def test_output_limit_never_becomes_complete_success(source: str, tmp_path: Path) -> None:
    """输出按 UTF-8 字节核验；捕获输出异常也不把未保存正文冒充成功。"""
    child = _Child.start(tmp_path)
    try:
        await child.execute(source)
        terminal = await child.receive()
        assert terminal["kind"] == "failed"
        assert terminal["error_type"] == "WorkerLimitError"
        assert terminal["stdout"] == ""
    finally:
        await child.close()


@pytest.mark.parametrize("case", ["arguments", "result", "calls"])
async def test_declared_json_and_call_limits_are_enforced(case: str, tmp_path: Path) -> None:
    """参数过大不进入宿主，最终结果及下一次调用超额均明确失败。"""
    child = _Child.start(tmp_path)
    limits = dict(_LIMITS)
    try:
        if case == "arguments":
            limits["call_arguments_bytes"] = 16
            source = "from world import me\nreturn await me.fixture.echo(value='x' * 128)"
        elif case == "result":
            limits["result_bytes"] = 16
            source = "return 'x' * 128"
        else:
            limits["max_calls"] = 1
            source = (
                "from world import me\nawait me.fixture.echo(value=1)\n"
                "await me.fixture.echo(value=2)\nreturn None"
            )
        await child.execute(source, limits=limits)
        if case == "calls":
            assert (await child.receive())["sequence"] == 1
            child.reply(1, None)
        terminal = await child.receive()
        assert terminal["kind"] == "failed"
        assert terminal["error_type"] == "WorkerLimitError"
    finally:
        await child.close()
