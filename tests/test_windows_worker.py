"""用真实标准库子进程验证 Windows Job 归属、额度和资源生命周期。

测试不连接网络或设备，所有文件只写入 pytest 的隔离目录。父异常退出场景
先取得子进程真实句柄再允许父退出，避免用可能复用的 PID 判定资源回收。
"""

from __future__ import annotations

import asyncio
import ctypes
import msvcrt
import os
import sys
from pathlib import Path
from typing import Protocol

import pytest

import purrcept_engine.infrastructure.processes.windows as windows
from purrcept_engine.infrastructure.processes.windows import WindowsProcess


class _NativeFunction(Protocol):
    """测试的原生读接口仍声明签名，避免 ctypes 默认截断 64 位句柄。"""

    argtypes: tuple[object, ...]
    restype: object


class _CurrentProcess(_NativeFunction, Protocol):
    def __call__(self) -> int: ...


class _HandleCount(_NativeFunction, Protocol):
    def __call__(self, process: int, count: object) -> int: ...


class _OpenProcess(_NativeFunction, Protocol):
    def __call__(self, access: int, inherit: bool, pid: int) -> int | None: ...


class _CloseHandle(_NativeFunction, Protocol):
    def __call__(self, handle: int) -> int: ...


class _Wait(_NativeFunction, Protocol):
    def __call__(self, handle: int, milliseconds: int) -> int: ...


class _CreateJob(_NativeFunction, Protocol):
    def __call__(self, attributes: object, name: str | None) -> int | None: ...


class _IsProcessInJob(_NativeFunction, Protocol):
    def __call__(self, process: int, job: int, result: object) -> int: ...


class _ProbeApi(ctypes.WinDLL):
    """只读句柄数并观察已知子进程，不按 PID 杀死任何外部进程。"""

    GetCurrentProcess: _CurrentProcess
    GetProcessHandleCount: _HandleCount
    GetHandleInformation: _HandleCount
    OpenProcess: _OpenProcess
    CloseHandle: _CloseHandle
    WaitForSingleObject: _Wait
    CreateJobObjectW: _CreateJob
    IsProcessInJob: _IsProcessInJob


def _probe() -> _ProbeApi:
    api = _ProbeApi("kernel32", use_last_error=True)
    api.GetCurrentProcess.argtypes = ()
    api.GetCurrentProcess.restype = ctypes.c_void_p
    api.GetProcessHandleCount.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    api.GetProcessHandleCount.restype = ctypes.c_int
    api.GetHandleInformation.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    api.GetHandleInformation.restype = ctypes.c_int
    api.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    api.OpenProcess.restype = ctypes.c_void_p
    api.CloseHandle.argtypes = (ctypes.c_void_p,)
    api.CloseHandle.restype = ctypes.c_int
    api.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    api.WaitForSingleObject.restype = ctypes.c_uint32
    api.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    api.CreateJobObjectW.restype = ctypes.c_void_p
    api.IsProcessInJob.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    api.IsProcessInJob.restype = ctypes.c_int
    return api


def _handle_count(api: _ProbeApi) -> int:
    count = ctypes.c_uint32()
    if not api.GetProcessHandleCount(api.GetCurrentProcess(), ctypes.byref(count)):
        raise ctypes.WinError(ctypes.get_last_error())
    return count.value


def _command(source: str) -> tuple[str, ...]:
    return (sys.executable, "-I", "-S", "-c", source)


async def _release(process: WindowsProcess) -> None:
    """失败断言仍收回本次所有者，不留下等待中的标准库子进程。"""
    if process.poll() is None:
        process.terminate()
    await asyncio.wait_for(process.wait(), timeout=5)
    process.close()


