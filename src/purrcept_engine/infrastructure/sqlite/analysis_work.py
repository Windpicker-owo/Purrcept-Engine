"""在唯一数据库事务中维护按用途隔离的证据复核责任。

来源版本与真正复核范围决定续页；领取只锁定当前页，不能确认理解。完成页面后
重置下一页的尝试次数，失败采用持久冷却，延期等待相关证据或人工重新安排。
模型派发额度由 model_requests 在真实尝试开始时原子核验，本模块只提供调度观察。
"""

from __future__ import annotations

import json
import sqlite3
import time

from purrcept_engine.values import decode_object

SCHEMA = (
    "CREATE TABLE analysis_work (purpose TEXT NOT NULL, evidence_id TEXT NOT NULL, "
    "version INTEGER NOT NULL, state TEXT NOT NULL, queued_at REAL NOT NULL, "
    "next_attempt_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, "
    "slice_ref TEXT, batch_id TEXT, reason TEXT, PRIMARY "
    "KEY(purpose,evidence_id,version), FOREIGN KEY(evidence_id,version) REFERENCES "
    "knowledge_evidence(evidence_id,version))",
    "CREATE INDEX analysis_work_ready ON analysis_work(purpose,state,next_attempt_at,queued_at)",
    "CREATE TABLE analysis_claims (batch_id TEXT NOT NULL, slice_ref TEXT NOT "
    "NULL, purpose TEXT NOT NULL, epoch INTEGER NOT NULL, successful_turns INTEGER "
    "NOT NULL, attempted_at REAL NOT NULL, PRIMARY KEY(batch_id,slice_ref), "
    "FOREIGN KEY(slice_ref) REFERENCES knowledge_slices(slice_ref))",
    "CREATE INDEX analysis_claims_epoch ON analysis_claims(purpose,epoch DESC)",
    "CREATE TABLE analysis_policy(purpose TEXT PRIMARY KEY,capacity_key TEXT NOT NULL)",
    "CREATE TABLE analysis_links(evidence_id TEXT NOT NULL,version INTEGER NOT "
    "NULL,relation TEXT NOT NULL,PRIMARY KEY(evidence_id,version,relation),FOREIGN "
    "KEY(evidence_id,version) REFERENCES knowledge_evidence(evidence_id,version))",
    "CREATE INDEX analysis_links_relation ON analysis_links(relation,evidence_id,version)",
    "CREATE INDEX analysis_work_source ON analysis_work(evidence_id,version,purpose,state)",
)


def register_links(
    connection: sqlite3.Connection, evidence_id: str, version: int
) -> tuple[str, ...]:
    """借用来源接管或格式迁移事务登记确切因果关系，本身不唤醒任何待办。"""
    row = connection.execute(
        "SELECT json_extract(payload_json,'$.source_json'),json_extract(payload_json,'$.kind') "
        "FROM knowledge_evidence "
        "WHERE evidence_id=? AND version=?",
        (evidence_id, version),
    ).fetchone()
    source = decode_object(row[0].encode())
    relations = {f"evidence:{evidence_id}"}
    for kind in ("input", "turn", "action", "call"):
        identity = source.get(f"{kind}_id")
        if isinstance(identity, str) and identity:
            relations.add(f"{kind}:{identity}")
    target_kind, target = source.get("target_kind"), source.get("target_id")
    if target_kind in ("action", "turn", "call") and isinstance(target, str):
        relations.add(f"{target_kind}:{target}")
    if row[1] == "input":
        relations.add(f"input:{evidence_id}")
    inputs = source.get("input_ids")
    if isinstance(inputs, list):
        relations.update(f"input:{identity}" for identity in inputs if isinstance(identity, str))
    # 来源所有者交付明确因果身份；分析账本不反向依赖输入、行动或轮次表。
    related = source.get("related_evidence_ids")
    if isinstance(related, list):
        relations.update(
            f"evidence:{identity}" for identity in related if isinstance(identity, str)
        )
    for relation in sorted(relations):
        connection.execute(
            "INSERT OR IGNORE INTO analysis_links VALUES(?,?,?)",
            (evidence_id, version, relation),
        )
    return tuple(sorted(relations))


