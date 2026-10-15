"""以内容版本约束 Notebook 文件修改，以宿主不可变证据提供分页读取。

工厂只产生纯声明，build 后才创建目录与原有示例。文件 I/O 统一在有所有者的
to_thread 任务中执行；同一 Notebook 串行修改，外部编辑通过稳定读取与最终
版本核验发现。临时文件发布是原子替换，但版本比较与替换之间仍可能被外部
编辑器原子替换，不能把它宣称为跨编辑器 CAS。移动/删除有同样的最终核验窗口。
ReplaceFileW 或成功后的备份清理失败时保留恢复文件和观测状态，回执为 unknown；
恢复须核对实际文件版本，不能把代码回退当作文件自动回滚。

技能目录在 prepare_request 异步阶段重新扫描，仅交名称、适用说明与读取入口；
正文不进入固定能力前缀。首读完整保存 EvidenceSnapshot，续读只通过宿主游标，
没有插件分页缓存。原始读取与模型实际交付由宿主分别记录。
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import msvcrt
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Generator, Iterator
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import BinaryIO, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from purrcept_engine.domain.execution.catalog import CapabilityDeclaration, ObjectDeclaration
from purrcept_engine.domain.execution.providers import CallContext, CallOutcome, FunctionCapability
from purrcept_engine.domain.knowledge.models import EvidencePage, EvidenceSnapshot
from purrcept_engine.errors import EngineError, HostStateError
from purrcept_engine.plugin_system.contracts import (
    EvidencePort,
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
    typed_factory,
)
from purrcept_engine.plugins.skills import ParsedNote, parse_note
from purrcept_engine.values import JsonObject, JsonValue, decode_object, encode_json

_SUFFIXES = frozenset({".md", ".markdown"})
_INVALID_SEGMENT = re.compile(r'[\x00-\x1f<>:"|?*]')
_DEVICE_NAME = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.IGNORECASE)
_HOW_TO_REST = """---
name: how-to-rest
description: 想睡觉、打盹或长时间休息时查看。到点不会自动醒来。
---

# 休息

1. 调用 `me.bed.rest(expected_duration_minutes=...)` 躺下。已经在休息时再调用只更新打算时长。
2. 时间到了也不会自动醒来。休息够了调用 `me.bed.leave()`。
3. 醒着时 `leave` 没有副作用。
"""
_JOURNAL = """# 笔记本