async def test_real_process_has_effective_limits_and_closes_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Job 回读与真实退出共同证明设置成立，重复 close 不关闭别人的句柄。"""
    api = _probe()
    # 首次 CreateProcess 会建立 Windows 的长驻初始化资源。先完成一次
    # 同路径，再检查后续拥有与释放循环没有增长，避免把初始化混作泄漏。
    warmup = WindowsProcess.launch(_command("pass"))
    await _release(warmup)
    before = _handle_count(api)
    created_handles: list[int] = []

    def confirm(process: int, job: int, result: object) -> int:
        created_handles.extend((process, job))
        return api.IsProcessInJob(process, job, result)

    monkeypatch.setattr(
        "purrcept_engine.infrastructure.processes.windows._Kernel32.IsProcessInJob",
        staticmethod(confirm),
        raising=False,
    )
    process = WindowsProcess.launch(_command("import time; time.sleep(0.1)"))
    try:
        assert process.pid > 0
        assert process.process_handle > 0
        limits = process.query_limits()
        assert limits.user_cpu_seconds == 15
        assert limits.memory_bytes == 512 * 1024 * 1024
        assert limits.kill_on_close
        assert await asyncio.wait_for(process.wait(), timeout=5) == 0
        assert process.total_cpu_seconds > 0
    finally:
        await _release(process)
    process.close()
    assert process.poll() == 0
    assert len(created_handles) == 2
    for handle in created_handles:
        flags = ctypes.c_uint32()
        assert not api.GetHandleInformation(handle, ctypes.byref(flags))
        assert ctypes.get_last_error() == 6
    # 直接核验本次归属句柄，再防止总数增长；别的测试资源被回收不属于本次泄漏。
    assert _handle_count(api) <= before


async def test_wait_cancellation_does_not_release_running_process() -> None:
    """观察等待取消保留进程所有权，活跃时 close 必须明确拒绝。"""
    process = WindowsProcess.launch(_command("import time; time.sleep(10)"))
    try:
        waiting = asyncio.create_task(process.wait())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert process.poll() is None
        with pytest.raises(RuntimeError, match="仍在运行"):
            process.close()
        process.terminate(73)
        assert await asyncio.wait_for(process.wait(), timeout=5) == 73
    finally:
        await _release(process)


async def test_device_helper_job_owns_process_tree_without_worker_resource_limits() -> None:
    """设备助手只借用 Job 所有权；Worker 的 CPU/内存默认限额不受影响。"""
    process = WindowsProcess.launch(
        _command("import time; time.sleep(10)"), cpu_seconds=None, memory_bytes=None
    )
    try:
        with pytest.raises(OSError):
            process.query_limits()
        assert process.poll() is None
        process.terminate(74)
        assert await asyncio.wait_for(process.wait(), timeout=5) == 74
    finally:
        await _release(process)
    with pytest.raises(ValueError):
        WindowsProcess.launch(_command("pass"), cpu_seconds=None)


async def test_native_cpu_limit_stops_busy_python_process() -> None:
    """真实循环触发用户态 CPU 限制，不以返回结构存在替代实际终止。"""
    process = WindowsProcess.launch(_command("while True: pass"), cpu_seconds=0.1)
    try:
        assert await asyncio.wait_for(process.wait(), timeout=8) != 0
        assert process.total_cpu_seconds >= 0.1
    finally:
        await _release(process)


async def test_native_commit_limit_rejects_oversized_allocation() -> None:
    """内存限制应拒绝分配；本层不把捕获 MemoryError 宣称为自动终止。"""
    source = (
        "import sys\n"
        "try:\n"
        "    payload = bytearray(128 * 1024 * 1024)\n"
        "except MemoryError:\n"
        "    sys.exit(37)\n"
        "sys.exit(0)\n"
    )
    process = WindowsProcess.launch(_command(source), memory_bytes=64 * 1024 * 1024)
    try:
        assert await asyncio.wait_for(process.wait(), timeout=5) == 37
    finally:
        await _release(process)


async def test_explicit_inheritable_pipe_handle_is_delivered() -> None:
    """真实 IPC 句柄交给子进程，调用方持有的原句柄仍归调用方关闭。"""
    reading, writing = os.pipe()
    process: WindowsProcess | None = None
    try:
        os.set_inheritable(writing, True)
        handle = msvcrt.get_osfhandle(writing)
        source = (
            "import msvcrt, os\n"
            f"descriptor = msvcrt.open_osfhandle({handle}, os.O_WRONLY)\n"
            "os.write(descriptor, b'worker-boundary')\n"
            "os.close(descriptor)\n"
        )
        # venv 重定向器是否把额外句柄继续传给它的子解释器不是本接口契约。
        # 使用真实解释器直接验证创建时的 HANDLE_LIST，不依赖中间程序转交。
        executable = Path(sys.base_prefix) / "python.exe"
        process = WindowsProcess.launch(
            (str(executable), "-I", "-S", "-c", source), inherited_handles=(handle,)
        )
        assert await asyncio.wait_for(process.wait(), timeout=5) == 0
        assert os.read(reading, 4096) == b"worker-boundary"
        assert os.get_inheritable(writing)
    finally:
        if process is not None:
            await _release(process)
        os.close(reading)
        os.close(writing)


def test_noninheritable_pipe_is_rejected_before_launch() -> None:
    """启动器不偷偷扩大继承资格，调用方必须明确准备专用 IPC。"""
    reading, writing = os.pipe()
    try:
        assert not os.get_inheritable(writing)
        with pytest.raises(ValueError, match="可继承"):
            WindowsProcess.launch(
                _command("pass"), inherited_handles=(msvcrt.get_osfhandle(writing),)
            )
    finally:
        os.close(reading)
        os.close(writing)


def test_limit_setup_failure_prevents_process_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """故障注入位于真实原生设置边界，脚本不能在额度未成立时执行。"""
    marker = tmp_path / "executed.txt"
    api = _probe()
    before = _handle_count(api)
    created_jobs: list[int] = []

    def reject_setup(native_api: object, job: int, *limits: object) -> None:
        del native_api, limits
        created_jobs.append(job)
        raise OSError(5, "synthetic native limit failure")

    monkeypatch.setattr(windows, "_configure_job", reject_setup)
    with pytest.raises(OSError, match="synthetic native limit failure"):
        WindowsProcess.launch(_command(f"from pathlib import Path; Path({str(marker)!r}).touch()"))
    assert not marker.exists()
    assert len(created_jobs) == 1
    flags = ctypes.c_uint32()
    assert not api.GetHandleInformation(created_jobs[0], ctypes.byref(flags))
    assert ctypes.get_last_error() == 6
    # 其他 asyncio/SDK 的已结束资源可能在此时被收回。具体 Job 已失效及
    # 全进程句柄不增长共同验证本次清理，不能要求无关句柄数保持绝对相等。
    assert _handle_count(api) <= before


def test_failed_create_process_releases_job_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不存在的 executable 传播原生错误，之前取得的 Job 不能泄漏。"""
    api = _probe()
    # 原生错误的首次消息格式化加载系统资源。重复同一真实失败路径
    # 仍须完全归还本次 Job 句柄，不能用任意允许增长阈值掩盖泄漏。
    with pytest.raises(OSError):
        WindowsProcess.launch((str(tmp_path / "missing.exe"),))
    before = _handle_count(api)
    created_jobs: list[int] = []

    def create_job(attributes: object, name: str | None) -> int | None:
        job = api.CreateJobObjectW(attributes, name)
        if job is not None:
            created_jobs.append(job)
        return job

    monkeypatch.setattr(
        "purrcept_engine.infrastructure.processes.windows._Kernel32.CreateJobObjectW",
        staticmethod(create_job),
        raising=False,
    )
    for _ in range(3):
        with pytest.raises(OSError):
            WindowsProcess.launch((str(tmp_path / "missing.exe"),))
    assert len(created_jobs) == 3
    for job in created_jobs:
        flags = ctypes.c_uint32()
        assert not api.GetHandleInformation(job, ctypes.byref(flags))
        assert ctypes.get_last_error() == 6
    assert _handle_count(api) <= before


