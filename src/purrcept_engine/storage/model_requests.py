"""保存逻辑模型请求、独立尝试及可取得的公开材料。

本模块定义主体交给 Store 的不可变值与单连接 SQL 事务，不打开连接、不调用
模型，也不决定重试。请求材料和完整响应属于权威事实；详细流诊断另带覆盖
标记。开始记录先于外部请求，结果记录不能覆盖旧尝试。启动恢复只标记中断，
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
    FailureEvidence,
    RequestEvidence,
    RequestOutcome,
    RequestRecord,
    RequestScope,
)
from purrcept_engine.errors import StorageError
from purrcept_engine.serialization import encode
from purrcept_engine.storage import retention

_logger = logging.getLogger(__name__)


SCHEMA = (
    "CREATE TABLE model_requests (request_id TEXT PRIMARY KEY, evidence_json TEXT NOT NULL, "
    "state TEXT NOT NULL CHECK(state IN "
    "('running','succeeded','failed','timed_out','cancelled','interrupted')), "
    "created_at REAL NOT NULL, finished_at REAL)",
    "CREATE INDEX model_requests_scope ON model_requests "
    "(json_extract(evidence_json,'$.scope.owner_id'),json_extract(evidence_json,'$.scope.turn_id'))",
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
)


def begin_request(connection: sqlite3.Connection, request: RequestEvidence) -> None:
    """原子保存逻辑材料；同身份同材料幂等，不覆盖已有材料。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        materials, media = intern_request_media(connection, request.materials_json)
        encoded = encode(replace(request, materials_json=materials))
        row: tuple[object, ...] | None = connection.execute(
            "SELECT evidence_json FROM model_requests WHERE request_id=?", (request.request_id,)
        ).fetchone()
        if row is not None:
            if row != (encoded,):
                raise StorageError("模型请求身份与材料冲突。", code="request_conflict")
            return
        connection.execute(
            "INSERT INTO model_requests VALUES (?, ?, 'running', ?, NULL)",
            (request.request_id, encoded, time.time()),
        )
        connection.executemany(
            "INSERT INTO model_request_media VALUES (?, ?)",
            ((request.request_id, media_id) for media_id in media),
        )


def begin_attempt(connection: sqlite3.Connection, start: AttemptStart) -> None:
    """一次开始门槛只能提交一次，重复身份不提供重复发送许可。"""
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
        connection.execute(
            "INSERT INTO model_attempts VALUES (?, ?, ?, ?, NULL, ?, NULL)",
            (start.attempt_id, start.request_id, start.sequence, encode(start), time.time()),
        )


def finish_attempt(connection: sqlite3.Connection, result: AttemptEvidence) -> None:
    """独立提交公开响应、失败和流证据，后续尝试不能抹掉此项。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        media: tuple[str, ...] = ()
        response = result.response_json
        if response is not None:
            response, media = intern_core_media(connection, response)
        encoded = _encode_authority_attempt(replace(result, response_json=response))
        row: tuple[object, ...] | None = connection.execute(
            "SELECT start_json,result_json FROM model_attempts WHERE attempt_id=?",
            (result.start.attempt_id,),
        ).fetchone()
        if row is None or row[0] != encode(result.start):
            raise StorageError("模型尝试身份不存在或不匹配。", code="attempt_conflict")
        if row[1] is not None:
            if row[1] != encoded:
                raise StorageError("模型尝试结果不能被覆盖。", code="attempt_conflict")
            return
        connection.execute(
            "UPDATE model_attempts SET result_json=?,finished_at=? WHERE attempt_id=?",
            (encoded, time.time(), result.start.attempt_id),
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
    value["diagnostic_events_digest"] = retention.diagnostic_events_digest(
        result.diagnostic_events_json
    )
    return encode(value)


def finish_request(connection: sqlite3.Connection, request_id: str, state: RequestOutcome) -> None:
    """结束逻辑状态；不修改任何独立尝试的公开结果。"""
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
    """在单一读快照中重建公开值；损坏材料显式报告，不生成空结果。"""
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
        request = replace(
            request, materials_json=restore_request_media(connection, request.materials_json)
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
                None if item[3] is None else _restore_attempt(connection, _text(item[3])),
            )
            for item in rows
        )
        return RequestRecord(request, state, _number(row[2]), _optional_number(row[3]), attempts)


def initialize(connection: sqlite3.Connection) -> None:
    """在调用方启动事务内记录旧请求中断，不提交或声称模型物理停止。"""
    connection.execute("SELECT request_id,evidence_json,state FROM model_requests LIMIT 0")
    connection.execute("SELECT attempt_id,start_json,result_json FROM model_attempts LIMIT 0")
    connection.execute("SELECT request_id,media_id FROM model_request_media LIMIT 0")
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
        connection.execute(
            "UPDATE model_attempts SET result_json=?,finished_at=? WHERE attempt_id=?",
            (_encode_authority_attempt(result), time.time(), start.attempt_id),
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

    仅识别编译 content 中的 image_url/input_image data URI 与 input_audio。
    文本、远端 URL、工具 schema/参数和未知 Provider 项保持原文，不能因为
    字符串看起来像媒体而修改它。返回引用由调用方与请求关系同事务保存。
    """
    media: set[str] = set()
    result = _request_media(connection, encoded, media, restore=False)
    return result, tuple(sorted(media))


