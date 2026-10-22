"""在临时目录验证宿主命令、进程树回收和真实行动回执，不连接桌面或网络。"""

from __future__ import annotations

import asyncio
import base64
import ctypes
from pathlib import Path
from typing import Protocol

import pytest
from pydantic import ValidationError
from test_computer import OfflineDriver

from purrcept_engine import EngineConfig, EngineHost
from purrcept_engine.plugin_system import CapabilityInputError
from purrcept_engine.plugins.computer.command import CommandParameters, CommandRunner
from purrcept_engine.plugins.computer.plugin import make_factory
from purrcept_engine.values import decode_object


class _Function(Protocol):
    """原生测试探针的签名元数据，避免 64 位句柄截断。"""

    argtypes: tuple[object, ...]
    restype: object


class _Open(_Function, Protocol):
    def __call__(self, access: int, inherit: bool, pid: int) -> int | None: ...


class _Wait(_Function, Protocol):
    def __call__(self, handle: int, milliseconds: int) -> int: ...


class _Close(_Function, Protocol):
    def __call__(self, handle: int) -> int: ...


class _Probe(ctypes.WinDLL):
    """只取得测试子进程的句柄和退出事实，不按 PID 终止外部进程。"""

    OpenProcess: _Open
    WaitForSingleObject: _Wait
    CloseHandle: _Close


def _probe() -> _Probe:
    api = _Probe("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    api.OpenProcess.restype = ctypes.c_void_p
    api.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    api.WaitForSingleObject.restype = ctypes.c_uint32
    api.CloseHandle.argtypes = (ctypes.c_void_p,)
    api.CloseHandle.restype = ctypes.c_int
    return api


async def _wait_file(path: Path) -> None:
    """外部进程不能发 asyncio.Event；有限轮询等待临时文件写入完成。"""

    def written() -> bool:
        return path.exists() and path.stat().st_size > 0

    for _ in range(500):
        if await asyncio.to_thread(written):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"测试命令没有写入 {path.name}。")


