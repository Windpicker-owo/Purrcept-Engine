"""CLI 的可取消标准输入接入，EOF 只停止此通道，不关闭持续主体。

Windows 终端按已有键盘字符读取；重定向管道先查询可读字节再短读，CLI
独占标准输入，没有另一个读取线程竞争同一句柄。等待发生在事件循环中，
关闭不留下卡在 readline 的后台线程。读取器借用标准流，不关闭调用者的
文件句柄；正式输入仍经过宿主原子接管和真实处理查询。
"""

from __future__ import annotations

import asyncio
import codecs
import ctypes
import logging
import msvcrt
import os
from datetime import UTC, datetime
from typing import Protocol, TextIO
from uuid import uuid4

from purrcept_engine.domain.inputs.models import InputContext, InputSource, InputSubmission
from purrcept_engine.errors import ConfigError, StorageError
from purrcept_engine.plugin_system.contracts import InputPort

_logger = logging.getLogger(__name__)


class LineReader(Protocol):
    """CLI 借用的可取消单行入口；None 表示 EOF，不代表宿主结束。"""

    async def read_line(self) -> str | None:
        """返回完整一行或 EOF；取消必须结束等待而不留下线程借用。"""
        ...


class _FileType(Protocol):
    """GetFileType 的指针宽度与返回值，不用默认 ctypes int 截断句柄。"""

    argtypes: tuple[object, ...]
    restype: object

    def __call__(self, handle: int) -> int: ...


class _Peek(Protocol):
    """PeekNamedPipe 的无消费查询，available 按 DWORD 写入。"""

    argtypes: tuple[object, ...]
    restype: object

    def __call__(
        self, handle: int, buffer: None, size: int, read: None, available: object, remaining: None
    ) -> int: ...


class _InputApi(ctypes.WinDLL):
    """仅绑定借用标准输入的两项 Win32 API，不取得另一个设备或句柄。"""

    GetFileType: _FileType
    PeekNamedPipe: _Peek

    def __init__(self) -> None:
        """固定真实调用 ABI；没有等待或另一个标准输入句柄所有者。"""
        super().__init__("kernel32", use_last_error=True)
        self.GetFileType.argtypes = (ctypes.c_void_p,)
        self.GetFileType.restype = ctypes.c_uint32
        self.PeekNamedPipe.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self.PeekNamedPipe.restype = ctypes.c_int


