"""连续会话、轮次事务、摘要维护、用量锚点与未决工作投影存储实现。"""

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import replace

import purrcept_engine.domain.evidence.models as evidence_models
import purrcept_engine.serialization as serialization
from purrcept_engine.domain.context.maintenance import (
    Reservation,
    SummaryProposal,
    SummarySource,
)
from purrcept_engine.domain.context.usage import UsageAnchor
from purrcept_engine.domain.execution.models import ActionRecord, CallRecord
from purrcept_engine.domain.subject.models import (
    SessionRecord,
    SessionScope,
    TurnOutcome,
    TurnRecord,
)
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite import activities as activity_storage
from purrcept_engine.infrastructure.sqlite import evidence as evidence_storage
from purrcept_engine.infrastructure.sqlite import execution as execution_storage
from purrcept_engine.infrastructure.sqlite import feedback as feedback_storage
from purrcept_engine.infrastructure.sqlite import maintenance as maintenance_storage
from purrcept_engine.infrastructure.sqlite import subject as subject_storage
from purrcept_engine.infrastructure.sqlite.rows import as_int, as_opt_text, as_text
from purrcept_engine.infrastructure.sqlite.store_base import StoreBase
from purrcept_engine.values import JsonObject, JsonValue, decode_object


def _active_supervision(
    connection: sqlite3.Connection,
) -> Iterator[evidence_models.SupervisionRecord]:
    """分块扫描全部仍有效且未回应的监管意见。"""
    cursor = connection.execute(
        "SELECT id FROM knowledge_supervision WHERE state='open' ORDER BY created_at,id"
    )
    while rows := cursor.fetchmany(32):
        for row in rows:
            record = evidence_storage.get_supervision(connection, as_text(row[0]))
            if record is None:
                raise StorageError("监管投影引用缺失意见。", code="invalid_record")
            if record.active:
                yield record


def _action_snapshot(
    connection: sqlite3.Connection,
    snapshot: evidence_models.EvidenceSnapshot,
    *,
    current: ActionRecord | None = None,
) -> ActionRecord:
    """按回执历史版本核验并还原即时行动快照。"""
    source = decode_object(snapshot.source_json.encode("utf-8"))
    action_id = source.get("action_id")
    if (
        snapshot.version != 1
        or not isinstance(action_id, str)
        or source != {"source": "actual_action_result", "action_id": action_id}
    ):
        raise StorageError("即时行动快照缺少确切内部来源。", code="invalid_record")
    action = current if current is not None else execution_storage.get_action(connection, action_id)
    if action is None:
        raise StorageError("即时行动快照引用缺失行动。", code="invalid_record")
    if action.action_id != action_id:
        raise StorageError("即时行动快照与给定行动身份不一致。", code="invalid_record")
    payload = decode_object(snapshot.text.encode("utf-8"))
    raw_calls = payload.get("calls")
    if not isinstance(raw_calls, list) or len(raw_calls) != len(action.calls):
        raise StorageError("即时行动快照与实际调用不一致。", code="invalid_record")
    frozen: list[CallRecord] = []
    for original, call in zip(raw_calls, action.calls, strict=True):
        if not isinstance(original, dict):
            raise StorageError("即时行动快照调用形状无效。", code="invalid_record")
        revision = original.get("receipt_revision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or not (1 <= revision <= len(call.history))
        ):
            raise StorageError("即时行动快照回执版本无效。", code="invalid_record")
        receipt = call.history[revision - 1]
        frozen.append(
            replace(
                call,
                state=receipt.state,
                receipt_revision=revision,
                finished_at=receipt.finished_at,
                result_json=receipt.result_json,
                error_type=receipt.error_type,
                error=receipt.error,
                history=call.history[:revision],
                media=receipt.media,
            )
        )
    candidate = replace(action, calls=tuple(frozen))
    if (
        snapshot.text != serialization.encode(candidate)
        or snapshot.occurred_at != action.finished_at
        or snapshot.media
        != tuple(dict.fromkeys(reference for call in candidate.calls for reference in call.media))
    ):
        raise StorageError("即时行动快照与冻结实际结果不一致。", code="invalid_record")
    return candidate


