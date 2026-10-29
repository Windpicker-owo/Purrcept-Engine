"""保存局部评审及纠偏后的真实观察机会，独立于前台接受意见的回应。

建议限定会话或能力，最多观察二十次相关前台轮次且不超过七天。每次机会生成
带来源的独立证据，由后续评审判断改善、复发或样本不足；没有观察不能推导成功。
本模块借用外层事务，不创建任务，不重发行动，也不修改人工评价的可靠投递责任。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from purrcept_engine.domain.knowledge.models import (
    BehaviorConclusion,
    EvidenceSnapshot,
    KnowledgeInputError,
    ObservationAssessment,
    ReflectionBatch,
    SupervisionNotice,
)
from purrcept_engine.domain.subject.behavior_signals import repetition_signals
from purrcept_engine.serialization import encode
from purrcept_engine.values import JsonObject, JsonValue, decode_object

SCHEMA = (
    "CREATE TABLE behavior_reviews(batch_id TEXT NOT NULL,version INTEGER NOT "
    "NULL, payload_json TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY "
    "KEY(batch_id,version), FOREIGN KEY(batch_id,version) REFERENCES "
    "reflection_batches(batch_id,version))",
    "CREATE TABLE supervision_outcomes(notice_id TEXT PRIMARY KEY REFERENCES "
    "knowledge_supervision(id), batch_id TEXT NOT NULL,dedup_key TEXT NOT "
    "NULL,opportunities INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT "
    "'insufficient_samples')",
    "CREATE INDEX supervision_dedup ON supervision_outcomes(dedup_key)",
    "CREATE TABLE supervision_observations(notice_id TEXT NOT NULL REFERENCES "
    "knowledge_supervision(id), turn_id TEXT NOT NULL REFERENCES "
    "subject_turns(turn_id),evidence_id TEXT NOT NULL, outcome TEXT,payload_json "
    "TEXT,PRIMARY KEY(notice_id,turn_id))",
    "CREATE TABLE behavior_suggestions(batch_id TEXT NOT NULL,notice_id TEXT NOT "
    "NULL REFERENCES knowledge_supervision(id),PRIMARY KEY(batch_id,notice_id))",
)


def normalize_notice(batch: ReflectionBatch, notice: SupervisionNotice) -> SupervisionNotice:
    """局部评审建议必须有可匹配范围、可观察目标与有限有效期。"""
    if notice.scope is None or notice.expected_change is None:
        raise KnowledgeInputError("纠偏建议必须声明 scope 和 expected_change。")
    expires = (batch.created_at or time.time()) + 7 * 86400
    if notice.expires_at is not None and notice.expires_at > expires:
        raise KnowledgeInputError("纠偏观察有效期不能超过七天。")
    return notice.model_copy(update={"expires_at": notice.expires_at or expires})


def notice_key(notice: SupervisionNotice) -> str:
    """同问题与范围共享去重键；证据不同不重复投递仍有效的同类建议。"""
    scope = None if notice.scope is None else notice.scope.model_dump(mode="json")
    raw = json.dumps(
        {
            "issue": " ".join(notice.issue.split()),
            "scope": scope,
            "generalized": notice.generalized,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def record_opportunities(connection: sqlite3.Connection, turn_id: str) -> None:
    """成功前台轮次完成时，同事务登记匹配建议的有限后续观察机会。"""
    from purrcept_engine.infrastructure.sqlite import knowledge

    rows = connection.execute(
        "SELECT n.payload_json,o.opportunities FROM knowledge_supervision n JOIN "
        "supervision_outcomes o ON n.id=o.notice_id WHERE o.opportunities<20 AND "
        "json_extract(n.payload_json,'$.expires_at')>? ORDER BY n.created_at LIMIT 32",
        (time.time(),),
    ).fetchall()
    turn = connection.execute(
        "SELECT started_at FROM subject_turns WHERE turn_id=?", (turn_id,)
    ).fetchone()
    for encoded, _ in rows:
        notice = SupervisionNotice.model_validate_json(encoded)
        scope = notice.scope
        if scope is None:
            continue
        created = connection.execute(
            "SELECT created_at FROM knowledge_supervision WHERE id=?", (notice.id,)
        ).fetchone()[0]
        if turn[0] <= created:
            continue
        matched = connection.execute(
            "SELECT i.input_id FROM turn_inputs t JOIN inputs i USING(input_id) WHERE "
            "t.turn_id=? AND (? IS NULL OR i.adapter_id=?) AND (? IS NULL OR "
            "i.conversation_id=?) LIMIT 1",
            (
                turn_id,
                scope.adapter_id,
                scope.adapter_id,
                scope.conversation_id,
                scope.conversation_id,
            ),
        ).fetchone()
        if matched is None:
            continue
        if (
            scope.capability_path is not None
            and connection.execute(
                "SELECT 1 FROM calls c JOIN actions a USING(action_id) JOIN turn_inputs t ON "
                "a.input_id=t.input_id WHERE t.turn_id=? AND c.path=? LIMIT 1",
                (turn_id, scope.capability_path),
            ).fetchone()
            is None
        ):
            continue
        if connection.execute(
            "SELECT 1 FROM supervision_observations WHERE notice_id=? AND turn_id=?",
            (notice.id, turn_id),
        ).fetchone():
            continue
        identity = f"behavior-observation:{notice.id}:{turn_id}"
        payload: JsonObject = {
            "notice_id": notice.id,
            "turn_id": turn_id,
            "input_id": matched[0],
            "expected_change": notice.expected_change,
        }
        from purrcept_engine.infrastructure.sqlite import execution

        action_rows = connection.execute(
            "SELECT DISTINCT a.action_id FROM actions a JOIN turn_inputs t ON "
            "a.input_id=t.input_id WHERE t.turn_id=? ORDER BY a.created_at,a.action_id",
            (turn_id,),
        ).fetchall()
        payload["actions"] = [
            decode_object(encode(action).encode())
            for (action_id,) in action_rows
            if (action := execution.get_action_in_transaction(connection, action_id)) is not None
        ]
        knowledge.put_evidence_in_transaction(
            connection,
            EvidenceSnapshot(
                evidence_id=identity,
                version=1,
                kind="behavior_observation",
                text=encode(payload),
                source_json=encode(
                    {key: value for key, value in payload.items() if key != "actions"}
                ),
                occurred_at=time.time(),
            ),
        )
        connection.execute(
            "INSERT INTO supervision_observations VALUES(?,?,?,NULL,NULL)",
            (notice.id, turn_id, identity),
        )
        connection.execute(
            "UPDATE supervision_outcomes SET opportunities=opportunities+1 WHERE notice_id=?",
            (notice.id,),
        )


def finish(
    connection: sqlite3.Connection, batch: ReflectionBatch, result: BehaviorConclusion
) -> None:
    """在结案事务内保存结果及证据核验后的观察，防止把意见回应记成改善。"""
    if (
        result.decision == "suggestion"
        and connection.execute(
            "SELECT 1 FROM behavior_suggestions WHERE batch_id=? LIMIT 1", (batch.batch_id,)
        ).fetchone()
        is None
    ):
        raise KnowledgeInputError("suggestion 必须对应本批实际保存的监管建议。")
    for observation in result.observations:
        apply_observation(connection, batch, observation)
    connection.execute(
        "INSERT INTO behavior_reviews VALUES(?,?,?,?)",
        (batch.batch_id, batch.version, result.model_dump_json(), time.time()),
    )


def apply_observation(
    connection: sqlite3.Connection, batch: ReflectionBatch, observation: ObservationAssessment
) -> None:
    """观察必须引用本次实际呈现的对应机会证据，同一机会只接受同一结论。"""
    row = connection.execute(
        "SELECT evidence_id,outcome,payload_json FROM supervision_observations WHERE "
        "notice_id=? AND turn_id=?",
        (observation.notice_id, observation.turn_id),
    ).fetchone()
    if row is None:
        raise KnowledgeInputError("观察不对应真实的相关前台轮次。")
    qualified = connection.execute(
        "SELECT 1 FROM reflection_presented p JOIN knowledge_slices s USING(slice_ref) "
        "WHERE p.batch_id=? AND p.version=? AND s.evidence_id=? AND s.slice_ref IN "
        "(SELECT value FROM json_each(?)) LIMIT 1",
        (batch.batch_id, batch.version, row[0], json.dumps(observation.evidence)),
    ).fetchone()
    if qualified is None:
        raise KnowledgeInputError("观察证据尚未在本批真实呈现。")
    if row[1] is not None and row[2] != observation.model_dump_json():
        raise KnowledgeInputError("同一观察机会已有不同结论。")
    connection.execute(
        "UPDATE supervision_observations SET outcome=?,payload_json=? WHERE "
        "notice_id=? AND turn_id=?",
        (
            observation.outcome,
            observation.model_dump_json(),
            observation.notice_id,
            observation.turn_id,
        ),
    )
    improved, recurred = connection.execute(
        "SELECT sum(outcome='improved'),sum(outcome='recurred') FROM "
        "supervision_observations WHERE notice_id=?",
        (observation.notice_id,),
    ).fetchone()
    state = (
        "recurred" if recurred else "improved" if (improved or 0) >= 3 else "insufficient_samples"
    )
    connection.execute(
        "UPDATE supervision_outcomes SET state=? WHERE notice_id=?", (state, observation.notice_id)
    )


def context(connection: sqlite3.Connection, identities: tuple[str, ...]) -> JsonObject:
    """组装有限的真实刺激、行动及观察目标，重复词统计仅作为待核验线索。"""
    from purrcept_engine.infrastructure.sqlite import human_feedback

    actions: list[JsonValue] = []
    seen: set[str] = set()
    for identity in identities:
        row = connection.execute(
            "SELECT payload_json FROM knowledge_evidence WHERE evidence_id=? ORDER BY "
            "version DESC LIMIT 1",
            (identity,),
        ).fetchone()
        if row is None:
            continue
        snapshot = EvidenceSnapshot.model_validate_json(row[0])
        source = decode_object(snapshot.source_json.encode())
        target_kind, target_id = source.get("target_kind"), source.get("target_id")
        if target_kind in ("turn", "call") and isinstance(target_id, str):
            selected = human_feedback.read_context(connection, f"{target_kind}:{target_id}")
            if selected is not None:
                actions.append(selected)
        action_id = source.get("action_id")
        input_id = source.get("input_id", identity)
        rows = connection.execute(
            "SELECT action_id FROM actions WHERE input_id=? ORDER BY created_at DESC LIMIT 8",
            (input_id if isinstance(input_id, str) else identity,),
        ).fetchall()
        candidates = ([action_id] if isinstance(action_id, str) else []) + [
            entry[0] for entry in rows
        ]
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            detail = human_feedback.read_context(connection, candidate)
            if detail is not None:
                actions.append(detail)
    # 仅将明确内置输出算入统计。未知插件的调用语义不由路径片段猜测。
    recent = connection.execute(
        "SELECT call_id,path,state,arguments_json,receipt_revision FROM calls ORDER BY "
        "prepared_at DESC,call_id DESC LIMIT 200"
    ).fetchall()
    outputs: list[tuple[str, str]] = []
    for call_id, path, state, arguments, revision in recent:
        if state != "confirmed" or path not in (
            "me.console.output",
            "me.phone.qq.send",
            "me.microphone.speak",
        ):
            continue
        values = decode_object(arguments.encode())
        text = values.get("text")
        identity = f"call:{call_id}:{revision}"
        available = (
            connection.execute(
                "SELECT 1 FROM knowledge_evidence WHERE evidence_id=? AND version=1 AND valid=1",
                (identity,),
            ).fetchone()
            is not None
        )
        if isinstance(text, str) and available:
            outputs.append((identity, text))
            if len(outputs) == 40:
                break
    return {
        "actions": actions,
        "signals": repetition_signals(tuple(outputs)),
        "coverage": "每来源最多八次关联行动；近期统计只扫描最近二百次调用，其余事实可继续取证。",
    }


def initialize(connection: sqlite3.Connection) -> None:
    """核验评审和观察账本的必要结构，不把缺表伪装成没有结果。"""
    connection.execute("SELECT batch_id,version,payload_json FROM behavior_reviews LIMIT 0")
    connection.execute(
        "SELECT notice_id,batch_id,dedup_key,opportunities,state FROM supervision_outcomes LIMIT 0"
    )
    connection.execute(
        "SELECT notice_id,turn_id,evidence_id,outcome FROM supervision_observations LIMIT 0"
    )
    connection.execute("SELECT batch_id,notice_id FROM behavior_suggestions LIMIT 0")