这里是普通笔记，没有 when-to-use description，因此不会出现在每轮技能目录里。
可以用来记下今天发生的事、未完成的想法，或整理后的草稿。
"""


class NotebookError(EngineError):
    """文件、路径或版本的明确业务拒绝；权威存储错误不转成此类型。"""

    def __init__(self, message: str, *, code: str) -> None:
        """保留冲突及扫描问题类别，调用者可重新读取当前实际版本。"""
        super().__init__(message)
        self.code = code


class NotebookPublicationError(EngineError):
    """原生发布已开始后的不确定结果，携带实际恢复路径而不承诺未发生效果。"""

    def __init__(self, native_error: OSError, recovery: JsonObject) -> None:
        """保留原 WinError 与本次观测；调用者先核对文件再决定手工恢复。"""
        super().__init__(str(native_error) + "；发布结果须核对实际文件，恢复位置见回执。")
        self.native_error = native_error
        self.recovery = recovery


class _Function(Protocol):
    argtypes: tuple[object, ...]
    restype: object


class _CreateFile(_Function, Protocol):
    def __call__(
        self,
        path: str,
        access: int,
        sharing: int,
        security: None,
        disposition: int,
        flags: int,
        template: None,
    ) -> int: ...


class _FinalPath(_Function, Protocol):
    def __call__(self, handle: int, buffer: object, size: int, flags: int) -> int: ...


class _CloseHandle(_Function, Protocol):
    def __call__(self, handle: int) -> int: ...


class _ReplaceFile(_Function, Protocol):
    def __call__(
        self, target: str, staged: str, backup: str, flags: int, exclude: None, reserved: None
    ) -> int: ...


class _Kernel32(ctypes.WinDLL):
    """显式声明 64 位文件句柄接口；不通过默认 c_int 截断句柄。"""

    CreateFileW: _CreateFile
    GetFinalPathNameByHandleW: _FinalPath
    CloseHandle: _CloseHandle
    ReplaceFileW: _ReplaceFile


@cache
def _file_api() -> _Kernel32:
    """首次文件操作绑定本机接口，后续复用同一 DLL，避免反复增加加载引用。"""
    api = _Kernel32("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    api.CreateFileW.restype = wintypes.HANDLE
    api.GetFinalPathNameByHandleW.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    api.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.CloseHandle.restype = wintypes.BOOL
    api.ReplaceFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    )
    api.ReplaceFileW.restype = wintypes.BOOL
    return api


def _parts(raw: str) -> tuple[str, ...]:
    """保留相对路径习惯，拒绝逃逸、Windows 设备名和备用数据流。"""
    text = raw.strip().replace("\\", "/")
    if not text or text in (".", "./"):
        return ()
    if text.startswith(("/", "~")) or len(text) > 512:
        raise NotebookError("笔记路径须为根目录内的有限相对路径。", code="invalid_path")
    parts = tuple(part for part in text.split("/") if part not in ("", "."))
    if any(
        part == ".."
        or _INVALID_SEGMENT.search(part)
        or _DEVICE_NAME.match(part)
        or part.endswith((".", " "))
        or len(part) > 128
        for part in parts
    ):
        raise NotebookError("笔记路径包含无效、保留或上级目录片段。", code="invalid_path")
    return parts


def _version(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class _Scan:
    kind: Literal["dir", "file"]
    entries: tuple[JsonValue, ...]
    skills: tuple[JsonObject, ...]
    issues: tuple[JsonObject, ...]


class _Files:
    """同步磁盘边界，仅由 Notebook 所有的工作线程调用，不持有持久句柄。"""

    def __init__(self, root: Path, *, max_bytes: int, max_entries: int, max_depth: int) -> None:
        self.root = root
        self.max_bytes, self.max_entries, self.max_depth = max_bytes, max_entries, max_depth

    def start(self, seed_examples: bool) -> None:
        """固定实际根目录，示例仅在目标不存在时发布，不覆盖外部笔记。"""
        if os.name != "nt":
            raise RuntimeError("Notebook 文件发布路径要求 Windows。")
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve(strict=True)
        if seed_examples:
            for path, content in (("how-to-rest.md", _HOW_TO_REST), ("journal.md", _JOURNAL)):
                if not (self.root / path).exists():
                    self.write(path, content, None)

    def target(self, raw: str, *, note: bool = False) -> Path:
        """解析后的真实目标必须留在固定根内；缺失叶节点同样检查其真实父链。"""
        parts = _parts(raw)
        if note and (not parts or Path(parts[-1]).suffix.lower() not in _SUFFIXES):
            raise NotebookError("正文路径必须是 Markdown 文件。", code="invalid_path")
        if self.root.is_symlink() or self.root.is_junction():
            raise NotebookError("运行中的笔记根目录已被链接替换。", code="path_escape")
        target = self.root.joinpath(*parts).resolve(strict=False)
        if not target.is_relative_to(self.root):
            raise NotebookError("笔记路径或链接逃出根目录。", code="path_escape")
        return target

    def relative(self, target: Path) -> str:
        text = target.relative_to(self.root).as_posix()
        return "" if text == "." else text

    def _final_path(self, api: _Kernel32, fd: int) -> Path:
        """按实际打开的对象核边界，不能只依赖打开前的一次 resolve。"""
        handle = msvcrt.get_osfhandle(fd)
        size = api.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not size:
            raise ctypes.WinError(ctypes.get_last_error())
        if size > 32768:
            raise NotebookError("实际文件路径超过平台支持范围。", code="invalid_path")
        buffer = ctypes.create_unicode_buffer(size)
        written = api.GetFinalPathNameByHandleW(handle, buffer, size, 0)
        if not written:
            raise ctypes.WinError(ctypes.get_last_error())
        if written >= size:
            raise NotebookError("打开的文件在路径核验期间被移动。", code="version_conflict")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        actual = Path(os.path.normpath(value))
        if not actual.is_relative_to(self.root):
            raise NotebookError("实际打开的文件不在笔记根内。", code="path_escape")
        return actual

    @contextmanager
    def opened(self, target: Path) -> Generator[BinaryIO]:
        """共享读写/删除，保留同一源句柄核验；不独占所有外部编辑器。

        FILE_SHARE_DELETE 允许 ReplaceFileW 替换时持有源句柄；它也允许外部替换，
        因而最终版本比较和 replace 之间仍不是跨编辑器原子 CAS。
        """
        api = _file_api()
        handle = api.CreateFileW(str(target), 0x80000000, 7, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except OSError as error:
            if not api.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error()) from error
            raise
        with os.fdopen(fd, "rb") as source:
            self._final_path(api, source.fileno())
            yield source

    def stable(self, target: Path, source: BinaryIO) -> bytes:
        """两次有限读取须一致，stat 与当前路径仍指向同一源对象才采用正文。"""
        before = os.fstat(source.fileno())
        source.seek(0)
        first = source.read(self.max_bytes + 1)
        source.seek(0)
        second = source.read(self.max_bytes + 1)
        after = os.fstat(source.fileno())
        if len(first) > self.max_bytes or len(second) > self.max_bytes:
            raise NotebookError("笔记正文超过配置字节上限。", code="file_too_large")
        if (
            first != second
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or not os.path.samestat(after, target.stat())
        ):
            raise NotebookError("笔记在稳定读取期间发生变化，请重新读取。", code="version_conflict")
        return first

    def read_target(self, target: Path) -> ParsedNote:
        """取得同一源句柄的稳定内容与真实哈希，不把坏文件当作不存在。"""
        try:
            with self.opened(target) as source:
                data = self.stable(target, source)
            content = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise NotebookError("笔记不是有效 UTF-8。", code="invalid_encoding") from error
        except OSError as error:
            raise NotebookError(
                f"笔记文件不能读取：{self.relative(target)}。", code="file_unreadable"
            ) from error
        return parse_note(
            self.relative(target),
            content,
            version=_version(data),
            byte_count=len(data),
            occurred_at=time.time(),
        )

    def read(self, raw: str) -> ParsedNote:
        """原路径优先，单段 name/stem 别名只在完整扫描且唯一时解析。"""
        target = self.target(raw)
        if len(_parts(raw)) == 1 and (
            not target.is_file() or target.suffix.lower() not in _SUFFIXES
        ):
            scan = self.scan("")
            if scan.issues:
                raise NotebookError(
                    "目录不完整，不能确认别名的唯一目标。", code="catalog_incomplete"
                )
            matches = [
                entry["path"]
                for entry in self._flat(scan.entries)
                if entry.get("type") == "file"
                and (
                    entry.get("name") == raw.strip() or Path(str(entry["path"])).stem == raw.strip()
                )
            ]
            if len(matches) == 1 and isinstance(matches[0], str):
                target = self.target(matches[0], note=True)
        if target.suffix.lower() not in _SUFFIXES or not target.is_file():
            raise NotebookError("指定笔记不存在或不是 Markdown 文件。", code="file_missing")
        return self.read_target(target)

    def _flat(self, entries: tuple[JsonValue, ...] | list[JsonValue]) -> Iterator[JsonObject]:
        for entry in entries:
            if isinstance(entry, dict):
                yield entry
                children = entry.get("entries")
                if isinstance(children, list):
                    yield from self._flat(children)

    def scan(self, raw: str) -> _Scan:
        """全扫描有总条数/深度/单文件预算，任何缺口明确写入 issues。"""
        target = self.target(raw)
        issues: list[JsonObject] = []
        skills: list[JsonObject] = []
        count = 0

        def file_entry(path: Path) -> JsonObject:
            rel = self.relative(path)
            try:
                note = self.read_target(self.target(rel, note=True))
            except NotebookError as error:
                issues.append({"path": rel, "code": error.code, "error": str(error)})
                return {"path": rel, "type": "file", "unavailable": True}
            if note.skill:
                skills.append(note.catalog_entry("me.notebook.read"))
            return {
                "path": rel,
                "type": "file",
                "name": note.name,
                "skill": note.skill,
                "description": note.description,
                "version": note.version,
                "bytes": note.byte_count,
            }

        def walk(directory: Path, depth: int) -> list[JsonValue]:
            nonlocal count
            rel = self.relative(directory)
            if depth >= self.max_depth:
                issues.append(
                    {"path": rel, "code": "scan_depth", "error": "目录扫描达到深度上限。"}
                )
                return []
            entries: list[JsonValue] = []
            try:
                with os.scandir(directory) as iterator:
                    names: list[str] = []
                    for item in iterator:
                        if count >= self.max_entries:
                            issues.append(
                                {
                                    "path": rel,
                                    "code": "scan_entries",
                                    "error": "目录扫描达到总条数上限。",
                                }
                            )
                            break
                        count += 1
                        names.append(item.name)
                for name in sorted(names, key=str.casefold):
                    child = directory / name
                    child_rel = self.relative(child)
                    try:
                        actual = self.target(child_rel)
                        if child.is_dir():
                            if child.is_symlink() or child.is_junction():
                                issues.append(
                                    {
                                        "path": child_rel,
                                        "code": "linked_directory",
                                        "error": "目录链接不递归扫描。",
                                    }
                                )
                            else:
                                entries.append(
                                    {
                                        "path": child_rel,
                                        "type": "dir",
                                        "entries": walk(actual, depth + 1),
                                    }
                                )
                        elif child.suffix.lower() in _SUFFIXES:
                            entries.append(file_entry(child))
                    except NotebookError as error:
                        issues.append({"path": child_rel, "code": error.code, "error": str(error)})
            except OSError as error:
                issues.append({"path": rel, "code": "directory_unreadable", "error": str(error)})
            return entries

        if target.is_file() and target.suffix.lower() in _SUFFIXES:
            entries: list[JsonValue] = [file_entry(target)]
            kind: Literal["dir", "file"] = "file"
        elif target.is_dir():
            entries = walk(target, 0)
            kind = "dir"
        else:
            raise NotebookError("目录目标不存在。", code="file_missing")
        return _Scan(
            kind,
            tuple(entries),
            tuple(sorted(skills, key=lambda item: str(item["path"]))),
            tuple(issues),
        )

    def _expected(self, target: Path, source: BinaryIO, expected: str | None) -> None:
        """已有文件必须以同一打开对象核正文版本；不自动合并或补造版本。"""
        if expected is None or _version(self.stable(target, source)) != expected:
            raise NotebookError(
                "文件版本已变化或未提供，须重新读取后修改。", code="version_conflict"
            )

    def _observed(self, path: Path) -> JsonObject:
        """发布失败后取得可读取事实；无法稳定核验时明确未知，不回填预期哈希。"""
        observed: JsonObject = {"path": str(path)}
        try:
            path.stat()
        except FileNotFoundError:
            return {**observed, "state": "missing"}
        except OSError as error:
            return {**observed, "state": "unavailable", "error": str(error)}
        try:
            note = self.read_target(path)
        except NotebookError as error:
            return {**observed, "state": "unavailable", "code": error.code, "error": str(error)}
        return {**observed, "state": "stable", "version": note.version, "bytes": note.byte_count}

    def write(self, raw: str, content: str, expected: str | None) -> JsonObject:
        """完整临时文件落盘后发布；新建不覆盖，更新在源句柄仍打开时最终核验。"""
        target = self.target(raw, note=True)
        if target.is_dir():
            raise NotebookError("笔记路径已被目录占用。", code="file_type")
        if target == self.root or not target.parent.is_dir():
            raise NotebookError("笔记父目录不存在。", code="parent_missing")
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError as error:
            raise NotebookError("笔记正文包含无效 UTF-8 字符。", code="invalid_encoding") from error
        if len(data) > self.max_bytes:
            raise NotebookError("笔记正文超过配置字节上限。", code="file_too_large")
        fd, temporary = tempfile.mkstemp(prefix=".purrcept-", suffix=".tmp", dir=target.parent)
        staged = Path(temporary)
        preserve_recovery = False
        try:
            with os.fdopen(fd, "wb") as output:
                self._final_path(_file_api(), output.fileno())
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            if expected is None:
                # Windows rename 在目标存在时失败，不能用 replace 实现新建。
                try:
                    os.rename(staged, target)
                except FileExistsError as error:
                    raise NotebookError(
                        "新建目标已经存在，不能覆盖。", code="version_conflict"
                    ) from error
            else:
                backup = target.parent / (".purrcept-recovery-" + uuid4().hex + ".bak")
                attempted = False
                try:
                    with self.opened(target) as source:
                        self._expected(target, source, expected)
                        # MoveFileExW 在本机不能替换仍打开的目标，不能关闭源后
                        # 静默换用另一套发布。ReplaceFileW 保留同一源核验及属性。
                        # 平台部分失败码不能保证原名仍存在；备份只在成功后删除。
                        api = _file_api()
                        attempted = True
                        if not api.ReplaceFileW(
                            str(target), str(staged), str(backup), 0, None, None
                        ):
                            raise ctypes.WinError(ctypes.get_last_error())
                    backup.unlink()
                except OSError as error:
                    if not attempted:
                        raise
                    preserve_recovery = True
                    recovery: JsonObject = {
                        "code": "publication_unknown",
                        "native_code": error.winerror,
                        "target": self._observed(target),
                        "staged": self._observed(staged),
                        "backup": self._observed(backup),
                    }
                    raise NotebookPublicationError(error, recovery) from error
        finally:
            if not preserve_recovery and staged.exists():
                staged.unlink()
        parsed = parse_note(
            self.relative(target),
            content,
            version=_version(data),
            byte_count=len(data),
            occurred_at=time.time(),
        )
        return {
            "path": parsed.path,
            "name": parsed.name,
            "skill": parsed.skill,
            "description": parsed.description,
            "version": parsed.version,
            "bytes": len(data),
            "created": expected is None,
            "changed": True,
        }

    def mkdir(self, raw: str) -> JsonObject:
        """递归建立分类目录，已有目录没有副作用，不能把根目录当作新建目标。"""
        target = self.target(raw)
        if target == self.root:
            raise NotebookError("不能创建笔记根目录。", code="root_operation")
        created = not target.exists()
        target.mkdir(parents=True, exist_ok=True)
        self.target(raw)
        return {"path": self.relative(target), "created": created, "changed": created}

    def move(self, src: str, dest: str, expected: str | None) -> JsonObject:
        """Windows 重命名不覆盖目标；目录不虚构正文版本，文件先核同一源对象。"""
        source, target = self.target(src), self.target(dest)
        if source == self.root or target == self.root:
            raise NotebookError("不能移动或覆盖笔记根目录。", code="root_operation")
        if not target.parent.is_dir():
            raise NotebookError("移动目标的父目录不存在。", code="parent_missing")
        if source.is_dir():
            if expected is not None:
                raise NotebookError(
                    "目录没有正文版本，不能填写 expected_version。", code="invalid_version"
                )
            os.rename(source, target)
            kind, version = "dir", None
        elif source.is_file() and source.suffix.lower() in _SUFFIXES:
            with self.opened(source) as opened:
                self._expected(source, opened, expected)
                os.rename(source, target)
            kind, version = "file", expected
        else:
            raise NotebookError("移动源不存在或不是笔记/目录。", code="file_missing")
        return {
            "src": self.relative(source),
            "dest": self.relative(target),
            "type": kind,
            "version": version,
            "changed": True,
        }

    def delete(self, raw: str, expected: str | None) -> JsonObject:
        """只删除版本匹配的笔记或空目录，不递归删除根目录或非空内容。"""
        target = self.target(raw)
        if target == self.root:
            raise NotebookError("不能删除笔记根目录。", code="root_operation")
        if target.is_dir():
            if expected is not None:
                raise NotebookError(
                    "目录没有正文版本，不能填写 expected_version。", code="invalid_version"
                )
            target.rmdir()
            kind = "dir"
        elif target.is_file() and target.suffix.lower() in _SUFFIXES:
            with self.opened(target) as source:
                self._expected(target, source, expected)
                target.unlink()
            kind = "file"
        else:
            raise NotebookError("删除目标不存在或不是笔记/目录。", code="file_missing")
        return {"path": self.relative(target), "type": kind, "version": expected, "changed": True}


class Notebook:
    """插件拥有的异步文件边界与只读技能快照，不拥有主脑或分页数据库。

    Host build 创建，所有方法在同一事件循环调用；每项磁盘操作串行交给实际
    工作线程。取消观察仍等待该线程结束再传播取消，close 不提前收回文件任务。
    prepare_request 由逻辑请求异步阶段调用，扫描不会触发唤醒或技能执行。
    """

    def __init__(
        self,
        root: Path,
        evidence: EvidencePort,
        *,
        instance_id: str,
        max_bytes: int = 64 * 1024,
        max_entries: int = 500,
        max_depth: int = 8,
        skills_dir: str = "",
        seed_examples: bool = True,
    ) -> None:
        """绑定明确目录与宿主证据端口，构造不建立目录或扫描文件。"""
        self._files = _Files(
            root, max_bytes=max_bytes, max_entries=max_entries, max_depth=max_depth
        )
        self._evidence = evidence
        self._instance_id = instance_id
        self._skills_dir = skills_dir
        self._seed_examples = seed_examples
        self._lock = asyncio.Lock()
        self._closing = False

    async def _io[Result](self, operation: Callable[[], Result]) -> Result:
        """一个真实线程任务持有锁到结束，不把取消等待当作文件操作已撤销。"""
        async with self._lock:
            if self._closing:
                raise HostStateError("Notebook 已开始关闭。")
            owner = asyncio.create_task(asyncio.to_thread(operation))
            cancelled: asyncio.CancelledError | None = None
            while True:
                try:
                    result = await asyncio.shield(owner)
                    break
                except asyncio.CancelledError as error:
                    if owner.cancelled():
                        raise
                    # 原生文件线程不能被 Task.cancel 撤销。重复观察取消也
                    # 保持串行锁到实际结束，随后原样传播取消，不提前关闭资源。
                    cancelled = error
            if cancelled is not None:
                raise cancelled
            return result

    async def start(self) -> None:
        """创建固定根与原示例，目标存在时始终保留已有内容。"""
        await self._io(lambda: self._files.start(self._seed_examples))

    async def close(self) -> None:
        """停止新的磁盘借用，实际线程结束后才返回，没有持久打开的文件句柄。"""
        self._closing = True
        async with self._lock:
            pass

    async def prepare_request(self) -> JsonObject:
        """每次逻辑请求前重新取得技能目录；正文和普通笔记不进入动态目录。"""
        try:
            scan = await self._io(lambda: self._files.scan(self._skills_dir))
        except NotebookError as error:
            # 目录缺失或逃逸只形成本次不完整材料，不把旧缓存当成新目录。
            scan = _Scan(
                "dir",
                (),
                (),
                ({"path": self._skills_dir, "code": error.code, "error": str(error)},),
            )
        payload: JsonObject = {
            "skills": list(scan.skills),
            "skills_directory": self._skills_dir,
            "incomplete": bool(scan.issues),
            "issues": list(scan.issues),
        }
        return payload

    async def tree(self, path: str = "") -> JsonObject:
        """保留目录层级，不显示正文；缺口明确表示 truncated 与 issues。"""
        scan = await self._io(lambda: self._files.scan(path))
        return {
            "path": "/".join(_parts(path)),
            "type": scan.kind,
            "entries": list(scan.entries),
            "truncated": bool(scan.issues),
            "issues": list(scan.issues),
        }

    async def read(
        self, path: str, *, cursor: str | None = None, max_chars: int | None = None
    ) -> JsonObject:
        """首次取得稳定全文并保存证据；续读不再访问文件或重新解析别名。"""
        requested = "/".join(_parts(path))
        page_chars = self._evidence.page_chars if max_chars is None else max_chars
        if cursor is None:
            note = await self._io(lambda: self._files.read(path))
            source = encode_json(
                {
                    "plugin": self._instance_id,
                    "path": note.path,
                    "requested_path": requested,
                    "file_version": note.version,
                    "name": note.name,
                    "description": note.description,
                    "body_start": note.body_start,
                    "bytes": note.byte_count,
                    "scope": "complete-file",
                    "method": "stable-utf8-read-v1",
                }
            ).decode("utf-8")
            snapshot = EvidenceSnapshot(
                evidence_id="notebook-" + uuid4().hex,
                version=1,
                kind="notebook",
                text=note.content,
                source_json=source,
                occurred_at=note.occurred_at,
            )
            await self._evidence.put_evidence(snapshot)
            page = await self._evidence.read_evidence(snapshot.evidence_id, max_chars=page_chars)
        else:
            page = await self._evidence.read_evidence_cursor(cursor, max_chars=page_chars)
        return self._page(page, requested)

    def _page(self, page: EvidencePage, requested: str) -> JsonObject:
        """核游标来源，并只交付当前片段，旧 body 字段也不能重新带出整篇正文。"""
        snapshot = page.snapshot
        if snapshot is None:
            raise RuntimeError("Notebook 原文页不能用认识记录代替文件快照。")
        source = decode_object(snapshot.source_json.encode("utf-8"))
        if (
            snapshot.kind != "notebook"
            or source.get("plugin") != self._instance_id
            or requested not in (source.get("path"), source.get("requested_path"))
        ):
            raise NotebookError("续读游标不属于同一笔记路径。", code="cursor_binding")
        fragment = page.slice
        body_start = source.get("body_start")
        if fragment is None or isinstance(body_start, bool) or not isinstance(body_start, int):
            raise RuntimeError("Notebook 原文页缺少合法范围或正文起点。")
        body = page.text[max(0, body_start - fragment.start) :] if fragment.end > body_start else ""
        return {
            "path": source["path"],
            "name": source["name"],
            "description": source["description"],
            "skill": source["description"] is not None,
            "version": source["file_version"],
            "bytes": source["bytes"],
            "content": page.text,
            "body": body,
            "source": source,
            "evidence_ref": snapshot.evidence_id,
            "slice_ref": fragment.slice_ref,
            "range": {"unit": fragment.unit, "start": fragment.start, "end": fragment.end},
            "next_cursor": page.next_cursor,
            "complete": page.next_cursor is None,
        }

    async def write(
        self, path: str, content: str, *, expected_version: str | None = None
    ) -> JsonObject:
        """新建要求目标不存在，已有文件核正文哈希，结果直接报告新版本。"""
        return await self._io(lambda: self._files.write(path, content, expected_version))

    async def mkdir(self, path: str) -> JsonObject:
        """创建笔记分类目录，不能改变根目录或覆盖已有文件。"""
        return await self._io(lambda: self._files.mkdir(path))

    async def move(self, src: str, dest: str, *, expected_version: str | None = None) -> JsonObject:
        """文件须核版本，目录不填正文版本；目标不存在才可移动。"""
        return await self._io(lambda: self._files.move(src, dest, expected_version))

    async def delete(self, path: str, *, expected_version: str | None = None) -> JsonObject:
        """只删除已核版本的文件或空目录，不能递归删除根和非空目录。"""
        return await self._io(lambda: self._files.delete(path, expected_version))


class NotebookSettings(BaseModel):
    """显式配置文件根与扫描资源界，技能目录是该根下的相对路径。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    notebook_dir: str = Field(min_length=1)
    skills_dir: str = ""
    max_bytes: int = Field(default=64 * 1024, gt=0)
    max_entries: int = Field(default=500, gt=0)
    max_depth: int = Field(default=8, gt=0)
    seed_examples: bool = True


