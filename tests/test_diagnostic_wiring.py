"""从正式Store/Host验证诊断过期及故障不损坏权威请求。

仅在关闭的临时Engine样本上生成过期时刻；再次启动使用生产维护入口。
独立诊断表故障注入验证响应先提交、原始参数完整且查询明确表达缺口。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest

from purrcept_engine import EngineConfig, EngineHost, StorageError
from purrcept_engine.domain.model_requests.models import (
    AttemptEvidence,
    AttemptStart,
    RequestEvidence,
    RequestScope,
)
from purrcept_engine.infrastructure.sqlite import model_requests as sql
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.values import JsonObject


def evidence() -> tuple[RequestEvidence, AttemptEvidence]:
    """明确提供公开请求、完整响应和修复前参数，与可淘汰流预览分开。"""
    request = RequestEvidence(
        "request-1",
        RequestScope("foreground", "turn-1"),
        "generation",
        "offline",
        '{"payload":{"messages":[]}}',
        "offline-source-digest",
        2,
        120,
        180,
    )
    result = AttemptEvidence(
        AttemptStart("request-1", "attempt-1", 1, 1000),
        "succeeded",
        '{"message":{"content":[]}}',
        None,
        '[{"raw":"before-repair"}]',
        '[{"text":"diagnostic-preview"}]',
        True,
        0,
    )
    return request, result


async def test_host_retention_clears_old_diagnostics_and_keeps_authority_after_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "engine.sqlite3"
    store = await Store.open(path)
    request, result = evidence()
    try:
        await store.begin_request(request)
        await store.begin_attempt(result.start)
        await store.finish_attempt(result)
        await store.finish_request(request.request_id, "succeeded")
    finally:
        await store.close()
    # 样本此时无所有者；只更改可淘汰诊断的时刻，不伪造权威结果或时间。
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE model_attempt_diagnostics SET created_at=?", (time.time() - 864000,)
        )
    config = EngineConfig.validate(
        {
            "host": {"data_dir": tmp_path},
            "console": False,
            "diagnostics": {"retention_days": 1},
            "models": {
                "main": {
                    "kind": "generation",
                    "model": "openai/offline",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "context_window": 100000,
                    "max_output_tokens": 1000,
                }
            },
            "subject": {"foreground": "main", "reflection": "main", "summary": "main"},
        }
    )
    host = EngineHost(config)
    cleaned = asyncio.Event()
    original_cleanup = Store.expire_diagnostics

    async def observed_cleanup(
        store: Store, *, before: float, expired_at: float, limit: int = 128
    ) -> JsonObject:
        result = await original_cleanup(store, before=before, expired_at=expired_at, limit=limit)
        cleaned.set()
        return result

    monkeypatch.setattr(Store, "expire_diagnostics", observed_cleanup)
    await host.start()
    try:
        await asyncio.wait_for(cleaned.wait(), 3)
        record = await host.query_request(request.request_id)
        assert record is not None and record.state == "succeeded"
        actual = record.attempts[0].result
        assert actual is not None and actual.diagnostics_expired_at is not None
        assert actual.diagnostic_events_json == "[]" and actual.diagnostics_complete
        assert actual.argument_events_json == result.argument_events_json
        assert actual.response_json == result.response_json
        assert record.request.materials_json == request.materials_json
        assert not actual.diagnostics_unavailable and host.query().phase == "running"
    finally:
        await host.close()


def test_diagnostic_write_failure_keeps_authority_and_digest_identity() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in (
            "CREATE TABLE media(media_id TEXT PRIMARY KEY,content BLOB)",
            *sql.SCHEMA,
        ):
            connection.execute(statement)
        request, result = evidence()
        sql.begin_request(connection, request)
        sql.begin_attempt(connection, result.start)
        connection.execute(
            "CREATE TRIGGER fail_diagnostics BEFORE INSERT ON model_attempt_diagnostics "
            "BEGIN SELECT RAISE(FAIL, 'diagnostic-only'); END"
        )
        sql.finish_attempt(connection, result)
        sql.finish_request(connection, request.request_id, "succeeded")
        record = sql.get_request(connection, request.request_id)
        assert record is not None
        actual = record.attempts[0].result
        assert actual is not None and actual.diagnostics_unavailable
        assert actual.argument_events_json == result.argument_events_json
        assert actual.response_json == result.response_json
        sql.finish_attempt(connection, result)
        with pytest.raises(StorageError, match="不能被覆盖"):
            sql.finish_attempt(
                connection, replace(result, diagnostic_events_json='[{"changed":true}]')
            )
    finally:
        connection.close()
