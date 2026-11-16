"""验证本地时区终端日志、异常堆栈输出、精准凭据脱敏与滚动文件清理策略。"""

from __future__ import annotations

import io
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from purrcept_engine.infrastructure.logging import CliLogging, console_logging, redact_secrets


def test_redact_secrets_masks_credentials_while_preserving_normal_errors() -> None:
    raw = (
        "请求 http://127.0.0.1:4000/v1 失败 (401): "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9, "
        "api_key='my-super-secret-value', key=sk-proj1234567890abcdef"
    )
    cleaned = redact_secrets(raw)
    assert "http://127.0.0.1:4000/v1" in cleaned
    assert "401" in cleaned
    assert "eyJhbGci" not in cleaned
    assert "my-super-secret-value" not in cleaned
    assert "sk-proj1234567890abcdef" not in cleaned
    assert "[REDACTED]" in cleaned


def test_cli_logging_preserves_readable_exception_reason_and_redacts_secrets(
    tmp_path: Path,
) -> None:
    console = io.StringIO()
    owner = CliLogging.open(tmp_path, console=console)
    failure = RuntimeError("连接超时 (api_key=secret-provider-key)")
    try:
        logging.getLogger("purrcept_engine.retention_test").error(
            "[能力调用] me.phone.qq.send -> 失败",
            extra={
                "action_id": "action-1",
                "call_id": "call-1",
                "outcome": "failed",
                "request_payload": "secret-request",
                "api_key": "secret-key",
            },
            exc_info=(type(failure), failure, None),
        )
        summary = console.getvalue()
        assert "action_id=action-1" in summary and "call_id=call-1" in summary
        assert "outcome=failed" in summary and "exception_type=RuntimeError" in summary
        assert "连接超时" in summary
        assert "secret-provider-key" not in summary
        assert "secret-request" not in summary
        assert "secret-key" not in summary

        path = owner.file_path
        assert path is not None
        diagnostic = path.read_text(encoding="utf-8")
        decoded = json.loads(diagnostic)
        assert decoded["call_id"] == "call-1"
        assert decoded["exception"]["type"] == "RuntimeError"
        assert "连接超时" in decoded["exception"]["message"]
        assert "secret-provider-key" not in diagnostic
    finally:
        owner.close()


def test_cli_logging_supports_debug_level_and_restores_package_config(tmp_path: Path) -> None:
    root = logging.getLogger()
    package = logging.getLogger("purrcept_engine")
    root_state = (tuple(root.handlers), root.level, root.propagate)
    package_state = (tuple(package.handlers), package.level, package.propagate)
    console = io.StringIO()

    owner = CliLogging.open(tmp_path, level="DEBUG", console=console)
    try:
        logging.getLogger("purrcept_engine.retention_test").debug("调试细节 input_id=input-1")
        file_path = owner.file_path
        assert file_path is not None
        assert "调试细节 input_id=input-1" in file_path.read_text(encoding="utf-8")
        assert "调试细节 input_id=input-1" in console.getvalue()
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


def test_console_logging_context_manager_respects_level() -> None:
    package = logging.getLogger("purrcept_engine")
    before = (tuple(package.handlers), package.level, package.propagate)
    with console_logging(level="DEBUG"):
        assert package.level == logging.DEBUG
    assert (tuple(package.handlers), package.level, package.propagate) == before


def test_storage_latency_fields_survive_structured_logging(tmp_path: Path) -> None:
    owner = CliLogging.open(tmp_path, console=io.StringIO())
    try:
        logging.getLogger("purrcept_engine.infrastructure.sqlite.queue").warning(
            "存储操作等待或执行较慢。",
            extra={
                "operation": "FeedbackLedger.bridge",
                "queue": "control",
                "queue_wait_ms": 1200.0,
                "execution_ms": 1500.0,
                "sql": "secret-query",
                "arguments": "secret-arguments",
            },
        )
        path = owner.file_path
        assert path is not None
        diagnostic = path.read_text(encoding="utf-8")
        record = json.loads(diagnostic)
        assert record["operation"] == "FeedbackLedger.bridge"
        assert record["queue"] == "control"
        assert record["queue_wait_ms"] == 1200.0
        assert record["execution_ms"] == 1500.0
        assert "secret" not in diagnostic
    finally:
        owner.close()


def test_audio_transfer_progress_survives_structured_logging(tmp_path: Path) -> None:
    owner = CliLogging.open(tmp_path, console=io.StringIO())
    try:
        logging.getLogger("purrcept_engine.plugins.audio.tts").info(
            "语音传输阶段状态已更新。",
            extra={
                "call_id": "audio-call",
                "stage": "文件合成",
                "transport_step": "http11.receive_response_body.started",
                "headers_ms": 3000.0,
                "first_byte_ms": 3100.0,
                "body_bytes": 128,
                "event_loop_lag_ms": 2.5,
                "trace_info": "secret-headers",
                "text": "secret-text",
            },
        )
        path = owner.file_path
        assert path is not None
        diagnostic = path.read_text(encoding="utf-8")
        record = json.loads(diagnostic)
        assert record["transport_step"] == "http11.receive_response_body.started"
        assert record["headers_ms"] == 3000.0
        assert record["first_byte_ms"] == 3100.0
        assert record["body_bytes"] == 128
        assert record["event_loop_lag_ms"] == 2.5
        assert "secret" not in diagnostic
    finally:
        owner.close()


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


def test_diagnostic_keeps_nested_locations_and_redacted_messages(tmp_path: Path) -> None:
    """异常组和原因链完整保留代码位置与可读报错，同时掩码敏感密钥。"""
    owner = CliLogging.open(tmp_path, console=io.StringIO())
    try:
        try:
            try:
                raise ValueError("参数错误 api_key=secret-key-val")
            except ValueError as cause:
                raise RuntimeError("上游网关拒绝 sk-secret9876543210") from cause
        except RuntimeError as failure:
            group = ExceptionGroup("批量任务异常", [failure])
            logging.getLogger("purrcept_engine.test").error(
                "后台任务失败。", exc_info=(type(group), group, failure.__traceback__)
            )
        path = owner.file_path
        assert path is not None
        body = path.read_text(encoding="utf-8")
        assert "secret-key-val" not in body
        assert "sk-secret9876543210" not in body
        parsed = json.loads(body)["exception"]
        assert parsed["message"].startswith("批量任务异常")
        nested = parsed["exceptions"][0]
        assert "上游网关拒绝" in nested["message"]
        assert (
            nested["frames"][-1]["function"]
            == "test_diagnostic_keeps_nested_locations_and_redacted_messages"
        )
        assert nested["cause"]["type"] == "ValueError"
        assert "参数错误" in nested["cause"]["message"]
    finally:
        owner.close()


def test_component_cause_chain_reuses_primary_fault_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from purrcept_engine.domain.health import ComponentHealth

    root = OSError("磁盘空间不足")
    storage = ComponentHealth("storage")
    storage.fail(root)
    translated = RuntimeError("上层包装失败")
    translated.__cause__ = root
    subject = ComponentHealth("subject")
    subject.fail(translated)
    subject.fail(ValueError("later"))
    shutdown = ComponentHealth("shutdown")
    shutdown.fail(ExceptionGroup("收尾异常", [translated]))
    assert storage.error_id == subject.error_id
    assert storage.failed_at == subject.failed_at
    assert shutdown.error_id == storage.error_id
    assert subject.error is translated
    assert subject.snapshot()["can_process"] is False
    primary = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(primary) == 1


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
