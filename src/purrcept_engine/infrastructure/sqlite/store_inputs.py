"""输入准入、原生去重、睡眠积压分流与收件箱调度存储实现。"""

import hashlib
import json
import logging
import sqlite3
from uuid import uuid4

import purrcept_engine.domain.evidence.models as evidence_models
import purrcept_engine.serialization as serialization
from purrcept_engine.domain.inputs.models import (
    Admission,
    InputContext,
    InputRecord,
    InputSource,
    InputSubmission,
    MediaReference,
)
from purrcept_engine.domain.subject.models import (
    AttentionLevel,
    InputProcessing,
    InputStage,
    MinimumWakeAssessment,
)
from purrcept_engine.domain.time.models import RestState
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite import evidence as evidence_storage
from purrcept_engine.infrastructure.sqlite import subject as subject_storage
from purrcept_engine.infrastructure.sqlite import time as time_storage
from purrcept_engine.infrastructure.sqlite.rows import (
    as_datetime,
    as_int,
    as_opt_text,
    as_text,
    require_positive,
    to_timestamp,
)
from purrcept_engine.infrastructure.sqlite.store_base import StoreBase
from purrcept_engine.values import ActionProtocolError, decode_object

_logger = logging.getLogger("purrcept_engine.infrastructure.sqlite.store")

_INPUT_COLUMNS = (
    "input_id, adapter_id, account_id, conversation_id, event_kind, native_event_id, "
    "sender_id, display_name, text, occurred_at, received_at, logical_bytes, context_json"
)


def _input_context(value: object) -> InputContext:
    """校验并还原持久化输入信号。"""
    try:
        fields = decode_object(as_text(value).encode("utf-8"))
        if (
            set(fields)
            != {
                "type",
                "details_json",
                "minimum_wake",
                "interrupt_generation",
                "speech_started",
                "missing_materials",
            }
            or fields["type"] != "InputContext"
        ):
            raise ValueError("持久化输入关系的类型或字段不符合格式。")
        if any(
            type(fields[key]) is not bool
            for key in (
                "minimum_wake",
                "interrupt_generation",
                "speech_started",
            )
        ):
            raise ValueError("持久化程序信号必须是 bool。")
        missing = fields["missing_materials"]
        if not isinstance(missing, list):
            raise ValueError("持久化材料缺口必须是数组。")
        return InputContext(
            details_json=as_text(fields["details_json"]),
            minimum_wake=fields["minimum_wake"] is True,
            interrupt_generation=fields["interrupt_generation"] is True,
            speech_started=fields["speech_started"] is True,
            missing_materials=tuple(as_text(item) for item in missing),
        )
    except (ActionProtocolError, ValueError, KeyError) as error:
        raise StorageError("输入关系和程序信号损坏。", code="invalid_record") from error


def _sleep_record_only(adapter_id: str, context: InputContext) -> bool:
    """睡眠期间仅允许高优先外部输入保留前台资格。"""
    return adapter_id == "subject.idle" or not (
        context.minimum_wake or context.interrupt_generation or context.speech_started
    )


def _record_sleep_input(connection: sqlite3.Connection, identity: str) -> None:
    """睡眠普通输入仅记录历史并释放待办名额，保留原文、媒体与证据。"""
    connection.execute(
        "UPDATE input_processing SET version=version+1,stage='processed',"
        "reason='subject.sleeping.recorded' WHERE input_id=?",
        (identity,),
    )
    connection.execute("DELETE FROM pending_inputs WHERE input_id=?", (identity,))


def record_sleep_inputs(connection: sqlite3.Connection) -> RestState | None:
    """在当前事务内收回睡眠期间的普通待办积压。"""
    rest = time_storage.get_rest(connection)
    if rest is None:
        return None
    rows: list[tuple[str, str, str]] = connection.execute(
        "SELECT p.input_id,i.adapter_id,i.context_json FROM pending_inputs p "
        "JOIN input_processing s USING(input_id) JOIN inputs i USING(input_id) "
        "WHERE s.stage IN ('attention','assessing','submitted','foreground','priority')"
    ).fetchall()
    for identity, adapter_id, context_json in rows:
        if _sleep_record_only(adapter_id, _input_context(context_json)):
            _record_sleep_input(connection, identity)
    return rest


