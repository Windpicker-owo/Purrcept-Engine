"""保存可限期淘汰的模型正文，永久元数据仍由请求与尝试账本拥有。

正文摘要与原始提交摘要在淘汰后保留，既能解释缺口，也能拒绝身份冲突。在线清理
借用 Store 的唯一队列，保护在途请求和未安装摘要的校验材料；不删除业务证据。
空间统计由显式维护更新缓存，面板轮询不扫描全库。文件压缩只在离线维护中进行。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path

from purrcept_engine.domain.model_requests.models import BodyStatus
from purrcept_engine.errors import StorageError
from purrcept_engine.values import JsonObject, JsonValue, decode_object

BODY_SCHEMA = (
    "CREATE TABLE model_bodies(kind TEXT NOT NULL,id TEXT NOT NULL,content_json "
    "TEXT, digest TEXT NOT NULL,original_bytes INTEGER NOT NULL,record_digest TEXT "
    "NOT NULL, summary TEXT NOT NULL,created_at REAL NOT NULL,expired_at REAL,expired_reason "
    "TEXT,PRIMARY KEY(kind,id))",
    "CREATE INDEX model_bodies_retained ON model_bodies(created_at,kind,id) WHERE "
    "expired_at IS NULL",
)

TOTAL_SCHEMA = (
    "CREATE TABLE retained_body_totals(id INTEGER PRIMARY KEY CHECK(id=1),bytes INTEGER NOT NULL)",
    "CREATE TRIGGER model_body_insert AFTER INSERT ON model_bodies WHEN "
    "NEW.expired_at IS NULL BEGIN INSERT OR IGNORE INTO retained_body_totals "
    "VALUES(1,0); UPDATE retained_body_totals SET bytes=bytes+NEW.original_bytes "
    "WHERE id=1; END",
    "CREATE TRIGGER model_body_expire AFTER UPDATE OF content_json ON model_bodies "
    "BEGIN INSERT OR IGNORE INTO retained_body_totals VALUES(1,0); UPDATE "
    "retained_body_totals SET bytes=bytes+(CASE WHEN NEW.expired_at IS NULL THEN "
    "NEW.original_bytes ELSE 0 END)-(CASE WHEN OLD.expired_at IS NULL THEN "
    "OLD.original_bytes ELSE 0 END) WHERE id=1; END",
    "CREATE TRIGGER diagnostic_body_insert AFTER INSERT ON "
    "model_attempt_diagnostics WHEN NEW.expired_at IS NULL BEGIN INSERT OR IGNORE "
    "INTO retained_body_totals VALUES(1,0); UPDATE retained_body_totals SET "
    "bytes=bytes+length(CAST(NEW.events_json AS BLOB)) WHERE id=1; END",
    "CREATE TRIGGER diagnostic_body_expire AFTER UPDATE OF events_json ON "
    "model_attempt_diagnostics BEGIN INSERT OR IGNORE INTO retained_body_totals "
    "VALUES(1,0); UPDATE retained_body_totals SET bytes=bytes+(CASE WHEN "
    "NEW.expired_at IS NULL THEN length(CAST(NEW.events_json AS BLOB)) ELSE 0 "
    "END)-(CASE WHEN OLD.expired_at IS NULL THEN length(CAST(OLD.events_json AS "
    "BLOB)) ELSE 0 END) WHERE id=1; END",
)


SCHEMA = (
    "CREATE TABLE storage_statistics(id INTEGER PRIMARY KEY "
    "CHECK(id=1),payload_json TEXT NOT NULL)",
    "CREATE INDEX actions_recent ON actions(created_at DESC,action_id DESC)",
    "CREATE INDEX turn_inputs_reverse ON turn_inputs(input_id,turn_id)",
    "CREATE INDEX knowledge_index_model ON knowledge_index(model,kind,id,version)",
    "CREATE INDEX model_attempts_recent ON model_attempts(created_at,request_id)",
    "CREATE INDEX calls_recent ON calls(prepared_at DESC,call_id DESC)",
    "CREATE TABLE human_feedback_links(evidence_id TEXT NOT NULL,version INTEGER "
    "NOT NULL, target_kind TEXT NOT NULL,target_id TEXT NOT NULL,created_at REAL "
    "NOT NULL, PRIMARY KEY(evidence_id,version),FOREIGN KEY(evidence_id,version) "
    "REFERENCES knowledge_evidence(evidence_id,version))",
    "CREATE INDEX human_feedback_target ON "
    "human_feedback_links(target_kind,target_id,created_at DESC,evidence_id DESC)",
)


def digest(value: str) -> str:
    """摘要精确 UTF-8 提交字节，不把不同编码正文视为相同提交。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _summary(kind: str, content: str, size: int) -> str:
    """保存有限结构摘要，不复制提示或推测淘汰后的原文语义。"""
    label = {"request": "模型请求材料", "attempt": "尝试响应与参数", "summary": "已安装摘要副本"}[
        kind
    ]
    parts = [label, f"原始 {size} 字节"]
    if kind == "request":
        payload = decode_object(content.encode()).get("payload")
        if isinstance(payload, dict):
            for field, name in (("messages", "消息"), ("tools", "工具"), ("questions", "问题")):
                values = payload.get(field)
                if isinstance(values, list):
                    parts.append(f"{len(values)} 项{name}")
    return "；".join(parts)


