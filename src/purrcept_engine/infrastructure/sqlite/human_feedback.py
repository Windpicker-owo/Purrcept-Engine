"""在原有证据和反馈账本保存人工审查，不创建另一套历史或调度循环。

HumanFeedbackLedger 提供行动分页、完整审查材料和评价提交。材料包含行动发生时
所属轮次的全部刺激及逐调用参数、回执；提交再次核验材料摘要，冻结审查者实际
看到的行动版本，原始刺激通过不可变输入身份引用。评价、反思优先来源和前台 outbox
在同一事务提交，输入额度不足时沿原 FeedbackService 延后交接。
原始媒体仍由 Store 的 BLOB 持有。
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from purrcept_engine.domain.controls.models import ControlError
from purrcept_engine.domain.knowledge.models import EvidenceSnapshot
from purrcept_engine.domain.subject.human_feedback import HumanFeedback
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite import execution, feedback, knowledge
from purrcept_engine.infrastructure.sqlite.feedback_port import Submit
from purrcept_engine.infrastructure.sqlite.projections import action_projection
from purrcept_engine.serialization import encode
from purrcept_engine.values import JsonObject, JsonValue, decode_object, encode_json


def read_context(connection: sqlite3.Connection, action_id: str) -> JsonObject | None:
    """读取共用完整快照，提交复核时借用外层事务，不能提前提交评价责任。"""
    if connection.in_transaction:
        return _target_context(connection, action_id)
    with connection:
        connection.execute("BEGIN")
        return _target_context(connection, action_id)


def _read_context(connection: sqlite3.Connection, action_id: str) -> JsonObject | None:
    """按行动开始时的轮次恢复全部刺激，不能用单一锚点冒充合批输入。"""
    action = execution.get_action_in_transaction(connection, action_id)
    if action is None:
        return None
    turn: tuple[str] | None = connection.execute(
        "SELECT t.turn_id FROM subject_turns t JOIN turn_inputs i USING(turn_id) "
        "WHERE t.scope='foreground' AND i.input_id=? AND t.started_at<=? "
        "ORDER BY t.started_at DESC,t.turn_id DESC LIMIT 1",
        (action.input_id, action.created_at),
    ).fetchone()
    unresolved: str | None = None
    if turn is None:
        # 手动行动可以只有外部关联身份；没有正式输入记录不等同于损坏既有轮次。
        exists = (
            action.input_id is not None
            and connection.execute(
                "SELECT 1 FROM inputs WHERE input_id=?", (action.input_id,)
            ).fetchone()
            is not None
        )
        input_ids = (action.input_id,) if exists and action.input_id is not None else ()
        unresolved = action.input_id if action.input_id is not None and not exists else None
    else:
        rows: list[tuple[str]] = connection.execute(
            "SELECT input_id FROM turn_inputs WHERE turn_id=? ORDER BY ordinal", turn
        ).fetchall()
        input_ids = tuple(row[0] for row in rows)

    stimuli = _stimuli(connection, input_ids)
    return {
        "turn_id": None if turn is None else turn[0],
        "unresolved_input_id": unresolved,
        "stimuli": stimuli,
        "action": decode_object(encode(action).encode()),
        "actions": [],
        "target": {"kind": "action", "id": action_id, "state": action.state},
    }


def _target_context(connection: sqlite3.Connection, identity: str) -> JsonObject | None:
    """将行动、调用或轮次投影为同一审查契约，始终保留真实目标种类。"""
    if identity.startswith("call:"):
        call_id = identity.removeprefix("call:")
        row = connection.execute(
            "SELECT action_id,state FROM calls WHERE call_id=?", (call_id,)
        ).fetchone()
        if row is None:
            return None
        context = _read_context(connection, row[0])
        if context is not None:
            context["target"] = {"kind": "call", "id": call_id, "state": row[1]}
        return context
    if not identity.startswith("turn:"):
        return _read_context(connection, identity)
    turn_id = identity.removeprefix("turn:")
    row = connection.execute(
        "SELECT state FROM subject_turns WHERE turn_id=? AND scope='foreground'", (turn_id,)
    ).fetchone()
    if row is None:
        return None
    inputs = tuple(
        item[0]
        for item in connection.execute(
            "SELECT input_id FROM turn_inputs WHERE turn_id=? ORDER BY ordinal", (turn_id,)
        ).fetchall()
    )
    actions: list[JsonValue] = []
    rows = connection.execute(
        "SELECT DISTINCT a.action_id FROM actions a JOIN turn_inputs i ON "
        "a.input_id=i.input_id JOIN subject_turns t ON "
        "t.turn_id=i.turn_id WHERE i.turn_id=? AND "
        "a.created_at>=t.started_at AND (t.finished_at IS NULL OR "
        "a.created_at<=t.finished_at) ORDER BY a.created_at,a.action_id",
        (turn_id,),
    ).fetchall()
    for (action_id,) in rows:
        action = execution.get_action_in_transaction(connection, action_id)
        if action is not None:
            actions.append(decode_object(encode(action).encode()))
    return {
        "target": {"kind": "turn", "id": turn_id, "state": row[0]},
        "turn_id": turn_id,
        "stimuli": _stimuli(connection, inputs),
        "action": None,
        "actions": actions,
    }


def _target(identity: str) -> tuple[str, str]:
    """控制目标只解析明确命名空间，普通行动身份保持原值。"""
    for kind in ("turn", "call"):
        if identity.startswith(kind + ":"):
            return kind, identity.removeprefix(kind + ":")
    return "action", identity


def _stimuli(connection: sqlite3.Connection, input_ids: tuple[str, ...]) -> list[JsonValue]:
    """展开明确关联的不可变输入，轮次遗漏不需要伪造行动。"""
    stimuli: list[JsonValue] = []
    for identity in input_ids:
        row: tuple[str, ...] | None = connection.execute(
            "SELECT input_id,adapter_id,account_id,conversation_id,event_kind,"
            "sender_id,display_name,text,occurred_at,received_at FROM inputs WHERE input_id=?",
            (identity,),
        ).fetchone()
        if row is None:
            raise StorageError("行动关联的原始刺激缺失。", code="invalid_record")
        fields = (
            "input_id",
            "adapter_id",
            "account_id",
            "conversation_id",
            "event_kind",
            "sender_id",
            "display_name",
            "text",
            "occurred_at",
            "received_at",
        )
        stimulus: JsonObject = dict(zip(fields, row, strict=True))
        media: list[tuple[str, str, str | None]] = connection.execute(
            "SELECT media_id,media_type,name FROM input_media WHERE input_id=? ORDER BY ordinal",
            (identity,),
        ).fetchall()
        stimulus["media"] = [
            {"media_id": mid, "media_type": kind, "name": name} for mid, kind, name in media
        ]
        stimuli.append(stimulus)
    return stimuli


def _revision(context: JsonObject) -> str:
    # 审查包含完整参数和回执历史；迟到回执即使不改变行动终态，也必须使旧页面失效。
    return hashlib.sha256(encode_json(context)).hexdigest()


def _history(connection: sqlite3.Connection, action_id: str) -> list[JsonValue]:
    """评价本身就是不可变证据，页面读取不确认前台或反思的消费责任。"""
    rows: list[tuple[str, int]] = connection.execute(
        "SELECT evidence_id,version FROM human_feedback_links WHERE target_kind=? AND target_id=? "
        "ORDER BY created_at DESC,evidence_id DESC LIMIT 32",
        _target(action_id),
    ).fetchall()
    result: list[JsonValue] = []
    for identity, version in rows:
        snapshot = knowledge.get_evidence(connection, identity, version=version)
        if snapshot is None:
            raise StorageError("人工评价原文缺失。", code="invalid_record")
        result.append(
            {
                **decode_object(snapshot.source_json.encode()),
                "evidence_id": identity,
                "created_at": snapshot.occurred_at,
            }
        )
    return result


class HumanFeedbackLedger:
    """应用借用的同库人工审查端口；连接、队列、通知和关闭仍归 Store。

    读取不运行模型。提交只保存评价和两个可靠消费者的责任，后续由原后台
    工作器交接；同请求身份幂等，无法在旧材料上覆盖新增回执。
    """

    def __init__(self, submit: Submit) -> None:
        """借用已有有界提交器，不取得独立连接或后台任务。

        Args:
            submit: Store 所有的同库提交入口。
        """
        self._submit = submit

    async def page(self, before: str | None = None, *, kind: str = "action") -> JsonObject:
        """按稳定行动游标读取至多二十条历史，支持离开最近快照继续审查。

        Args:
            before: 上页签发的行动身份；None 从最新记录开始。

        Returns:
            items 为有限行动预览，next_cursor 为下一页游标或 None。

        Raises:
            ControlError: 游标已不存在。
        """

        def read(connection: sqlite3.Connection) -> JsonObject:
            if kind in ("turn", "call"):
                table, key, created = (
                    ("subject_turns", "turn_id", "started_at")
                    if kind == "turn"
                    else ("calls", "call_id", "prepared_at")
                )
                conditions = "scope='foreground'" if kind == "turn" else "1=1"
                parameters: tuple[object, ...] = ()
                if before is not None:
                    boundary = connection.execute(
                        f"SELECT {created},{key} FROM {table} WHERE {key}=?", (before,)
                    ).fetchone()
                    if boundary is None:
                        raise ControlError("审查游标不存在。", code="review_cursor")
                    conditions += f" AND ({created},{key})<(?,?)"
                    parameters = tuple(boundary)
                target_rows = connection.execute(
                    f"SELECT {key},state,{created} FROM {table} WHERE {conditions} "
                    f"ORDER BY {created} DESC,{key} DESC LIMIT 21",
                    parameters,
                ).fetchall()
                return {
                    "items": [
                        {
                            "target_kind": kind,
                            "target_id": row[0],
                            "state": row[1],
                            "created_at": row[2],
                            "label": "前台轮次" if kind == "turn" else "能力调用",
                        }
                        for row in target_rows[:20]
                    ],
                    "next_cursor": target_rows[19][0] if len(target_rows) > 20 else None,
                }
            if kind != "action":
                raise ControlError("未知审查目标种类。", code="review_target")
            boundary: tuple[float, str] | None = None
            if before is not None:
                boundary = connection.execute(
                    "SELECT created_at,action_id FROM actions WHERE action_id=?", (before,)
                ).fetchone()
                if boundary is None:
                    raise ControlError("历史审查游标不存在。", code="review_cursor")
            where = "" if boundary is None else "WHERE (created_at,action_id)<(?,?) "
            rows: list[tuple[str]] = connection.execute(
                "SELECT action_id FROM actions "
                + where
                + "ORDER BY created_at DESC,action_id DESC LIMIT 21",
                boundary or (),
            ).fetchall()
            items: list[JsonValue] = []
            for (identity,) in rows[:20]:
                action = execution.get_action(connection, identity)
                if action is None:
                    raise StorageError("审查行动记录缺失。", code="invalid_record")
                items.append(
                    {
                        **action_projection(action),
                        "target_kind": "action",
                        "target_id": action.action_id,
                        "label": f"{len(action.calls)} 项操作",
                    }
                )
            return {"items": items, "next_cursor": rows[19][0] if len(rows) > 20 else None}

        return await self._submit(read)

    async def detail(self, action_id: str) -> JsonObject | None:
        """读取完整刺激、行动参数与结果及最近三十二条人工评价。

        Args:
            action_id: 页面选中的真实行动身份。

        Returns:
            完整材料、当前 revision 与评价历史；行动不存在时返回 None。
        """

        def read(connection: sqlite3.Connection) -> JsonObject | None:
            context = read_context(connection, action_id)
            if context is None:
                return None
            return {
                **context,
                "revision": _revision(context),
                "feedback": _history(connection, action_id),
            }

        return await self._submit(read)

    async def submit(
        self, request_id: str, action_id: str, revision: str, value: HumanFeedback
    ) -> None:
        """将评价、所见材料与两个消费者的责任原子保存。

        Args:
            request_id: 面板已接管的稳定请求身份，用于重传去重。
            action_id: 评价所针对的真实行动。
            revision: detail 返回的完整材料摘要。
            value: 好坏判断及可选留言。

        Raises:
            ControlError: 行动缺失、尚未结束、材料更新或身份参数冲突。
            StorageError: 真实存储故障，不能声称评价已保存。

        Note:
            先读取材料申请真实队列字节额度，再在写事务复核版本。取消观察不
            撤销已接管事务；正式调用所有者由 PanelController 持有至收尾。
        """
        context = await self._submit(lambda connection: read_context(connection, action_id))
        if context is None:
            raise ControlError("找不到这次行动。", code="review_missing")
        prepared_revision = _revision(context)
        source: JsonObject = {
            "source": "human_feedback",
            "request_id": request_id,
            "action_id": action_id if _target(action_id)[0] == "action" else None,
            "target_kind": _target(action_id)[0],
            "target_id": _target(action_id)[1],
            "revision": revision,
            "rating": value.rating,
            "comment": value.comment,
        }
        # 原始刺激不可变，冻结身份即可还原所见材料。刺激可能本身包含旧评价；
        # 再复制其全文会让连续纠偏递归扩张，挤占两个消费者的输入预算。
        stimuli = context["stimuli"]
        assert isinstance(stimuli, list)
        stimulus_references: list[JsonValue] = []
        for stimulus in stimuli:
            assert isinstance(stimulus, dict)
            stimulus_references.append(
                {
                    "input_id": stimulus["input_id"],
                    "evidence_ref": {"id": stimulus["input_id"], "version": 1},
                }
            )
        source["input_ids"] = [item["input_id"] for item in stimuli if isinstance(item, dict)]
        source["turn_id"] = context["turn_id"]
        reviewed_context = {**context, "stimuli": stimulus_references}
        payload = encode_json({**source, "reviewed_context": reviewed_context}).decode()
        identity = "human-feedback:" + request_id
        rating_label = "好" if value.rating == "good" else "坏"

        def write(connection: sqlite3.Connection) -> None:
            """去重必须先于版本校验，让已保存评价不因后来回执变化而变成失败。"""
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = knowledge.get_evidence(connection, identity, version=1)
                if existing is not None:
                    if existing.source_json != encode_json(source).decode():
                        raise ControlError("评价身份对应不同参数。", code="control_conflict")
                    return
                current = read_context(connection, action_id)
                if (
                    prepared_revision != revision
                    or current is None
                    or _revision(current) != revision
                ):
                    raise ControlError("行动材料已更新，请重新阅读后评价。", code="stale_revision")
                target = current["target"]
                if not isinstance(target, dict) or target["state"] in (
                    "running",
                    "prepared",
                    "started",
                ):
                    raise ControlError("请等待这次行动结束后再评价。", code="review_running")
                snapshot = EvidenceSnapshot(
                    evidence_id=identity,
                    version=1,
                    kind="human_feedback",
                    text=payload,
                    source_json=encode_json(source).decode(),
                    occurred_at=time.time(),
                    summary=f"人工评价：{rating_label}。{value.comment}",
                )
                feedback.record_human_feedback(connection, snapshot)
                connection.execute(
                    "INSERT INTO human_feedback_links VALUES(?,1,?,?,?)",
                    (identity, *_target(action_id), snapshot.occurred_at),
                )

        await self._submit(write, logical_bytes=len(payload.encode()), control=True, writing=True)

    async def media(self, action_id: str, media_id: str) -> tuple[bytes, str] | None:
        """读取审查材料实际关联的原始媒体，不接受任意 BLOB 身份。

        Args:
            action_id: 当前被审查的真实行动。
            media_id: 所见刺激中的媒体身份。

        Returns:
            原始字节与媒体类型；行动或媒体不属于该审查时返回 None。

        Raises:
            StorageError: 已关联媒体的权威字节缺失或摘要不一致。
        """

        def read(connection: sqlite3.Connection) -> tuple[bytes, str] | None:
            context = read_context(connection, action_id)
            if context is None:
                return None
            stimuli = context["stimuli"]
            assert isinstance(stimuli, list)
            for stimulus in stimuli:
                assert isinstance(stimulus, dict)
                references = stimulus["media"]
                assert isinstance(references, list)
                for reference in references:
                    assert isinstance(reference, dict)
                    if reference["media_id"] != media_id:
                        continue
                    row: tuple[bytes] | None = connection.execute(
                        "SELECT content FROM media WHERE media_id=?", (media_id,)
                    ).fetchone()
                    if row is None or hashlib.sha256(row[0]).hexdigest() != media_id:
                        raise StorageError("审查媒体权威字节损坏。", code="invalid_record")
                    kind = reference["media_type"]
                    assert isinstance(kind, str)
                    return row[0], kind
            return None

        return await self._submit(read)
