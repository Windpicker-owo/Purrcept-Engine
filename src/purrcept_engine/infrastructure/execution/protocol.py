"""以有界 UTF-8 JSON 帧连接宿主和标准库 Worker。

每帧先发送四字节网络序长度，再发送 JSON 对象；接收者在读取正文前检查长度，
不使用 pickle 或猜测对象表示。这里拥有编码、帧完整性和 FD 生命周期，行动
身份、调用序号及消息种类由 execution 上层核验，不建立另一套 RPC 调度。

FrameChannel 独占两个管道端。发送锁覆盖整帧的全部部分写入；读取由一个专属
接收线程调用。所有者结束实际接收后关闭 FD，不能以取消异步等待冒充线程已退出。
"""

import os
import struct
import threading
from collections.abc import Mapping
from typing import TypeGuard

from purrcept_engine.values import (
    ActionProtocolError,
    JsonObject,
    JsonValue,
    decode_object,
    encode_json,
)

_HEADER = struct.Struct("!I")
_MAX_FRAME_LENGTH = (1 << 32) - 1


def _validate_fd(fd: int) -> None:
    if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0:
        raise ActionProtocolError("管道 FD 必须是非负整数。", code="invalid_channel")


def _validate_limit(max_bytes: int) -> None:
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 0 < max_bytes <= _MAX_FRAME_LENGTH
    ):
        raise ActionProtocolError("帧上限必须是四字节长度可表示的正整数。", code="invalid_channel")


def _read_exact(fd: int, count: int, *, code: str, clean_eof: bool = False) -> bytes:
    """拼接部分读取；仅在尚未开始下一帧时将 EOF 视为正常连接结束。"""
    result = bytearray()
    while len(result) < count:
        try:
            chunk = os.read(fd, count - len(result))
        except OSError as error:
            raise ActionProtocolError("管道读取失败。", code="io_error") from error
        if not chunk:
            if clean_eof and not result:
                raise EOFError("行动管道已正常结束。")
            raise ActionProtocolError("行动帧在声明长度前结束。", code=code)
        result.extend(chunk)
    return bytes(result)


def _is_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


def read_frame(fd: int, *, max_bytes: int) -> JsonObject:
    """读取一帧，在读取或分配正文前拒绝超限长度。

    完整帧之间的 EOF 抛出 EOFError；长度头或正文截断抛出不同 code 的
    ActionProtocolError。调用者须保证该 FD 同时只有一个读取者。
    """
    _validate_fd(fd)
    _validate_limit(max_bytes)
    header = _read_exact(fd, _HEADER.size, code="truncated_header", clean_eof=True)
    length: int = _HEADER.unpack(header)[0]
    if length > max_bytes:
        raise ActionProtocolError("行动帧超过接收上限。", code="frame_too_large")
    payload = _read_exact(fd, length, code="truncated_payload")
    return decode_object(payload)


def _write_all(fd: int, data: bytes) -> None:
    """逐次推进部分写入，用 memoryview 避免反复复制剩余帧。"""
    remaining = memoryview(data)
    while remaining:
        try:
            written = os.write(fd, remaining)
        except OSError as error:
            raise ActionProtocolError("管道写入失败。", code="io_error") from error
        if written <= 0:
            raise ActionProtocolError("管道写入没有推进。", code="io_error")
        remaining = remaining[written:]


def write_frame(fd: int, payload: object, *, max_bytes: int) -> None:
    """核验并发送整帧；超限在写入长度头前拒绝。

    顶层必须是对象，发送方也执行接收方的 JSON 值约束。此函数不拥有 FD
    或并发锁；多个发送者共用 FD 时应使用 FrameChannel。
    """
    _validate_fd(fd)
    _validate_limit(max_bytes)
    if not _is_mapping(payload):
        raise ActionProtocolError("帧正文必须是 JSON 对象。")
    data = encode_json(payload)
    if len(data) > max_bytes:
        raise ActionProtocolError("行动帧超过发送上限。", code="frame_too_large")
    _write_all(fd, _HEADER.pack(len(data)) + data)


class FrameChannel:
    """独占一对 FD，并保持整帧发送及单读取者约束。

    execution 装配后移交 FD 所有权；send 可由多个能力回传线程调用，read 由
    专属接收线程使用。close 必须发生在实际接收结束之后；重复关闭不会再次
    处置可能已被操作系统复用的 FD。这里不拥有接收线程或行动取消策略。
    """

    def __init__(self, read_fd: int, write_fd: int, max_bytes: int) -> None:
        """接管已有管道端，构造失败时 FD 仍归调用者清理。"""
        _validate_fd(read_fd)
        _validate_fd(write_fd)
        _validate_limit(max_bytes)
        self._read_fd = read_fd
        self._write_fd = write_fd
        self._max_bytes = max_bytes
        self._send_lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._closed = False
        self._close_error: ActionProtocolError | None = None

    def send(self, payload: object) -> None:
        """进入发送锁后编码并发送整帧，关闭不会交错使用写 FD。

        调用者在本次发送返回前不得并发修改载荷。
        """
        with self._send_lock:
            if self._closed:
                raise ActionProtocolError("行动通道已关闭。", code="channel_closed")
            write_frame(self._write_fd, payload, max_bytes=self._max_bytes)

    def read(self) -> JsonObject:
        """取得完整下一帧；并发读取明确拒绝，EOF 沿用 read_frame 契约。"""
        if not self._read_lock.acquire(blocking=False):
            raise ActionProtocolError("行动通道已有实际读取者。", code="active_reader")
        try:
            if self._closed:
                raise ActionProtocolError("行动通道已关闭。", code="channel_closed")
            return read_frame(self._read_fd, max_bytes=self._max_bytes)
        finally:
            self._read_lock.release()

    def close(self) -> None:
        """实际读取结束后关闭两个端点；失败仍尝试释放另一个端点。

        不等待或强行中断读取线程。重复调用不再次关闭 FD；错误通过异常链
        保留，所有者不能把异常当成资源已成功释放。
        """
        if not self._read_lock.acquire(blocking=False):
            raise ActionProtocolError("实际接收尚未结束，不能关闭 FD。", code="active_reader")
        try:
            with self._send_lock:
                if self._closed:
                    if self._close_error is not None:
                        raise self._close_error
                    return
                self._closed = True
                errors: list[OSError] = []
                for fd in dict.fromkeys((self._read_fd, self._write_fd)):
                    try:
                        os.close(fd)
                    except OSError as error:
                        errors.append(error)
                if errors:
                    self._close_error = ActionProtocolError(
                        "行动通道资源释放失败。", code="close_failed"
                    )
                    self._close_error.__cause__ = ExceptionGroup("管道端点关闭失败。", errors)
                    raise self._close_error
        finally:
            self._read_lock.release()


__all__ = [
    "ActionProtocolError",
    "FrameChannel",
    "JsonObject",
    "JsonValue",
    "decode_object",
    "encode_json",
    "read_frame",
    "write_frame",
]