def _call_receipt_ref(
    connection: sqlite3.Connection, action: ActionRecord, call: CallRecord
) -> JsonObject | None:
    """核验可靠反馈桥接生成的当前调用原文引用。"""
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT e.evidence_id,e.version FROM knowledge_evidence e "
        "JOIN feedback_sources f USING(evidence_id,version) WHERE e.valid=1 AND "
        "json_extract(e.payload_json,'$.kind')='call_receipt' AND "
        "json_extract(json_extract(e.payload_json,'$.source_json'),'$.call_id')=? AND "
        "json_extract(json_extract(e.payload_json,'$.source_json'),'$.action_id')=? AND "
        "json_extract(json_extract(e.payload_json,'$.source_json'),'$.receipt_revision')=? "
        "ORDER BY e.created_at,e.evidence_id,e.version",
        (call.call_id, action.action_id, call.receipt_revision),
    ).fetchall()
    for row in rows:
        snapshot = evidence_storage.get_evidence(
            connection, as_text(row[0]), version=as_int(row[1])
        )
        if snapshot is None:
            raise StorageError("调用原文引用缺失可靠反馈快照。", code="invalid_record")
        expected_source = {
            "call_id": call.call_id,
            "action_id": action.action_id,
            "receipt_revision": call.receipt_revision,
        }
        expected_text = serialization.encode(
            {
                "source": "actual_call_receipt",
                "call": feedback_storage.call_material(call),
                "causal_input_id": action.input_id,
            }
        )
        if (
            snapshot.version != 1
            or snapshot.source_json != serialization.encode(expected_source)
            or snapshot.text != expected_text
            or snapshot.occurred_at != call.finished_at
            or snapshot.media != call.media
        ):
            raise StorageError("可靠调用原文与当前完整回执不一致。", code="invalid_record")
        return {"id": snapshot.evidence_id, "version": snapshot.version}
    return None


def _call_original_refs(
    connection: sqlite3.Connection, action: ActionRecord
) -> dict[str, JsonObject]:
    """解析行动内各次调用与当前版本一致的已存原文引用。"""
    originals: dict[str, JsonObject] = {}
    calls = {call.call_id: call for call in action.calls}
    cursor = connection.execute(
        "SELECT evidence_id,version FROM knowledge_evidence WHERE valid=1 AND "
        "json_extract(payload_json,'$.kind')='action_result' AND "
        "json_extract(json_extract(payload_json,'$.source_json'),'$.action_id')=? "
        "ORDER BY created_at,evidence_id,version",
        (action.action_id,),
    )
    while rows := cursor.fetchmany(32):
        for row in rows:
            snapshot = evidence_storage.get_evidence(
                connection, as_text(row[0]), version=as_int(row[1])
            )
            if snapshot is None:
                raise StorageError("调用原文引用缺失有效即时快照。", code="invalid_record")
            frozen = _action_snapshot(connection, snapshot, current=action)
            for original in frozen.calls:
                if original == calls[original.call_id]:
                    originals.setdefault(
                        original.call_id, {"id": snapshot.evidence_id, "version": snapshot.version}
                    )
    for call in action.calls:
        if call.call_id not in originals:
            reference = _call_receipt_ref(connection, action, call)
            if reference is not None:
                originals[call.call_id] = reference
    return originals


def _evidence_inputs(connection: sqlite3.Connection, references: tuple[str, ...]) -> frozenset[str]:
    """沿真实证据快照、切片与可靠回执追溯关联输入身份。"""
    encoded = json.dumps(references)
    roots = (
        "WITH roots(evidence_id,version) AS ("
        "SELECT e.evidence_id,max(e.version) FROM knowledge_evidence e WHERE e.evidence_id IN "
        "(SELECT value FROM json_each(?)) GROUP BY e.evidence_id UNION "
        "SELECT evidence_id,version FROM knowledge_slices WHERE slice_ref IN "
        "(SELECT value FROM json_each(?))) "
    )
    rows: list[tuple[object, ...]] = connection.execute(
        roots + "SELECT i.input_id FROM roots r JOIN inputs i ON i.input_id=r.evidence_id UNION "
        "SELECT a.input_id FROM roots r JOIN knowledge_evidence e USING(evidence_id,version) "
        "JOIN feedback_sources f USING(evidence_id,version) "
        "JOIN actions a ON a.action_id=json_extract("
        "json_extract(e.payload_json,'$.source_json'),'$.action_id') "
        "WHERE json_extract(e.payload_json,'$.kind')='call_receipt' AND a.input_id IS NOT NULL",
        (encoded, encoded),
    ).fetchall()
    identities = {as_text(row[0]) for row in rows}
    snapshots: list[tuple[object, ...]] = connection.execute(
        roots + "SELECT evidence_id,version FROM roots JOIN knowledge_evidence "
        "USING(evidence_id,version) WHERE json_extract(payload_json,'$.kind')='action_result'",
        (encoded, encoded),
    ).fetchall()
    for row in snapshots:
        snapshot = evidence_storage.get_evidence(
            connection, as_text(row[0]), version=as_int(row[1]), include_inactive=True
        )
        if snapshot is None:
            raise StorageError("未决来源引用缺失即时快照。", code="invalid_record")
        identity = _action_snapshot(connection, snapshot).input_id
        if identity is not None:
            identities.add(identity)
    return frozenset(identities)


