"""显式电脑连接的扩展面与内置独立进程代理。

ComputerRuntime在connect创建驱动，disconnect释放。内置进程不加载Engine、
Core、模型或凭据；只在显式请求读取Windows桌面。IPC是有界JSON，图像作为
base64完整交接，父端验证后冻结。关闭先终止实际进程，再收回管道等待。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import msvcrt
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from purrcept_engine.infrastructure.execution.protocol import FrameChannel
from purrcept_engine.infrastructure.processes.windows import WindowsProcess
from purrcept_engine.plugins.computer.models import (
    ComputerError,
    ComputerFrame,
    ForegroundInfo,
    ObservationPolicy,
)
from purrcept_engine.values import ActionProtocolError, encode_json

_MAX_FRAME = 12 * 1024 * 1024


class ComputerDriver(Protocol):
    """插件按一次显式连接创建的驱动，事件循环串行借用，断开时实际释放。

    start失败自行清理；capture不返回缓存，native保持原始分辨率。close必须
    能结束正在阻塞的采集等待，调用者仍收回在途任务。内置ProcessDriver用
    独立Windows进程；测试可注入离线驱动，不将驱动暴露给行动Worker。
    """

    @property
    def alive(self) -> bool:
        """只查连接资源，不读取桌面。"""
        ...

    async def start(self) -> None:
        """建立设备进程，不在这里截图。"""
        ...

    async def foreground(self) -> ForegroundInfo:
        """读取当前实际前台，不可访问桌面时明确失败。"""
        ...

    async def capture(self, policy: ObservationPolicy, *, native: bool = False) -> ComputerFrame:
        """取得完整当次画面及身份；目标消失不能选其他屏幕补位。"""
        ...

    async def close(self) -> None:
        """释放真正设备资源，重复调用加入同一收尾。"""
        ...


class ProcessDriver:
    """唯一标准库/Pillow采集子进程，无网络、pickle或未知命令。"""

    def __init__(self) -> None:
        """构造不触碰设备，只有start才创建进程。"""
        self._process: WindowsProcess | None = None
        self._channel: FrameChannel | None = None
        self._io: set[asyncio.Future[object]] = set()
        self._lock = asyncio.Lock()
        self._start_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None

    @property
    def alive(self) -> bool:
        """空闲也能观察实际进程退出。"""
        return self._process is not None and self._process.poll() is None

    async def start(self) -> None:
        """新连接创建可信脚本，隐藏Windows窗口，不读取环境中的模型配置。"""
        if self._close_task is not None:
            raise ComputerError("电脑驱动已关闭。")
        if self._start_task is None:
            self._start_task = asyncio.create_task(self._start(), name="computer-driver-start")
        await asyncio.shield(self._start_task)

    async def _start(self) -> None:
        """关闭等待此有限启动真正结束，创建期间取消不能遗留迟到进程。"""
        if os.name != "nt":
            raise ComputerError("实体桌面采集需要Windows。")
        worker = Path(__file__).with_name("worker.py")
        interpreter: object = vars(sys)["_base_executable"]
        if not isinstance(interpreter, str):
            raise RuntimeError("CPython没有提供真实基础解释器。")
        # -I基础解释器不继承venv环境；只把已配置Engine的依赖目录交给可信脚本。
        dependencies = Path(sys.prefix) / "Lib" / "site-packages"
        read_fd, child_write_fd = os.pipe()
        try:
            child_read_fd, write_fd = os.pipe()
        except BaseException:
            # 第二对分配失败时，第一对尚无其他所有者，必须在此收回。
            os.close(read_fd)
            os.close(child_write_fd)
            raise
        try:
            self._channel = FrameChannel(read_fd, write_fd, _MAX_FRAME)
        except BaseException:
            for descriptor in (read_fd, write_fd, child_read_fd, child_write_fd):
                os.close(descriptor)
            raise
        try:
            read_handle = msvcrt.get_osfhandle(child_read_fd)
            write_handle = msvcrt.get_osfhandle(child_write_fd)
            os.set_handle_inheritable(read_handle, True)
            os.set_handle_inheritable(write_handle, True)
            self._process = WindowsProcess.launch(
                (
                    interpreter,
                    "-I",
                    str(worker),
                    str(read_handle),
                    str(write_handle),
                    str(dependencies),
                ),
                cpu_seconds=None,
                memory_bytes=None,
                inherited_handles=(read_handle, write_handle),
            )
        except BaseException:
            self._channel.close()
            self._channel = None
            raise
        finally:
            os.close(child_read_fd)
            os.close(child_write_fd)
        try:
            ready = await asyncio.wait_for(self._request("ready"), 10.0)
            if ready != {"ready": True}:
                raise ComputerError("采集进程就绪协议不匹配。")
        except BaseException as failure:
            try:
                await self._release()
            except BaseException as cleanup:
                raise BaseExceptionGroup("电脑握手及资源释放失败。", [failure, cleanup]) from None
            raise

    async def _request(self, command: str, **arguments: object) -> dict[str, object]:
        """锁只串行采集，close不取此锁，阻塞原生采集仍能退出。"""
        channel = self._channel
        if channel is None or not self.alive:
            raise ComputerError("电脑采集进程未连接。")
        payload = {"command": command, **arguments}
        if len(encode_json(payload)) > 256 * 1024:
            raise ComputerError("电脑请求超过256KiB协议额度。")
        async with self._lock:
            await self._io_call(lambda: channel.send(payload))
            response = await self._io_call(channel.read)
        error = response.get("error")
        if isinstance(error, str):
            raise ComputerError(error)
        result = response.get("result")
        if not isinstance(result, dict):
            raise ComputerError("电脑回复缺少完整结果对象。")
        return dict(result)

    async def _io_call[Result](self, operation: Callable[[], Result]) -> Result:
        """实际管道读写归本实例，取消等待后关闭仍收回真正的阻塞操作。"""
        task = asyncio.create_task(asyncio.to_thread(operation))
        owned = cast(asyncio.Future[object], task)
        self._io.add(owned)
        owned.add_done_callback(self._io_done)
        return await asyncio.shield(task)

    def _io_done(self, task: asyncio.Future[object]) -> None:
        self._io.discard(task)
        if not task.cancelled():
            task.exception()

    async def foreground(self) -> ForegroundInfo:
        """在IPC边界校验完整身份，不返回旧窗口。"""
        return ForegroundInfo.model_validate(await self._bounded_request("foreground"))

    async def _bounded_request(self, command: str, **arguments: object) -> dict[str, object]:
        """沿用10秒设备请求期限；超时回收实际进程，不复用错位的迟到回复。"""
        try:
            return await asyncio.wait_for(self._request(command, **arguments), 10.0)
        except TimeoutError as error:
            await self.close()
            raise ComputerError("电脑采集请求超过10秒期限，连接已释放。") from error
        except ActionProtocolError as error:
            if error.code != "eof":
                raise
            raise ComputerError("电脑采集进程已结束，未返回完整当次画面。") from error

    async def capture(self, policy: ObservationPolicy, *, native: bool = False) -> ComputerFrame:
        """原生编码材料先严格base64解码，再验证8MiB单帧边界。"""
        result = await self._bounded_request(
            "capture", policy=policy.model_dump(mode="json"), native=native
        )
        encoded = result.pop("data", None)
        if not isinstance(encoded, str):
            raise ComputerError("电脑回复缺少图像编码。")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ComputerError("电脑回复包含无效base64。") from error
        return ComputerFrame.model_validate({**result, "data": data})

    async def close(self) -> None:
        """已取消观察不会留下采集进程，所有调用加入实际收尾。"""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="computer-driver-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        """完整Job退出后才收回管道及句柄，不能以主解释器退出代替进程树退出。"""
        if self._start_task is not None:
            await asyncio.gather(self._start_task, return_exceptions=True)
        await self._release()

    async def _release(self) -> None:
        """启动失败与正常关闭共用真实资源回收，不清空仍在运行的引用。"""
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        await process.wait()
        if self._io:
            await asyncio.gather(*tuple(self._io), return_exceptions=True)
        if self._channel is not None:
            self._channel.close()
            self._channel = None
        process.close()
        self._process = None
