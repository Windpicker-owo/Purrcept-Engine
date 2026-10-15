"""在 Store 唯一连接中登记摘要覆盖并原子替换已提交会话。

reserve_summary 在模型派发前保存完整冻结来源及媒体关系；同一覆盖已尝试的
事实跨重启和版本变化保留。install_summary 复核原版本、空闲条件、真实成功
模型响应、受保护后缀与完整请求缩小，候选、归档关联和会话版本同事务生效。
失败或过期不删除旧历史，不产生另一连接、自动迁移或重新计算摘要。
"""

from __future__ import annotations

import sqlite3
import time

from purrcept_core.models import ConversationCheckpoint, Message

from purrcept_engine.domain.context.checkpoints import encode_checkpoint
from purrcept_engine.domain.context.maintenance import (
    Reservation,
    SummaryError,
    SummaryProposal,
    SummarySource,
    candidate_checkpoint,
    validate_summary_proposal,
)
from purrcept_engine.domain.context.summary_materials import summary_message
from purrcept_engine.domain.context.summary_references import summary_references
from purrcept_engine.domain.subject.models import SessionScope
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite.checkpoints import intern_checkpoint, restore_checkpoint
from purrcept_engine.infrastructure.sqlite.knowledge import get_evidence
from purrcept_engine.infrastructure.sqlite.model_requests import (
    decode_attempt,
    decode_request,
    restore_core_media,
)
from purrcept_engine.serialization import encode
from purrcept_engine.values import JsonObject, JsonValue, decode_object

SCHEMA = (
    "CREATE TABLE summary_attempts (owner_id TEXT NOT NULL REFERENCES subject_sessions(scope), "
    "coverage_id TEXT NOT NULL,source_version INTEGER NOT NULL,source_json TEXT NOT NULL, "
    "source_checkpoint_json TEXT NOT NULL,candidate_checkpoint_json TEXT,proposal_json TEXT, "
    "summary_request_id TEXT REFERENCES model_requests(request_id),attempted_at REAL NOT NULL, "
    "installed_at REAL,installed_version INTEGER,PRIMARY KEY(owner_id,coverage_id))",
    "CREATE TABLE summary_source_media (owner_id TEXT NOT NULL,coverage_id TEXT NOT NULL, "
    "media_id TEXT NOT NULL REFERENCES media(media_id),PRIMARY KEY(owner_id,coverage_id,media_id), "
    "FOREIGN KEY(owner_id,coverage_id) REFERENCES summary_attempts(owner_id,coverage_id))",
    "CREATE TABLE summary_candidate_media (owner_id TEXT NOT NULL,coverage_id TEXT NOT NULL, "
    "media_id TEXT NOT NULL REFERENCES media(media_id),PRIMARY KEY(owner_id,coverage_id,media_id), "
    "FOREIGN KEY(owner_id,coverage_id) REFERENCES summary_attempts(owner_id,coverage_id))",
    "CREATE TABLE summary_dependencies (owner_id TEXT NOT NULL,coverage_id TEXT NOT NULL, "
    "kind TEXT NOT NULL CHECK(kind IN('evidence','summary')),id TEXT NOT NULL, "
    "version INTEGER NOT NULL, "
    "PRIMARY KEY(owner_id,coverage_id,kind,id,version), "
    "FOREIGN KEY(owner_id,coverage_id) REFERENCES summary_attempts(owner_id,coverage_id))",
)


def _scope(owner_id: str) -> SessionScope:
    if owner_id not in ("foreground", "reflection"):
        raise SummaryError("摘要所有者不是正式前台或反思会话。")
    return owner_id


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise StorageError("摘要记录文本字段损坏。", code="invalid_record")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StorageError("摘要记录版本字段损坏。", code="invalid_record")
    return value


def _source_identity(source: SummarySource) -> str:
    """覆盖与保护字段显式入账，完整原检查点另存媒体引用编码，避免重复嵌入BLOB。"""
    return encode(
        {
            "owner_id": source.owner_id,
            "source_version": source.source_version,
            "end_turn": source.end_turn,
            "end_message": source.end_message,
            "coverage_id": source.coverage_id,
            "protected_turns": source.protected_turns,
            "protected_message_indices": source.protected_message_indices,
        }
    )