def _protected_inputs(
    connection: sqlite3.Connection, selected: tuple[str, ...] | None
) -> frozenset[str]:
    """从在途调用、活动控制、未承接失败、未完成计划与监管中提取受保护输入集合。"""
    selection = "" if selected is None else " WHERE i.input_id IN (SELECT value FROM json_each(?))"
    rows: list[tuple[object, ...]] = connection.execute(
        "WITH unresolved(input_id) AS ("
        "SELECT a.input_id FROM calls c JOIN actions a ON a.action_id=c.action_id "
        "WHERE c.state IN ('prepared','started','unknown') UNION "
        "SELECT a.input_id FROM actions a JOIN calls c ON c.action_id=a.action_id "
        "JOIN activity_commands cmd ON cmd.command_id=c.call_id "
        "JOIN activities act ON act.activity_id=cmd.activity_id "
        "WHERE act.state NOT IN ('ended','completed') AND "
        "(cmd.command NOT IN ('pause','resume') OR "
        "cmd.status NOT IN ('confirmed','rejected') OR "
        "cmd.version >= act.control_version) UNION "
        "SELECT t.input_id FROM failed_turns f JOIN turn_inputs t ON t.turn_id=f.turn_id "
        "WHERE f.presented_in IS NULL) "
        "SELECT i.input_id FROM unresolved u JOIN inputs i ON i.input_id=u.input_id" + selection,
        () if selected is None else (json.dumps(selected),),
    ).fetchall()
    protected = {as_text(row[0]) for row in rows}
    selected_ids = None if selected is None else frozenset(selected)
    cursor = connection.execute(
        "SELECT json_extract(payload_json,'$.evidence') FROM knowledge_plans WHERE "
        "json_extract(payload_json,'$.state')='pending'"
    )
    while plans := cursor.fetchmany(32):
        plan_inputs = _evidence_inputs(connection, tuple(as_text(row[0]) for row in plans))
        protected.update(
            identity for identity in plan_inputs if selected_ids is None or identity in selected_ids
        )
    for record in _active_supervision(connection):
        linked: list[tuple[object, ...]] = connection.execute(
            "SELECT input_id FROM feedback_deliveries WHERE kind='supervision' AND identity=? "
            "AND input_id IS NOT NULL",
            (record.notice.id,),
        ).fetchall()
        for identity in (
            *(as_text(row[0]) for row in linked),
            *_evidence_inputs(connection, record.notice.evidence),
        ):
            if selected_ids is None or identity in selected_ids:
                protected.add(identity)
    return frozenset(protected)