def _wake_related(connection: sqlite3.Connection, relation: str) -> None:
    """只唤醒有确切关联的证据等待；正在冷却或处理的范围保持原预算。"""
    connection.execute(
        "UPDATE analysis_work SET state='pending',attempts=0,next_attempt_at=0,"
        "reason='related_evidence' WHERE state='waiting_evidence' AND (evidence_id,version) IN("
        "SELECT evidence_id,version FROM analysis_links WHERE relation=?)",
        (relation,),
    )


def relate_input(
    connection: sqlite3.Connection, evidence_id: str, version: int, input_id: str
) -> None:
    """由事实所有者为新证据补充输入因果索引，不能改写原文固定信封。

    只在新事实接管事务内调用；重放已有事实不能用来重置旧范围的失败额度。
    """
    relation = f"input:{input_id}"
    connection.execute(
        "INSERT OR IGNORE INTO analysis_links VALUES(?,?,?)",
        (evidence_id, version, relation),
    )
    _wake_related(connection, relation)


def enqueue(
    connection: sqlite3.Connection, evidence_id: str, version: int, queued_at: float
) -> None:
    """接管两类责任，仅用明确因果关系唤醒等待证据的旧范围。

    input/turn/action/call 身份来自来源所有者保存的 source_json。时间接近、
    新的无关来源和面板查询都不构成新证据。冷却中的可重试失败不会提前重试；
    只有已延期或耗尽的相关范围在新证据到达后重新取得三次尝试额度。
    """
    for relation in register_links(connection, evidence_id, version):
        _wake_related(connection, relation)
    connection.executemany(
        "INSERT OR IGNORE INTO "
        "analysis_work(purpose,evidence_id,version,state,queued_at) "
        "VALUES(?,?,?,'pending',?)",
        ((purpose, evidence_id, version, queued_at) for purpose in ("memory", "behavior")),
    )


def settle(connection: sqlite3.Connection, batch_id: str, *, failure: str | None = None) -> None:
    """结束实际领取的范围；已成功范围不能因为会话后置失败回退。

    本函数借用调用者事务。只有真实完成回执中的 reviewed 范围允许推进，延期不
    重试。失败最多三次；独立新来源不会重置同一来源版本的尝试计数。
    """
    rows: list[tuple[str, str, int, str, int]] = connection.execute(
        "SELECT purpose,evidence_id,version,slice_ref,attempts FROM analysis_work "
        "WHERE batch_id=? AND state='processing'",
        (batch_id,),
    ).fetchall()
    now = time.time()
    for purpose, identity, version, reference, attempts in rows:
        if failure in ("BackgroundBudgetExceeded", "host_closing"):
            _, reset = model_budget(connection, now)
            retry_at = (reset or now + 3600) if failure == "BackgroundBudgetExceeded" else now
            connection.execute(
                "UPDATE analysis_work SET "
                "state='retry_wait',attempts=max(0,attempts-1),next_attempt_at=?,reason=? "
                "WHERE purpose=? AND evidence_id=? AND version=?",
                (retry_at, failure, purpose, identity, version),
            )
            continue
        review = connection.execute(
            "SELECT state FROM knowledge_review_ranges WHERE batch_id=? AND slice_ref=?",
            (batch_id, reference),
        ).fetchone()
        if review is not None and review[0] == "reviewed":
            # 下一页只从已完成批次的连续覆盖前缀恢复，不能采用领取时的 next_cursor。
            from purrcept_engine.infrastructure.sqlite import feedback

            snapshot = feedback.knowledge.get_evidence(connection, identity, version=version)
            if snapshot is None:
                raise feedback.StorageError("已领取来源缺失。", code="invalid_record")
            total = (
                len(snapshot.text)
                if snapshot.items_json is None
                else len(json.loads(snapshot.items_json))
            )
            rows = connection.execute(
                "SELECT s.payload_json FROM knowledge_review_ranges r JOIN knowledge_slices s "
                "USING(slice_ref) JOIN reflection_batches b ON r.batch_id=b.batch_id AND "
                "r.batch_version=b.version WHERE s.evidence_id=? AND s.version=? AND "
                "r.state='reviewed' AND b.state='completed' AND "
                "coalesce(json_extract(b.payload_json,'$.purpose'),'memory')=?",
                (identity, version, purpose),
            ).fetchall()
            fragments = [
                feedback.knowledge_models.EvidenceSlice.model_validate_json(row[0]) for row in rows
            ]
            frontier = 0
            for fragment in sorted(fragments, key=lambda item: (item.start, item.end)):
                if fragment.start > frontier:
                    break
                frontier = max(frontier, fragment.end)
            media = {media_id for fragment in fragments for media_id in fragment.media_ids}
            complete = frontier >= total and {item.media_id for item in snapshot.media} <= media
            state = "completed" if complete else "pending"
            connection.execute(
                "UPDATE analysis_work SET state=?,attempts=0,next_attempt_at=0,reason=NULL "
                "WHERE purpose=? AND evidence_id=? AND version=?",
                (state, purpose, identity, version),
            )
        else:
            waiting = review is not None or attempts >= 3
            state = "waiting_evidence" if waiting else "retry_wait"
            next_at = 0 if waiting else now + (60 if attempts == 1 else 300)
            connection.execute(
                "UPDATE analysis_work SET state=?,next_attempt_at=?,reason=? WHERE purpose=? "
                "AND evidence_id=? AND version=?",
                (state, next_at, failure or "deferred", purpose, identity, version),
            )