def restore_request_media(connection: sqlite3.Connection, encoded: str) -> str:
    """用同库 BLOB 还原原编译 payload，不取远端 URL 或创建模型请求。"""
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

    输入是 encode 产生的请求、响应、消息或完整 ConversationCheckpoint。
    只沿 Core 的内容关系读取 ImageBytes/AudioBytes.data，不深入工具参数、
    Provider 元数据或 continuation。调用方负责将返回引用与其检查点或尝试
    关系同事务保存；本函数不开始、提交或回滚事务。
    """
    value = _load_json(encoded)
    media: set[str] = set()
    return encode(_core_media(connection, value, media, restore=False)), tuple(sorted(media))


def restore_core_media(connection: sqlite3.Connection, encoded: str) -> str:
    """还原同库 Core 媒体字节表示，缺失或摘要不符属于权威材料损坏。"""
    value = _load_json(encoded)
    return encode(_core_media(connection, value, set(), restore=True))


def _restore_attempt(connection: sqlite3.Connection, encoded: str) -> AttemptEvidence:
    """还原权威响应，再读取可过期诊断；诊断缺口不阻断权威查询。"""
    result = decode_attempt(encoded)
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


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise StorageError("模型材料字符串损坏。", code="invalid_record")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise StorageError("模型材料数字损坏。", code="invalid_record")
    return float(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError("模型材料整数损坏。", code="invalid_record")
    return value


def _optional_number(value: object) -> float | None:
    return None if value is None else _number(value)


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise StorageError("模型引用列表损坏。", code="invalid_record")
    return tuple(_text(item) for item in cast(list[object], value))


def decode_request(encoded: str) -> RequestEvidence:
    """重建逻辑身份与冻结材料，不解码为可执行请求或客户端。"""
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
        _text(row["materials_json"]),
        _text(row["compiler"]),
        _integer(row["attempts"]),
        _number(row["attempt_seconds"]),
        _number(row["total_seconds"]),
        _optional_text(row["capacity_json"]),
    )


def decode_start(encoded: str) -> AttemptStart:
    """重建已保存开始身份，单调截止只用于本次宿主的诊断。"""
    row = _object(encoded)
    return AttemptStart(
        _text(row["request_id"]),
        _text(row["attempt_id"]),
        _integer(row["sequence"]),
        _number(row["deadline"]),
    )


def decode_attempt(encoded: str) -> AttemptEvidence:
    """重建一项独立结果；不将失败诊断转换成可执行助手响应。"""
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
        _text(row["argument_events_json"]),
        _text(row["diagnostic_events_json"]),
        complete,
        _integer(row["omitted_events"]),
        _optional_number(row["retry_wait_seconds"]),
        _optional_number(row["diagnostics_expired_at"]),
        unavailable,
    )