class SessionStoreMixin(StoreBase):
    """负责会话状态、轮次生命周期、摘要占位、容量锚点与媒体字节读取。"""

    async def get_session_state(self, scope: SessionScope) -> SessionRecord:
        """读取会话版本与在途轮次元数据（不加载检查点正文）。"""
        return await self._submit(
            lambda connection: subject_storage.session_state(connection, scope)
        )

    async def get_session(self, scope: SessionScope) -> SessionRecord:
        """读取会话已提交版本与完整检查点历史。"""
        return await self._submit(lambda connection: subject_storage.session(connection, scope))

    async def protected_input_ids(self, input_ids: tuple[str, ...]) -> frozenset[str]:
        """查询给定输入集合中仍关联未决事项的输入身份。"""
        for identity in input_ids:
            self._validate_id(identity)
        return await self._submit(
            lambda connection: _protected_inputs(connection, input_ids),
            logical_bytes=sum(len(value.encode()) for value in input_ids),
        )

    async def subject_work(self) -> JsonObject:
        """组装当前前台会话、未决计划、有效监管、活动与未结案能力调用快照。"""

        def query(connection: sqlite3.Connection) -> JsonObject:
            session = subject_storage.session_state(connection, "foreground")
            turn = (
                None
                if session.active_turn is None
                else subject_storage.get_turn(connection, session.active_turn)
            )
            if session.active_turn is not None and turn is None:
                raise StorageError("当前会话引用缺失轮次。", code="invalid_record")
            count = as_int(
                connection.execute(
                    "SELECT count(*) FROM knowledge_plans WHERE "
                    "json_extract(payload_json,'$.state')='pending'"
                ).fetchone()[0]
            )
            plans = () if count == 0 else evidence_storage.pending_plans(connection, limit=count)
            supervision = tuple(_active_supervision(connection))
            activities = activity_storage.activity_records(connection)
            protected = _protected_inputs(connection, None)
            cursor = connection.execute(
                "SELECT action_id FROM actions a WHERE a.state='running' OR "
                "EXISTS(SELECT 1 FROM calls c WHERE c.action_id=a.action_id AND "
                "c.state IN ('prepared','started','unknown')) OR "
                "a.input_id IN (SELECT value FROM json_each(?)) ORDER BY created_at,action_id",
                (json.dumps(tuple(sorted(protected))),),
            )
            calls: list[JsonValue] = []
            while rows := cursor.fetchmany(32):
                for row in rows:
                    action = execution_storage.get_action(connection, as_text(row[0]))
                    if action is None:
                        raise StorageError("当前调用投影引用缺失行动。", code="invalid_record")
                    original_refs = _call_original_refs(connection, action)
                    for call in action.calls:
                        if call.state not in ("prepared", "started", "unknown") and (
                            action.state != "running" and action.input_id not in protected
                        ):
                            continue
                        calls.append(
                            {
                                "action_id": action.action_id,
                                "action_state": action.state,
                                "input_id": action.input_id,
                                "call_id": call.call_id,
                                "path": call.path,
                                "target_id": call.target_id,
                                "arguments_json": call.arguments_json,
                                "state": call.state,
                                "receipt_revision": call.receipt_revision,
                                "started_at": call.started_at,
                                "finished_at": call.finished_at,
                                "result_json": call.result_json,
                                "error_type": call.error_type,
                                "error": call.error,
                                "original_ref": original_refs.get(call.call_id),
                            }
                        )
            return {
                "coverage": "all_current_pending_plans; active_supervision; active_activities; "
                "unresolved_calls_and_effects_linked_to_unfinished_work",
                "assembled_at": time.time(),
                "foreground_session_version": session.version,
                "current_turn": None
                if turn is None
                else {
                    "turn_id": turn.turn_id,
                    "input_ids": list(turn.input_ids),
                    "started_at": turn.started_at,
                    "generation": turn.generation,
                },
                "plans": [item.model_dump(mode="json") for item in plans],
                "supervision": [item.model_dump(mode="json") for item in supervision],
                "activities": [
                    {
                        "activity_id": item.activity_id,
                        "version": item.version,
                        "goal": item.goal,
                        "state": item.state,
                        "run_generation": item.run_generation,
                        "remaining_seconds": item.remaining_seconds,
                        "reconciliation_required": item.needs_reconciliation,
                    }
                    for item in activities
                ],
                "calls": calls,
            }

        return await self._submit(query)

    async def reserve_summary(self, source: SummarySource) -> Reservation:
        """在历史压缩请求发出前原子占位指定范围。"""
        return await self._submit(
            lambda connection: maintenance_storage.reserve_summary(connection, source),
            logical_bytes=len(serialization.encode(source).encode("utf-8")),
            control=True,
            writing=True,
        )

    async def get_usage_anchor(self, binding: str) -> UsageAnchor | None:
        """读取指定模型与用途绑定的真实输入 token 用量锚点。"""

        def read(connection: sqlite3.Connection) -> UsageAnchor | None:
            row: tuple[object, ...] | None = connection.execute(
                "SELECT request_id,anchor_json FROM capacity_usage WHERE binding=?", (binding,)
            ).fetchone()
            if row is None:
                return None
            try:
                value = decode_object(as_text(row[1]).encode("utf-8"))
            except ValueError as error:
                raise StorageError("用量锚点编码损坏。", code="invalid_record") from error
            if value.get("type") != "UsageAnchor" or value.get("request_id") != row[0]:
                raise StorageError("用量锚点来源身份不一致。", code="invalid_record")
            prefix = value.get("prefix")
            if not isinstance(prefix, list):
                raise StorageError("用量锚点前缀形状无效。", code="invalid_record")
            try:
                return UsageAnchor(
                    as_text(value.get("request_id")),
                    as_text(value.get("compiler")),
                    as_text(value.get("static_digest")),
                    as_text(value.get("field")),
                    tuple(as_text(item) for item in prefix),
                    as_int(value.get("input_tokens")),
                    as_opt_text(value.get("history_digest")),
                )
            except ValueError as error:
                raise StorageError("用量锚点字段损坏。", code="invalid_record") from error

        return await self._submit(read)

    async def put_usage_anchor(self, binding: str, anchor: UsageAnchor) -> None:
        """为已成功提交的模型请求保存真实输入用量锚点。"""
        encoded = serialization.encode(anchor)

        def save(connection: sqlite3.Connection) -> None:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row: tuple[object, ...] | None = connection.execute(
                    "SELECT state FROM model_requests WHERE request_id=?", (anchor.request_id,)
                ).fetchone()
                if row != ("succeeded",):
                    raise StorageError("用量锚点缺少真实成功请求。", code="invalid_record")
                connection.execute(
                    "INSERT INTO capacity_usage(binding,request_id,anchor_json) VALUES(?,?,?) "
                    "ON CONFLICT(binding) DO UPDATE SET request_id=excluded.request_id, "
                    "anchor_json=excluded.anchor_json",
                    (binding, anchor.request_id, encoded),
                )

        await self._submit(save, logical_bytes=len(encoded.encode("utf-8")), writing=True)

    async def release_summary(self, source: SummarySource) -> None:
        """归还未安装摘要候选的临时占位。"""
        await self._submit(
            lambda connection: maintenance_storage.release_summary(connection, source),
            control=True,
            writing=True,
        )

    async def install_summary(self, proposal: SummaryProposal) -> int | None:
        """在会话空闲且版本匹配时归档旧区间并安装摘要检查点。"""
        return await self._submit(
            lambda connection: maintenance_storage.install_summary(connection, proposal),
            logical_bytes=len(serialization.encode(proposal).encode("utf-8")),
            control=True,
            writing=True,
        )

    async def begin_turn(
        self,
        turn_id: str,
        scope: SessionScope,
        expected_version: int,
        generation: int,
        input_ids: tuple[str, ...],
    ) -> TurnRecord:
        """原子锁定会话版本并登记在途轮次与初始输入。"""
        return await self._submit(
            lambda connection: subject_storage.begin_turn(
                connection, turn_id, scope, expected_version, generation, input_ids
            ),
            logical_bytes=len(serialization.encode(input_ids).encode("utf-8")),
            control=True,
            writing=True,
        )

    async def finish_turn(
        self,
        turn: TurnRecord,
        outcome: TurnOutcome,
        *,
        checkpoint_json: str | None = None,
        error_json: str | None = None,
        accepted_failures: tuple[str, ...] = (),
        released_inputs: tuple[str, ...] = (),
    ) -> SessionRecord:
        """原子结束轮次：安装新检查点或记录待承接失败经历。"""
        return await self._submit(
            lambda connection: subject_storage.finish_turn(
                connection,
                turn,
                outcome,
                checkpoint_json=checkpoint_json,
                error_json=error_json,
                accepted_failures=accepted_failures,
                released_inputs=released_inputs,
            ),
            logical_bytes=len((checkpoint_json or "").encode("utf-8"))
            + len((error_json or "").encode("utf-8")),
            control=True,
            writing=True,
        )

    async def failed_turns(self, scope: SessionScope) -> tuple[TurnRecord, ...]:
        """读取指定会话下尚未承接的失败轮次。"""
        return await self._submit(lambda connection: subject_storage.failures(connection, scope))

    async def get_turn(self, turn_id: str) -> TurnRecord | None:
        """按身份读取轮次记录。"""
        self._validate_id(turn_id)
        return await self._submit(lambda connection: subject_storage.get_turn(connection, turn_id))

    async def get_media(self, media_id: str) -> bytes | None:
        """读取内容寻址媒体 BLOB 并校验 SHA-256 摘要完整性。"""
        self._validate_id(media_id)
        return await self._submit(lambda connection: self._get_media(connection, media_id))

    @staticmethod
    def _get_media(connection: sqlite3.Connection, media_id: str) -> bytes | None:
        row: tuple[object, ...] | None = connection.execute(
            "SELECT content FROM media WHERE media_id = ?", (media_id,)
        ).fetchone()
        if row is None:
            return None
        content = row[0]
        if not isinstance(content, bytes) or hashlib.sha256(content).hexdigest() != media_id:
            raise StorageError("正式媒体内容与摘要不符。", code="invalid_record")
        return content