def initialize(connection: sqlite3.Connection) -> None:
    """在启动恢复事务中保留故障尝试并释放上个进程的未决领取。"""
    connection.execute("SELECT purpose,state,next_attempt_at,attempts FROM analysis_work LIMIT 0")
    connection.execute("SELECT evidence_id,version,relation FROM analysis_links LIMIT 0")
    batches: list[tuple[str]] = connection.execute(
        "SELECT DISTINCT batch_id FROM analysis_work WHERE state='processing'"
    ).fetchall()
    for (identity,) in batches:
        settle(connection, identity, failure="process_interrupted")


def postpone_preparation(
    connection: sqlite3.Connection,
    identities: tuple[tuple[str, int], ...],
    *,
    purpose: str,
    reason: str,
) -> None:
    """借用队列事务延期未领取页；不创建传输尝试或移动真实覆盖前缀。"""
    now = time.time()
    with connection:
        for identity, version in identities:
            row = connection.execute(
                "SELECT attempts FROM analysis_work WHERE purpose=? AND evidence_id=? "
                "AND version=? AND state IN('pending','retry_wait')",
                (purpose, identity, version),
            ).fetchone()
            if row is None:
                continue
            attempts = row[0] + 1
            next_at = now + (60 if attempts == 1 else 300) if attempts < 3 else 0
            connection.execute(
                "UPDATE analysis_work SET attempts=?,state=?,next_attempt_at=?,reason=? "
                "WHERE purpose=? AND evidence_id=? AND version=?",
                (
                    attempts,
                    "retry_wait" if attempts < 3 else "waiting_evidence",
                    next_at,
                    reason,
                    purpose,
                    identity,
                    version,
                ),
            )


def model_budget(connection: sqlite3.Connection, now: float) -> tuple[int, float | None]:
    """观察过去一小时的真实后台尝试；持久记录使重启不能刷新额度。"""
    count, oldest = connection.execute(
        "SELECT count(*),min(a.created_at) FROM model_attempts a JOIN model_requests r "
        "USING(request_id) WHERE a.created_at>? AND "
        "json_extract(r.evidence_json,'$.scope.owner_id') "
        "IN('reflection','behavior_review','memory_consolidation')",
        (now - 3600,),
    ).fetchone()
    return max(0, 32 - count), None if oldest is None else float(oldest) + 3600
