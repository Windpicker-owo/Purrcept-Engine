"""保存逻辑模型请求、独立尝试及可取得的公开材料。

本模块定义主体交给 Store 的不可变值与单连接 SQL 事务，不打开连接、不调用
模型，也不决定重试。请求材料和完整响应属于权威事实；详细流诊断另带覆盖
标记。用量汇总读取尝试的长期元数据，无需还原正文。开始记录先于外部请求，
结果记录不能覆盖旧尝试。启动恢复只标记中断，
不重发模型请求。Store 在自己的线程和权威提交队列中调用这些函数。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import cast

from purrcept_engine.domain.model_requests.models import (
    AttemptEvidence,
    AttemptRecord,
    AttemptStart,
    BackgroundBudgetExceeded,
    FailureEvidence,
    RequestEvidence,
    RequestOutcome,
    RequestRecord,
    RequestScope,
)
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite import analysis_work, archive, retention
from purrcept_engine.infrastructure.sqlite.rows import as_int, as_opt_text, as_text
from purrcept_engine.serialization import encode
from purrcept_engine.values import JsonObject, JsonValue

_logger = logging.getLogger(__name__)


USAGE_SCHEMA = (
    "CREATE TABLE capacity_usage (binding TEXT PRIMARY KEY, "
    "request_id TEXT NOT NULL REFERENCES model_requests(request_id), anchor_json TEXT NOT NULL)",
)

SCHEMA = (
    "CREATE TABLE model_requests (request_id TEXT PRIMARY KEY, evidence_json TEXT NOT NULL, "
    "state TEXT NOT NULL CHECK(state IN "
    "('running','succeeded','failed','timed_out','cancelled','interrupted')), "
    "created_at REAL NOT NULL, finished_at REAL)",
    "CREATE INDEX model_requests_scope ON model_requests "
    "(json_extract(evidence_json,'$.scope.owner_id'),json_extract(evid"
    "ence_json,'$.scope.turn_id'))",
    "CREATE TABLE model_attempts (attempt_id TEXT PRIMARY KEY, "
    "request_id TEXT NOT NULL REFERENCES model_requests(request_id), sequence INTEGER NOT NULL "
    "CHECK(sequence > 0), start_json TEXT NOT NULL, result_json TEXT, "
    "created_at REAL NOT NULL, finished_at REAL, UNIQUE(request_id,sequence))",
    "CREATE TABLE model_request_media ("
    "request_id TEXT NOT NULL REFERENCES model_requests(request_id), "
    "media_id TEXT NOT NULL REFERENCES media(media_id), PRIMARY KEY(request_id,media_id))",
    "CREATE TABLE model_attempt_media ("
    "attempt_id TEXT NOT NULL REFERENCES model_attempts(attempt_id), "
    "media_id TEXT NOT NULL REFERENCES media(media_id), PRIMARY KEY(attempt_id,media_id))",
    *retention.SCHEMA,
    *USAGE_SCHEMA,
    *archive.BODY_SCHEMA,
    *archive.TOTAL_SCHEMA,
)


def begin_request(connection: sqlite3.Connection, request: RequestEvidence) -> None:
    """原子保存逻辑材料。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        request: 本次逻辑请求冻结的完整公开材料与因果身份。

    Raises:
        StorageError: 模型请求身份与材料冲突。

    Note:
        同身份同材料幂等，不覆盖已有材料。
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if request.materials_json is None:
            raise ValueError("新请求必须包含完整材料。")
        material_bytes = len(request.materials_json.encode("utf-8"))
        materials, media = intern_request_media(connection, request.materials_json)
        original = encode(replace(request, materials_json=materials))
        encoded = encode(replace(request, materials_json=None))
        row: tuple[object, ...] | None = connection.execute(
            "SELECT evidence_json FROM model_requests WHERE request_id=?", (request.request_id,)
        ).fetchone()
        if row is not None:
            if row != (encoded,):
                raise StorageError("模型请求身份与材料冲突。", code="request_conflict")
            archive.put(
                connection,
                "request",
                request.request_id,
                materials,
                original,
                time.time(),
                original_bytes=material_bytes,
            )
            return
        connection.execute(
            "INSERT INTO model_requests VALUES (?, ?, 'running', ?, NULL)",
            (request.request_id, encoded, time.time()),
        )
        archive.put(
            connection,
            "request",
            request.request_id,
            materials,
            original,
            time.time(),
            original_bytes=material_bytes,
        )
        connection.executemany(
            "INSERT INTO model_request_media VALUES (?, ?)",
            ((request.request_id, media_id) for media_id in media),
        )