def _current(
    connection: sqlite3.Connection, source: SummarySource
) -> tuple[Reservation, ConversationCheckpoint | None]:
    """读取同事务版本和真实检查点；不存在或版本变化是过期，坏记录是故障。"""
    row: tuple[object, ...] | None = connection.execute(
        "SELECT version,checkpoint_json,active_turn FROM subject_sessions WHERE scope=?",
        (_scope(source.owner_id),),
    ).fetchone()
    if row is None or _integer(row[0]) != source.source_version:
        return "stale", None
    if row[2] is not None:
        return "busy", None
    if row[1] is None:
        raise SummaryError("摘要没有真实已提交的来源检查点。")
    current = restore_checkpoint(connection, _text(row[1]))
    if current != source.checkpoint:
        raise SummaryError("摘要来源与该版本已提交检查点不一致。")
    return "reserved", current


def _dependencies(
    connection: sqlite3.Connection, source: SummarySource
) -> tuple[tuple[str, str, int], ...]:
    """冻结每个真实来源的版本，旧摘要引用指向已安装归档而不是新外部证据。"""
    result: list[tuple[str, str, int]] = []
    for reference in summary_references(source):
        if reference.startswith("summary:"):
            coverage = reference[len("summary:") :]
            row: tuple[object, ...] | None = connection.execute(
                "SELECT installed_version FROM summary_attempts WHERE owner_id=? AND coverage_id=?",
                (source.owner_id, coverage),
            ).fetchone()
            if row is None or row[0] is None:
                raise SummaryError("原历史引用的摘要尚未实际安装。")
            version = _integer(row[0])
            if version > source.source_version:
                raise SummaryError("来源引用的摘要比冻结会话更新。")
            result.append(("summary", coverage, version))
            _check_dependencies(connection, source.owner_id, coverage)
        else:
            # 本标记只由宿主正式输入装配生成，Input ID 的发生事实固定在version1；
            # 后来同名材料不能成为过去消息的来源。其他引用须先明确版本表示。
            snapshot = get_evidence(connection, reference, version=1, include_inactive=True)
            if snapshot is None or not snapshot.valid:
                raise SummaryError("原历史的正式来源已经失效。")
            result.append(("evidence", reference, snapshot.version))
    return tuple(result)


def _check_dependencies(connection: sqlite3.Connection, owner_id: str, coverage_id: str) -> None:
    """沿实际已安装版本链迭代复查原始来源，不因反复压缩把撤销事实洗掉。"""
    pending = [coverage_id]
    seen: set[str] = set()
    while pending:
        coverage = pending.pop()
        if coverage in seen:
            continue
        seen.add(coverage)
        source_row: tuple[object, ...] | None = connection.execute(
            "SELECT source_version FROM summary_attempts WHERE owner_id=? AND coverage_id=?",
            (owner_id, coverage),
        ).fetchone()
        if source_row is None:
            raise StorageError("摘要依赖缺少归档来源。", code="invalid_record")
        source_version = _integer(source_row[0])
        rows: list[tuple[object, ...]] = connection.execute(
            "SELECT kind,id,version FROM summary_dependencies WHERE owner_id=? AND coverage_id=?",
            (owner_id, coverage),
        ).fetchall()
        for kind, identity, version in rows:
            if kind == "evidence":
                snapshot = get_evidence(
                    connection, _text(identity), version=_integer(version), include_inactive=True
                )
                if snapshot is None:
                    raise StorageError("摘要依赖缺少原文版本。", code="invalid_record")
                if not snapshot.valid:
                    raise SummaryError("摘要依赖的原文版本已经撤销。")
            elif kind == "summary":
                parent: tuple[object, ...] | None = connection.execute(
                    "SELECT installed_version FROM summary_attempts "
                    "WHERE owner_id=? AND coverage_id=?",
                    (owner_id, identity),
                ).fetchone()
                # 引用版本严格早于该摘要的安装版本，避免可写循环成为永久资格。
                if parent is None or parent[0] != version or _integer(version) > source_version:
                    raise StorageError("摘要版本依赖不符合历史顺序。", code="invalid_record")
                pending.append(_text(identity))
            else:
                raise StorageError("摘要来源类别损坏。", code="invalid_record")