class WindowsLineReader:
    """CLI 创建并独占读取的 Windows 标准流；不更改流的编码或控制台模式。

    max_bytes 限制尚未正式接管的一行 UTF-8 正文，长行明确失败。管道必须
    由本读取器独占，已有 TextIO 缓存和并行读取不满足此入口契约。终端不
    启用自造命令或退出关键字；Ctrl+C 的处置仍属于 CLI 信号处理器。
    """

    def __init__(self, stream: TextIO, *, max_bytes: int) -> None:
        """借用流，在构造边界确认文件/管道/终端类型，不等待用户输入。"""
        if isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("控制台行额度必须为正整数。")
        self._fd = stream.fileno()
        self._handle = msvcrt.get_osfhandle(self._fd)
        self._api = _InputApi()
        self._kind = self._api.GetFileType(self._handle)
        if self._kind not in (1, 2, 3):
            raise ConfigError("标准输入必须是 Windows 文件、管道或终端。")
        if self._kind == 2 and not stream.isatty():
            raise ConfigError("标准字符设备不是可读取的交互终端。")
        self._decoder = codecs.getincrementaldecoder(stream.encoding or "utf-8")("strict")
        self._max_bytes = max_bytes
        self._buffer = ""
        self._eof = False

    async def read_line(self) -> str | None:
        """在真实可读边界短读，组装完整行；等待和取消不创建阻塞线程。"""
        if self._kind == 2:
            return await self._terminal_line()
        pieces: list[str] = []
        size = 0
        while True:
            newline = self._buffer.find("\n")
            if newline >= 0:
                piece, self._buffer = self._buffer[:newline], self._buffer[newline + 1 :]
                pieces.append(piece)
                self._check_size(size + len(piece.encode("utf-8")))
                return "".join(pieces).removesuffix("\r")
            if self._buffer:
                size += len(self._buffer.encode("utf-8"))
                self._check_size(size)
                pieces.append(self._buffer)
                self._buffer = ""
            if self._eof:
                return "".join(pieces) if pieces else None
            available = 4096
            if self._kind == 3:
                count = ctypes.c_uint32()
                if not self._api.PeekNamedPipe(
                    self._handle, None, 0, None, ctypes.byref(count), None
                ):
                    error = ctypes.get_last_error()
                    if error != 109:  # ERROR_BROKEN_PIPE 确认 EOF，其他原生错误原样失败。
                        raise ctypes.WinError(error)
                    self._eof = True
                    self._buffer = self._decoder.decode(b"", final=True)
                    continue
                available = count.value
            if available == 0:
                await asyncio.sleep(0.02)
                continue
            data = os.read(self._fd, min(available, 4096))
            self._eof = not data
            self._buffer = self._decoder.decode(data, final=self._eof)

    async def _terminal_line(self) -> str | None:
        """沿 Windows 宽字符键盘输入回显，保留 Unicode、退格和 EOF。

        getwch 只在 kbhit 后调用；特殊键的第二字节也等到可读后消费。UTF-16
        代理对先合为完整字符，再进入 UTF-8 输入额度，不把孤立代理项入账。
        """
        if self._eof:
            return None
        characters: list[str] = []
        size = 0
        special = False
        high: str | None = None
        while True:
            if not msvcrt.kbhit():
                await asyncio.sleep(0.02)
                continue
            character = msvcrt.getwch()
            if special:
                special = False
                continue
            if character in ("\x00", "\xe0"):
                special = True
                continue
            if character == "\x1a":
                if high is not None:
                    raise UnicodeError("标准输入在 UTF-16 字符完成前结束。")
                self._eof = True
                return "".join(characters) if characters else None
            if character == "\r":
                if high is not None:
                    raise UnicodeError("标准输入含未完成 UTF-16 字符。")
                msvcrt.putwch("\r")
                msvcrt.putwch("\n")
                return "".join(characters)
            if character == "\b":
                if characters:
                    size -= len(characters.pop().encode("utf-8"))
                    for value in ("\b", " ", "\b"):
                        msvcrt.putwch(value)
                continue
            msvcrt.putwch(character)
            if 0xD800 <= ord(character) <= 0xDBFF:
                if high is not None:
                    raise UnicodeError("标准输入含未完成 UTF-16 字符。")
                high = character
                continue
            if high is not None:
                if not 0xDC00 <= ord(character) <= 0xDFFF:
                    raise UnicodeError("标准输入含无效 UTF-16 字符。")
                character = chr(0x10000 + ((ord(high) - 0xD800) << 10) + ord(character) - 0xDC00)
                high = None
            size += len(character.encode("utf-8"))
            self._check_size(size)
            characters.append(character)

    def _check_size(self, size: int) -> None:
        if size > self._max_bytes:
            raise StorageError("控制台原文超过单条输入额度。", code="input_capacity")


class ConsoleIngress:
    """CLI 拥有的单个标准输入工作器，与持续宿主生命周期分别结束。

    EOF 结束输入循环；fatal 保留真实异常供 CLI 收尾。额度拒收明确显示且
    没有隐藏重试队列。正式接管的原生事件使用本次接入 UUID，不把文本或
    显示用户名当权限或去重身份。
    """

    def __init__(self, inputs: InputPort, reader: LineReader) -> None:
        """借用已运行宿主和可取消读取器，构造不开始读取。"""
        self.inputs, self.reader = inputs, reader
        self._task: asyncio.Task[None] | None = None
        self._failed = asyncio.Event()
        self._failure: BaseException | None = None

    def start(self) -> None:
        """开始唯一接入任务，重复调用不增加读取者。"""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="console-input")

    async def _run(self) -> None:
        try:
            while True:
                try:
                    line = await self.reader.read_line()
                except (OSError, UnicodeError) as error:
                    self._failure = error
                    _logger.error("控制台输入通道已停止：%s", error)
                    return
                except StorageError as error:
                    if error.code != "input_capacity":
                        raise
                    self._failure = error
                    _logger.error("控制台输入通道已停止：%s", error)
                    return
                if line is None:
                    return
                if not line.strip():
                    continue
                now = datetime.now(UTC)
                try:
                    await self.inputs.submit(
                        InputSubmission(
                            InputSource("console", "local", "console", "text", uuid4().hex),
                            line,
                            now,
                            now,
                            context=InputContext(minimum_wake=True),
                        )
                    )
                except StorageError as error:
                    if error.code not in ("input_capacity", "queue_capacity"):
                        raise
                    _logger.warning("控制台输入未接管：%s", error)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            # 所有权边界记录原故障并唤醒 CLI，没有空成功或内部错误转换。
            self._failure = error
            self._failed.set()

    async def wait_failed(self) -> None:
        """EOF 后继续等待宿主；只有真实输入故障结束此等待并传播原异常。"""
        await self._failed.wait()
        assert self._failure is not None
        raise self._failure

    async def close(self) -> None:
        """取消轮询读取并收回同一个任务，不关闭标准流或持续宿主。"""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