def begin_attempt(connection: sqlite3.Connection, start: AttemptStart) -> None:
    """一次开始门槛只能提交一次，重复身份不提供重复发送许可。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        start: 真实尝试开始前须持久化的编号、身份和截止事实。

    Raises:
        StorageError: 逻辑请求不再接受尝试；模型尝试顺序冲突。
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        row: tuple[object, ...] | None = connection.execute(
            "SELECT state FROM model_requests WHERE request_id=?", (start.request_id,)
        ).fetchone()
        if row != ("running",):
            raise StorageError("逻辑请求不再接受尝试。", code="request_inactive")
        previous: tuple[object, ...] = connection.execute(
            "SELECT count(*), coalesce(max(sequence),0) FROM model_attempts WHERE request_id=?",
            (start.request_id,),
        ).fetchone()
        if previous != (start.sequence - 1, start.sequence - 1):
            raise StorageError("模型尝试顺序冲突。", code="attempt_conflict")
        scope = connection.execute(
            "SELECT json_extract(evidence_json,'$.scope.owner_id') FROM "
            "model_requests WHERE request_id=?",
            (start.request_id,),
        ).fetchone()[0]
        if scope in ("behavior_review",):
            remaining, reset = analysis_work.model_budget(connection, time.time())
            if not remaining:
                raise BackgroundBudgetExceeded(
                    max(1.0, (reset or time.time() + 3600) - time.time())
                )
        connection.execute(
            "INSERT INTO model_attempts VALUES (?, ?, ?, ?, NULL, ?, NULL)",
            (start.attempt_id, start.request_id, start.sequence, encode(start), time.time()),
        )


def finish_attempt(connection: sqlite3.Connection, result: AttemptEvidence) -> None:
    """独立提交公开响应、失败和流证据，后续尝试不能抹掉此项。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        result: 本次尝试的实际响应、失败分类与有限诊断。

    Raises:
        StorageError: 模型尝试身份不存在或不匹配；模型尝试结果不能被覆盖。
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        # 计量完整逻辑正文，BLOB 去重只改变物理表示，不能赠送诊断保留额度。
        body_bytes = len(
            encode(
                {
                    "response_json": result.response_json,
                    "argument_events_json": result.argument_events_json,
                }
            ).encode("utf-8")
        )
        media: tuple[str, ...] = ()
        response = result.response_json
        if response is not None:
            response, media = intern_core_media(connection, response)
        original = _encode_authority_attempt(replace(result, response_json=response))
        metadata = dict(
            _object(
                _encode_authority_attempt(
                    replace(result, response_json=None, argument_events_json=None)
                )
            )
        )
        metadata["usage_json"] = (
            None
            if response is None or _object(response).get("usage") is None
            else encode(_object(response)["usage"])
        )
        encoded = encode(metadata)
        content = encode(
            {"response_json": response, "argument_events_json": result.argument_events_json}
        )
        row: tuple[object, ...] | None = connection.execute(
            "SELECT start_json,result_json FROM model_attempts WHERE attempt_id=?",
            (result.start.attempt_id,),
        ).fetchone()
        if row is None or row[0] != encode(result.start):
            raise StorageError("模型尝试身份不存在或不匹配。", code="attempt_conflict")
        if row[1] is not None:
            if row[1] != encoded:
                raise StorageError("模型尝试结果不能被覆盖。", code="attempt_conflict")
            archive.put(
                connection,
                "attempt",
                result.start.attempt_id,
                content,
                original,
                time.time(),
                original_bytes=body_bytes,
            )
            return
        connection.execute(
            "UPDATE model_attempts SET result_json=?,finished_at=? WHERE attempt_id=?",
            (encoded, time.time(), result.start.attempt_id),
        )
        archive.put(
            connection,
            "attempt",
            result.start.attempt_id,
            content,
            original,
            time.time(),
            original_bytes=body_bytes,
        )
        connection.executemany(
            "INSERT INTO model_attempt_media VALUES (?, ?)",
            ((result.start.attempt_id, media_id) for media_id in media),
        )
    # 诊断是可淘汰的副本；权威结果先提交，独立诊断故障不能抹去已取得的响应。
    # 原正文摘要留在权威行，过期或诊断写失败后仍能拒绝身份冲突的重复提交。
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO model_attempt_diagnostics VALUES (?,?,?,?,?,?,NULL)",
                (
                    result.start.attempt_id,
                    result.diagnostic_events_json,
                    retention.diagnostic_events_digest(result.diagnostic_events_json),
                    int(result.diagnostics_complete),
                    result.omitted_events,
                    time.time(),
                ),
            )
    except sqlite3.Error as error:
        _logger.warning(
            "模型诊断未保存；权威尝试结果已提交。",
            extra={"attempt_id": result.start.attempt_id, "error_type": type(error).__name__},
        )


def _encode_authority_attempt(result: AttemptEvidence) -> str:
    """冻结结果及原诊断摘要，诊断正文与查询时的淘汰标记不写入权威结果。"""
    value = dict(
        _object(
            encode(
                replace(
                    result,
                    diagnostic_events_json="[]",
                    diagnostics_expired_at=None,
                    diagnostics_unavailable=False,
                )
            )
        )
    )
    value.pop("body_status", None)
    value.pop("usage_json", None)
    value["diagnostic_events_digest"] = retention.diagnostic_events_digest(
        result.diagnostic_events_json
    )
    return encode(value)


