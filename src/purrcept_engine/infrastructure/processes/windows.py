"""在创建时即归入独立 Windows Job 的短期进程所有者。

``WindowsProcess.launch`` 先建立 Job 限制，再用 JOB_LIST 原子关联子进程；
HANDLE_LIST 只交接调用方明确提供的 IPC 句柄。Job 留在宿主，最后一个句柄
关闭会终止所属进程。本模块不执行脚本、解释 IPC 或保存业务回执。

原生 CPU 限制只计用户态时间；``total_cpu_seconds`` 提供用户态与内核态之和，
由执行宿主核对总 CPU 预算。提交内存限制拒绝超额分配，不保证进程自动退出；
资源违规的识别与后续派发撤销仍属于执行宿主和可信 Worker 入口。
"""

from __future__ import annotations

import asyncio
import ctypes
import math
from dataclasses import dataclass
from pathlib import Path
from subprocess import list2cmdline
from typing import Protocol, Self

__all__ = ["WindowsLimits", "WindowsProcess"]

_PROCESS_TIME = 0x00000002
_JOB_TIME = 0x00000004
_PROCESS_MEMORY = 0x00000100
_JOB_MEMORY = 0x00000200
_KILL_ON_CLOSE = 0x00002000
_EXTENDED_LIMITS = 9
_BASIC_ACCOUNTING = 1
_HANDLE_LIST = 0x00020002
_JOB_LIST = 0x0002000D
_EXTENDED_STARTUP = 0x00080000
_UNICODE_ENVIRONMENT = 0x00000400
_NO_WINDOW = 0x08000000
_HANDLE_INHERIT = 0x00000001
_WAIT_TIMEOUT = 258
_WAIT_FAILED = 0xFFFFFFFF
_ABORT_WAIT_MS = 5000


class _BasicLimits(ctypes.Structure):
    """winnt.h 的用户时间、标志和指针宽度字段；原生布局由 ctypes 对齐。"""

    PerProcessUserTimeLimit: int
    PerJobUserTimeLimit: int
    LimitFlags: int
    MinimumWorkingSetSize: int
    MaximumWorkingSetSize: int
    ActiveProcessLimit: int
    Affinity: int
    PriorityClass: int
    SchedulingClass: int
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    """IO_COUNTERS 的六个 64 位计数，仅作为扩展限制的原生布局组成。"""

    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    """提交内存按 SIZE_T 表示；512 MiB 不代表工作集或 RSS。"""

    BasicLimitInformation: _BasicLimits
    ProcessMemoryLimit: int
    JobMemoryLimit: int
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _StartupInfo(ctypes.Structure):
    """STARTUPINFOW 的固定字段及标准流句柄，不让 Job 句柄进入此结构。"""

    cb: int
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_uint32),
        ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32),
        ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16),
        ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _StartupInfoEx(ctypes.Structure):
    """扩展属性列表在 CreateProcessW 返回前一直由 launch 持有。"""

    StartupInfo: _StartupInfo
    lpAttributeList: int | None
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    """CreateProcessW 交接的两个真实句柄及身份，不能以 PID 替代所有权。"""

    hProcess: int | None
    hThread: int | None
    dwProcessId: int
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    ]


class _FileTime(ctypes.Structure):
    """FILETIME 的两个 32 位部分，组合后以 100 纳秒计量。"""

    low: int
    high: int
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _Accounting(ctypes.Structure):
    """累计 Job 内所有进程的 CPU，包含已退出的 venv 重定向器或解释器。"""

    TotalUserTime: int
    TotalKernelTime: int
    ActiveProcesses: int
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class _Function(Protocol):
    """ctypes 函数的装配元数据；具体调用签名由各个原生 API 声明。"""

    argtypes: tuple[object, ...]
    restype: object


