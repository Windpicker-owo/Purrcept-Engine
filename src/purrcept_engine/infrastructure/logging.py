"""由 CLI 显式拥有包级终端摘要与有限文本日志。

导入本模块不会安装 handler 或修改 root logger。独立进程在取得数据目录锁后
调用 CliLogging.open，关闭宿主后调用 close；嵌入应用沿用调用者的日志配置。
本模块不记录模型证据、不打开 SQLite，也不创建后台线程或维护任务。

日志只写 data_dir/logs 下本模块命名的普通文件。按 UTC 日期与单文件字节额度
轮转，时间和总文件数分别限制保留；其他文件、子目录与符号链接不参与清理。
"""

from __future__ import annotations

import logging
import re
import sys
from copy import copy
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TextIO

_FILE_NAME = re.compile(r"engine-(\d{8})-(\d{6})\.log")
_MIN_FILE_BYTES = 64
_SUMMARY_FIELDS = (
    "host_instance",
    "input_id",
    "turn_id",
    "scope",
    "request_id",
    "attempt_id",
    "action_id",
    "call_id",
    "path",
    "activity_id",
    "command_id",
    "playback_id",
    "plugin_id",
    "outcome",
    "receipt_revision",
    "error_type",
)


@dataclass(frozen=True, slots=True)
class LogCleanup:
    """一次日志清理的实际数量；has_more 表示仍有待删文件。

    expired_files 表示超过时间边界，capacity_files 表示仍在保留期但超过文件
    数量额度。当前写入文件始终保留，单次只删除至多 limit 个文件。
    """

    expired_files: int
    capacity_files: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class _LogFile:
    path: Path
    modified_at: float


def _owned_files(directory: Path) -> list[_LogFile]:
    """只枚举本入口的普通日志文件，按实际更新时间稳定排列。

    自有文件数量由轮转时清理约束。扫描不递归，也不跟随符号链接；即使日志
    目录内放有媒体、第三方日志或外部链接，也不会扩展清理所有权。
    """
    files: list[_LogFile] = []
    for path in directory.iterdir():
        if _FILE_NAME.fullmatch(path.name) is None or path.is_symlink() or not path.is_file():
            continue
        files.append(_LogFile(path, path.stat().st_mtime))
    files.sort(key=lambda item: (item.modified_at, item.path.name))
    return files


class _UtcFormatter(logging.Formatter):
    """终端和文件共用 UTC 时间，避免跨时区时无法辨认保留边界。"""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """保留事件产生时间；格式参数不改变统一的时区表示。"""
        del datefmt
        return datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")

    def format(self, record: logging.LogRecord) -> str:
        """仅展示关联身份及分类，默认摘要不回显第三方异常正文或所有extra。

        复制LogRecord避免修改调用者其它handler；原异常继续由API传播，完整
        请求/回执仍从权威查询取得，普通日志不承担其完整性。
        """
        public = copy(record)
        public.exc_info = None
        public.exc_text = None
        public.stack_info = None
        fields: dict[str, object] = record.__dict__
        summaries = [
            f"{name}={value}"
            for name in _SUMMARY_FIELDS
            if isinstance(value := fields.get(name), str | int)
        ]
        if record.exc_info is not None and record.exc_info[0] is not None:
            summaries.append(f"exception_type={record.exc_info[0].__name__}")
        rendered = super().format(public)
        return rendered + (" | " + " ".join(summaries) if summaries else "")