def reserve_summary(connection: sqlite3.Connection, source: SummarySource) -> Reservation:
    """同事务取得一次覆盖资格并保留完整原历史，查询不会安装摘要或重置预算。"""
    _scope(source.owner_id)
    if (
        isinstance(source.source_version, bool)
        or not isinstance(source.source_version, int)
        or source.source_version < 0
    ):
        raise SummaryError("摘要来源版本必须是非负整数。")
    candidate_checkpoint(source, Message.user("来源保护核验占位"))
    encoded = encode_checkpoint(source.checkpoint)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        state, _ = _current(connection, source)
        if state != "reserved":
            return state
        existing = connection.execute(
            "SELECT 1 FROM summary_attempts WHERE owner_id=? AND coverage_id=?",
            (source.owner_id, source.coverage_id),
        ).fetchone()
        if existing is not None:
            return "already_attempted"
        dependencies = _dependencies(connection, source)
        archived, media = intern_checkpoint(connection, encoded)
        connection.execute(
            "INSERT INTO summary_attempts(owner_id,coverage_id,source_version,source_json, "
            "source_checkpoint_json,attempted_at) VALUES(?,?,?,?,?,?)",
            (
                source.owner_id,
                source.coverage_id,
                source.source_version,
                _source_identity(source),
                archived,
                time.time(),
            ),
        )
        connection.executemany(
            "INSERT INTO summary_source_media VALUES(?,?,?)",
            ((source.owner_id, source.coverage_id, media_id) for media_id in media),
        )
        connection.executemany(
            "INSERT INTO summary_dependencies VALUES(?,?,?,?,?)",
            (
                (source.owner_id, source.coverage_id, kind, identity, version)
                for kind, identity, version in dependencies
            ),
        )
    return "reserved"


def _object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        raise StorageError("摘要模型响应对象形状损坏。", code="invalid_record")
    return value


def _response_summary(response_json: str) -> str:
    """核验已保存的成功响应，文字须来自实际TextBlock而不是候选自行声称。"""
    response = decode_object(response_json.encode())
    if response.get("type") != "ModelResponse" or response.get("finish_reason") != "stop":
        raise SummaryError("摘要请求没有完整STOP响应。")
    message = _object(response.get("message"))
    if message.get("type") != "Message" or message.get("role") != "assistant":
        raise StorageError("摘要模型响应缺少assistant消息。", code="invalid_record")
    raw = message.get("content")
    if not isinstance(raw, list):
        raise StorageError("摘要模型响应内容形状损坏。", code="invalid_record")
    texts: list[str] = []
    for value in raw:
        block = _object(value)
        if block.get("type") == "TextBlock":
            texts.append(_text(block.get("text")))
        elif block.get("type") != "ReasoningBlock":
            raise SummaryError("摘要响应包含工具或非文本正文。")
    try:
        result = decode_object("".join(texts).encode())
    except ValueError as error:
        raise SummaryError("摘要响应正文不是完整JSON对象。") from error
    summary = result.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise SummaryError("摘要响应没有非空summary正文。")
    return summary