def finish_request(connection: sqlite3.Connection, request_id: str, state: RequestOutcome) -> None:
    """结束逻辑状态。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。
        state: 逻辑请求最终状态，独立于各次传输尝试。

    Raises:
        StorageError: 模型请求身份不存在；模型请求结束结果冲突。

    Note:
        不修改任何独立尝试的公开结果。
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        row: tuple[object, ...] | None = connection.execute(
            "SELECT state FROM model_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise StorageError("模型请求身份不存在。", code="request_missing")
        if row[0] != "running":
            if row != (state,):
                raise StorageError("模型请求结束结果冲突。", code="request_conflict")
            return
        connection.execute(
            "UPDATE model_requests SET state=?,finished_at=? WHERE request_id=?",
            (state, time.time(), request_id),
        )


def get_request(connection: sqlite3.Connection, request_id: str) -> RequestRecord | None:
    """在单一读快照中重建公开值。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。

    Returns:
        一个逻辑模型请求的冻结材料及按实际顺序保存的尝试。

    Raises:
        StorageError: 模型请求状态损坏。

    Note:
        损坏材料显式报告，不生成空结果。
    """
    with connection:
        connection.execute("BEGIN")
        row: tuple[object, ...] | None = connection.execute(
            "SELECT evidence_json,state,created_at,finished_at FROM model_requests "
            "WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        request = decode_request(_text(row[0]))
        materials, status = archive.read(connection, "request", request_id)
        request = replace(
            request,
            materials_json=None
            if materials is None
            else restore_request_media(connection, materials),
        )
        state = row[1]
        if state not in ("running", "succeeded", "failed", "timed_out", "cancelled", "interrupted"):
            raise StorageError("模型请求状态损坏。", code="invalid_record")
        rows: list[tuple[object, ...]] = connection.execute(
            "SELECT start_json,created_at,finished_at,result_json FROM model_attempts "
            "WHERE request_id=? ORDER BY sequence",
            (request_id,),
        ).fetchall()
        attempts = tuple(
            AttemptRecord(
                decode_start(_text(item[0])),
                float(_number(item[1])),
                _optional_number(item[2]),
                None if item[3] is None else restore_attempt(connection, _text(item[3])),
            )
            for item in rows
        )
        return RequestRecord(
            request, state, _number(row[2]), _optional_number(row[3]), attempts, status
        )


_USAGE_COUNT_FIELDS = (
    "attempts",
    "reported_attempts",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "reasoning_tokens",
)

_BUCKET_STEPS_SECONDS = (
    300,
    900,
    1800,
    3600,
    7200,
    21600,
    43200,
    86400,
    2 * 86400,
    7 * 86400,
    14 * 86400,
    30 * 86400,
)


def _zero_counts() -> dict[str, int]:
    return dict.fromkeys(_USAGE_COUNT_FIELDS, 0)


def _percentile_ms(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return round(sorted_values[0], 1)
    pos = (len(sorted_values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return round(sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac, 1)


def _latency_summary(latencies_ms: list[float]) -> JsonObject:
    if not latencies_ms:
        return {
            "avg_latency_ms": None,
            "p50_latency_ms": None,
            "p95_latency_ms": None,
            "min_latency_ms": None,
            "max_latency_ms": None,
        }
    ordered = sorted(latencies_ms)
    return {
        "avg_latency_ms": round(sum(ordered) / len(ordered), 1),
        "p50_latency_ms": _percentile_ms(ordered, 0.50),
        "p95_latency_ms": _percentile_ms(ordered, 0.95),
        "min_latency_ms": round(ordered[0], 1),
        "max_latency_ms": round(ordered[-1], 1),
    }


def _select_bucket_grid(
    *, since: float, now: float, first_at: float | None, last_at: float | None
) -> tuple[float, int, int]:
    """返回 (grid_start, bucket_seconds, bucket_count)，覆盖所选查询时间窗。"""
    end_anchor = max(now, since, last_at if last_at is not None else now)
    if since > 0.0:
        span = max(end_anchor - since, 300.0)
        if span <= 2 * 3600:
            step = 300
        elif span <= 36 * 3600:
            step = 3600
        elif span <= 10 * 86400:
            step = 21600
        elif span <= 45 * 86400:
            step = 86400
        else:
            step = next((s for s in _BUCKET_STEPS_SECONDS if math.ceil(span / s) <= 28), 30 * 86400)
        count = max(1, min(32, int(math.ceil(span / step))))
        return max(0.0, end_anchor - count * step), step, count

    if first_at is None:
        step = 7200
        count = 12
        return max(0.0, end_anchor - count * step), step, count

    raw_span = max(end_anchor - first_at, 3600.0)
    step = next(
        (s for s in _BUCKET_STEPS_SECONDS if math.ceil(raw_span / s) <= 24),
        max(30 * 86400, int(math.ceil(raw_span / 24.0))),
    )
    count = max(8, min(28, int(math.ceil(raw_span / step))))
    return max(0.0, end_anchor - count * step), step, count


class _TelemetryBucket:
    __slots__ = (
        "counts",
        "requests",
        "succeeded_attempts",
        "failed_attempts",
        "timed_out_attempts",
        "running_attempts",
        "retry_attempts",
        "latencies_ms",
        "gen_output_tokens",
        "gen_duration_s",
        "last_used_at",
    )

    def __init__(self) -> None:
        self.counts: dict[str, int] = _zero_counts()
        self.requests: set[str] = set()
        self.succeeded_attempts = 0
        self.failed_attempts = 0
        self.timed_out_attempts = 0
        self.running_attempts = 0
        self.retry_attempts = 0
        self.latencies_ms: list[float] = []
        self.gen_output_tokens = 0
        self.gen_duration_s = 0.0
        self.last_used_at: float | None = None

    def add(
        self,
        *,
        request_id: str,
        sequence: int,
        created_at: float,
        duration_s: float | None,
        state: str | None,
        has_usage: bool,
        in_tok: int,
        out_tok: int,
        cached_tok: int,
        cache_write_tok: int,
        reasoning_tok: int,
    ) -> None:
        self.requests.add(request_id)
        self.counts["attempts"] += 1
        if has_usage:
            self.counts["reported_attempts"] += 1
            self.counts["input_tokens"] += in_tok
            self.counts["output_tokens"] += out_tok
            self.counts["cached_input_tokens"] += cached_tok
            self.counts["cache_write_input_tokens"] += cache_write_tok
            self.counts["reasoning_tokens"] += reasoning_tok
        if sequence > 1:
            self.retry_attempts += 1
        if state is None:
            self.running_attempts += 1
        elif state == "succeeded":
            self.succeeded_attempts += 1
        else:
            self.failed_attempts += 1
            if state == "timed_out":
                self.timed_out_attempts += 1
        if duration_s is not None:
            self.latencies_ms.append(duration_s * 1000.0)
            if has_usage and out_tok > 0 and duration_s > 0.001:
                self.gen_output_tokens += out_tok
                self.gen_duration_s += duration_s
        if self.last_used_at is None or created_at > self.last_used_at:
            self.last_used_at = created_at

    def success_rate(self) -> float | None:
        finished = self.succeeded_attempts + self.failed_attempts
        return round(self.succeeded_attempts / finished, 4) if finished > 0 else None

    def avg_tokens_per_second(self) -> float | None:
        if self.gen_duration_s <= 0.0 or self.gen_output_tokens <= 0:
            return None
        return round(self.gen_output_tokens / self.gen_duration_s, 1)


def usage_statistics(connection: sqlite3.Connection, *, since: float) -> JsonObject:
    """按尝试的开始时间汇总模型用量、延迟分位、时序趋势与认知回路分布。"""
    # 单次 SELECT 快照保证 total、models、series、scopes 与 recent_attempts 完全同源。
    # 重试计作独立尝试，只在有用量回执时累加 token；正文与诊断过期清理不影响此查询。
    now = float(time.time())
    rows: list[tuple[object, ...]] = connection.execute(
        """
        WITH attempt_rows AS (
            SELECT a.attempt_id AS attempt_id,
                   a.request_id AS request_id,
                   a.sequence AS sequence,
                   a.created_at AS created_at,
                   a.finished_at AS finished_at,
                   json_extract(a.result_json, '$.state') AS state,
                   json_extract(a.result_json, '$.usage_json') AS usage,
                   json_extract(r.evidence_json, '$.model') AS model,
                   coalesce(json_extract(r.evidence_json, '$.scope.owner_id'), 'other') AS scope,
                   coalesce(json_extract(r.evidence_json, '$.kind'), 'generation') AS kind
            FROM model_attempts a
            JOIN model_requests r ON r.request_id = a.request_id
            WHERE a.created_at >= ?
        )
        SELECT attempt_id,
               request_id,
               sequence,
               created_at,
               finished_at,
               state,
               usage IS NOT NULL AS has_usage,
               coalesce(json_extract(usage, '$.input_tokens'), 0) AS input_tokens,
               coalesce(json_extract(usage, '$.output_tokens'), 0) AS output_tokens,
               coalesce(json_extract(usage, '$.cached_input_tokens'), 0) AS cached_input_tokens,
               coalesce(json_extract(usage, '$.cache_write_input_tokens'), 0) AS cache_write_input_tokens,
               coalesce(json_extract(usage, '$.reasoning_tokens'), 0) AS reasoning_tokens,
               model,
               scope,
               kind
        FROM attempt_rows
        ORDER BY created_at ASC, sequence ASC, attempt_id ASC
        """,
        (since,),
    ).fetchall()

    overall = _TelemetryBucket()
    by_model: dict[str, _TelemetryBucket] = {}
    by_scope: dict[str, _TelemetryBucket] = {}

    parsed_rows: list[tuple[str, str, int, float, float | None, str | None, bool, int, int, int, int, int, str, str, str]] = []
    for raw in rows:
        attempt_id = as_text(raw[0])
        request_id = as_text(raw[1])
        sequence = as_int(raw[2])
        created_at = float(_number(raw[3]))
        finished_at = _optional_number(raw[4])
        duration_s = (
            finished_at - created_at
            if finished_at is not None and finished_at >= created_at
            else None
        )
        state = as_opt_text(raw[5])
        has_usage = as_int(raw[6]) != 0
        in_tok = as_int(raw[7])
        out_tok = as_int(raw[8])
        cached_tok = as_int(raw[9])
        cache_write_tok = as_int(raw[10])
        reasoning_tok = as_int(raw[11])
        model_name = as_text(raw[12])
        scope_name = as_text(raw[13]) or "other"
        kind_name = as_text(raw[14]) or "generation"

        parsed_rows.append(
            (
                attempt_id,
                request_id,
                sequence,
                created_at,
                duration_s,
                state,
                has_usage,
                in_tok,
                out_tok,
                cached_tok,
                cache_write_tok,
                reasoning_tok,
                model_name,
                scope_name,
                kind_name,
            )
        )
        overall.add(
            request_id=request_id,
            sequence=sequence,
            created_at=created_at,
            duration_s=duration_s,
            state=state,
            has_usage=has_usage,
            in_tok=in_tok,
            out_tok=out_tok,
            cached_tok=cached_tok,
            cache_write_tok=cache_write_tok,
            reasoning_tok=reasoning_tok,
        )
        by_model.setdefault(model_name, _TelemetryBucket()).add(
            request_id=request_id,
            sequence=sequence,
            created_at=created_at,
            duration_s=duration_s,
            state=state,
            has_usage=has_usage,
            in_tok=in_tok,
            out_tok=out_tok,
            cached_tok=cached_tok,
            cache_write_tok=cache_write_tok,
            reasoning_tok=reasoning_tok,
        )
        by_scope.setdefault(scope_name, _TelemetryBucket()).add(
            request_id=request_id,
            sequence=sequence,
            created_at=created_at,
            duration_s=duration_s,
            state=state,
            has_usage=has_usage,
            in_tok=in_tok,
            out_tok=out_tok,
            cached_tok=cached_tok,
            cache_write_tok=cache_write_tok,
            reasoning_tok=reasoning_tok,
        )

    first_at = parsed_rows[0][3] if parsed_rows else None
    last_at = parsed_rows[-1][3] if parsed_rows else None
    grid_start, bucket_seconds, bucket_count = _select_bucket_grid(
        since=since, now=now, first_at=first_at, last_at=last_at
    )
    time_buckets = [_TelemetryBucket() for _ in range(bucket_count)]
    for item in parsed_rows:
        (
            _aid,
            req_id,
            seq,
            c_at,
            dur_s,
            st,
            has_u,
            i_tok,
            o_tok,
            c_tok,
            cw_tok,
            r_tok,
            _m,
            _sc,
            _k,
        ) = item
        idx = int((c_at - grid_start) // bucket_seconds)
        clamped_idx = min(bucket_count - 1, max(0, idx))
        time_buckets[clamped_idx].add(
            request_id=req_id,
            sequence=seq,
            created_at=c_at,
            duration_s=dur_s,
            state=st,
            has_usage=has_u,
            in_tok=i_tok,
            out_tok=o_tok,
            cached_tok=c_tok,
            cache_write_tok=cw_tok,
            reasoning_tok=r_tok,
        )

    sorted_model_names = sorted(
        by_model,
        key=lambda m: (
            -(by_model[m].counts["input_tokens"] + by_model[m].counts["output_tokens"]),
            m,
        ),
    )
    models: list[JsonValue] = [
        {"model": m, **_usage_summary(by_model[m].counts)} for m in sorted_model_names
    ]
    model_metrics: list[JsonValue] = []
    for m in sorted_model_names:
        mb = by_model[m]
        lat = _latency_summary(mb.latencies_ms)
        model_metrics.append(
            {
                "model": m,
                "requests": len(mb.requests),
                **_usage_summary(mb.counts),
                "succeeded_attempts": mb.succeeded_attempts,
                "failed_attempts": mb.failed_attempts,
                "timed_out_attempts": mb.timed_out_attempts,
                "running_attempts": mb.running_attempts,
                "retry_attempts": mb.retry_attempts,
                "success_rate": mb.success_rate(),
                "avg_latency_ms": lat["avg_latency_ms"],
                "p50_latency_ms": lat["p50_latency_ms"],
                "p95_latency_ms": lat["p95_latency_ms"],
                "max_latency_ms": lat["max_latency_ms"],
                "avg_tokens_per_second": mb.avg_tokens_per_second(),
                "last_used_at": mb.last_used_at,
            }
        )

    sorted_scope_names = sorted(
        by_scope,
        key=lambda s: (
            -(by_scope[s].counts["input_tokens"] + by_scope[s].counts["output_tokens"]),
            -by_scope[s].counts["attempts"],
            s,
        ),
    )
    scopes: list[JsonValue] = []
    for s in sorted_scope_names:
        sb = by_scope[s]
        lat = _latency_summary(sb.latencies_ms)
        scopes.append(
            {
                "scope": s,
                "requests": len(sb.requests),
                **_usage_summary(sb.counts),
                "succeeded_attempts": sb.succeeded_attempts,
                "failed_attempts": sb.failed_attempts,
                "retry_attempts": sb.retry_attempts,
                "success_rate": sb.success_rate(),
                "avg_latency_ms": lat["avg_latency_ms"],
                "p95_latency_ms": lat["p95_latency_ms"],
            }
        )

    series: list[JsonValue] = []
    for i, tb in enumerate(time_buckets):
        b_start = round(grid_start + i * bucket_seconds, 3)
        b_end = round(b_start + bucket_seconds, 3)
        lat = _latency_summary(tb.latencies_ms)
        series.append(
            {
                "start_at": b_start,
                "end_at": b_end,
                **_usage_summary(tb.counts),
                "succeeded_attempts": tb.succeeded_attempts,
                "failed_attempts": tb.failed_attempts,
                "retry_attempts": tb.retry_attempts,
                "avg_latency_ms": lat["avg_latency_ms"],
                "p95_latency_ms": lat["p95_latency_ms"],
            }
        )

    recent_attempts: list[JsonValue] = []
    for item in reversed(parsed_rows[-12:]):
        (
            aid,
            req_id,
            seq,
            c_at,
            dur_s,
            st,
            has_u,
            i_tok,
            o_tok,
            c_tok,
            _cw_tok,
            r_tok,
            m_name,
            sc_name,
            k_name,
        ) = item
        recent_attempts.append(
            {
                "attempt_id": aid,
                "request_id": req_id,
                "model": m_name,
                "scope": sc_name,
                "kind": k_name,
                "sequence": seq,
                "outcome": st if st is not None else "running",
                "created_at": c_at,
                "latency_ms": round(dur_s * 1000.0, 1) if dur_s is not None else None,
                "has_usage": has_u,
                "input_tokens": i_tok,
                "output_tokens": o_tok,
                "total_tokens": i_tok + o_tok,
                "cached_input_tokens": c_tok,
                "reasoning_tokens": r_tok,
            }
        )

    overall_lat = _latency_summary(overall.latencies_ms)
    overview: JsonObject = {
        "requests": len(overall.requests),
        "succeeded_attempts": overall.succeeded_attempts,
        "failed_attempts": overall.failed_attempts,
        "timed_out_attempts": overall.timed_out_attempts,
        "running_attempts": overall.running_attempts,
        "retry_attempts": overall.retry_attempts,
        "success_rate": overall.success_rate(),
        **overall_lat,
        "avg_tokens_per_second": overall.avg_tokens_per_second(),
        "since": since,
        "observed_at": now,
        "bucket_seconds": bucket_seconds,
        "first_attempt_at": first_at,
        "last_attempt_at": last_at,
    }

    return {
        "total": _usage_summary(overall.counts),
        "models": models,
        "overview": overview,
        "series": series,
        "model_metrics": model_metrics,
        "scopes": scopes,
        "recent_attempts": recent_attempts,
    }


def _usage_summary(counts: dict[str, int]) -> JsonObject:
    """从累计 token 计算比率，空分母或超出输入量的缓存计数无法解释为命中率。"""
    inputs = counts["input_tokens"]
    cached = counts["cached_input_tokens"]
    # 与 Core 的 total_tokens 口径一致，缓存和推理子项单独展示，避免重复计数。
    return {
        **counts,
        "total_tokens": inputs + counts["output_tokens"],
        "cache_hit_rate": cached / inputs if 0 < inputs and cached <= inputs else None,
    }


def initialize(connection: sqlite3.Connection) -> None:
    """在调用方启动事务内记录旧请求中断，不提交或声称模型物理停止。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
    """
    connection.execute("SELECT request_id,evidence_json,state FROM model_requests LIMIT 0")
    connection.execute("SELECT attempt_id,start_json,result_json FROM model_attempts LIMIT 0")
    connection.execute("SELECT request_id,media_id FROM model_request_media LIMIT 0")
    connection.execute("SELECT binding,request_id,anchor_json FROM capacity_usage LIMIT 0")
    connection.execute("SELECT attempt_id,media_id FROM model_attempt_media LIMIT 0")
    # 启动事务由 Store 持有；恢复失败不能单独提交任何子系统。
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT start_json FROM model_attempts WHERE result_json IS NULL"
    ).fetchall()
    for row in rows:
        start = decode_start(_text(row[0]))
        result = AttemptEvidence(
            start,
            "interrupted",
            None,
            FailureEvidence("host_interrupted", "interrupted"),
            "[]",
            "[]",
            False,
            0,
        )
        original = _encode_authority_attempt(result)
        archive.put(
            connection,
            "attempt",
            start.attempt_id,
            encode({"response_json": None, "argument_events_json": "[]"}),
            original,
            time.time(),
        )
        metadata = _encode_authority_attempt(replace(result, argument_events_json=None))
        connection.execute(
            "UPDATE model_attempts SET result_json=?,finished_at=? WHERE attempt_id=?",
            (metadata, time.time(), start.attempt_id),
        )
    connection.execute(
        "UPDATE model_requests SET state='interrupted',finished_at=? WHERE state='running'",
        (time.time(),),
    )


def intern_request_media(
    connection: sqlite3.Connection,
    encoded: str,
) -> tuple[str, tuple[str, ...]]:
    """在调用方事务内缩为同库媒体引用，保持 SDK 公开消息和输入结构。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        encoded: 冻结 SDK 公开 payload 的完整 JSON 编码。

    Returns:
        当前状态文本和其实际引用的来源身份，供同一请求记录关联。

    Note:
        仅识别编译 content 中的 image_url/input_image data URI 与 input_audio。
        文本、远端 URL、工具 schema/参数和未知 Provider 项保持原文，不能因为
        字符串看起来像媒体而修改它。返回引用由调用方与请求关系同事务保存。
    """
    media: set[str] = set()
    result = _request_media(connection, encoded, media, restore=False)
    return result, tuple(sorted(media))


def restore_request_media(connection: sqlite3.Connection, encoded: str) -> str:
    """用同库 BLOB 还原原编译 payload，不取远端 URL 或创建模型请求。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        encoded: 冻结 SDK 公开 payload 的完整 JSON 编码。
    """
    return _request_media(connection, encoded, set(), restore=True)


def _request_media(
    connection: sqlite3.Connection,
    encoded: str,
    media: set[str],
    *,
    restore: bool,
) -> str:
    """两种方向沿同一 SDK 路径遍历，防止新增一种媒体只在一侧可读取。"""
    value = _object(encoded)
    payload = _mapping(value["payload"])
    result = dict(payload)
    for field in ("messages", "input"):
        messages = payload.get(field)
        if not isinstance(messages, list):
            continue
        rewritten: list[object] = []
        for raw in cast(list[object], messages):
            message = _mapping(raw)
            replacement = dict(message)
            content = message.get("content")
            if isinstance(content, list):
                replacement["content"] = [
                    _wire_media(connection, part, media, restore=restore)
                    for part in cast(list[object], content)
                ]
            rewritten.append(replacement)
        result[field] = rewritten
    return encode({**value, "payload": result})


def intern_core_media(
    connection: sqlite3.Connection,
    encoded: str,
) -> tuple[str, tuple[str, ...]]:
    """在调用方事务内缩为 Core 编码的媒体引用，供响应及检查点共用。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        encoded: 按 Engine 公开 Core 编码协议保存的完整材料 JSON。

    Returns:
        当前状态文本和其实际引用的来源身份，供同一请求记录关联。

    Note:
        输入是 encode 产生的请求、响应、消息或完整 ConversationCheckpoint。
        只沿 Core 的内容关系读取 ImageBytes/AudioBytes.data，不深入工具参数、
        Provider 元数据或 continuation。调用方负责将返回引用与其检查点或尝试
        关系同事务保存；本函数不开始、提交或回滚事务。
    """
    value = _load_json(encoded)
    media: set[str] = set()
    return encode(_core_media(connection, value, media, restore=False)), tuple(sorted(media))


def restore_core_media(connection: sqlite3.Connection, encoded: str) -> str:
    """还原同库 Core 媒体字节表示，缺失或摘要不符属于权威材料损坏。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        encoded: 按 Engine 公开 Core 编码协议保存的完整材料 JSON。
    """
    value = _load_json(encoded)
    return encode(_core_media(connection, value, set(), restore=True))


def restore_attempt(connection: sqlite3.Connection, encoded: str) -> AttemptEvidence:
    """还原权威响应，再读取可过期诊断；诊断缺口不阻断权威查询。"""
    result = decode_attempt(encoded)
    body, status = archive.read(connection, "attempt", result.start.attempt_id)
    if body is not None:
        values = _object(body)
        result = replace(
            result,
            response_json=_optional_text(values["response_json"]),
            argument_events_json=_optional_text(values["argument_events_json"]),
        )
    result = replace(result, body_status=status)
    if result.response_json is not None:
        result = replace(result, response_json=restore_core_media(connection, result.response_json))
    try:
        row: tuple[object, ...] | None = connection.execute(
            "SELECT events_json,original_events_digest,diagnostics_complete,omitted_events,"
            "expired_at "
            "FROM model_attempt_diagnostics WHERE attempt_id=?",
            (result.start.attempt_id,),
        ).fetchone()
        if row is None:
            return replace(result, diagnostics_unavailable=True)
        events, digest = _text(row[0]), _text(row[1])
        complete, omitted = _integer(row[2]), _integer(row[3])
        expired = _optional_number(row[4])
        if (
            digest != _text(_object(encoded)["diagnostic_events_digest"])
            or complete != int(result.diagnostics_complete)
            or omitted != result.omitted_events
            or (expired is None and retention.diagnostic_events_digest(events) != digest)
            or (expired is not None and events != "[]")
        ):
            raise StorageError("模型诊断材料不匹配。", code="invalid_diagnostics")
        return replace(result, diagnostic_events_json=events, diagnostics_expired_at=expired)
    except (sqlite3.Error, StorageError) as error:
        _logger.warning(
            "模型诊断不可读取；保留权威响应及原始参数。",
            extra={"attempt_id": result.start.attempt_id, "error_type": type(error).__name__},
        )
        return replace(result, diagnostics_unavailable=True)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise StorageError("公开模型材料结构损坏。", code="invalid_record")
    return _Fields(cast(Mapping[str, object], value))


def _wire_media(
    connection: sqlite3.Connection,
    value: object,
    media: set[str],
    *,
    restore: bool,
) -> object:
    """只在编译器明确的媒体槽位更换字符串，正文和工具内容不按模式猜测。"""
    part = _mapping(value)
    result = dict(part)
    if part.get("type") == "image_url":
        image = _mapping(part["image_url"])
        result["image_url"] = {
            **image,
            "url": _media_slot(connection, image["url"], media, restore=restore, data_uri=True),
        }
    elif part.get("type") == "input_image":
        result["image_url"] = _media_slot(
            connection, part["image_url"], media, restore=restore, data_uri=True
        )
    elif part.get("type") == "input_audio":
        audio = _mapping(part["input_audio"])
        result["input_audio"] = {
            **audio,
            "data": _media_slot(connection, audio["data"], media, restore=restore),
        }
    return result


def _core_media(
    connection: sqlite3.Connection,
    value: object,
    media: set[str],
    *,
    restore: bool,
) -> object:
    """恢复同库保存的完整原生媒体，缺失或损坏时保留权威错误。"""
    if isinstance(value, list):
        return [
            _core_media(connection, item, media, restore=restore)
            for item in cast(list[object], value)
        ]
    item = _mapping(value)
    result = dict(item)
    kind = item.get("type")
    fields: tuple[str, ...] = ()
    if kind == "ConversationCheckpoint":
        fields = ("history", "reminders")
    elif kind == "ConversationState":
        fields = ("messages",)
    elif kind == "ReminderState":
        fields = ("reminders",)
    elif kind == "ModelRequest":
        fields = ("messages", "instructions", "reminders")
    elif kind == "ModelResponse":
        fields = ("message",)
    elif kind in ("Message", "SystemInstruction", "SystemReminder", "ToolResultBlock"):
        fields = ("content",)
    elif kind in ("ImageBlock", "AudioBlock"):
        fields = ("source",)
    elif kind in ("ImageBytes", "AudioBytes"):
        data = _mapping(item["data"])
        if data.get("type") != "bytes":
            raise StorageError("Core 媒体字节表示损坏。", code="invalid_record")
        result["data"] = {
            **data,
            "base64": _media_slot(connection, data["base64"], media, restore=restore),
        }
    for field in fields:
        result[field] = _core_media(connection, item[field], media, restore=restore)
    return result


def _media_slot(
    connection: sqlite3.Connection,
    value: object,
    media: set[str],
    *,
    restore: bool,
    data_uri: bool = False,
) -> object:
    """引用格式仅出现于原字符串槽位，不占用工具 JSON 的命名空间。"""
    if restore and isinstance(value, Mapping):
        # Mapping 的参数尚未经过校验，先保留 object 边界再逐字段解码。
        reference = _mapping(cast(object, value))
        media_id = _text(reference["purrcept_media_id"])
        row: tuple[object, ...] | None = connection.execute(
            "SELECT content FROM media WHERE media_id=?", (media_id,)
        ).fetchone()
        if row is None or not isinstance(row[0], bytes):
            raise StorageError("模型引用媒体缺失。", code="invalid_record")
        if hashlib.sha256(row[0]).hexdigest() != media_id:
            raise StorageError("模型媒体摘要不符。", code="invalid_record")
        return _text(reference["prefix"]) + base64.b64encode(row[0]).decode("ascii")
    original = _text(value)
    if restore:
        return original
    prefix, encoded = "", original
    if data_uri:
        header, separator, body = original.partition(",")
        if not separator or not header.startswith("data:") or not header.endswith(";base64"):
            return original
        prefix, encoded = header + separator, body
    try:
        content = base64.b64decode(encoded, validate=True)
    except ValueError:
        if data_uri:
            # 不可解码的 URL 尚不是已取得像素；保留原 URL 供 SDK 自己判断。
            return original
        raise StorageError(
            "已取得音频或 Core 媒体不是有效 base64。", code="invalid_record"
        ) from None
    if base64.b64encode(content).decode("ascii") != encoded:
        if data_uri:
            # URL 原文可能采用其他表达方式。仅缩为可无损还原的规范表示，
            # 不通过重新编码改变实际模型输入。
            return original
        raise StorageError("Core 媒体编码不是规范 base64。", code="invalid_record")
    media_id = hashlib.sha256(content).hexdigest()
    existing: tuple[object, ...] | None = connection.execute(
        "SELECT content FROM media WHERE media_id=?", (media_id,)
    ).fetchone()
    if existing is None:
        connection.execute("INSERT INTO media VALUES (?, ?)", (media_id, content))
    elif existing != (content,):
        raise StorageError("已保存媒体与内容摘要不符。", code="invalid_record")
    media.add(media_id)
    return {"purrcept_media_id": media_id, "prefix": prefix}


def _object(encoded: str) -> Mapping[str, object]:
    value = _load_json(encoded)
    if not isinstance(value, dict):
        raise StorageError("模型材料对象损坏。", code="invalid_record")
    return _Fields(cast(dict[str, object], value))


def _load_json(encoded: str) -> object:
    """持久化 JSON 是独立信任边界，拒绝非有限数字及损坏结构。"""
    try:
        value: object = json.loads(
            encoded, parse_constant=_reject_constant, parse_float=_finite_float
        )
    except (ValueError, TypeError, RecursionError) as error:
        raise StorageError("模型材料 JSON 损坏。", code="invalid_record") from error
    return value


def _reject_constant(value: str) -> object:
    raise ValueError(f"非标准 JSON 常量：{value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("模型 JSON 数字必须有限。")
    return number


class _Fields(dict[str, object]):
    """持久化读取将缺字段转换为明确损坏，而不是泄漏无上下文 KeyError。"""

    def __missing__(self, key: str) -> object:
        raise StorageError(f"模型材料缺少字段：{key}", code="invalid_record")


_text = as_text


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise StorageError("模型材料数字损坏。", code="invalid_record")
    return float(value)


def _integer(value: object) -> int:
    return as_int(value, "模型材料整数损坏。", minimum=None)


def _optional_number(value: object) -> float | None:
    return None if value is None else _number(value)


_optional_text = as_opt_text


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise StorageError("模型引用列表损坏。", code="invalid_record")
    return tuple(_text(item) for item in cast(list[object], value))


def decode_request(encoded: str) -> RequestEvidence:
    """重建逻辑身份与冻结材料，不解码为可执行请求或客户端。

    Args:
        encoded: 数据库保存的一份完整 RequestEvidence JSON。

    Returns:
        可重建的完整逻辑请求材料。

    Raises:
        StorageError: 模型请求种类损坏。
    """
    row = _object(encoded)
    scope = _object(encode(row["scope"]))
    kind = row["kind"]
    if kind not in ("generation", "decision"):
        raise StorageError("模型请求种类损坏。", code="invalid_record")
    return RequestEvidence(
        _text(row["request_id"]),
        RequestScope(
            _text(scope["owner_id"]),
            _text(scope["turn_id"]),
            _strings(scope["input_ids"]),
            _strings(scope["material_refs"]),
        ),
        kind,
        _text(row["model"]),
        _optional_text(row["materials_json"]),
        _text(row["compiler"]),
        _integer(row["attempts"]),
        _number(row["attempt_seconds"]),
        _number(row["total_seconds"]),
        _optional_text(row["capacity_json"]),
    )


def decode_start(encoded: str) -> AttemptStart:
    """重建已保存开始身份，单调截止只用于本次宿主的诊断。

    Args:
        encoded: 数据库保存的一份完整 AttemptStart JSON。

    Returns:
        该次真实尝试的开始事实。
    """
    row = _object(encoded)
    return AttemptStart(
        _text(row["request_id"]),
        _text(row["attempt_id"]),
        _integer(row["sequence"]),
        _number(row["deadline"]),
    )


def decode_attempt(encoded: str) -> AttemptEvidence:
    """重建一项独立结果。

    Args:
        encoded: 数据库保存的一份完整 AttemptEvidence JSON。

    Returns:
        该次尝试实际响应或失败的不可变证据。

    Raises:
        StorageError: 模型尝试结果损坏；诊断覆盖标记损坏。

    Note:
        不将失败诊断转换成可执行助手响应。
    """
    row = _object(encoded)
    state = row["state"]
    if state not in ("succeeded", "failed", "timed_out", "cancelled", "interrupted"):
        raise StorageError("模型尝试结果损坏。", code="invalid_record")
    failure = None
    if row["failure"] is not None:
        item = _object(encode(row["failure"]))
        failure = FailureEvidence(
            _text(item["error_type"]),
            _text(item["category"]),
            None if item["status_code"] is None else _integer(item["status_code"]),
            _optional_text(item["provider_code"]),
            _optional_number(item["retry_after_seconds"]),
        )
    complete = row["diagnostics_complete"]
    unavailable = row["diagnostics_unavailable"]
    if not isinstance(complete, bool) or not isinstance(unavailable, bool):
        raise StorageError("诊断覆盖标记损坏。", code="invalid_record")
    return AttemptEvidence(
        decode_start(encode(row["start"])),
        state,
        _optional_text(row["response_json"]),
        failure,
        _optional_text(row["argument_events_json"]),
        _text(row["diagnostic_events_json"]),
        complete,
        _integer(row["omitted_events"]),
        _optional_number(row["retry_wait_seconds"]),
        _optional_number(row["diagnostics_expired_at"]),
        unavailable,
        usage_json=_optional_text(row.get("usage_json")),
    )