async def test_failed_ownership_confirmation_reclaims_created_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """进程已经创建后校验失败，也必须终止所属 Job 并回收全部句柄。"""
    warmup = WindowsProcess.launch(_command("pass"))
    await _release(warmup)
    # 首次原生错误格式化的长驻资源与本次创建的资源分开计量。
    ctypes.WinError(5)
    probe = _probe()
    before = _handle_count(probe)
    confirmation_attempted = False
    created_handles: list[int] = []

    def denied(process: int, job: int, result: object) -> int:
        nonlocal confirmation_attempted
        del result
        created_handles.extend((process, job))
        confirmation_attempted = True
        ctypes.set_last_error(5)
        return 0

    monkeypatch.setattr(
        "purrcept_engine.infrastructure.processes.windows._Kernel32.IsProcessInJob",
        staticmethod(denied),
        raising=False,
    )
    with pytest.raises(OSError):
        WindowsProcess.launch(_command("import time; time.sleep(10)"))
    assert confirmation_attempted
    for handle in created_handles:
        flags = ctypes.c_uint32()
        assert not probe.GetHandleInformation(handle, ctypes.byref(flags))
        assert ctypes.get_last_error() == 6  # ERROR_INVALID_HANDLE。
    # 整进程计数还包含其他测试的异步资源；下降不表示本次归属句柄泄漏。
    # 上面逐一检查本次 Job/进程已经失效，此处继续防止总量增长。
    assert _handle_count(probe) <= before


async def test_nested_worker_dies_when_its_owner_process_exits(tmp_path: Path) -> None:
    """真实嵌套 Job 在父异常退出时回收 Worker，而不是仅记录取消状态。"""
    module_path = Path(windows.__file__)
    pid_path = tmp_path / "worker.pid"
    release_path = tmp_path / "release-parent"
    source = (
        "import importlib.util, os, sys, time\n"
        "from pathlib import Path\n"
        f"spec = importlib.util.spec_from_file_location('worker_windows', {str(module_path)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name] = module\n"
        "spec.loader.exec_module(module)\n"
        "worker = module.WindowsProcess.launch((sys.executable, '-I', '-S', '-c', "
        "'import time; time.sleep(60)'))\n"
        "assert worker.query_limits().kill_on_close\n"
        f"Path({str(pid_path)!r}).write_text(str(worker.pid))\n"
        f"while not Path({str(release_path)!r}).exists():\n"
        "    time.sleep(0.01)\n"
        "os._exit(41)\n"
    )
    parent = WindowsProcess.launch(_command(source))
    api = _probe()
    child_handle: int | None = None
    try:
        async with asyncio.timeout(8):
            while not pid_path.exists():
                if parent.poll() is not None:
                    pytest.fail("嵌套 Worker 启动失败。")
                await asyncio.sleep(0.01)
        pid = int(pid_path.read_text())
        child_handle = api.OpenProcess(0x00100000 | 0x1000, False, pid)
        if child_handle is None:
            raise ctypes.WinError(ctypes.get_last_error())
        assert api.WaitForSingleObject(child_handle, 0) == 258
        release_path.touch()
        assert await asyncio.wait_for(parent.wait(), timeout=5) == 41
        assert api.WaitForSingleObject(child_handle, 5000) == 0
    finally:
        await _release(parent)
        if child_handle is not None and not api.CloseHandle(child_handle):
            raise ctypes.WinError(ctypes.get_last_error())
