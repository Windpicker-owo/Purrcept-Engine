"""用生成的临时日志核验 CLI 所有权、轮转和时间/容量边界。

测试只改变当前进程显式借用的包级 logger；root/第三方配置和调用者流必须
保留。所有文本文件均在 pytest 临时目录生成，不读取或清理现有用户日志。
"""

from __future__ import annotations

import io
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from purrcept_engine.infrastructure.logging import CliLogging


def test_cli_summary_keeps_causal_identity_without_exception_or_arbitrary_extra(
    tmp_path: Path,
) -> None:
    console = io.StringIO()
    owner = CliLogging.open(tmp_path, console=console)
    failure = RuntimeError("secret-provider-body")
    try:
        logging.getLogger("purrcept_engine.retention_test").error(
            "能力回执已入账。",
            extra={
                "action_id": "action-1",
                "call_id": "call-1",
                "outcome": "unknown",
                "request_payload": "secret-request",
                "api_key": "secret-key",
            },
            exc_info=(type(failure), failure, None),
        )
        summary = console.getvalue()
        assert "action_id=action-1" in summary and "call_id=call-1" in summary
        assert "outcome=unknown" in summary and "exception_type=RuntimeError" in summary
        assert "secret" not in summary
        path = owner.file_path
        assert path is not None
        diagnostic = path.read_text(encoding="utf-8")
        decoded = json.loads(diagnostic)
        assert decoded["call_id"] == "call-1"
        assert decoded["exception"] == {"type": "RuntimeError", "frames": []}
        assert "secret" not in diagnostic
    finally:
        owner.close()


def test_cli_logging_restores_package_config_and_leaves_root_untouched(tmp_path: Path) -> None:
    root = logging.getLogger()
    package = logging.getLogger("purrcept_engine")
    root_state = (tuple(root.handlers), root.level, root.propagate)
    package_state = (tuple(package.handlers), package.level, package.propagate)
    console = io.StringIO()

    owner = CliLogging.open(tmp_path, console=console)
    try:
        logging.getLogger("purrcept_engine.retention_test").warning("真实失败摘要 input_id=input-1")
        file_path = owner.file_path
        assert file_path is not None
        assert "真实失败摘要 input_id=input-1" in file_path.read_text(encoding="utf-8")
        assert "真实失败摘要 input_id=input-1" in console.getvalue()
        assert (tuple(root.handlers), root.level, root.propagate) == root_state
        assert owner.failure is None
        with pytest.raises(RuntimeError, match="活动所有者"):
            CliLogging.open(tmp_path, console=console)
    finally:
        owner.close()
        owner.close()

    assert (tuple(package.handlers), package.level, package.propagate) == package_state
    assert (tuple(root.handlers), root.level, root.propagate) == root_state
    assert not console.closed


def test_generated_logs_bound_file_bytes_and_total_files(tmp_path: Path) -> None:
    owner = CliLogging.open(tmp_path, max_bytes=128, max_files=2, console=io.StringIO())
    try:
        logger = logging.getLogger("purrcept_engine.retention_test")
        for index in range(8):
            logger.warning("%s %s", index, "中" * 300)
        files = list((tmp_path / "logs").glob("engine-*.log"))
        assert len(files) == 2
        for path in files:
            assert path.stat().st_size <= 128
            assert json.loads(path.read_text(encoding="utf-8"))["truncated"] is True
        assert owner.cleanup_totals.capacity_files == 6
        assert owner.failure is None
    finally:
        owner.close()


def test_time_cleanup_is_batched_and_only_owns_engine_logs(tmp_path: Path) -> None:
    owner = CliLogging.open(tmp_path, max_bytes=128, max_files=20, console=io.StringIO())
    now = datetime.now(UTC)
    try:
        logger = logging.getLogger("purrcept_engine.retention_test")
        for index in range(4):
            logger.warning("%s %s", index, "x" * 200)
        active = owner.file_path
        files = sorted((tmp_path / "logs").glob("engine-*.log"))
        old_files = [path for path in files if path != active]
        assert len(old_files) == 3
        expired = (now - timedelta(days=8)).timestamp()
        for path in old_files:
            os.utime(path, (expired, expired))

        unrelated = tmp_path / "logs" / "third-party.log"
        unrelated.write_text("must remain", encoding="utf-8")
        os.utime(unrelated, (expired, expired))
        first = owner.cleanup(now=now, limit=1)
        assert first.expired_files == 1
        assert first.capacity_files == 0
        assert first.has_more
        second = owner.cleanup(now=now, limit=2)
        assert second.expired_files == 2
        assert not second.has_more
        assert unrelated.read_text(encoding="utf-8") == "must remain"
        assert active is not None and active.exists()
    finally:
        owner.close()