def put(
    connection: sqlite3.Connection,
    kind: str,
    identity: str,
    content: str,
    record: str,
    created_at: float,
    *,
    original_bytes: int | None = None,
) -> None:
    """在记录所有者的事务中保存一份正文，重复身份仅核对原提交摘要。"""
    row = connection.execute(
        "SELECT record_digest FROM model_bodies WHERE kind=? AND id=?", (kind, identity)
    ).fetchone()
    if row is not None:
        if row[0] != digest(record):
            raise StorageError("模型正文身份与原提交冲突。", code="request_conflict")
        return
    size = len(content.encode("utf-8")) if original_bytes is None else original_bytes
    connection.execute(
        "INSERT INTO model_bodies VALUES(?,?,?,?,?,?,?,?,NULL,NULL)",
        (
            kind,
            identity,
            content,
            digest(content),
            size,
            digest(record),
            _summary(kind, content, size),
            created_at,
        ),
    )


def read(connection: sqlite3.Connection, kind: str, identity: str) -> tuple[str | None, BodyStatus]:
    """读取正文或其明确淘汰标记；缺少账本不是正常过期。"""
    row = connection.execute(
        "SELECT content_json,digest,original_bytes,expired_at,expired_reason,summary FROM "
        "model_bodies WHERE kind=? AND id=?",
        (kind, identity),
    ).fetchone()
    if row is None:
        raise StorageError("模型正文元数据缺失。", code="invalid_record")
    content, expected, size, expired, reason, summary = row
    if (content is None) != (expired is not None) or (
        content is not None and digest(content) != expected
    ):
        raise StorageError("模型正文与摘要或淘汰状态不一致。", code="invalid_record")
    return content, BodyStatus(content is not None, expected, size, expired, reason, summary)


