"""提供面向人类阅读的本地时区终端日志与结构化文件滚动日志。

导入本模块不会修改全局 root logger。CLI 进程通过 ``console_logging`` 与
``CliLogging.open`` 显式安装日志处理器，嵌入式调用方沿用自身的日志配置。
终端默认输出本地时间、精简模块名、关键上下文标签以及完整的异常原因与堆栈，
仅对明确的密钥凭据（如 Bearer Token、sk-*、api_key=...）做精准脱敏。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TextIO, cast

_FILE_NAME = re.compile(r"engine-(\d{8})-(\d{6})\.log")
_MIN_FILE_BYTES = 64

_CREDENTIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{6,}"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "sk-[REDACTED]"),
    (
        re.compile(
            r"(?i)\b(api_key|access_token|secret|password|authorization|private_key)"
            r'(\s*[:=]\s*["\']?)([^"\'\s,;}{]+)'
        ),
        r"\1\2[REDACTED]",
    ),
)

_SUMMARY_FIELDS = (
    "source",
    "stage",
    "outcome",
    "path",
    "model",
    "scope",
    "status_code",
    "category",
    "duration_ms",
    "calls",
    "failed_calls",
    "unknown_calls",
    "input_count",
    "plugin_count",
    "attempts",
    "retry_wait",
    "remaining_seconds",
    "component",
    "error_code",
    "error_type",
    "error_id",
    "host_instance",
    "input_id",
    "turn_id",
    "request_id",
    "attempt_id",
    "action_id",
    "call_id",
    "activity_id",
    "command_id",
    "playback_id",
    "plugin_id",
    "receipt_revision",
    "operation",
    "queue",
    "queue_wait_ms",
    "execution_ms",
    "transport_step",
    "headers_ms",
    "first_byte_ms",
    "body_bytes",
    "event_loop_lag_ms",
    "transport_id",
    "connection_attempts",
)

_LEVEL_ANSI = {
    "DEBUG": "\033[90m",
    "INFO": "\033[36m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[1;31m",
}
_DIM = "\033[90m"
_RESET = "\033[0m"


def redact_secrets(text: str) -> str:
    """对文本中明确形态的密钥、令牌做局部掩码，保留正常报错内容与路径。"""
    result = text
    for pattern, replacement in _CREDENTIAL_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _resolve_level(level: int | str) -> int:
    """将字符串或整数日志级别统一解析为标准 logging 级别数值。"""
    if isinstance(level, bool):
        raise ValueError("日志级别不能是布尔值。")
    if isinstance(level, int):
        return level
    normalized = level.strip().upper()
    mapping = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    if normalized not in mapping:
        raise ValueError(f"不支持的日志级别：{level}（可选 DEBUG/INFO/WARNING/ERROR）。")
    return mapping[normalized]


def _short_logger_name(name: str) -> str:
    """缩短冗长的包名前缀，让控制台日志紧凑易读。"""
    if name == "purrcept_engine":
        return "engine"
    trimmed = name.removeprefix("purrcept_engine.")
    for layer in ("domain.", "infrastructure.", "app."):
        if trimmed.startswith(layer):
            trimmed = trimmed[len(layer) :]
            break
    return trimmed


def _supports_color(stream: object) -> bool:
    """仅在真实交互式终端且未设置 NO_COLOR 时启用 ANSI 颜色。"""
    if "NO_COLOR" in os.environ:
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(callable(isatty) and isatty())


@dataclass(frozen=True, slots=True)
class LogCleanup:
    """单次日志文件清理统计结果。

    Attributes:
        expired_files: 超出保留天数而被删除的文件数。
        capacity_files: 超出最大文件数上限而被淘汰的文件数。
        has_more: 是否仍有待继续分批清理的旧文件。
    """

    expired_files: int
    capacity_files: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class _LogFile:
    path: Path
    modified_at: float


def _owned_files(directory: Path) -> list[_LogFile]:
    """枚举日志目录下由引擎自身创建的普通日志文件（按修改时间升序）。"""
    files: list[_LogFile] = []
    for path in directory.iterdir():
        if _FILE_NAME.fullmatch(path.name) is None or path.is_symlink() or not path.is_file():
            continue
        files.append(_LogFile(path, path.stat().st_mtime))
    files.sort(key=lambda item: (item.modified_at, item.path.name))
    return files


class ConsoleFormatter(logging.Formatter):
    """面向开发与日常使用的本地时区终端日志格式器，完整保留异常原因与堆栈。"""

    def __init__(self, *, color: bool = False) -> None:
        super().__init__()
        self._color = color

    def format(self, record: logging.LogRecord) -> str:
        """输出可读的本地时间、级别、短模块名、消息正文、上下文标签及脱敏堆栈。"""
        local_time = datetime.fromtimestamp(record.created).astimezone().strftime("%H:%M:%S")
        level = record.levelname
        module = _short_logger_name(record.name)
        message = redact_secrets(record.getMessage())

        fields: dict[str, object] = record.__dict__
        tags: list[str] = []
        for name in _SUMMARY_FIELDS:
            value = fields.get(name)
            if isinstance(value, bool) or not isinstance(value, str | int | float):
                continue
            if name == "duration_ms" and isinstance(value, int | float):
                tags.append(f"duration_ms={value:.1f}")
            elif isinstance(value, str):
                tags.append(f"{name}={redact_secrets(value)}")
            else:
                tags.append(f"{name}={value}")
        if isinstance(record.exc_info, tuple) and record.exc_info[0] is not None:
            exc_name = record.exc_info[0].__name__
            if f"error_type={exc_name}" not in tags:
                tags.append(f"exception_type={exc_name}")

        suffix = f" | {' '.join(tags)}" if tags else ""
        if self._color:
            lvl_color = _LEVEL_ANSI.get(level, "")
            header = (
                f"{_DIM}{local_time}{_RESET} "
                f"{lvl_color}{level:<7}{_RESET} "
                f"{_DIM}[{module}]{_RESET}"
            )
            tail = f"{_DIM}{suffix}{_RESET}" if suffix else ""
            line = f"{header} {message}{tail}"
        else:
            line = f"{local_time} {level:<7} [{module}] {message}{suffix}"

        if isinstance(record.exc_info, tuple):
            exc_text = redact_secrets(self.formatException(record.exc_info)).strip()
            if exc_text:
                line = f"{line}\n{exc_text}"
        elif record.stack_info:
            line = f"{line}\n{redact_secrets(self.formatStack(record.stack_info))}"
        return line


def _exception_frames(error: BaseException, seen: set[int], depth: int = 0) -> dict[str, object]:
    """序列化异常类型、脱敏后的错误消息、栈帧位置以及嵌套原因链。"""
    if id(error) in seen or depth >= 16:
        return {"type": type(error).__name__, "truncated": True}
    seen.add(id(error))
    frames: list[dict[str, object]] = []
    trace = error.__traceback__
    while trace is not None:
        frame = trace.tb_frame
        frames.append(
            {
                "module": frame.f_globals.get("__name__", ""),
                "file": Path(frame.f_code.co_filename).name,
                "function": frame.f_code.co_name,
                "line": trace.tb_lineno,
            }
        )
        trace = trace.tb_next
    result: dict[str, object] = {
        "type": type(error).__name__,
        "message": redact_secrets(str(error)),
        "frames": frames[-64:],
    }
    cause = error.__cause__
    if cause is None and not error.__suppress_context__:
        cause = error.__context__
    if cause is not None:
        result["cause"] = _exception_frames(cause, seen, depth + 1)
    if isinstance(error, BaseExceptionGroup):
        group = cast(BaseExceptionGroup[BaseException], error)
        result["exceptions"] = [
            _exception_frames(child, seen, depth + 1) for child in group.exceptions[:32]
        ]
        result["omitted_exceptions"] = max(0, len(group.exceptions) - 32)
    return result


class _DiagnosticFormatter(logging.Formatter):
    """文件日志采用 JSON Lines 格式，保存本地带时区时间戳、上下文标签与完整异常信息。"""

    def format(self, record: logging.LogRecord) -> str:
        """将单条日志编码为单行 JSON 文本。"""
        public: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created)
            .astimezone()
            .isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_secrets(record.getMessage()),
            "location": {
                "module": record.module,
                "function": record.funcName,
                "line": record.lineno,
            },
        }
        for name in _SUMMARY_FIELDS:
            value = record.__dict__.get(name)
            if not isinstance(value, bool) and isinstance(value, str | int | float):
                public[name] = redact_secrets(value) if isinstance(value, str) else value
        if isinstance(record.exc_info, tuple) and record.exc_info[1] is not None:
            public["exception"] = _exception_frames(record.exc_info[1], set())
        return json.dumps(public, ensure_ascii=False, allow_nan=False)


@contextmanager
def console_logging(*, level: int | str = logging.INFO) -> Generator[None]:
    """在数据目录锁取得前先开启终端日志，退出时还原包级 logger 配置。"""
    resolved = _resolve_level(level)
    logger = logging.getLogger("purrcept_engine")
    previous = logger.level, logger.propagate
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(resolved)
    handler.setFormatter(ConsoleFormatter(color=_supports_color(sys.stderr)))
    logger.addHandler(handler)
    logger.setLevel(resolved)
    logger.propagate = False
    try:
        yield
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous[0])
        logger.propagate = previous[1]


class _TextLogHandler(logging.Handler):
    """带单文件大小上限、按日轮转与过期淘汰的同步文件日志处理器。"""

    def __init__(
        self,
        directory: Path,
        retention_days: int,
        max_bytes: int,
        max_files: int,
        *,
        level: int = logging.INFO,
    ) -> None:
        super().__init__(level=level)
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
        """创建日志目录并打开本次运行的新日志文件。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        self._new_file(datetime.now().astimezone().date())

    def _new_file(self, day: date) -> None:
        self._close_stream()
        prefix = f"engine-{day:%Y%m%d}-"
        ordinal = 0
        for item in _owned_files(self.directory):
            if item.path.name.startswith(prefix):
                ordinal = max(ordinal, int(item.path.stem.rsplit("-", 1)[1]))
        if ordinal >= 999999:
            raise OSError("当天日志文件序号已达上限。")
        path = self.directory / f"{prefix}{ordinal + 1:06d}.log"
        self._stream = path.open("x", encoding="utf-8", newline="\n")
        self.file_path = path
        self._day = day
        self._size = 0
        self.cleanup(now=datetime.now(UTC), limit=max(self.max_files, 128))

    def emit(self, record: logging.LogRecord) -> None:
        """写入一条结构化日志，超长单条自动裁剪并保留有效 JSON 结构。"""
        if not self._accepting:
            return
        try:
            text = self.format(record) + "\n"
            encoded = text.encode("utf-8")
            if len(encoded) > self.max_bytes:
                minimal: dict[str, object] = {
                    "level": record.levelname,
                    "truncated": True,
                    "message": "",
                }
                overhead = (
                    len(json.dumps(minimal, ensure_ascii=False, separators=(",", ":")).encode()) + 1
                )
                raw_msg = redact_secrets(record.getMessage()).encode("utf-8")[
                    : max(0, self.max_bytes - overhead)
                ]
                minimal["message"] = raw_msg.decode("utf-8", errors="ignore")
                text = json.dumps(minimal, ensure_ascii=False, separators=(",", ":")) + "\n"
                encoded = text.encode("utf-8")
                while len(encoded) > self.max_bytes:
                    minimal["message"] = str(minimal["message"])[
                        : max(0, len(str(minimal["message"])) // 2)
                    ]
                    text = json.dumps(minimal, ensure_ascii=False, separators=(",", ":")) + "\n"
                    encoded = text.encode("utf-8")

            day = datetime.now().astimezone().date()
            if (
                self._day != day
                or self._stream is None
                or self._size + len(encoded) > self.max_bytes
            ):
                self._new_file(day)
            stream = self._stream
            if stream is None:
                raise RuntimeError("日志文件句柄未就绪。")
            stream.write(text)
            stream.flush()
            self._size += len(encoded)
        except (OSError, UnicodeError) as error:
            self.failure = error

    def cleanup(self, *, now: datetime, limit: int) -> LogCleanup:
        """清理过期或超出文件数上限的历史日志文件（跳过当前正在写入的文件）。"""
        if now.utcoffset() is None:
            raise ValueError("日志清理时间必须携带时区。")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("日志清理批量必须是正整数。")
        self.acquire()
        try:
            local_today = now.astimezone().date()
            if self._day is not None and self._day < local_today:
                self._close_stream()
            cutoff = (now.astimezone(UTC) - timedelta(days=self.retention_days)).timestamp()
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
        stream = self._stream
        if stream is not None:
            try:
                stream.close()
            finally:
                if stream.closed:
                    self._stream = None

    def close(self) -> None:
        """幂等关闭当前写入流。"""
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
        """返回本次运行累计删除的过期与超额日志文件数量。"""
        self.acquire()
        try:
            return LogCleanup(self._expired_files, self._capacity_files, self._cleanup_pending)
        finally:
            self.release()


class CliLogging:
    """管理 CLI 进程运行期的终端与滚动文件日志生命周期。"""

    def __init__(
        self,
        file_handler: _TextLogHandler,
        console_handler: logging.Handler,
        *,
        level: int = logging.INFO,
    ) -> None:
        self._logger = logging.getLogger("purrcept_engine")
        self._old_level = self._logger.level
        self._old_propagate = self._logger.propagate
        self._file = file_handler
        self._console = console_handler
        self._closed = False
        target_level = (
            min(self._old_level, level) if self._old_level != logging.NOTSET else level
        )
        self._logger.setLevel(target_level)
        self._logger.propagate = False
        self._logger.addHandler(file_handler)
        self._logger.addHandler(console_handler)

    def record_startup_phase(self, stage: str, duration_ms: float) -> None:
        """将目录锁取得前的早期启动阶段耗时补写入日志文件。"""
        if self._closed:
            raise RuntimeError("CLI 日志已经关闭。")
        if (
            stage not in ("configuration", "plugin_declarations", "web")
            or not math.isfinite(duration_ms)
            or duration_ms < 0
        ):
            raise ValueError("启动阶段名称或耗时数值无效。")
        record = self._logger.makeRecord(
            "purrcept_engine.app.cli",
            logging.INFO,
            __file__,
            0,
            "启动早期阶段耗时已记录。",
            (),
            None,
            extra={"stage": stage, "duration_ms": duration_ms},
        )
        self._file.handle(record)

    @classmethod
    def open(
        cls,
        data_dir: Path,
        *,
        retention_days: int = 7,
        max_bytes: int = 10485760,
        max_files: int = 7,
        level: int | str = logging.INFO,
        console: TextIO | None = None,
        include_console: bool = True,
    ) -> CliLogging:
        """打开数据目录下的滚动日志文件并绑定到包级 logger。

        Args:
            data_dir: 当前实例独占的数据目录。
            retention_days: 日志文件保留天数，默认 7 天。
            max_bytes: 单个日志文件最大字节数，最小 64 字节。
            max_files: 最多保留的日志文件份数，默认 7 份。
            level: 日志级别（如 ``"DEBUG"`` 或 ``"INFO"``）。
            console: 可选的终端输出流，默认使用 ``sys.stderr``。
            include_console: 若外层 ``console_logging`` 已挂载终端输出，可设为 False 避免重复。

        Returns:
            已挂载处理器的 ``CliLogging`` 实例。
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
        resolved = _resolve_level(level)
        logger = logging.getLogger("purrcept_engine")
        if any(isinstance(handler, _TextLogHandler) for handler in logger.handlers):
            raise RuntimeError("CLI 日志已经有活动所有者。")

        file_handler = _TextLogHandler(
            data_dir / "logs", retention_days, max_bytes, max_files, level=resolved
        )
        try:
            file_handler.open()
        except (OSError, UnicodeError):
            file_handler.close()
            raise

        file_handler.setFormatter(_DiagnosticFormatter())
        target_stream = sys.stderr if console is None else console
        console_handler = logging.StreamHandler(target_stream)
        console_handler.setLevel(resolved if include_console else logging.CRITICAL + 1)
        console_handler.setFormatter(ConsoleFormatter(color=_supports_color(target_stream)))
        return cls(file_handler, console_handler, level=resolved)

    @property
    def file_path(self) -> Path | None:
        """当前正在写入的日志文件路径。"""
        return self._file.file_path

    @property
    def failure(self) -> OSError | UnicodeError | None:
        """最近一次文件写入 I/O 异常（无异常时为 None）。"""
        return self._file.failure

    @property
    def cleanup_totals(self) -> LogCleanup:
        """累计清理文件统计。"""
        return self._file.cleanup_totals

    def cleanup(self, *, now: datetime | None = None, limit: int = 128) -> LogCleanup:
        """执行一次过期与容量超额日志清理。"""
        if self._closed:
            raise RuntimeError("CLI 日志已经关闭。")
        return self._file.cleanup(now=datetime.now(UTC) if now is None else now, limit=limit)

    def close(self) -> None:
        """卸载处理器、关闭日志文件并恢复包级 logger 原始设置。"""
        if self._closed:
            self._file.close()
            return
        self._closed = True
        self._logger.removeHandler(self._file)
        self._logger.removeHandler(self._console)
        self._file.close()
        self._console.close()
        self._logger.setLevel(self._old_level)
        self._logger.propagate = self._old_propagate