def _verified_response(connection: sqlite3.Connection, proposal: SummaryProposal) -> None:
    """绑定实际独立摘要请求和最后成功尝试，不接受其他会话或普通聊天响应借用。"""
    row: tuple[object, ...] | None = connection.execute(
        "SELECT evidence_json,state FROM model_requests WHERE request_id=?",
        (proposal.summary_request_id,),
    ).fetchone()
    if row is None or row[1] != "succeeded":
        raise SummaryError("摘要尚无已提交的成功模型请求。")
    request = decode_request(_text(row[0]))
    reference = "summary:" + proposal.source.coverage_id
    if (
        request.kind != "generation"
        or request.scope.owner_id != proposal.source.owner_id
        or request.scope.turn_id != reference
        or request.scope.material_refs != (reference,)
    ):
        raise SummaryError("摘要请求与冻结来源的因果身份不一致。")
    attempt: tuple[object, ...] | None = connection.execute(
        "SELECT result_json FROM model_attempts WHERE request_id=? ORDER BY sequence DESC LIMIT 1",
        (proposal.summary_request_id,),
    ).fetchone()
    if attempt is None or attempt[0] is None:
        raise SummaryError("摘要模型请求缺少实际成功尝试。")
    result = decode_attempt(_text(attempt[0]))
    if result.state != "succeeded" or result.response_json is None:
        raise SummaryError("摘要最后尝试没有已确认正文。")
    summary = _response_summary(restore_core_media(connection, result.response_json))
    if proposal.summary != summary_message(proposal.source, summary):
        raise SummaryError("摘要候选正文或来源标签与实际响应不一致。")


def install_summary(connection: sqlite3.Connection, proposal: SummaryProposal) -> int | None:
    """版本及idle仍有效时原子保存候选与归档关系，返回提交完成的新会话版本。"""
    validate_summary_proposal(proposal)
    source = proposal.source
    _scope(source.owner_id)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        state, current = _current(connection, source)
        if state != "reserved":
            return None
        row: tuple[object, ...] | None = connection.execute(
            "SELECT source_json,source_checkpoint_json,installed_version FROM summary_attempts "
            "WHERE owner_id=? AND coverage_id=?",
            (source.owner_id, source.coverage_id),
        ).fetchone()
        if row is None:
            raise SummaryError("摘要来源覆盖没有在模型开始前保留尝试资格。")
        if (
            _text(row[0]) != _source_identity(source)
            or restore_checkpoint(connection, _text(row[1])) != current
        ):
            raise SummaryError("摘要尝试的冻结保护或完整来源被改变。")
        if row[2] is not None:
            raise StorageError("已安装摘要仍指向旧会话版本。", code="invalid_record")
        _check_dependencies(connection, source.owner_id, source.coverage_id)
        _verified_response(connection, proposal)
        candidate, media = intern_checkpoint(connection, encode_checkpoint(proposal.candidate))
        next_version = source.source_version + 1
        proposal_json = encode(
            {
                "source": _source_identity(source),
                "summary": proposal.summary,
                "summary_request_id": proposal.summary_request_id,
                "before": proposal.before,
                "after": proposal.after,
            }
        )
        connection.execute(
            "UPDATE summary_attempts SET candidate_checkpoint_json=?,proposal_json=?, "
            "summary_request_id=?,"
            "installed_at=?,installed_version=? WHERE owner_id=? AND coverage_id=?",
            (
                candidate,
                proposal_json,
                proposal.summary_request_id,
                time.time(),
                next_version,
                source.owner_id,
                source.coverage_id,
            ),
        )
        connection.executemany(
            "INSERT INTO summary_candidate_media VALUES(?,?,?)",
            ((source.owner_id, source.coverage_id, media_id) for media_id in media),
        )
        connection.execute(
            "UPDATE subject_sessions SET version=?,checkpoint_json=? WHERE scope=?",
            (next_version, candidate, source.owner_id),
        )
        connection.execute("DELETE FROM session_media WHERE scope=?", (source.owner_id,))
        connection.executemany(
            "INSERT INTO session_media VALUES(?,?)",
            ((source.owner_id, media_id) for media_id in media),
        )
    return next_version


def initialize(connection: sqlite3.Connection) -> None:
    """只核验当前格式必需表；尝试覆盖跨重启保留，不自动迁移或重新计算。"""
    for table in (
        "summary_attempts",
        "summary_source_media",
        "summary_candidate_media",
        "summary_dependencies",
    ):
        connection.execute(f"SELECT * FROM {table} LIMIT 0")
