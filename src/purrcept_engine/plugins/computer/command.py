"""电脑插件的宿主 PowerShell 命令、有限输出与进程树生命周期。

CommandRunner 由电脑实例持有；每次调用创建独立 Windows Job，命令使用宿主
账号和环境，属于明确授予的真实机器权限，不是行动 Worker 的沙箱内执行。
本模块不解释行动源码、不持有主脑或数据库，逐次回执仍由执行服务入账。
"""

from __future__ import annotations

import asyncio
import base64
import msvcrt
import os
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from purrcept_engine.infrastructure.processes.windows import WindowsProcess
from purrcept_engine.plugin_system import CallOutcome, CapabilityInputError
from purrcept_engine.values import JsonObject


class CommandParameters(BaseModel):
    """单次非交互 PowerShell 命令；目录相对主配置目录解析。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    command: str = Field(
        min_length=1, max_length=8000, pattern=r"\S", description="PowerShell 脚本文本。"
    )
    cwd: str | None = Field(default=None, min_length=1, description="工作目录，默认主配置目录。")
    timeout_seconds: float = Field(default=30.0, ge=0.1, le=300, allow_inf_nan=False)
    max_output_bytes: int = Field(
        default=32768, ge=256, le=65536, description="每个输出流保留的字节上限。"
    )


@dataclass(slots=True)
class _Output:
    """持续排空管道，仅保留有限前缀，避免输出洪泛耗尽内存或堵住子进程。"""

    data: bytearray = field(default_factory=bytearray)
    truncated: bool = False

    def drain(self, descriptor: int, limit: int) -> None:
        """由独占线程排到 EOF；Job 退出后所有写端关闭，线程才能归还。"""
        while chunk := os.read(descriptor, 8192):
            remaining = limit - len(self.data)
            self.data.extend(chunk[:remaining])
            self.truncated |= len(chunk) > remaining


@dataclass(slots=True)
class _Running:
    """正在运行的真实进程及显式断开事实，只由所属事件循环修改。"""

    process: WindowsProcess
    interrupted: bool = False


def _pipe(parent: ExitStack, child: ExitStack) -> tuple[int, int]:
    """立即登记两端所有权，后续管道分配或启动失败也能逆序释放。"""
    reader, writer = os.pipe()
    parent.callback(os.close, reader)
    child.callback(os.close, writer)
    os.set_inheritable(writer, True)
    return reader, writer


def _powershell(command: str) -> tuple[str, ...]:
    """编码脚本避免命令行二次引用，并为文本输出声明统一 UTF-8 编码。"""
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        "$OutputEncoding = [Console]::OutputEncoding\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        "try {\n" + command + "\n"
        "$succeeded = $?\n"
        "if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }; if (-not $succeeded) { exit 1 }\n"
        "} catch { [Console]::Error.WriteLine($_.ToString()); exit 1 }\n"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    # Windows PowerShell 的默认进度流可能写入 CLIXML；显式文本格式保持回执可读。
    return (
        str(executable),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-OutputFormat",
        "Text",
        "-EncodedCommand",
        encoded,
    )


class CommandRunner:
    """电脑实例独占的命令所有者，同一事件循环可接管多次调用。

    Args:
        base_dir: 主配置目录，用于解析每次命令的工作目录。

    Note:
        上层在每次 run 前检查电脑连接；stop 撤销旧代次并终止在途命令。
        取消调用等待不取消已接管的命令，stop 仍会等进程树和输出线程真正归还。
    """

    def __init__(self, base_dir: Path) -> None:
        """登记目录和本实例任务所有权，构造阶段不创建进程或输出线程。"""
        self._base_dir = base_dir.resolve()
        self._generation = 0
        self._running: list[_Running] = []
        self._tasks: set[asyncio.Task[CallOutcome]] = set()

    async def run(self, parameters: CommandParameters, deadline: float) -> CallOutcome:
        """接管一次命令，并返回退出码、有限输出与超时/中断事实。

        Args:
            parameters: 经能力边界校验的脚本、目录和输出预算。
            deadline: 执行服务提供的绝对单调期限，不能被命令参数扩大。

        Returns:
            退出码为零时 confirmed；非零或启动失败为 failed；超时或断开为 unknown。

        Raises:
            CapabilityInputError: 目录不存在、不是目录，或参数包含空字符。
            OSError: 进程清理或管道读取失败，不能把资源故障伪装成命令退出。
        """
        if "\0" in parameters.command or (parameters.cwd is not None and "\0" in parameters.cwd):
            raise CapabilityInputError("命令与工作目录不能包含空字符。")
        directory = self._base_dir if parameters.cwd is None else self._base_dir / parameters.cwd
        directory = directory.resolve()
        if not directory.is_dir():
            raise CapabilityInputError("命令工作目录不存在或不是目录。")
        task = asyncio.create_task(self._run(parameters, directory, deadline, self._generation))
        self._tasks.add(task)
        task.add_done_callback(self._completed)
        return await asyncio.shield(task)

    def _completed(self, task: asyncio.Task[CallOutcome]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _run(
        self, parameters: CommandParameters, directory: Path, deadline: float, generation: int
    ) -> CallOutcome:
        """先建立可回收的进程树，再并行排空输出；收回线程后才交付回执。"""
        loop = asyncio.get_running_loop()
        output, errors = _Output(), _Output()
        value: JsonObject = {
            "started": False,
            "exit_code": None,
            "cwd": str(directory),
            "stdout": "",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "timed_out": False,
            "interrupted": False,
        }
        if generation != self._generation or loop.time() >= deadline:
            return CallOutcome("failed", value, "CommandNotStarted", "连接已撤销或行动期限已到。")

        with ExitStack() as parent, ExitStack() as child:
            stdout_read, stdout_write = _pipe(parent, child)
            stderr_read, stderr_write = _pipe(parent, child)
            stdin = os.open(os.devnull, os.O_RDONLY)
            child.callback(os.close, stdin)
            os.set_inheritable(stdin, True)
            handles = tuple(msvcrt.get_osfhandle(fd) for fd in (stdin, stdout_write, stderr_write))
            try:
                process = WindowsProcess.launch(
                    _powershell(parameters.command),
                    cpu_seconds=None,
                    memory_bytes=None,
                    inherited_handles=handles,
                    standard_handles=(handles[0], handles[1], handles[2]),
                    directory=directory,
                    inherit_environment=True,
                )
            except OSError as error:
                return CallOutcome("failed", value, "CommandStartFailed", str(error))
            running = _Running(process)
            self._running.append(running)
            value["started"] = True
            readers: list[asyncio.Task[None]] = []
            try:
                # 父端立即关掉子进程专属句柄，否则 EOF 会被宿主自己持有的写端阻止。
                child.close()
                readers.extend(
                    (
                        asyncio.create_task(
                            asyncio.to_thread(
                                output.drain, stdout_read, parameters.max_output_bytes
                            )
                        ),
                        asyncio.create_task(
                            asyncio.to_thread(
                                errors.drain, stderr_read, parameters.max_output_bytes
                            )
                        ),
                    )
                )
                timeout = min(parameters.timeout_seconds, max(0.0, deadline - loop.time()))
                try:
                    value["exit_code"] = await asyncio.wait_for(process.wait(), timeout)
                except TimeoutError:
                    value["timed_out"] = True
            finally:
                # Job 包含 shell 后代；超时、断开和异常都必须确认整棵树退出后再关闭管道。
                try:
                    if process.poll() is None:
                        process.terminate()
                    await process.wait()
                    await asyncio.gather(*readers)
                finally:
                    process.close()
                    self._running.remove(running)

        value.update(
            stdout=output.data.decode("utf-8", errors="replace"),
            stderr=errors.data.decode("utf-8", errors="replace"),
            stdout_truncated=output.truncated,
            stderr_truncated=errors.truncated,
            interrupted=running.interrupted,
        )
        if value["timed_out"] or running.interrupted:
            reason = "CommandTimeout" if value["timed_out"] else "CommandInterrupted"
            return CallOutcome("unknown", value, reason, "命令已终止；此前产生的外部效果不会回滚。")
        if value["exit_code"] != 0:
            return CallOutcome("failed", value, "CommandExitError", "命令返回非零退出码。")
        return CallOutcome("confirmed", value)

    async def stop(self) -> None:
        """撤销旧代次，终止并等候全部在途命令，之后允许新连接重新调用。

        Raises:
            BaseExceptionGroup: 终止或归还失败；保留所有故障，不能声称资源已释放。
        """
        self._generation += 1
        failures: list[BaseException] = []
        for running in tuple(self._running):
            if running.process.poll() is None:
                running.interrupted = True
                try:
                    running.process.terminate()
                except OSError as error:
                    failures.append(error)
        results = await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        failures.extend(result for result in results if isinstance(result, BaseException))
        if failures:
            raise BaseExceptionGroup("宿主命令资源收尾失败。", failures)