class _CreateJob(_Function, Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def __call__(self, security: None, name: None) -> int | None:
        """创建当前所有者独占的原生 Job Object。

        Args:
            security: 原生安全属性；None 使用默认属性。
            name: 当前声明、字段或协议操作的明确名称。
        """
        ...


class _SetJob(_Function, Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def __call__(self, job: int, kind: int, data: object, size: int) -> int:
        """向已有 Job 写入明确的原生资源限制结构。

        Args:
            job: 当前所有者创建的 Windows Job Object 句柄。
            kind: 此业务值所属的稳定种类。
            data: 当前边界接收的原始协议或媒体内容。
            size: 当前缓冲区的字节容量。
        """
        ...


class _QueryJob(_Function, Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def __call__(self, job: int, kind: int, data: object, size: int, returned: None) -> int:
        """读取当前 Job 的原生资源事实到给定缓冲。

        Args:
            job: 当前所有者创建的 Windows Job Object 句柄。
            kind: 此业务值所属的稳定种类。
            data: 当前边界接收的原始协议或媒体内容。
            size: 当前缓冲区的字节容量。
            returned: 原生 API 返回实际写入长度的输出位置。
        """
        ...


class _CloseHandle(_Function, Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def __call__(self, handle: int) -> int:
        """释放当前所有者已经取得的原生句柄。

        Args:
            handle: 当前所有者持有的原生句柄。
        """
        ...


class _InitializeAttributes(_Function, Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def __call__(self, data: object, count: int, flags: int, size: object) -> int:
        """为显式进程启动属性分配初始化范围。

        Args:
            data: 当前边界接收的原始协议或媒体内容。
            count: 原生缓冲区或句柄数组的有效元素数量。
            flags: 原生 API 的显式标志位组合。
            size: 当前缓冲区的字节容量。
        """
        ...


class _UpdateAttributes(_Function, Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def __call__(
        self,
        data: object,
        flags: int,
        kind: int,
        value: object,
        size: int,
        previous: None,
        returned: None,
    ) -> int:
        """将明确的可继承句柄等原生属性写入启动结构。

        Args:
            data: 当前边界接收的原始协议或媒体内容。
            flags: 原生 API 的显式标志位组合。
            kind: 此业务值所属的稳定种类。
            value: 此校验边界接收的原始值，校验失败时保留明确错误。
            size: 当前缓冲区的字节容量。
            previous: 调用方持有的先前预设版本，用于拒绝覆盖并发修改。
            returned: 原生 API 返回实际写入长度的输出位置。
        """
        ...


class _DeleteAttributes(_Function, Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def __call__(self, data: object) -> None:
        """释放当前进程启动属性列表的原生存储。

        Args:
            data: 当前边界接收的原始协议或媒体内容。
        """
        ...


class _CreateProcess(_Function, Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def __call__(
        self,
        executable: str,
        command: object,
        process_security: None,
        thread_security: None,
        inherit: bool,
        flags: int,
        environment: object,
        directory: None,
        startup: object,
        output: object,
    ) -> int:
        """按显式命令和启动属性创建隔离子进程。

        Args:
            executable: 显式配置的外部程序路径，不自动寻找或安装替代程序。
            command: Windows CreateProcessW 使用的可变原生命令行缓冲。
            process_security: 原生进程安全属性，此适配层传 None。
            thread_security: 原生线程安全属性，此适配层传 None。
            inherit: 是否允许原生句柄继承，仍受显式继承列表限制。
            flags: 原生 API 的显式标志位组合。
            environment: 子进程启动的原生环境块。
            directory: 本次操作使用的明确目录。
            startup: 已填写的原生启动属性结构。
            output: 接收原生 API 输出的已分配结构或指针。
        """
        ...


class _HandleInformation(_Function, Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def __call__(self, handle: int, flags: object) -> int:
        """为当前句柄设置明确的继承标志。

        Args:
            handle: 当前所有者持有的原生句柄。
            flags: 原生 API 的显式标志位组合。
        """
        ...


class _IsProcessInJob(_Function, Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def __call__(self, process: int, job: int, result: object) -> int:
        """查询实际进程与 Job 的归属关系。

        Args:
            process: 当前所有者持有的 Windows 进程句柄。
            job: 当前所有者创建的 Windows Job Object 句柄。
            result: 接收原生 API 返回结果的已分配结构或指针。
        """
        ...


class _Wait(_Function, Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def __call__(self, process: int, milliseconds: int) -> int:
        """在原生毫秒期限内等待实际进程结束。

        Args:
            process: 当前所有者持有的 Windows 进程句柄。
            milliseconds: 原生进程等待的毫秒上限。
        """
        ...


class _ExitCode(_Function, Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def __call__(self, process: int, code: object) -> int:
        """读取实际进程报告的原生退出码。

        Args:
            process: 当前所有者持有的 Windows 进程句柄。
            code: 供调用方分类处理的稳定错误码。
        """
        ...


class _TerminateJob(_Function, Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def __call__(self, job: int, exit_code: int) -> int:
        """结束当前 Job 中的实际进程并保留退出码。

        Args:
            job: 当前所有者创建的 Windows Job Object 句柄。
            exit_code: 请求终止 Job 时使用的原生退出码。
        """
        ...


class _ProcessTimes(_Function, Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def __call__(
        self,
        process: int,
        creation: object,
        exit_time: object,
        kernel: object,
        user: object,
    ) -> int:
        """取得进程实际 CPU 与创建、退出时间。

        Args:
            process: 当前所有者持有的 Windows 进程句柄。
            creation: 接收原生进程创建时间的输出位置。
            exit_time: 接收原生进程退出时间的输出位置。
            kernel: 接收进程内核态累计 CPU 时间的输出位置。
            user: 接收进程用户态累计 CPU 时间的输出位置。
        """
        ...


class _WindowsDirectory(_Function, Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def __call__(self, buffer: object, size: int) -> int:
        """读取系统实际 Windows 目录到给定缓冲。

        Args:
            buffer: 本次操作使用的有界缓冲或原生缓冲地址。
            size: 当前缓冲区的字节容量。
        """
        ...


class _Kernel32(ctypes.WinDLL):
    """按 Win32 返回值和显式 argtypes 绑定有限 API，避免 64 位句柄截断。"""

    CreateJobObjectW: _CreateJob
    SetInformationJobObject: _SetJob
    QueryInformationJobObject: _QueryJob
    CloseHandle: _CloseHandle
    InitializeProcThreadAttributeList: _InitializeAttributes
    UpdateProcThreadAttribute: _UpdateAttributes
    DeleteProcThreadAttributeList: _DeleteAttributes
    CreateProcessW: _CreateProcess
    GetHandleInformation: _HandleInformation
    IsProcessInJob: _IsProcessInJob
    WaitForSingleObject: _Wait
    GetExitCodeProcess: _ExitCode
    TerminateJobObject: _TerminateJob
    GetProcessTimes: _ProcessTimes
    GetWindowsDirectoryW: _WindowsDirectory


def _kernel32() -> _Kernel32:
    """为本次所有者建立原生调用声明，不修改进程全局 SDK 或 Registry。"""
    api = _Kernel32("kernel32", use_last_error=True)
    api.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    api.CreateJobObjectW.restype = ctypes.c_void_p
    api.SetInformationJobObject.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    api.SetInformationJobObject.restype = ctypes.c_int
    api.QueryInformationJobObject.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    api.QueryInformationJobObject.restype = ctypes.c_int
    api.CloseHandle.argtypes = (ctypes.c_void_p,)
    api.CloseHandle.restype = ctypes.c_int
    api.InitializeProcThreadAttributeList.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    api.InitializeProcThreadAttributeList.restype = ctypes.c_int
    api.UpdateProcThreadAttribute.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    api.UpdateProcThreadAttribute.restype = ctypes.c_int
    api.DeleteProcThreadAttributeList.argtypes = (ctypes.c_void_p,)
    api.DeleteProcThreadAttributeList.restype = None
    api.CreateProcessW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    api.CreateProcessW.restype = ctypes.c_int
    api.GetHandleInformation.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    api.GetHandleInformation.restype = ctypes.c_int
    api.IsProcessInJob.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    api.IsProcessInJob.restype = ctypes.c_int
    api.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    api.WaitForSingleObject.restype = ctypes.c_uint32
    api.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    api.GetExitCodeProcess.restype = ctypes.c_int
    api.TerminateJobObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    api.TerminateJobObject.restype = ctypes.c_int
    api.GetProcessTimes.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    api.GetProcessTimes.restype = ctypes.c_int
    api.GetWindowsDirectoryW.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    api.GetWindowsDirectoryW.restype = ctypes.c_uint32
    return api


def _check(result: int) -> None:
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())


def _close(api: _Kernel32, handle: int) -> None:
    _check(api.CloseHandle(handle))


def _configure_job(
    api: _Kernel32, job: int, cpu_ticks: int | None, memory_bytes: int | None
) -> None:
    """创建前落实所有权和所选硬限制，可信设备辅助进程可只选择所有权。"""
    info = _ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = _KILL_ON_CLOSE
    if cpu_ticks is not None:
        assert memory_bytes is not None
        info.BasicLimitInformation.PerProcessUserTimeLimit = cpu_ticks
        info.BasicLimitInformation.PerJobUserTimeLimit = cpu_ticks
        info.BasicLimitInformation.LimitFlags |= (
            _PROCESS_TIME | _JOB_TIME | _PROCESS_MEMORY | _JOB_MEMORY
        )
        info.ProcessMemoryLimit = memory_bytes
        info.JobMemoryLimit = memory_bytes
    _check(
        api.SetInformationJobObject(job, _EXTENDED_LIMITS, ctypes.byref(info), ctypes.sizeof(info))
    )


def _verify_owned_job(api: _Kernel32, job: int) -> None:
    """只验证可信设备辅助进程的归属，不返回虚假的零 CPU/内存额度。"""
    info = _ExtendedLimits()
    _check(
        api.QueryInformationJobObject(
            job,
            _EXTENDED_LIMITS,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        )
    )
    if info.BasicLimitInformation.LimitFlags != _KILL_ON_CLOSE:
        raise OSError("辅助进程 Job 的退出清理设置与请求不一致。")


def _environment(api: _Kernel32) -> ctypes.Array[ctypes.c_wchar]:
    """只给解释器系统目录，不把宿主环境中的账号先交给 Worker 再清空。"""
    buffer = ctypes.create_unicode_buffer(32768)
    length = api.GetWindowsDirectoryW(buffer, len(buffer))
    if not length:
        raise ctypes.WinError(ctypes.get_last_error())
    if length >= len(buffer):
        raise OSError("Windows 系统目录超出原生环境缓冲区。")
    return ctypes.create_unicode_buffer(f"SystemRoot={buffer.value}\0\0")


@dataclass(frozen=True, slots=True)
class WindowsLimits:
    """所属 Job 已启用的设置；更严格的上级 Job 限制仍可能先发生。

    Attributes:
        user_cpu_seconds: 原生进程报告的用户态累计 CPU 秒数。
        memory_bytes: 此 Worker 允许使用的内存字节上限。
        kill_on_close: 关闭 Job 句柄时是否结束其仍持有的进程。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        用户态 CPU 与提交内存对本次 Job 累计和其中单进程采用同值。此快照
        不代表已经验证资源违规后的业务收尾，也不把上级约束误称为本次额度。
    """

    user_cpu_seconds: float
    memory_bytes: int
    kill_on_close: bool


def _read_limits(api: _Kernel32, job: int) -> WindowsLimits:
    """回读有效标志和数值；只写入字段而未启用标志不能算额度成立。"""
    info = _ExtendedLimits()
    _check(
        api.QueryInformationJobObject(
            job, _EXTENDED_LIMITS, ctypes.byref(info), ctypes.sizeof(info), None
        )
    )
    required = _PROCESS_TIME | _JOB_TIME | _PROCESS_MEMORY | _JOB_MEMORY | _KILL_ON_CLOSE
    if info.BasicLimitInformation.LimitFlags & required != required:
        raise OSError("Job 的必需限制标志未生效。")
    if (
        info.BasicLimitInformation.PerProcessUserTimeLimit
        != info.BasicLimitInformation.PerJobUserTimeLimit
        or info.ProcessMemoryLimit != info.JobMemoryLimit
    ):
        raise OSError("Job 和单进程额度不一致。")
    return WindowsLimits(
        info.BasicLimitInformation.PerJobUserTimeLimit / 10_000_000,
        info.JobMemoryLimit,
        True,
    )


class WindowsProcess:
    """独占进程和 Job 的宿主资源所有者，不向 Worker 暴露 Job 句柄。

    调用方须在实际退出后 close；取消 wait 只停止该等待。terminate 终止本次
    Job，不能用于外部设备进程。启动和资源状态操作由同一宿主所有者串行调用，
    不支持与 close 并发访问句柄。进程初始就在 Job 内，父进程异常退出也能收回它。
    """

    def __init__(self, api: _Kernel32, pid: int, process: int, job: int) -> None:
        """接管 launch 已经确认的真实句柄，构造不再创建操作系统资源。

        Args:
            api: 当前 Windows 进程使用的显式 Kernel32 调用表。
            pid: 需要纳入隔离资源约束的原生进程 ID。
            process: 当前所有者持有的 Windows 进程句柄。
            job: 当前所有者创建的 Windows Job Object 句柄。
        """
        self._api = api
        self._pid = pid
        self._process: int | None = process
        self._job: int | None = job
        self._exit_code: int | None = None
        self._final_cpu: float | None = None
        self._final_process_cpu: float | None = None

    @classmethod
    def launch(
        cls,
        command: tuple[str, ...],
        *,
        cpu_seconds: float | None = 15.0,
        memory_bytes: int | None = 512 * 1024 * 1024,
        inherited_handles: tuple[int, ...] = (),
    ) -> Self:
        """不经 shell 创建进程，限制与归属失败时回收本次全部资源。

        Args:
            command: 子进程使用的明确可执行文件与参数序列。
            cpu_seconds: 此 Worker 允许使用的 CPU 秒数上限。 默认 15.0。
            memory_bytes: 此 Worker 允许使用的内存字节上限。
            inherited_handles: 子进程启动时唯一允许继承的原生句柄集合。 默认不附加此类条目。

        Returns:
            校验完成的当前值或解析后的新配置，具体是否复制由实现说明。

        Raises:
            ValueError: 明确继承的 IPC 句柄必须已标记为可继承。
            ctypes.WinError: 参数、材料或当前状态不满足此入口的约束。
            OSError: 典型拒绝原因：Job 回读额度与请求不一致；CreateProcessW
                未返回完整进程所有权；新进程未归入指定 Job，拒绝采用；
                启动失败后的进程终止尚未确认。
            BaseExceptionGroup: 进程启动失败且资源回收失败。

        Note:
            executable 必须是绝对路径。明确传入的句柄须已经标记为可继承；本入口
            不临时扩大其他句柄的继承资格。环境只包含操作系统给出的 SystemRoot。
            原生失败传播 OSError；清理同时失败时汇总原始异常，不伪报成功。
            两项额度同时为 None 时只取得可信设备辅助进程的完整进程树所有权。
            模型行动调用始终传入已校验正额度，不能通过此选择取消 Worker 限制。
        """
        cpu_ticks = _validate_launch(command, cpu_seconds, memory_bytes, inherited_handles)
        api = _kernel32()
        for handle in inherited_handles:
            flags = ctypes.c_uint32()
            _check(api.GetHandleInformation(handle, ctypes.byref(flags)))
            if not flags.value & _HANDLE_INHERIT:
                raise ValueError("明确继承的 IPC 句柄必须已标记为可继承。")

        job = api.CreateJobObjectW(None, None)
        if job is None:
            raise ctypes.WinError(ctypes.get_last_error())
        process_info = _ProcessInfo()
        try:
            _configure_job(api, job, cpu_ticks, memory_bytes)
            if cpu_ticks is None:
                _verify_owned_job(api, job)
            else:
                actual_limits = _read_limits(api, job)
                if (
                    actual_limits.user_cpu_seconds != cpu_ticks / 10_000_000
                    or actual_limits.memory_bytes != memory_bytes
                ):
                    raise OSError("Job 回读额度与请求不一致。")
            environment = _environment(api)

            # Initialize 的第一次失败仅用于取得必需字节数，不是可忽略的设置失败。
            attribute_size = ctypes.c_size_t()
            attribute_count = 1 + bool(inherited_handles)
            initialized = api.InitializeProcThreadAttributeList(
                None, attribute_count, 0, ctypes.byref(attribute_size)
            )
            if initialized or ctypes.get_last_error() != 122 or not attribute_size.value:
                raise ctypes.WinError(ctypes.get_last_error())
            attributes = ctypes.create_string_buffer(attribute_size.value)
            _check(
                api.InitializeProcThreadAttributeList(
                    attributes, attribute_count, 0, ctypes.byref(attribute_size)
                )
            )
            try:
                jobs = (ctypes.c_void_p * 1)(job)
                _check(
                    api.UpdateProcThreadAttribute(
                        attributes, 0, _JOB_LIST, jobs, ctypes.sizeof(jobs), None, None
                    )
                )
                # 两个数组都保持存活至 CreateProcessW 返回；Job 本身不在继承列表中。
                handles = (ctypes.c_void_p * len(inherited_handles))(*inherited_handles)
                if inherited_handles:
                    _check(
                        api.UpdateProcThreadAttribute(
                            attributes, 0, _HANDLE_LIST, handles, ctypes.sizeof(handles), None, None
                        )
                    )
                startup = _StartupInfoEx()
                startup.StartupInfo.cb = ctypes.sizeof(startup)
                startup.lpAttributeList = ctypes.addressof(attributes)
                command_line = ctypes.create_unicode_buffer(list2cmdline(command))
                _check(
                    api.CreateProcessW(
                        command[0],
                        command_line,
                        None,
                        None,
                        bool(inherited_handles),
                        _EXTENDED_STARTUP | _UNICODE_ENVIRONMENT | _NO_WINDOW,
                        environment,
                        None,
                        ctypes.byref(startup),
                        ctypes.byref(process_info),
                    )
                )
            finally:
                api.DeleteProcThreadAttributeList(attributes)

            process = process_info.hProcess
            thread = process_info.hThread
            if process is None or thread is None:
                raise OSError("CreateProcessW 未返回完整进程所有权。")
            belongs = ctypes.c_int()
            _check(api.IsProcessInJob(process, job, ctypes.byref(belongs)))
            if not belongs.value:
                raise OSError("新进程未归入指定 Job，拒绝采用。")
            _close(api, thread)
            process_info.hThread = None
            return cls(api, process_info.dwProcessId, process, job)
        except BaseException as error:
            cleanup_errors: list[BaseException] = []
            # 原子 JOB_LIST 保证已创建的进程在此 Job 内。关闭最后 Job 句柄
            # 先请求终止，再等待进程实际退出，避免仅关闭进程句柄留下运行者。
            try:
                _close(api, job)
            except OSError as cleanup_error:
                cleanup_errors.append(cleanup_error)
            if process_info.hProcess is not None:
                try:
                    # 原生清理失败不能让启动失败路径无限阻塞宿主。结束确认失败
                    # 与原始启动异常共同报告，不能把请求终止冒充已经退出。
                    status = api.WaitForSingleObject(process_info.hProcess, _ABORT_WAIT_MS)
                    if status == _WAIT_FAILED:
                        raise ctypes.WinError(ctypes.get_last_error())
                    if status != 0:
                        raise OSError("启动失败后的进程终止尚未确认。")
                except OSError as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            for handle in (process_info.hThread, process_info.hProcess):
                if handle is not None:
                    try:
                        _close(api, handle)
                    except OSError as cleanup_error:
                        cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                raise BaseExceptionGroup(
                    "进程启动失败且资源回收失败。", [error, *cleanup_errors]
                ) from None
            raise

    @property
    def pid(self) -> int:
        """返回本次创建身份。

        Note:
            资源处置始终使用原始句柄，避免 PID 重用。
        """
        return self._pid

    @property
    def process_handle(self) -> int:
        """借出只在 close 前有效的原始句柄。

        Raises:
            RuntimeError: 进程所有者已经关闭。

        Note:
            调用者不能关闭或转交所有权。
        """
        if self._process is None:
            raise RuntimeError("进程所有者已经关闭。")
        return self._process

    def poll(self) -> int | None:
        """读取实际进程退出状态。

        Raises:
            ctypes.WinError: 参数、材料或当前状态不满足此入口的约束。
            OSError: 进程等待返回无效状态。

        Note:
            活跃时返回 None，不误解退出码 259。
        """
        if self._exit_code is not None:
            return self._exit_code
        status = self._api.WaitForSingleObject(self.process_handle, 0)
        if status == _WAIT_TIMEOUT:
            return None
        if status == _WAIT_FAILED:
            raise ctypes.WinError(ctypes.get_last_error())
        if status != 0:
            raise OSError("进程等待返回无效状态。")
        code = ctypes.c_uint32()
        _check(self._api.GetExitCodeProcess(self.process_handle, ctypes.byref(code)))
        # 一个 venv executable 可以由重定向器和真实解释器组成。主进程退出
        # 不能代替所有所属进程实际退出；剩余者仍消耗本次 Job 的同一份额度。
        if self._accounting().ActiveProcesses:
            return None
        self._exit_code = code.value
        return self._exit_code

    async def wait(self) -> int:
        """观察本次进程直至实际退出。

        Note:
            取消等待不会终止进程或释放句柄。

            观察回调目标间隔为 20 毫秒，避开 Windows 常见的 15.6 毫秒时钟
            分辨率造成的提前热轮询。实际调度仍受事件循环影响，不承诺零延迟
            终止确认。资源限额由 Job 执行，此等待不负责追加或重置额度。
        """
        loop = asyncio.get_running_loop()
        result: asyncio.Future[int] = loop.create_future()
        scheduled: asyncio.TimerHandle | None = None

        def observe_exit() -> None:
            """至多保留一个观察回调，不为进程等待创建不可取消的阻塞线程。"""
            nonlocal scheduled
            if result.done():
                return
            try:
                exit_code = self.poll()
            except OSError as error:
                result.set_exception(error)
                return
            if exit_code is None:
                scheduled = loop.call_later(0.02, observe_exit)
            else:
                result.set_result(exit_code)

        observe_exit()
        try:
            return await result
        finally:
            if scheduled is not None:
                scheduled.cancel()

    def terminate(self, exit_code: int = 1) -> None:
        """请求终止本次 Job。

        Args:
            exit_code: 请求终止 Job 时使用的原生退出码。 默认 1。

        Raises:
            ValueError: 退出码必须是无符号 32 位整数。

        Note:
            之后仍须 wait 确认并 close 释放所有权。
        """
        if (
            isinstance(exit_code, bool)
            or not isinstance(exit_code, int)
            or not 0 <= exit_code < 2**32
        ):
            raise ValueError("退出码必须是无符号 32 位整数。")
        if self._job is not None:
            _check(self._api.TerminateJobObject(self._job, exit_code))

    @property
    def process_cpu_seconds(self) -> float:
        """读取主进程的用户态与内核态 CPU。

        Note:
            不能代替 Job 内真实解释器总量。
        """
        if self._final_process_cpu is not None:
            return self._final_process_cpu
        created, exited, kernel, user = (_FileTime() for _ in range(4))
        _check(
            self._api.GetProcessTimes(
                self.process_handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
        )
        total_ticks = (kernel.high << 32) + kernel.low + (user.high << 32) + user.low
        return total_ticks / 10_000_000

    @property
    def total_cpu_seconds(self) -> float:
        """读取整个所属 Job 的用户态与内核态累计 CPU，供宿主监督同一额度。"""
        if self._final_cpu is not None:
            return self._final_cpu
        accounting = self._accounting()
        return (accounting.TotalUserTime + accounting.TotalKernelTime) / 10_000_000

    def _accounting(self) -> _Accounting:
        """Job 的累计量包含已经终止的所属进程，不因解释器转交重置计量。"""
        if self._job is None:
            raise RuntimeError("Job 所有者已经关闭。")
        info = _Accounting()
        _check(
            self._api.QueryInformationJobObject(
                self._job, _BASIC_ACCOUNTING, ctypes.byref(info), ctypes.sizeof(info), None
            )
        )
        return info

    def query_limits(self) -> WindowsLimits:
        """回读硬额度。

        Returns:
            已对当前 Worker 生效的平台资源约束。

        Raises:
            RuntimeError: Job 所有者已经关闭。

        Note:
            仅启用进程树所有权的辅助 Job 明确抛 OSError。
        """
        if self._job is None:
            raise RuntimeError("Job 所有者已经关闭。")
        return _read_limits(self._api, self._job)

    def close(self) -> None:
        """仅在实际退出后关闭进程和 Job。

        Raises:
            RuntimeError: 进程仍在运行，须先终止并等待实际退出。

        Note:
            重复调用不重新关闭已释放句柄。
        """
        if self.poll() is None:
            raise RuntimeError("进程仍在运行，须先终止并等待实际退出。")
        if self._process is not None:
            self._final_cpu = self.total_cpu_seconds
            self._final_process_cpu = self.process_cpu_seconds
            _close(self._api, self._process)
            self._process = None
        if self._job is not None:
            _close(self._api, self._job)
            self._job = None


def _validate_launch(
    command: tuple[str, ...],
    cpu_seconds: float | None,
    memory_bytes: int | None,
    handles: tuple[int, ...],
) -> int | None:
    """只在真实原生边界核验 Python 值，拒绝溢出与隐式 PATH/shell 选择。"""
    if not isinstance(command, tuple) or not command:
        raise ValueError("command 必须是包含绝对 executable 的非空元组。")
    if any(not isinstance(part, str) or "\0" in part for part in command):
        raise ValueError("command 只接受无空字符的字符串参数。")
    if not Path(command[0]).is_absolute():
        raise ValueError("executable 必须是绝对路径。")
    if (cpu_seconds is None) != (memory_bytes is None):
        raise ValueError("CPU 与内存硬限制须同时声明，或同时仅选择进程树所有权。")
    cpu_ticks: int | None = None
    if cpu_seconds is not None:
        if (
            isinstance(cpu_seconds, bool)
            or not isinstance(cpu_seconds, (int, float))
            or not math.isfinite(cpu_seconds)
            or cpu_seconds <= 0
        ):
            raise ValueError("cpu_seconds 必须是正有限时长。")
        cpu_ticks = math.ceil(cpu_seconds * 10_000_000)
        if cpu_ticks >= 2**63:
            raise ValueError("CPU 额度超出原生时间字段范围。")
    if memory_bytes is not None and (
        isinstance(memory_bytes, bool)
        or not isinstance(memory_bytes, int)
        or not 0 < memory_bytes < 2 ** (ctypes.sizeof(ctypes.c_size_t) * 8)
    ):
        raise ValueError("memory_bytes 必须是原生 SIZE_T 范围内的正整数。")
    if (
        not isinstance(handles, tuple)
        or any(
            isinstance(handle, bool)
            or not isinstance(handle, int)
            or not 0 < handle < 2 ** (ctypes.sizeof(ctypes.c_void_p) * 8)
            for handle in handles
        )
        or len(set(handles)) != len(handles)
    ):
        raise ValueError("inherited_handles 必须包含互不重复的真实句柄。")
    return cpu_ticks