def test_boundary_is_retained_and_idle_old_file_is_released(tmp_path: Path) -> None:
    owner = CliLogging.open(tmp_path, console=io.StringIO())
    try:
        path = owner.file_path
        assert path is not None
        logging.getLogger("purrcept_engine.retention_test").warning("boundary")
        now = datetime.now(UTC)
        boundary = (now - timedelta(days=7)).timestamp()
        os.utime(path, (boundary, boundary))
        assert owner.cleanup(now=now).expired_files == 0
        # 时间推进后，空闲日志也释放旧日句柄；Windows 无需等下一条日志才删除。
        result = owner.cleanup(now=now + timedelta(days=8))
        assert result.expired_files == 1
        assert not path.exists()
    finally:
        owner.close()


def test_log_file_failure_does_not_change_package_configuration(tmp_path: Path) -> None:
    package = logging.getLogger("purrcept_engine")
    state = (tuple(package.handlers), package.level, package.propagate)
    (tmp_path / "logs").write_text("this is a file", encoding="utf-8")
    with pytest.raises(OSError):
        CliLogging.open(tmp_path, console=io.StringIO())
    assert (tuple(package.handlers), package.level, package.propagate) == state


def test_stale_logging_callback_cannot_reopen_a_closed_file(tmp_path: Path) -> None:
    package = logging.getLogger("purrcept_engine")
    previous = set(package.handlers)
    owner = CliLogging.open(tmp_path, console=io.StringIO())
    installed = set(package.handlers) - previous
    path = owner.file_path
    assert path is not None
    owner.close()
    record = logging.LogRecord("purrcept_engine.test", logging.INFO, "test", 1, "late", (), None)

    # 模拟 logger 在关闭前取得 handler，关闭后才交付并发线程里的日志记录。
    for handler in installed:
        handler.handle(record)

    assert path.read_bytes() == b""
    assert list((tmp_path / "logs").glob("engine-*.log")) == [path]
    with pytest.raises(RuntimeError, match="已经关闭"):
        owner.cleanup()


@pytest.mark.parametrize("max_bytes", [0, 63, True])
def test_logging_rejects_invalid_file_capacity(tmp_path: Path, max_bytes: int) -> None:
    with pytest.raises(ValueError, match="64"):
        CliLogging.open(tmp_path, max_bytes=max_bytes, console=io.StringIO())
    assert not (tmp_path / "logs").exists()


def test_diagnostic_keeps_nested_locations_without_exception_body(tmp_path: Path) -> None:
    """异常组和原因链保留位置，正文与任意 extra 不能进入文件。"""
    owner = CliLogging.open(tmp_path, console=io.StringIO())
    try:
        try:
            try:
                raise ValueError("secret-key")
            except ValueError as cause:
                raise RuntimeError("secret-response") from cause
        except RuntimeError as failure:
            group = ExceptionGroup("secret-group", [failure])
            logging.getLogger("purrcept_engine.test").error(
                "后台任务失败。", exc_info=(type(group), group, None)
            )
        path = owner.file_path
        assert path is not None
        body = path.read_text(encoding="utf-8")
        assert "secret" not in body
        nested = json.loads(body)["exception"]["exceptions"][0]
        assert (
            nested["frames"][-1]["function"]
            == "test_diagnostic_keeps_nested_locations_without_exception_body"
        )
        assert nested["cause"]["type"] == "ValueError"
    finally:
        owner.close()


def test_component_cause_chain_reuses_primary_fault_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """不同所有者仍保留自己的失败状态，但同一原因链只报告一个主 ERROR。"""
    from purrcept_engine.domain.health import ComponentHealth

    root = OSError("private-response")
    storage = ComponentHealth("storage")
    storage.fail(root)
    translated = RuntimeError("private-wrapper")
    translated.__cause__ = root
    subject = ComponentHealth("subject")
    subject.fail(translated)
    subject.fail(ValueError("later"))
    shutdown = ComponentHealth("shutdown")
    shutdown.fail(ExceptionGroup("private-shutdown", [translated]))
    assert storage.error_id == subject.error_id
    assert storage.failed_at == subject.failed_at
    assert shutdown.error_id == storage.error_id
    assert subject.error is translated
    assert subject.snapshot()["can_process"] is False
    primary = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(primary) == 1
    assert "private" not in primary[0].getMessage()


def test_prelock_startup_timings_are_written_to_file_without_duplicate_console(
    tmp_path: Path,
) -> None:
    console = io.StringIO()
    owner = CliLogging.open(tmp_path, console=console)
    try:
        owner.record_startup_phase("configuration", 12.5)
        path = owner.file_path
        assert path is not None
        event = json.loads(path.read_text(encoding="utf-8"))
        assert event["stage"] == "configuration" and event["duration_ms"] == 12.5
        assert console.getvalue() == ""
    finally:
        owner.close()