def input_logical_bytes(submission: InputSubmission) -> int:
    """计量单条输入的正文、上下文与媒体字节总量。"""
    try:
        text_bytes = len(submission.text.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise StorageError("输入正文不能表示为 UTF-8。", code="invalid_input") from error
    context_bytes = (
        0
        if submission.context.details_json == "{}"
        else len(submission.context.details_json.encode("utf-8"))
    )
    context_bytes += sum(
        len(reason.encode("utf-8")) for reason in submission.context.missing_materials
    )
    return text_bytes + context_bytes + sum(len(asset.content) for asset in submission.media)


def admit_in_transaction(
    connection: sqlite3.Connection,
    submission: InputSubmission,
    *,
    max_inputs: int,
    max_input_bytes: int,
    logical_bytes: int,
) -> Admission:
    """在调用方当前短事务中原子保存输入原文、媒体、待办与证据快照。"""
    source = submission.source
    media_ids = tuple(hashlib.sha256(asset.content).hexdigest() for asset in submission.media)
    source_facts = (
        source.adapter_id,
        source.account_id,
        source.conversation_id,
        source.event_kind,
        source.native_event_id,
        source.sender_id,
        source.display_name,
    )
    dedup_key = None
    if source.native_event_id is not None:
        dedup_key = json.dumps(source_facts[:5], separators=(",", ":"))
    facts = (
        source_facts,
        submission.text,
        serialization.encode(submission.context),
        to_timestamp(submission.occurred_at),
        tuple(
            (media_id, asset.media_type, asset.name)
            for media_id, asset in zip(media_ids, submission.media, strict=True)
        ),
    )
    fingerprint = hashlib.sha256(
        json.dumps(facts, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if dedup_key is not None:
        existing: tuple[object, ...] | None = connection.execute(
            "SELECT input_id, fingerprint FROM inputs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        if existing is not None:
            if as_text(existing[1]) != fingerprint:
                raise StorageError("原生输入身份与已接管事实冲突。", code="source_conflict")
            return Admission(as_text(existing[0]), duplicate=True)

    rest = record_sleep_inputs(connection)
    record_only = rest is not None and _sleep_record_only(source.adapter_id, submission.context)
    usage: tuple[object, ...] = connection.execute(
        "SELECT count(*), coalesce(sum(i.logical_bytes), 0) "
        "FROM pending_inputs p JOIN inputs i ON i.input_id = p.input_id"
    ).fetchone()
    if logical_bytes > max_input_bytes or (
        not record_only
        and (as_int(usage[0]) >= max_inputs or as_int(usage[1]) + logical_bytes > max_input_bytes)
    ):
        raise StorageError("未处置输入的接管额度已满。", code="input_capacity")

    input_id = uuid4().hex
    connection.execute(
        "INSERT INTO inputs (" + _INPUT_COLUMNS + ", dedup_key, fingerprint) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            input_id,
            *source_facts,
            submission.text,
            to_timestamp(submission.occurred_at),
            to_timestamp(submission.received_at),
            logical_bytes,
            serialization.encode(submission.context),
            dedup_key,
            fingerprint,
        ),
    )
    for ordinal, (media_id, asset) in enumerate(zip(media_ids, submission.media, strict=True)):
        connection.execute(
            "INSERT OR IGNORE INTO media (media_id, content) VALUES (?, ?)",
            (media_id, asset.content),
        )
        connection.execute(
            "INSERT INTO input_media (input_id, ordinal, media_id, media_type, name) "
            "VALUES (?, ?, ?, ?, ?)",
            (input_id, ordinal, media_id, asset.media_type, asset.name),
        )
    connection.execute("INSERT INTO pending_inputs (input_id) VALUES (?)", (input_id,))
    subject_storage.register_input(
        connection,
        input_id,
        minimum_wake=(
            submission.context.minimum_wake
            or submission.context.interrupt_generation
            or submission.context.speech_started
        ),
    )
    if record_only:
        _record_sleep_input(connection, input_id)
    evidence_storage.put_evidence_in_transaction(
        connection,
        evidence_models.EvidenceSnapshot(
            evidence_id=input_id,
            version=1,
            kind="input",
            text=submission.text,
            source_json=serialization.encode({"source": source, "context": submission.context}),
            occurred_at=submission.occurred_at.timestamp(),
            media=tuple(
                MediaReference(media_id, asset.media_type, len(asset.content), asset.name)
                for media_id, asset in zip(media_ids, submission.media, strict=True)
            ),
        ),
    )
    return Admission(input_id, duplicate=False)


class InputStoreMixin(StoreBase):
    """负责输入接管、睡眠分流与收件箱批次流转的存储方法集。"""

    def _admit_business(
        self, connection: sqlite3.Connection, submission: InputSubmission
    ) -> Admission:
        """供时间与反馈子账本在同一短事务内复用输入准入逻辑。"""
        return admit_in_transaction(
            connection,
            submission,
            max_inputs=self._max_inputs,
            max_input_bytes=self._max_input_bytes,
            logical_bytes=input_logical_bytes(submission),
        )

    async def admit(self, submission: InputSubmission) -> Admission:
        """原子接管输入，原生重复事件返回首次确认。"""
        if not isinstance(submission, InputSubmission):
            raise ValueError("submission 必须是 InputSubmission。")
        logical_bytes = input_logical_bytes(submission)
        if logical_bytes > self._max_input_bytes:
            raise StorageError("单条输入超过逻辑负载额度。", code="input_capacity")
        admission = await self._submit(
            lambda connection: self._admit(connection, submission, logical_bytes),
            logical_bytes=logical_bytes,
            writing=True,
        )
        if not admission.duplicate:
            _logger.debug("输入已正式接管。", extra={"input_id": admission.input_id})
        return admission

    async def idle_input_state(self) -> tuple[int, bool]:
        """返回最新 inputs 接管游标及是否存在尚未调度的输入。"""
        return await self._submit(self._idle_input_state)

    async def apply_sleep_policy(self, owner_id: str) -> RestState | None:
        """读取休息状态并将普通待办仅记历史，保留高优先唤醒输入。"""
        self._validate_id(owner_id)

        def apply(connection: sqlite3.Connection) -> RestState | None:
            self._queue.check_writing()
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                rest = record_sleep_inputs(connection)
                if rest is None:
                    return None
                connection.execute(
                    "UPDATE minimum_wake_assessments SET state='completed',level='wake',"
                    "reason='subject.sleeping' WHERE owner_id=? AND state='pending'",
                    (owner_id,),
                )
                return rest

        return await self._submit(apply, control=True, writing=True)

    async def claim_sleep_compaction(self, expected: RestState) -> bool:
        """原子领取当前睡眠周期的一次历史压缩机会。"""
        return await self._submit(
            lambda connection: time_storage.claim_rest_compaction(connection, expected),
            control=True,
            writing=True,
        )

    @staticmethod
    def _idle_input_state(connection: sqlite3.Connection) -> tuple[int, bool]:
        row: tuple[int, int] = connection.execute(
            "SELECT COALESCE((SELECT MAX(rowid) FROM inputs),0), "
            "EXISTS(SELECT 1 FROM input_processing "
            "WHERE stage NOT IN ('submitted','processed','failed'))"
        ).fetchone()
        return row[0], bool(row[1])

    async def admit_idle_input(
        self,
        submission: InputSubmission,
        *,
        expected_sequence: int,
        foreground_overdue: bool = False,
    ) -> Admission | None:
        """原子复查静默或主脑未激活期限并保存主动思考刺激。"""
        if (
            isinstance(expected_sequence, bool)
            or not isinstance(expected_sequence, int)
            or expected_sequence < 0
        ):
            raise ValueError("接管游标必须是非负整数。")
        if not isinstance(foreground_overdue, bool):
            raise ValueError("主脑到期标记必须是布尔值。")
        if not isinstance(submission, InputSubmission) or (
            not submission.context.minimum_wake
            or submission.context.interrupt_generation
            or submission.context.speech_started
        ):
            raise ValueError("空闲刺激必须保底唤起，且不能停音或直接抢占。")
        logical_bytes = input_logical_bytes(submission)

        def admit(connection: sqlite3.Connection) -> Admission | None:
            self._queue.check_writing()
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if time_storage.get_rest(connection) is not None:
                    return None
                if foreground_overdue:
                    scheduled = connection.execute(
                        "SELECT 1 FROM input_processing "
                        "WHERE stage IN ('priority','foreground','claimed','processing') LIMIT 1"
                    ).fetchone()
                    if scheduled is not None:
                        return None
                else:
                    sequence, pending = self._idle_input_state(connection)
                    if sequence != expected_sequence or pending:
                        return None
                return admit_in_transaction(
                    connection,
                    submission,
                    max_inputs=self._max_inputs,
                    max_input_bytes=self._max_input_bytes,
                    logical_bytes=logical_bytes,
                )

        return await self._submit(admit, logical_bytes=logical_bytes, writing=True)

    async def get_input(self, input_id: str) -> InputRecord | None:
        """按身份读取输入记录与当前待办状态。"""
        self._validate_id(input_id)
        return await self._submit(lambda connection: self._get_input(connection, input_id))

    async def get_native_input(self, source: InputSource) -> InputRecord | None:
        """按平台原生事件身份查询首次接管记录。"""
        if not isinstance(source, InputSource):
            raise ValueError("source 必须是 InputSource。")
        if source.native_event_id is None:
            return None
        scope = (
            source.adapter_id,
            source.account_id,
            source.conversation_id,
            source.event_kind,
            source.native_event_id,
        )
        key = json.dumps(scope, separators=(",", ":"))

        def read(connection: sqlite3.Connection) -> InputRecord | None:
            row: tuple[object, ...] | None = connection.execute(
                "SELECT input_id FROM inputs WHERE dedup_key=?", (key,)
            ).fetchone()
            if row is None:
                return None
            record = self._get_input(connection, as_text(row[0]))
            if (
                record is None
                or (
                    record.source.adapter_id,
                    record.source.account_id,
                    record.source.conversation_id,
                    record.source.event_kind,
                    record.source.native_event_id,
                )
                != scope
            ):
                raise StorageError("原生身份索引与首次事实不符。", code="invalid_record")
            return record

        return await self._submit(read)

    async def pending(self, *, limit: int = 8) -> tuple[InputRecord, ...]:
        """按接管顺序读取一批待处理输入。"""
        require_positive(limit, "limit")
        bounded_limit = min(limit, self._max_inputs)
        return await self._submit(lambda connection: self._pending(connection, bounded_limit))

    async def complete_input(self, input_id: str) -> None:
        """标记输入已处置并释放待办名额，保留历史原文与媒体。"""
        self._validate_id(input_id)
        await self._submit(
            lambda connection: self._complete_input(connection, input_id),
            control=True,
            writing=True,
        )

    async def input_processing(self, input_id: str) -> InputProcessing | None:
        """读取指定输入的当前调度阶段与版本。"""
        self._validate_id(input_id)
        return await self._submit(
            lambda connection: subject_storage.processing(connection, input_id)
        )

    async def staged_inputs(self, stage: InputStage, *, limit: int = 8) -> tuple[InputRecord, ...]:
        """按接管顺序加载处于指定阶段的输入批次。"""
        require_positive(limit, "limit")

        def read(connection: sqlite3.Connection) -> tuple[InputRecord, ...]:
            records: list[InputRecord] = []
            for input_id in subject_storage.pending_ids(connection, stage, limit):
                record = self._get_input(connection, input_id)
                if record is None:
                    raise StorageError("处理队列引用不存在的原文。", code="invalid_record")
                records.append(record)
            return tuple(records)

        return await self._submit(read)

    async def submit_inputs(
        self, expected: tuple[InputProcessing, ...], *, minimum_wake: bool
    ) -> bool:
        """CAS 提交缓存批次至收件箱。"""
        return await self._submit(
            lambda connection: subject_storage.submit_inputs(
                connection, expected, minimum_wake=minimum_wake
            ),
            control=True,
            writing=True,
        )

    async def take_foreground_inputs(self) -> tuple[InputRecord, ...]:
        """存在唤醒级刺激时原子取出整个收件箱作为新一轮前台输入。"""

        def take(connection: sqlite3.Connection) -> tuple[InputRecord, ...]:
            records: list[InputRecord] = []
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                record_sleep_inputs(connection)
                for input_id in subject_storage.take_foreground_inputs(connection):
                    record = self._get_input(connection, input_id)
                    if record is None:
                        raise StorageError("处理队列引用不存在的原文。", code="invalid_record")
                    records.append(record)
            return tuple(records)

        return await self._submit(take, control=True, writing=True)

    async def absorb_turn_inputs(self, turn_id: str) -> tuple[InputRecord, ...]:
        """存在唤醒级刺激时将收件箱原子并入正在运行的前台轮次。"""
        self._validate_id(turn_id)

        def absorb(connection: sqlite3.Connection) -> tuple[InputRecord, ...]:
            records: list[InputRecord] = []
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                record_sleep_inputs(connection)
                for input_id in subject_storage.absorb_turn_inputs(connection, turn_id):
                    record = self._get_input(connection, input_id)
                    if record is None:
                        raise StorageError("处理队列引用不存在的原文。", code="invalid_record")
                    records.append(record)
            return tuple(records)

        return await self._submit(absorb, control=True, writing=True)

    async def buffered_inputs(self) -> tuple[InputRecord, ...]:
        """按接管顺序读取尚未提交收件箱的缓存输入。"""

        def read(connection: sqlite3.Connection) -> tuple[InputRecord, ...]:
            rows: list[tuple[str]] = connection.execute(
                "SELECT p.input_id FROM pending_inputs p JOIN input_processing s USING(input_id) "
                "WHERE s.stage IN ('attention','priority') ORDER BY p.sequence"
            ).fetchall()
            records: list[InputRecord] = []
            for (input_id,) in rows:
                record = self._get_input(connection, input_id)
                if record is None:
                    raise StorageError("缓存引用不存在的原文。", code="invalid_record")
                records.append(record)
            return tuple(records)

        return await self._submit(read)

    async def decide_inputs(
        self,
        expected: tuple[InputProcessing, ...],
        level: AttentionLevel,
        reason: str,
        *,
        request_id: str | None = None,
    ) -> bool:
        """CAS 采用注意模型对批次的处置分级。"""
        return await self._submit(
            lambda connection: subject_storage.decide_inputs(
                connection, expected, level, reason, request_id=request_id
            ),
            logical_bytes=len(serialization.encode((expected, reason)).encode("utf-8")),
            control=True,
            writing=True,
        )

    async def register_minimum_wake_assessment(
        self, expected: tuple[InputProcessing, ...], *, owner_id: str, observed_generation: int
    ) -> bool:
        """登记最低唤起输入在忙碌期的一次紧急升级评估资格。"""
        return await self._submit(
            lambda connection: subject_storage.register_minimum_wake_assessment(
                connection, expected, owner_id=owner_id, observed_generation=observed_generation
            ),
            control=True,
            writing=True,
        )

    async def minimum_wake_assessments(
        self, owner_id: str, *, limit: int = 8
    ) -> tuple[MinimumWakeAssessment, ...]:
        """读取当前宿主尚未完成的最低唤起升级评估。"""
        require_positive(limit, "limit")
        return await self._submit(
            lambda connection: subject_storage.minimum_wake_assessments(
                connection, owner_id, limit=limit
            )
        )

    async def finish_minimum_wake_assessment(
        self,
        assessment: MinimumWakeAssessment,
        level: AttentionLevel,
        reason: str,
        *,
        request_id: str | None = None,
    ) -> bool:
        """CAS 提交最低唤起升级评估结论。"""
        return await self._submit(
            lambda connection: subject_storage.finish_minimum_wake_assessment(
                connection, assessment, level, reason, request_id=request_id
            ),
            logical_bytes=len(reason.encode("utf-8")),
            control=True,
            writing=True,
        )

    def _admit(
        self, connection: sqlite3.Connection, submission: InputSubmission, logical_bytes: int
    ) -> Admission:
        self._queue.check_writing()
        connection.execute("BEGIN IMMEDIATE")
        try:
            admission = admit_in_transaction(
                connection,
                submission,
                max_inputs=self._max_inputs,
                max_input_bytes=self._max_input_bytes,
                logical_bytes=logical_bytes,
            )
            connection.commit()
            return admission
        except (sqlite3.Error, StorageError):
            connection.rollback()
            raise
        except UnicodeEncodeError as error:
            connection.rollback()
            raise StorageError(
                "输入来源或媒体信息不能表示为 UTF-8。", code="invalid_input"
            ) from error

    def _get_input(self, connection: sqlite3.Connection, input_id: str) -> InputRecord | None:
        row: tuple[object, ...] | None = connection.execute(
            f"SELECT {_INPUT_COLUMNS} FROM inputs WHERE input_id = ?", (input_id,)
        ).fetchone()
        if row is None:
            return None
        media_rows: list[tuple[object, ...]] = connection.execute(
            "SELECT r.media_id, r.media_type, length(m.content), r.name FROM input_media r "
            "LEFT JOIN media m ON m.media_id = r.media_id WHERE r.input_id = ? ORDER BY r.ordinal",
            (input_id,),
        ).fetchall()
        pending: tuple[object, ...] | None = connection.execute(
            "SELECT 1 FROM pending_inputs WHERE input_id = ?", (input_id,)
        ).fetchone()
        try:
            source = InputSource(
                as_text(row[1]),
                as_text(row[2]),
                as_text(row[3]),
                as_text(row[4]),
                as_opt_text(row[5]),
                as_opt_text(row[6]),
                as_opt_text(row[7]),
            )
        except ValueError as error:
            raise StorageError("权威记录中的输入来源无效。", code="invalid_record") from error
        media = tuple(
            MediaReference(
                as_text(item[0]), as_text(item[1]), as_int(item[2]), as_opt_text(item[3])
            )
            for item in media_rows
        )
        context = _input_context(row[12])
        return InputRecord(
            as_text(row[0]),
            source,
            as_text(row[8]),
            as_datetime(row[9]),
            as_datetime(row[10]),
            media,
            "pending" if pending is not None else "processed",
            as_int(row[11]),
            context,
        )

    def _pending(self, connection: sqlite3.Connection, limit: int) -> tuple[InputRecord, ...]:
        rows: list[tuple[object, ...]] = connection.execute(
            "SELECT input_id FROM pending_inputs ORDER BY sequence LIMIT ?", (limit,)
        ).fetchall()
        records: list[InputRecord] = []
        for row in rows:
            record = self._get_input(connection, as_text(row[0]))
            if record is None:
                raise StorageError("待办引用不存在的输入。", code="invalid_record")
            records.append(record)
        return tuple(records)

    def _complete_input(self, connection: sqlite3.Connection, input_id: str) -> None:
        self._queue.check_writing()
        with connection:
            existing: tuple[object, ...] | None = connection.execute(
                "SELECT 1 FROM inputs WHERE input_id = ?", (input_id,)
            ).fetchone()
            if existing is None:
                raise StorageError("待处置输入不存在。", code="input_missing")
            connection.execute("DELETE FROM pending_inputs WHERE input_id = ?", (input_id,))
            connection.execute(
                "UPDATE input_processing SET version=version+1,stage='processed',"
                "reason='explicit.complete' WHERE input_id=?",
                (input_id,),
            )