@pytest.mark.parametrize(
    "payload",
    [
        {"command": " "},
        {"command": "x" * 8001},
        {"command": "echo ok", "timeout_seconds": True},
        {"command": "echo ok", "timeout_seconds": float("nan")},
        {"command": "echo ok", "timeout_seconds": 301},
        {"command": "echo ok", "max_output_bytes": True},
        {"command": "echo ok", "max_output_bytes": 65537},
    ],
)
def test_command_parameters_reject_invalid_budgets(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CommandParameters.model_validate(payload)


async def test_command_uses_config_directory_host_environment_and_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "工作 目录"
    directory.mkdir()
    monkeypatch.setenv("PURRCEPT_COMMAND_TEST", "环境值")
    runner = CommandRunner(tmp_path)
    try:
        outcome = await runner.run(
            CommandParameters(
                command="[Console]::Out.Write('中文\"$'); [Console]::Error.Write('提示'); "
                "Set-Content -LiteralPath 'proof.txt' "
                "-Value $env:PURRCEPT_COMMAND_TEST -Encoding UTF8",
                cwd="工作 目录",
            ),
            asyncio.get_running_loop().time() + 15,
        )
        assert outcome.state == "confirmed" and isinstance(outcome.value, dict)
        assert outcome.value["exit_code"] == 0
        assert outcome.value["stdout"] == '中文"$'
        assert outcome.value["stderr"] == "提示"
        assert outcome.value["cwd"] == str(directory)
        assert (directory / "proof.txt").read_text(encoding="utf-8-sig").strip() == "环境值"
        # 每次都是独立 shell，不能把上一命令的 Set-Location 泄漏成下一次默认目录。
        await runner.run(
            CommandParameters(command="Set-Location .."), asyncio.get_running_loop().time() + 15
        )
        second = await runner.run(
            CommandParameters(command="(Get-Location).Path"), asyncio.get_running_loop().time() + 15
        )
        assert isinstance(second.value, dict) and str(tmp_path) in str(second.value["stdout"])
    finally:
        await runner.stop()


@pytest.mark.parametrize(
    "script,code", [("exit 7", 7), ("throw '明确失败'", 1), ("& cmd /c exit 9", 9)]
)
async def test_command_preserves_nonzero_exit_and_errors(
    tmp_path: Path, script: str, code: int
) -> None:
    runner = CommandRunner(tmp_path)
    try:
        outcome = await runner.run(
            CommandParameters(command=script), asyncio.get_running_loop().time() + 15
        )
        assert outcome.state == "failed" and outcome.error_type == "CommandExitError"
        assert isinstance(outcome.value, dict) and outcome.value["exit_code"] == code
        if "throw" in script:
            assert "明确失败" in str(outcome.value["stderr"])
    finally:
        await runner.stop()


async def test_command_drains_both_streams_after_output_limit(tmp_path: Path) -> None:
    runner = CommandRunner(tmp_path)
    try:
        outcome = await runner.run(
            CommandParameters(
                command="[Console]::Out.Write('x' * 200000); [Console]::Error.Write('y' * 200000)",
                max_output_bytes=256,
            ),
            asyncio.get_running_loop().time() + 15,
        )
        assert outcome.state == "confirmed" and isinstance(outcome.value, dict)
        assert outcome.value["stdout"] == "x" * 256 and outcome.value["stderr"] == "y" * 256
        assert outcome.value["stdout_truncated"] and outcome.value["stderr_truncated"]
    finally:
        await runner.stop()


async def test_timeout_keeps_output_and_prior_effects(tmp_path: Path) -> None:
    runner = CommandRunner(tmp_path)
    try:
        outcome = await runner.run(
            CommandParameters(
                command="Set-Content 'before.txt' '已完成'; "
                "[Console]::Out.Write('partial'); Start-Sleep 20",
                timeout_seconds=3,
            ),
            asyncio.get_running_loop().time() + 15,
        )
        assert outcome.state == "unknown" and outcome.error_type == "CommandTimeout"
        assert isinstance(outcome.value, dict) and outcome.value["timed_out"]
        assert outcome.value["stdout"] == "partial"
        assert (tmp_path / "before.txt").exists()
    finally:
        await runner.stop()


async def test_stop_reclaims_descendants_after_cancelled_waiter(tmp_path: Path) -> None:
    runner = CommandRunner(tmp_path)
    child_script = base64.b64encode("Start-Sleep 30".encode("utf-16-le")).decode("ascii")
    script = (
        "$p = Start-Process -WindowStyle Hidden -PassThru -FilePath "
        "($env:SystemRoot + '/System32/WindowsPowerShell/v1.0/powershell.exe') "
        f"-ArgumentList '-NoProfile -NonInteractive -EncodedCommand {child_script}'; "
        "Set-Content -LiteralPath 'child.pid' -Value $p.Id; Start-Sleep 30"
    )
    waiting = asyncio.create_task(
        runner.run(CommandParameters(command=script), asyncio.get_running_loop().time() + 20)
    )
    api, handle = _probe(), None
    try:
        await _wait_file(tmp_path / "child.pid")
        pid = int((tmp_path / "child.pid").read_text().strip())
        handle = api.OpenProcess(0x00100000 | 0x1000, False, pid)
        assert handle is not None and api.WaitForSingleObject(handle, 0) == 258
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert api.WaitForSingleObject(handle, 0) == 258
        await asyncio.wait_for(runner.stop(), 5)
        assert api.WaitForSingleObject(handle, 0) == 0
    finally:
        await runner.stop()
        await asyncio.gather(waiting, return_exceptions=True)
        if handle is not None:
            assert api.CloseHandle(handle)


async def test_stop_before_scheduled_start_and_expired_deadline_do_not_launch(
    tmp_path: Path,
) -> None:
    runner = CommandRunner(tmp_path)
    pending = asyncio.create_task(
        runner.run(
            CommandParameters(command="Set-Content 'unexpected.txt' yes"),
            asyncio.get_running_loop().time() + 15,
        )
    )
    await asyncio.sleep(0)
    await runner.stop()
    outcome = await pending
    assert outcome.state == "failed" and not (tmp_path / "unexpected.txt").exists()
    expired = await runner.run(
        CommandParameters(command="echo late"), asyncio.get_running_loop().time() - 1
    )
    assert isinstance(expired.value, dict) and expired.value["started"] is False


@pytest.mark.parametrize(
    "script,cwd", [("echo ok", "missing"), ("echo\0bad", None), ("echo ok", "bad\0path")]
)
async def test_command_rejects_invalid_input_before_launch(
    tmp_path: Path, script: str, cwd: str | None
) -> None:
    runner = CommandRunner(tmp_path)
    with pytest.raises(CapabilityInputError):
        await runner.run(
            CommandParameters(command=script, cwd=cwd), asyncio.get_running_loop().time() + 15
        )
    await runner.stop()


async def test_launch_failure_returns_explicit_not_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SystemRoot", str(tmp_path / "missing-windows"))
    runner = CommandRunner(tmp_path)
    outcome = await runner.run(
        CommandParameters(command="echo ok"), asyncio.get_running_loop().time() + 15
    )
    assert outcome.error_type == "CommandStartFailed" and isinstance(outcome.value, dict)
    assert outcome.value["started"] is False and outcome.value["exit_code"] is None
    await runner.stop()


def _host(tmp_path: Path) -> EngineHost:
    """只装配离线电脑驱动与真实执行器，模型端点不会被调用。"""
    config = EngineConfig.validate(
        {
            "host": {"data_dir": tmp_path / "data"},
            "console": False,
            "models": {
                "test": {
                    "kind": "generation",
                    "model": "openai/offline",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "context_window": 100000,
                    "max_output_tokens": 1000,
                }
            },
            "subject": {"foreground": "test", "reflection": "test", "summary": "test"},
            "plugins": [{"instance_id": "computer", "entry": "computer-fixture"}],
        },
        base_dir=tmp_path,
    )
    return EngineHost(config, plugin_factories={"computer-fixture": make_factory(OfflineDriver)})


async def test_host_worker_requires_connection_and_persists_command_receipt(tmp_path: Path) -> None:
    host = _host(tmp_path)
    await host.start()
    try:
        rejected = await host.execute(
            'from world import me\nreturn await me.computer.command(command="echo forbidden")'
        )
        assert rejected.calls[-1].state == "failed"
        action = await host.execute(
            "from world import me\nawait me.computer.connect()\n"
            "return await me.computer.command("
            'command="Set-Content receipt.txt ok; Write-Output 已执行")'
        )
        assert action.state == "completed" and len(action.calls) == 2
        receipt = action.calls[-1]
        assert receipt.state == "confirmed" and receipt.result_json is not None
        result = decode_object(receipt.result_json.encode())
        assert result["started"] and result["exit_code"] == 0
        assert "已执行" in str(result["stdout"]) and (tmp_path / "receipt.txt").exists()
    finally:
        await host.close()


async def test_disconnect_interrupts_running_command_and_reconnect_allows_new_call(
    tmp_path: Path,
) -> None:
    host = _host(tmp_path)
    await host.start()
    running: asyncio.Task[object] | None = None
    try:
        await host.execute("from world import me\nawait me.computer.connect()")
        running = asyncio.create_task(
            host.execute(
                "from world import me\nawait me.computer.command("
                'command="Set-Content started.txt yes; Start-Sleep 30")'
            )
        )
        await _wait_file(tmp_path / "started.txt")
        await host.execute("from world import me\nawait me.computer.disconnect()")
        action = await running
        receipt = action.calls[-1]
        assert receipt.state == "unknown" and receipt.result_json is not None
        assert decode_object(receipt.result_json.encode())["interrupted"] is True
        again = await host.execute(
            "from world import me\nawait me.computer.connect()\n"
            'return await me.computer.command(command="echo 再次运行")'
        )
        assert again.calls[-1].state == "confirmed"
    finally:
        await host.close()
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)
