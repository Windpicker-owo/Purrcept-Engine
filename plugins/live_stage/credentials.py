"""官方接入凭据的Windows用户保险库边界；导入和配置声明均不读取凭据。"""

from __future__ import annotations

import ctypes
import json
import sys
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Protocol, cast

from purrcept_engine.values import decode_object


class CredentialError(ValueError):
    """仅携带固定错误码的本机凭据错误，不保留原始秘密或系统错误正文。"""


@dataclass(frozen=True, slots=True)
class OpenLiveCredentials:
    """本机运行时密钥；repr排除全部字段，调用者不得序列化至配置或日志。"""

    access_key_id: str = field(repr=False)
    access_key_secret: str = field(repr=False)
    anchor_code: str = field(repr=False)

    def __post_init__(self) -> None:
        if any(
            not value or len(value) > 512 or not value.isascii() or any(c.isspace() for c in value)
            for value in (self.access_key_id, self.access_key_secret, self.anchor_code)
        ):
            raise CredentialError("credential_format_invalid")


def credential_target(app_id: int) -> str:
    """返回一个项目专用的Windows通用凭据目标名。

    Args:
        app_id: 正整数项目ID，不是房间号。

    Returns:
        用户保险库中的精确逻辑条目名称。

    Raises:
        CredentialError: 项目ID无效。
    """
    if type(app_id) is not int or not 0 < app_id < 2**63:
        raise CredentialError("app_id_invalid")
    return f"Purrcept/live_stage/bilibili-open/{app_id}"


class CredentialStore(Protocol):
    """仅处理显式项目条目，不枚举用户保险库。"""

    def read(self, app_id: int) -> OpenLiveCredentials:
        """读取所选项目；缺失时抛CredentialError。"""
        ...

    def write(self, app_id: int, value: OpenLiveCredentials) -> None:
        """保存用户明确提交的字段；失败抛CredentialError。"""
        ...

    def delete(self, app_id: int) -> None:
        """删除所选项目本地条目，不代表平台侧撤销。"""
        ...


class WinCredentialRecord(ctypes.Structure):
    """Windows CREDENTIALW 的原生布局，供受控FFI及替身共同校验。"""

    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class _Read(Protocol):
    def __call__(self, target: str, kind: int, flags: int, result: object) -> int: ...


class _Write(Protocol):
    def __call__(self, value: object, flags: int) -> int: ...


class _Delete(Protocol):
    def __call__(self, target: str, kind: int, flags: int) -> int: ...


class _Free(Protocol):
    def __call__(self, value: object) -> None: ...


class WindowsCredentialStore:
    """按需调用CredRead/Write/Delete；LOCAL_MACHINE持久性仍属于当前Windows用户。"""

    def __init__(self) -> None:
        """建立无I/O的保险库适配器；构造不访问任何已有条目。"""

    @staticmethod
    def _library() -> ctypes.WinDLL:
        if sys.platform != "win32":
            raise CredentialError("windows_required")
        library = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        library.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(WinCredentialRecord)),
        ]
        library.CredReadW.restype = wintypes.BOOL
        library.CredWriteW.argtypes = [ctypes.POINTER(WinCredentialRecord), wintypes.DWORD]
        library.CredWriteW.restype = wintypes.BOOL
        library.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        library.CredDeleteW.restype = wintypes.BOOL
        library.CredFree.argtypes = [ctypes.c_void_p]
        library.CredFree.restype = None
        return library

    def read(self, app_id: int) -> OpenLiveCredentials:
        """读取一个已授权项目条目；原始缓冲在复制后清零并归还。

        Args:
            app_id: 明确启用的官方项目。

        Returns:
            仅供运行时使用的密钥对象。

        Raises:
            CredentialError: 缺失、平台不支持、读取失败或格式损坏。
        """
        target = credential_target(app_id)
        library = self._library()
        value = ctypes.POINTER(WinCredentialRecord)()
        if not cast(_Read, library.CredReadW)(target, 1, 0, ctypes.byref(value)):
            code = (
                "credential_missing"
                if ctypes.get_last_error() == 1168
                else "credential_read_failed"
            )
            raise CredentialError(code)
        try:
            size = int(value.contents.CredentialBlobSize)
            if not 0 < size <= 2560:
                raise CredentialError("credential_format_invalid")
            raw = ctypes.string_at(value.contents.CredentialBlob, size)
        finally:
            if value and value.contents.CredentialBlobSize:
                ctypes.memset(value.contents.CredentialBlob, 0, value.contents.CredentialBlobSize)
            cast(_Free, library.CredFree)(value)
        try:
            data = decode_object(raw)
            key_id, secret, code = (
                data.get(k) for k in ("access_key_id", "access_key_secret", "anchor_code")
            )
            if not all(isinstance(v, str) for v in (key_id, secret, code)):
                raise CredentialError("credential_format_invalid")
            assert isinstance(key_id, str) and isinstance(secret, str) and isinstance(code, str)
            return OpenLiveCredentials(key_id, secret, code)
        except ValueError:
            raise CredentialError("credential_format_invalid") from None

    def write(self, app_id: int, value: OpenLiveCredentials) -> None:
        """保存用户本地提交的密钥，不产生明文文件。

        Args:
            app_id: 项目ID；仅覆盖此精确条目。
            value: 用户已授权保存的字段。

        Raises:
            CredentialError: 保存失败或超出Windows通用凭据大小限制。
        """
        target = credential_target(app_id)
        raw = json.dumps(
            {
                "access_key_id": value.access_key_id,
                "access_key_secret": value.access_key_secret,
                "anchor_code": value.anchor_code,
            },
            separators=(",", ":"),
        ).encode()
        if len(raw) > 2560:
            raise CredentialError("credential_too_large")
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        record = WinCredentialRecord()
        record.Type, record.TargetName, record.Persist = 1, target, 2
        record.UserName = "Purrcept OpenLive"
        record.CredentialBlobSize, record.CredentialBlob = len(raw), blob
        try:
            if not cast(_Write, self._library().CredWriteW)(ctypes.byref(record), 0):
                raise CredentialError("credential_write_failed")
        finally:
            ctypes.memset(blob, 0, len(raw))

    def delete(self, app_id: int) -> None:
        """删除此项目的本地保存；缺失视为已删除，其他失败明确报告。

        Args:
            app_id: 用户指定停用的项目。

        Raises:
            CredentialError: Windows拒绝删除。
        """
        if not cast(_Delete, self._library().CredDeleteW)(credential_target(app_id), 1, 0):
            if ctypes.get_last_error() != 1168:
                raise CredentialError("credential_delete_failed")