class _Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _Tree(_Parameters):
    path: str = ""


class _Read(_Parameters):
    path: str
    cursor: str | None = None
    max_chars: int | None = Field(default=None, gt=0)


class _Write(_Parameters):
    path: str
    content: str
    expected_version: str | None = None


class _Mkdir(_Parameters):
    path: str


class _Move(_Parameters):
    src: str
    dest: str
    expected_version: str | None = None


class _Delete(_Parameters):
    path: str
    expected_version: str | None = None


def _declare(instance_id: str, settings: NotebookSettings, base_dir: Path) -> PluginBlueprint:
    """纯声明保留精确模型路径与参数，不创建目录、不读取现有正文。"""
    root = Path(settings.notebook_dir)
    root = Path(os.path.abspath(root if root.is_absolute() else base_dir / root))
    _parts(settings.skills_dir)
    if settings.seed_examples and settings.max_bytes < max(
        len(_HOW_TO_REST.encode("utf-8")),
        len(_JOURNAL.encode("utf-8")),
    ):
        raise ValueError("启用原示例时 max_bytes 必须足以保留其完整正文。")
    object_id = instance_id + ":notebook"

    def declaration[Parameters: BaseModel](
        name: str, parameters: type[Parameters], description: str, example: str, usage: str = ""
    ) -> CapabilityDeclaration[Parameters]:
        return CapabilityDeclaration(
            instance_id + ":notebook." + name,
            object_id,
            name,
            description,
            parameters,
            5.0,
            example,
            usage,
            concurrency="target_serial",
        )

    tree = declaration(
        "tree", _Tree, "查看笔记目录层级、名称和技能说明，不含正文。", "await me.notebook.tree()"
    )
    read = declaration(
        "read",
        _Read,
        "读取指定笔记的稳定版本与原文片段。",
        'await me.notebook.read(path="how-to-rest.md")',
        "path 保留唯一 name/stem 别名；cursor 续读同一已存快照，不重新取文件。",
    )
    write = declaration(
        "write",
        _Write,
        "新建或按已读取版本更新完整 Markdown 笔记。",
        'await me.notebook.write(path="ideas.md", content="# 想法\\n")',
        "已有文件须 expected_version，冲突重新读取；不要同时与外部编辑器写同一路径。",
    )
    mkdir = declaration(
        "mkdir",
        _Mkdir,
        "建立笔记分类目录，已有目录无副作用。",
        'await me.notebook.mkdir(path="habits")',
    )
    move = declaration(
        "move",
        _Move,
        "重命名或移动笔记/目录，不能覆盖目标。",
        'await me.notebook.move(src="draft.md", dest="journal/draft.md", '
        'expected_version="读取的版本")',
    )
    delete = declaration(
        "delete",
        _Delete,
        "删除已核版本笔记或空目录，不能删除根和非空目录。",
        'await me.notebook.delete(path="obsolete.md", expected_version="读取的版本")',
    )

    async def build(ports: PluginPorts) -> PluginRuntime:
        evidence = ports.evidence
        if evidence is None:
            raise RuntimeError("Notebook 必须装配宿主原文端口。")
        notebook = Notebook(
            root,
            evidence,
            instance_id=instance_id,
            max_bytes=settings.max_bytes,
            max_entries=settings.max_entries,
            max_depth=settings.max_depth,
            skills_dir=settings.skills_dir,
            seed_examples=settings.seed_examples,
        )
        await notebook.start()

        async def outcome(operation: Awaitable[JsonObject]) -> CallOutcome:
            """只翻译明确文件业务拒绝；存储和程序错误仍交回真实执行边界。"""
            try:
                return CallOutcome("confirmed", await operation)
            except NotebookPublicationError as error:
                return CallOutcome(
                    "unknown", error.recovery, type(error.native_error).__name__, str(error)
                )
            except NotebookError as error:
                return CallOutcome("failed", {"code": error.code}, type(error).__name__, str(error))

        async def run_tree(parameters: _Tree, context: CallContext) -> CallOutcome:
            del context
            return await outcome(notebook.tree(parameters.path))

        async def run_read(parameters: _Read, context: CallContext) -> CallOutcome:
            del context
            return await outcome(
                notebook.read(
                    parameters.path, cursor=parameters.cursor, max_chars=parameters.max_chars
                )
            )

        async def run_write(parameters: _Write, context: CallContext) -> CallOutcome:
            del context
            return await outcome(
                notebook.write(
                    parameters.path,
                    parameters.content,
                    expected_version=parameters.expected_version,
                )
            )

        async def run_mkdir(parameters: _Mkdir, context: CallContext) -> CallOutcome:
            del context
            return await outcome(notebook.mkdir(parameters.path))

        async def run_move(parameters: _Move, context: CallContext) -> CallOutcome:
            del context
            return await outcome(
                notebook.move(
                    parameters.src, parameters.dest, expected_version=parameters.expected_version
                )
            )

        async def run_delete(parameters: _Delete, context: CallContext) -> CallOutcome:
            del context
            return await outcome(
                notebook.delete(parameters.path, expected_version=parameters.expected_version)
            )

        return PluginRuntime(
            (
                FunctionCapability(tree, run_tree),
                FunctionCapability(read, run_read),
                FunctionCapability(write, run_write),
                FunctionCapability(mkdir, run_mkdir),
                FunctionCapability(move, run_move),
                FunctionCapability(delete, run_delete),
            ),
            close=notebook.close,
            prepare_request=notebook.prepare_request,
        )

    return PluginBlueprint(
        (ObjectDeclaration(object_id, "notebook", "笔记本", "subject"),),
        (tree, read, write, mkdir, move, delete),
        build,
    )


factory: PluginFactory = typed_factory(NotebookSettings, _declare)