class _TextLogHandler(logging.Handler):
    """同步写有限日志，锁覆盖轮转与清理，窄 I/O 故障留给所有者查询。

    每条消息最多占一个文件额度，超长摘要带截断标记；这只影响普通日志。
    logging 的 handler 锁串行化 emit，外部维护借用同一锁，避免删除仍在写的
    文件。故障不改变业务结果，但 failure 明确保留最后一次真实写入失败。
    """

    def __init__(
        self, directory: Path, retention_days: int, max_bytes: int, max_files: int
    ) -> None:
        super().__init__(level=logging.INFO)
        self.directory = directory
        self.retention_days = retention_days
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.file_path: Path | None = None
        self.failure: OSError | UnicodeError | None = None
        self._stream: TextIO | None = None
        self._day: date | None = None
        self._size = 0
        self._accepting = True
        self._expired_files = 0
        self._capacity_files = 0
        self._cleanup_pending = False

    def open(self) -> None:
        """先取得文件再安装到 logger，失败不会留下部分日志配置。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        self._new_file(datetime.now(UTC).date())

    def _new_file(self, day: date) -> None:
        """每次取得新文件身份，不追加到上个进程遗留的写入文件。"""
        self._close_stream()
        prefix = f"engine-{day:%Y%m%d}-"
        ordinal = 0
        for item in _owned_files(self.directory):
            if item.path.name.startswith(prefix):
                ordinal = max(ordinal, int(item.path.stem.rsplit("-", 1)[1]))
        if ordinal >= 999999:
            raise OSError("当天日志文件身份额度已耗尽。")
        path = self.directory / f"{prefix}{ordinal + 1:06d}.log"
        self._stream = path.open("x", encoding="utf-8", newline="\n")
        self.file_path = path
        self._day = day
        self._size = 0

        # 文件数量在取得每个新文件后约束，不依赖宿主周期维护才阻止增长。
        self.cleanup(now=datetime.now(UTC), limit=max(self.max_files, 128))

    def emit(self, record: logging.LogRecord) -> None:
        """只抑制可识别的诊断 I/O/编码故障，格式或程序错误仍原样传播。"""
        # logging 可能已取出 handler 引用才遇到 close；关闭后旧引用不可重开文件。
        if not self._accepting:
            return
        try:
            text = self.format(record) + "\n"
            encoded = text.encode("utf-8")
            if len(encoded) > self.max_bytes:
                suffix = b" [truncated]\n"
                prefix = encoded[: self.max_bytes - len(suffix)]
                # 截断只舍弃边界上不完整的 UTF-8 字符，完整正文仍归权威存储。
                text = prefix.decode("utf-8", errors="ignore") + suffix.decode("ascii")
                encoded = text.encode("utf-8")

            # 文件日期归写入资源的当前寿命，事件自己的时间仍由 formatter 保留。
            day = datetime.now(UTC).date()
            if (
                self._day != day
                or self._stream is None
                or self._size + len(encoded) > self.max_bytes
            ):
                self._new_file(day)
            stream = self._stream
            if stream is None:
                raise RuntimeError("日志文件已取得但没有写入流。")
            stream.write(text)
            stream.flush()
            self._size += len(encoded)
        except (OSError, UnicodeError) as error:
            self.failure = error

    def cleanup(self, *, now: datetime, limit: int) -> LogCleanup:
        """在 handler 锁内清时间/容量超额文件，当前写入文件不参与删除。"""
        if now.utcoffset() is None:
            raise ValueError("日志清理时间必须携带时区。")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("日志清理批量必须是正整数。")
        self.acquire()
        try:
            current = now.astimezone(UTC)
            if self._day is not None and self._day < current.date():
                # 空闲宿主也须释放旧日期的句柄，否则 Windows 上不能淘汰旧日志。
                self._close_stream()
            cutoff = (current - timedelta(days=self.retention_days)).timestamp()
            files = _owned_files(self.directory)
            candidates: list[tuple[_LogFile, bool]] = []
            remaining = len(files)
            for item in files:
                if item.path == self.file_path and self._stream is not None:
                    continue
                expired = item.modified_at < cutoff
                if expired or remaining > self.max_files:
                    candidates.append((item, expired))
                    remaining -= 1

            expired_files = 0
            capacity_files = 0
            self._cleanup_pending = bool(candidates)
            for item, expired in candidates[:limit]:
                item.path.unlink()
                if expired:
                    expired_files += 1
                    self._expired_files += 1
                else:
                    capacity_files += 1
                    self._capacity_files += 1
            self._cleanup_pending = len(candidates) > limit
            return LogCleanup(expired_files, capacity_files, len(candidates) > limit)
        finally:
            self.release()

    def _close_stream(self) -> None:
        """确认流已关闭才归还引用；关闭失败仍持有未释放的资源。"""
        stream = self._stream
        if stream is not None:
            try:
                stream.close()
            finally:
                if stream.closed:
                    self._stream = None

    def close(self) -> None:
        """重复关闭不重复释放文件；已知 I/O 失败仍保留在 failure 中。"""
        self.acquire()
        try:
            self._accepting = False
            try:
                self._close_stream()
            except (OSError, UnicodeError) as error:
                self.failure = error
            super().close()
        finally:
            self.release()

    @property
    def cleanup_totals(self) -> LogCleanup:
        """合并自动轮转和显式维护的实际淘汰计数，部分失败也不丢失已删范围。"""
        self.acquire()
        try:
            return LogCleanup(self._expired_files, self._capacity_files, self._cleanup_pending)
        finally:
            self.release()


class CliLogging:
    """独立 CLI 对 purrcept_engine logger 的一次显式借用。

    open 安装终端与文件 handler，close 移除本实例 handler 并还原原 level 和
    propagate。调用方在借用期间独占该 logger 的配置；嵌入式宿主不创建本对象。
    root logger 和第三方 logger 始终沿用原配置。同进程不允许重复打开所有者。
    """

    def __init__(self, file_handler: _TextLogHandler, console_handler: logging.Handler) -> None:
        """保留已经取得的文件与本次 logger 配置，生命周期由 open/close 拥有。"""
        self._logger = logging.getLogger("purrcept_engine")
        self._old_level = self._logger.level
        self._old_propagate = self._logger.propagate
        self._file = file_handler
        self._console = console_handler
        self._closed = False
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._logger.addHandler(file_handler)
        self._logger.addHandler(console_handler)

    @classmethod
    def open(
        cls,
        data_dir: Path,
        *,
        retention_days: int = 7,
        max_bytes: int = 10485760,
        max_files: int = 7,
        console: TextIO | None = None,
    ) -> CliLogging:
        """取得日志资源后安装 handler；配置无效或文件取得失败原样报告。

        data_dir 必须已由本 CLI 持有实例锁。单文件至少 64 字节，以保证时间/
        级别摘要与截断标记可辨认；max_files 包含当前写入文件。console 默认
        stderr；传入流由调用者拥有，本对象关闭时不关闭它。
        """
        for name, value in (("retention_days", retention_days), ("max_files", max_files)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须是正整数。")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < _MIN_FILE_BYTES
        ):
            raise ValueError("单文件日志额度必须至少 64 字节。")
        logger = logging.getLogger("purrcept_engine")
        if any(isinstance(handler, _TextLogHandler) for handler in logger.handlers):
            raise RuntimeError("CLI 日志已经有活动所有者。")

        file_handler = _TextLogHandler(data_dir / "logs", retention_days, max_bytes, max_files)
        try:
            file_handler.open()
        except (OSError, UnicodeError):
            file_handler.close()
            raise

        formatter = _UtcFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        file_handler.setFormatter(formatter)
        console_handler = logging.StreamHandler(sys.stderr if console is None else console)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        return cls(file_handler, console_handler)

    @property
    def file_path(self) -> Path | None:
        """返回当前或最后一份写入文件，供 CLI 报告实际日志位置。"""
        return self._file.file_path

    @property
    def failure(self) -> OSError | UnicodeError | None:
        """返回最后一次已知日志 I/O/编码失败，不把缺失日志声称为完整。"""
        return self._file.failure

    @property
    def cleanup_totals(self) -> LogCleanup:
        """返回本所有者寿命内的淘汰计数，自动容量轮转也保留缺口事实。"""
        return self._file.cleanup_totals

    def cleanup(self, *, now: datetime | None = None, limit: int = 128) -> LogCleanup:
        """同步清一批自有日志；维护频率由 CLI 拥有，失败原样传播。"""
        if self._closed:
            raise RuntimeError("CLI 日志已经关闭。")
        return self._file.cleanup(now=datetime.now(UTC) if now is None else now, limit=limit)

    def close(self) -> None:
        """移除本实例 handler 并还原包级配置，不关闭调用者终端流。"""
        if self._closed:
            # 已移除的 handler 不再接受日志；显式重等可继续关闭失败的文件流。
            self._file.close()
            return
        self._closed = True
        self._logger.removeHandler(self._file)
        self._logger.removeHandler(self._console)
        self._file.close()
        self._console.close()
        self._logger.setLevel(self._old_level)
        self._logger.propagate = self._old_propagate
