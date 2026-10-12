"""可信Windows采集进程，不加载Engine、Core、模型或运行配置。

只接受ready/foreground/capture，逐条处理有界stdin/stdout帧。ready不读取
设备；每次实际读取重新核输入桌面、窗口及显示器，锁屏或目标消失明确失败。
父端断开直接终止本进程，因此阻塞的原生采集不会留在宿主线程。
"""

from __future__ import annotations

import base64
import ctypes
import io
import json
import msvcrt
import os
import struct
import sys
import time
from ctypes import wintypes
from typing import Protocol, cast

_MAX_REQUEST = 256 * 1024
_MAX_RESPONSE = 12 * 1024 * 1024


class _Native(Protocol):
    """已绑定C ABI的Win32函数；动态加载只在此窄边界转换类型。"""

    argtypes: list[object]
    restype: object

    def __call__(self, *arguments: object) -> int: ...


class _Monitor(ctypes.Structure):
    """MONITORINFOEXW，包括Windows设备名，不缓存可热拔插句柄。"""

    size: int
    monitor: wintypes.RECT
    device: str
    _fields_ = [
        ("size", wintypes.DWORD),
        ("monitor", wintypes.RECT),
        ("work", wintypes.RECT),
        ("flags", wintypes.DWORD),
        ("device", wintypes.WCHAR * 32),
    ]