def migrate(connection: sqlite3.Connection) -> None:
    """格式15升级事务拆出完整正文，不改变身份、结果、因果关系和媒体引用。"""
    from purrcept_engine.infrastructure.sqlite import model_requests

    connection.execute("INSERT OR IGNORE INTO retained_body_totals VALUES(1,0)")
    connection.execute(
        "UPDATE retained_body_totals SET bytes=(SELECT "
        "coalesce(sum(length(CAST(events_json AS BLOB))),0) FROM "
        "model_attempt_diagnostics WHERE expired_at IS NULL) WHERE id=1"
    )
    # 迁移仍是单个原子事务，但逐行搬运大正文，避免把整个历史载入内存。
    requests = connection.execute("SELECT request_id,evidence_json,created_at FROM model_requests")
    for identity, encoded, created in requests:
        # 先核对旧记录契约，再拆出正文；不能让置空字段掩盖既有格式损坏。
        model_requests.decode_request(encoded)
        metadata = decode_object(encoded.encode())
        content = metadata["materials_json"]
        if not isinstance(content, str):
            raise StorageError("迁移请求正文无效。", code="invalid_record")
        # 额度按还原后的完整材料计量，媒体指针不能使诊断正文绕过容量上限。
        restored = model_requests.restore_request_media(connection, content)
        put(
            connection,
            "request",
            identity,
            content,
            encoded,
            created,
            original_bytes=len(restored.encode("utf-8")),
        )
        metadata["materials_json"] = None
        connection.execute(
            "UPDATE model_requests SET evidence_json=? WHERE request_id=?",
            (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), identity),
        )
    attempts = connection.execute(
        "SELECT attempt_id,result_json,created_at FROM model_attempts WHERE result_json IS NOT NULL"
    )
    for identity, encoded, created in attempts:
        model_requests.decode_attempt(encoded)
        metadata = decode_object(encoded.encode())
        content = json.dumps(
            {
                "response_json": metadata["response_json"],
                "argument_events_json": metadata["argument_events_json"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        response = metadata["response_json"]
        restored_response = (
            None
            if response is None
            else model_requests.restore_core_media(
                connection,
                str(response),
            )
        )
        restored = json.dumps(
            {
                "response_json": restored_response,
                "argument_events_json": metadata["argument_events_json"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        put(
            connection,
            "attempt",
            identity,
            content,
            encoded,
            created,
            original_bytes=len(restored.encode("utf-8")),
        )
        response = metadata["response_json"]
        usage = None if response is None else decode_object(str(response).encode()).get("usage")
        metadata["usage_json"] = (
            None if usage is None else json.dumps(usage, ensure_ascii=False, separators=(",", ":"))
        )
        metadata["response_json"], metadata["argument_events_json"] = None, None
        connection.execute(
            "UPDATE model_attempts SET result_json=? WHERE attempt_id=?",
            (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), identity),
        )
    for owner, coverage in connection.execute(
        "SELECT owner_id,coverage_id FROM summary_attempts WHERE installed_at IS NOT NULL"
    ).fetchall():
        detach_summary(connection, owner, coverage)
    connection.execute(
        "INSERT INTO human_feedback_links SELECT "
        "evidence_id,version,'action',json_extract(json_extract(payload_json,"
        "'$.source_json'),'$.action_id'),created_at FROM knowledge_evidence "
        "WHERE json_extract(payload_json,'$.kind')='human_feedback'"
    )


def cleanup(
    connection: sqlite3.Connection,
    *,
    before: float,
    now: float,
    max_bytes: int,
    limit: int = 128,
    byte_budget: int = 16 * 1024 * 1024,
) -> JsonObject:
    """有限事务淘汰最旧可清理正文，保护根仍存在时明确报告超额。"""
    if (
        any(isinstance(value, bool) or not math.isfinite(value) for value in (before, now))
        or before > now
    ):
        raise ValueError("正文清理需要有限且有序的时间边界。")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("正文保留容量必须为非负整数。")
    if (
        isinstance(limit, bool)
        or not 1 <= limit <= 128
        or isinstance(byte_budget, bool)
        or byte_budget <= 0
    ):
        raise ValueError("正文清理必须采用有限条数和正数字节预算。")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        total = connection.execute(
            "SELECT coalesce((SELECT bytes FROM retained_body_totals WHERE id=1),0)"
        ).fetchone()[0]
        candidates = connection.execute(
            "SELECT b.kind,b.id,b.original_bytes,b.created_at FROM model_bodies b LEFT "
            "JOIN model_attempts a ON b.kind='attempt' AND a.attempt_id=b.id JOIN "
            "model_requests r ON r.request_id=CASE WHEN b.kind='request' THEN b.id ELSE "
            "a.request_id END WHERE b.expired_at IS NULL AND r.state<>'running' AND "
            "NOT EXISTS(SELECT 1 FROM summary_attempts s WHERE s.installed_at IS NULL AND "
            "(s.summary_request_id=r.request_id OR "
            "(s.owner_id=json_extract(r.evidence_json,'$.scope.owner_id') AND "
            "'summary:'||s.coverage_id=json_extract(r.evidence_json,'$.scope.turn_id')))) "
            "AND NOT EXISTS(SELECT 1 FROM subject_sessions ss WHERE "
            "ss.active_turn=json_extract(r.evidence_json,'$.scope.turn_id')) UNION ALL "
            "SELECT 'summary',id,original_bytes,created_at FROM model_bodies WHERE "
            "kind='summary' AND expired_at IS NULL UNION ALL SELECT "
            "'diagnostic',attempt_id,length(CAST(events_json AS BLOB)),created_at FROM "
            "model_attempt_diagnostics WHERE expired_at IS NULL ORDER BY 4,1,2 LIMIT ?",
            (limit + 1,),
        ).fetchall()
        removed = count = 0
        for kind, identity, size, created in candidates[:limit]:
            if created >= before and total <= max_bytes:
                break
            if count and removed + size > byte_budget:
                break
            reason = "retention_expired" if created < before else "capacity_limit"
            if kind == "diagnostic":
                connection.execute(
                    "UPDATE model_attempt_diagnostics SET events_json='[]',expired_at=? WHERE "
                    "attempt_id=?",
                    (now, identity),
                )
                total, removed, count = total - size, removed + size, count + 1
                continue
            connection.execute(
                "UPDATE model_bodies SET content_json=NULL,expired_at=?,expired_reason=? WHERE "
                "kind=? AND id=?",
                (now, reason, kind, identity),
            )
            if kind == "summary":
                owner, coverage = json.loads(identity)
                for table in ("summary_source_media", "summary_candidate_media"):
                    connection.execute(
                        f"DELETE FROM {table} WHERE owner_id=? AND coverage_id=?", (owner, coverage)
                    )
                total, removed, count = total - size, removed + size, count + 1
                continue
            table, key = (
                ("model_request_media", "request_id")
                if kind == "request"
                else ("model_attempt_media", "attempt_id")
            )
            connection.execute(f"DELETE FROM {table} WHERE {key}=?", (identity,))
            total, removed, count = total - size, removed + size, count + 1
        return {
            "expired_bodies": count,
            "removed_bytes": removed,
            "retained_bytes": total,
            "over_capacity": total > max_bytes,
            "has_more": bool(count and (len(candidates) > count)),
        }


def _media_owners(connection: sqlite3.Connection) -> tuple[tuple[str, str], ...]:
    """从实际模式取得所有媒体外键，使新增所有者默认受保护而非默认可清理。"""
    owners: list[tuple[str, str]] = []
    for (table,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall():
        for foreign in connection.execute(
            'PRAGMA foreign_key_list("' + table.replace('"', '""') + '")'
        ).fetchall():
            if foreign[2] == "media":
                owners.append((table.replace('"', '""'), foreign[3].replace('"', '""')))
    return tuple(owners)


def collect_media(
    connection: sqlite3.Connection,
    *,
    limit: int = 128,
    byte_budget: int = 16 * 1024 * 1024,
) -> int:
    """仅回收所有已声明外键引用均不存在的媒体，同事务阻止新增引用竞态。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 128:
        raise ValueError("媒体回收批次必须为1—128项。")
    if isinstance(byte_budget, bool) or not isinstance(byte_budget, int) or byte_budget <= 0:
        raise ValueError("媒体回收需要正整数字节预算。")
    predicates = [
        f'NOT EXISTS(SELECT 1 FROM "{table}" o WHERE o."{column}"=m.media_id)'
        for table, column in _media_owners(connection)
    ]
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT m.media_id,length(m.content) FROM media m WHERE "
            + " AND ".join(predicates)
            + " ORDER BY m.media_id LIMIT ?",
            (limit,),
        ).fetchall()
        selected: list[tuple[str]] = []
        removed = 0
        for identity, size in rows:
            if selected and removed + size > byte_budget:
                break
            selected.append((identity,))
            removed += size
        connection.executemany("DELETE FROM media WHERE media_id=?", selected)
    return len(selected)


def statistics(connection: sqlite3.Connection, path: Path) -> JsonObject:
    """显式维护时统计正文和可复用页，并缓存结果供轻量面板读取。"""
    pages = connection.execute("PRAGMA page_count").fetchone()[0]
    free = connection.execute("PRAGMA freelist_count").fetchone()[0]
    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    bodies = connection.execute(
        "SELECT kind,count(*),sum(original_bytes),sum(CASE WHEN expired_at IS NULL "
        "THEN original_bytes ELSE 0 END) FROM model_bodies GROUP BY kind"
    ).fetchall()
    sizes: list[JsonValue] = []
    for kind, count, original, retained in bodies:
        sizes.append(
            {"kind": kind, "records": count, "original_bytes": original, "retained_bytes": retained}
        )
    count, retained = connection.execute(
        "SELECT count(*),coalesce(sum(length(CAST(events_json AS BLOB))),0) "
        "FROM model_attempt_diagnostics WHERE expired_at IS NULL"
    ).fetchone()
    sizes.append(
        {"kind": "stream_diagnostic", "retained_records": count, "retained_bytes": retained}
    )
    result: JsonObject = {
        "measured_at": time.time(),
        "page_count": pages,
        "page_size": page_size,
        "free_pages": free,
        "reusable_bytes": free * page_size,
        "database_bytes": path.stat().st_size if path.exists() else 0,
        "wal_bytes": Path(str(path) + "-wal").stat().st_size
        if Path(str(path) + "-wal").exists()
        else 0,
        "bodies": sizes,
        "media_bytes": connection.execute(
            "SELECT coalesce(sum(length(content)),0) FROM media"
        ).fetchone()[0],
    }
    categories: list[JsonValue] = []
    for table, columns in (
        ("inputs", ("text", "context_json")),
        ("knowledge_evidence", ("payload_json",)),
        ("model_requests", ("evidence_json",)),
        ("model_attempts", ("start_json", "result_json")),
        ("capacity_usage", ("anchor_json",)),
        ("subject_sessions", ("checkpoint_json",)),
        (
            "summary_attempts",
            ("source_json", "source_checkpoint_json", "candidate_checkpoint_json", "proposal_json"),
        ),
        ("actions", ("source", "output", "result_json")),
    ):
        sizes_sql = "+".join(f"coalesce(length(CAST({column} AS BLOB)),0)" for column in columns)
        count, logical = connection.execute(
            f"SELECT count(*),coalesce(sum({sizes_sql}),0) FROM {table}"
        ).fetchone()
        categories.append({"table": table, "records": count, "logical_bytes": logical})
    result["categories"] = categories
    protected: list[JsonValue] = []
    for reason, query in (
        ("running", "SELECT request_id FROM model_requests WHERE state='running'"),
        (
            "summary_verification",
            "SELECT r.request_id FROM model_requests r JOIN summary_attempts s ON "
            "s.summary_request_id=r.request_id OR (s.owner_id="
            "json_extract(r.evidence_json,'$.scope.owner_id') AND 'summary:'||s.coverage_id="
            "json_extract(r.evidence_json,'$.scope.turn_id')) WHERE s.installed_at IS NULL",
        ),
        (
            "active_session",
            "SELECT r.request_id FROM model_requests r JOIN subject_sessions s ON "
            "s.active_turn=json_extract(r.evidence_json,'$.scope.turn_id')",
        ),
    ):
        count, size = connection.execute(
            "SELECT count(*),coalesce(sum(b.original_bytes),0) FROM model_bodies b LEFT "
            "JOIN model_attempts a ON b.kind='attempt' AND a.attempt_id=b.id WHERE "
            "b.expired_at IS NULL AND CASE WHEN b.kind='request' THEN b.id ELSE "
            "a.request_id END IN(" + query + ")"
        ).fetchone()
        protected.append({"reason": reason, "bodies": count, "bytes": size})
    result["protected"] = protected
    media_owners: list[JsonValue] = []
    for table, column in _media_owners(connection):
        count, size = connection.execute(
            f"SELECT count(*),coalesce(sum(length(m.content)),0) FROM media m WHERE "
            f'EXISTS(SELECT 1 FROM "{table}" o WHERE o."{column}"=m.media_id)'
        ).fetchone()
        media_owners.append({"reason": table, "media": count, "bytes": size})
    # 一份去重媒体可以有多种保护原因，各行并非互斥，不能相加作为总大小。
    result["protected_media"] = media_owners
    with connection:
        connection.execute(
            "INSERT INTO storage_statistics VALUES(1,?) ON CONFLICT(id) DO UPDATE SET "
            "payload_json=excluded.payload_json",
            (json.dumps(result, ensure_ascii=False),),
        )
    return result


def initialize(connection: sqlite3.Connection) -> None:
    """启动时核验必要列，避免到首次模型请求才发现缺表。"""
    connection.execute(
        "SELECT kind,id,content_json,digest,original_bytes,record_digest,summary,expired_at "
        "FROM model_bodies LIMIT 0"
    )
    connection.execute("SELECT id,bytes FROM retained_body_totals LIMIT 0")
    connection.execute("SELECT id,payload_json FROM storage_statistics LIMIT 0")
    connection.execute(
        "SELECT evidence_id,version,target_kind,target_id,created_at FROM "
        "human_feedback_links LIMIT 0"
    )


def detach_summary(connection: sqlite3.Connection, owner: str, coverage: str) -> None:
    """已安装摘要只保留验证依赖；完整源与候选转为限期材料。

    当前会话已经持有独立检查点和媒体关系。未来依赖核验只需要来源版本和
    summary_dependencies；原文正文仍由自身证据保存，不能依赖这份调试副本。
    本入口借用摘要安装或迁移的事务，不能先清空原件再独立提交正文归档。
    """
    row = connection.execute(
        "SELECT "
        "source_checkpoint_json,candidate_checkpoint_json,proposal_json,installed_at "
        "FROM summary_attempts WHERE owner_id=? AND coverage_id=?",
        (owner, coverage),
    ).fetchone()
    if row is None or row[3] is None or row[0] == "null":
        return
    content = json.dumps(
        {
            "source_checkpoint_json": row[0],
            "candidate_checkpoint_json": row[1],
            "proposal_json": row[2],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    identity = json.dumps([owner, coverage], separators=(",", ":"))
    put(connection, "summary", identity, content, content, row[3])
    connection.execute(
        "UPDATE summary_attempts SET "
        "source_checkpoint_json='null',candidate_checkpoint_json=NULL,"
        "proposal_json=NULL WHERE owner_id=? AND coverage_id=?",
        (owner, coverage),
    )