class WindowsDesktop:
    """只在此独立进程绑定桌面API，构造只设置本进程DPI感知。"""

    def __init__(self) -> None:
        """句柄用指针宽度返回，避免64位机器截断。"""
        if os.name != "nt":
            raise RuntimeError("实体桌面采集需要Windows。")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._function(
            self.user, "SetProcessDpiAwarenessContext", [ctypes.c_void_p], wintypes.BOOL
        )(ctypes.c_void_p(-4))

    @staticmethod
    def _function(
        library: ctypes.WinDLL, name: str, arguments: list[object], result: object
    ) -> _Native:
        """转换仅覆盖已声明ABI的原生入口，不用动态属性传播不明类型。"""
        function = cast(_Native, getattr(library, name))
        function.argtypes, function.restype = arguments, result
        return function

    def _monitor(self, handle: int) -> tuple[tuple[int, int, int, int], str]:
        info = _Monitor()
        info.size = ctypes.sizeof(info)
        valid = self._function(
            self.user,
            "GetMonitorInfoW",
            [ctypes.c_void_p, ctypes.c_void_p],
            wintypes.BOOL,
        )(handle, ctypes.byref(info))
        if not valid:
            raise RuntimeError("显示器已不可用。")
        rectangle = info.monitor
        return (rectangle.left, rectangle.top, rectangle.right, rectangle.bottom), info.device

    def foreground(self) -> dict[str, object]:
        """每次检查Default输入桌面，不让锁定/安全桌面的旧画面冒充当前画面。"""
        desktop = self._function(
            self.user,
            "OpenInputDesktop",
            [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
            ctypes.c_size_t,
        )(0, False, 0x0001)
        if not desktop:
            raise RuntimeError("桌面不可访问或已锁定。")
        try:
            name, needed = ctypes.create_unicode_buffer(256), wintypes.DWORD()
            readable = self._function(
                self.user,
                "GetUserObjectInformationW",
                [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
                wintypes.BOOL,
            )(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed))
            if not readable or name.value.casefold() != "default":
                raise RuntimeError("桌面不可访问或已锁定。")
        finally:
            self._function(self.user, "CloseDesktop", [ctypes.c_void_p], wintypes.BOOL)(desktop)

        window = self._function(self.user, "GetForegroundWindow", [], ctypes.c_size_t)()
        rectangle = wintypes.RECT()
        readable = self._function(
            self.user, "GetWindowRect", [ctypes.c_void_p, ctypes.c_void_p], wintypes.BOOL
        )(window, ctypes.byref(rectangle))
        if not window or not readable:
            raise RuntimeError("没有可用前台窗口。")
        length = self._function(self.user, "GetWindowTextLengthW", [ctypes.c_void_p], ctypes.c_int)(
            window
        )
        title = ctypes.create_unicode_buffer(length + 1)
        self._function(
            self.user,
            "GetWindowTextW",
            [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int],
            ctypes.c_int,
        )(window, title, len(title))
        process_id = wintypes.DWORD()
        self._function(
            self.user,
            "GetWindowThreadProcessId",
            [ctypes.c_void_p, ctypes.c_void_p],
            wintypes.DWORD,
        )(window, ctypes.byref(process_id))
        executable = ""
        process = self._function(
            self.kernel,
            "OpenProcess",
            [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
            ctypes.c_size_t,
        )(0x1000, False, process_id.value)
        if process:
            try:
                path, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
                if self._function(
                    self.kernel,
                    "QueryFullProcessImageNameW",
                    [ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR, ctypes.c_void_p],
                    wintypes.BOOL,
                )(process, 0, path, ctypes.byref(size)):
                    executable = path.value
            finally:
                self._function(self.kernel, "CloseHandle", [ctypes.c_void_p], wintypes.BOOL)(
                    process
                )
        monitor = self._function(
            self.user, "MonitorFromWindow", [ctypes.c_void_p, wintypes.DWORD], ctypes.c_size_t
        )(window, 2)
        bounds, monitor_id = self._monitor(monitor)
        return {
            "window_id": str(window),
            "title": title.value,
            "process_id": process_id.value,
            "executable": executable,
            "bounds": (rectangle.left, rectangle.top, rectangle.right, rectangle.bottom),
            "monitor_id": monitor_id,
            "monitor_bounds": bounds,
        }

    def capture(self, policy: dict[str, object], native: bool) -> dict[str, object]:
        """保留原始像素；连续采样等比缩至1920，单次截图不缩放。"""
        from PIL import ImageGrab

        started = time.perf_counter()
        foreground = self.foreground()
        target = policy.get("target", "foreground_monitor")
        if not isinstance(target, str) or target not in ("foreground_monitor", "monitor", "window"):
            raise ValueError("截图目标无效。")
        bounds = cast(
            tuple[int, int, int, int],
            foreground["bounds" if target == "window" else "monitor_bounds"],
        )
        monitor_id = foreground["monitor_id"]
        if target == "monitor":
            wanted = policy.get("monitor_id")
            monitors: dict[str, tuple[int, int, int, int]] = {}
            callback_type = ctypes.WINFUNCTYPE(
                wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.LPARAM
            )

            def visit(handle: int, _dc: int, _rectangle: int, _data: int) -> bool:
                box, identity = self._monitor(handle)
                monitors[identity] = box
                return True

            callback = callback_type(visit)
            self._function(
                self.user,
                "EnumDisplayMonitors",
                [ctypes.c_void_p, ctypes.c_void_p, callback_type, wintypes.LPARAM],
                wintypes.BOOL,
            )(None, None, callback, 0)
            if not isinstance(wanted, str) or wanted not in monitors:
                raise RuntimeError("指定显示器已不可用。")
            bounds, monitor_id = monitors[wanted], wanted
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            raise RuntimeError("截图区域为空。")
        with ImageGrab.grab(bbox=bounds, include_layered_windows=True, all_screens=True) as picture:
            if self.foreground()["window_id"] != foreground["window_id"]:
                raise RuntimeError("截图期间前台窗口已切换。")
            captured, original = time.time(), picture.size
            capture_ms = (time.perf_counter() - started) * 1000
            encoding = time.perf_counter()
            if not native:
                picture.thumbnail((1920, 1920))
            with picture.convert("RGB") as image, io.BytesIO() as output:
                image.save(output, format="JPEG", quality=85)
                data, delivered = output.getvalue(), image.size
        if not 0 < len(data) <= 8 * 1024 * 1024:
            raise RuntimeError("截图超过8MiB单帧额度。")
        return {
            "data": base64.b64encode(data).decode("ascii"),
            "captured_at": captured,
            "foreground": foreground,
            "target": target + ":" + cast(str, monitor_id),
            "original_size": original,
            "delivered_size": delivered,
            "capture_ms": capture_ms,
            "encode_ms": (time.perf_counter() - encoding) * 1000,
        }


def run() -> None:
    """有限协议循环；未知命令/坏帧结束进程，不猜字段或执行请求中的代码。"""
    if len(sys.argv) != 4:
        raise ValueError("采集启动句柄及依赖目录不完整。")
    sys.path.insert(0, sys.argv[3])
    input_fd = msvcrt.open_osfhandle(int(sys.argv[1]), os.O_RDONLY | os.O_BINARY)
    output_fd = msvcrt.open_osfhandle(int(sys.argv[2]), os.O_WRONLY | os.O_BINARY)
    with os.fdopen(input_fd, "rb") as incoming, os.fdopen(output_fd, "wb") as outgoing:
        _serve(incoming, outgoing)


def _serve(incoming: io.BufferedReader, outgoing: io.BufferedWriter) -> None:
    """管道只由一个循环使用，父端终止Job会使所有阻塞读写实际结束。"""
    desktop: WindowsDesktop | None = None
    while header := incoming.read(4):
        if len(header) != 4:
            raise ValueError("采集请求头截断。")
        length = struct.unpack("!I", header)[0]
        if not 0 < length <= _MAX_REQUEST:
            raise ValueError("采集请求超过协议额度。")
        body = incoming.read(length)
        if len(body) != length:
            raise ValueError("采集请求正文截断。")
        decoded: object = json.loads(body)
        if not isinstance(decoded, dict):
            raise ValueError("采集请求必须是对象。")
        request = cast(dict[str, object], decoded)
        command = request.get("command")
        try:
            if command == "ready":
                result: dict[str, object] = {"ready": True}
            else:
                if command not in ("foreground", "capture"):
                    raise ValueError("未知采集命令。")
                if desktop is None:
                    desktop = WindowsDesktop()
                if command == "foreground":
                    result = desktop.foreground()
                elif command == "capture":
                    policy = request.get("policy")
                    native = request.get("native")
                    if not isinstance(policy, dict) or type(native) is not bool:
                        raise ValueError("采集参数无效。")
                    result = desktop.capture(cast(dict[str, object], policy), native)
                else:
                    raise ValueError("未知采集命令。")
            response: dict[str, object] = {"result": result}
        except (OSError, RuntimeError, ValueError) as error:
            response = {"error": str(error), "error_type": type(error).__name__}
        payload = json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(payload) > _MAX_RESPONSE:
            raise ValueError("采集回复超过协议额度。")
        outgoing.write(struct.pack("!I", len(payload)) + payload)
        outgoing.flush()


if __name__ == "__main__":
    run()
