"""行动意图、逐次能力调用回执、模型请求证据与长时活动状态存储实现。"""

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Literal

import purrcept_engine.domain.execution.record_validation as record_validation
import purrcept_engine.serialization as serialization
from purrcept_engine.domain.activities.models import (
    ActivityCommand,
    ActivityFeedback,
    ActivityRecord,
)
from purrcept_engine.domain.execution.models import (
    ActionOutcome,
    ActionRecord,
    CallOutcome,
    CallRecord,
)
from purrcept_engine.domain.inputs.models import MediaReference
from purrcept_engine.domain.model_requests.models import (
    AttemptEvidence,
    AttemptStart,
    RequestEvidence,
    RequestOutcome,
    RequestRecord,
)
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite import activities as activity_storage
from purrcept_engine.infrastructure.sqlite import execution as execution_storage
from purrcept_engine.infrastructure.sqlite import model_requests as model_storage
from purrcept_engine.infrastructure.sqlite.rows import as_text, require_positive
from purrcept_engine.infrastructure.sqlite.store_base import StoreBase

_logger = logging.getLogger("purrcept_engine.infrastructure.sqlite.store")


class ExecutionStoreMixin(StoreBase):
    """负责行动/调用回执、逻辑模型请求与活动持久化的方法集。"""

    async def begin_action(
        self, action_id: str, *, catalog_version: str, source: str, input_id: str | None = None
    ) -> None:
        """在启动执行前持久化行动源码、能力目录版本与关联输入。"""
        self._validate_id(action_id)
        self._validate_id(catalog_version)
        if not isinstance(source, str):
            raise ValueError("行动源码必须是字符串。")
        size = record_validation.payload_bytes(action_id, catalog_version, source, input_id)
        await self._submit(
            lambda connection: execution_storage.begin_action(
                connection, action_id, catalog_version, source, input_id
            ),
            control=True,
            writing=True,
            logical_bytes=size,
        )
        _logger.debug(
            "行动意图已接管，尚不证明能力效果。",
            extra={"action_id": action_id, "input_id": input_id},
        )

    async def begin_request(self, request: RequestEvidence) -> None:
        """在向远端模型派发前保存冻结请求材料与媒体引用。"""
        await self._submit(
            lambda connection: model_storage.begin_request(connection, request),
            logical_bytes=len(serialization.encode(request).encode("utf-8")),
            control=True,
            writing=True,
        )
        _logger.debug(
            "逻辑模型请求已冻结。",
            extra={
                "request_id": request.request_id,
                "turn_id": request.scope.turn_id,
                "scope": request.scope.owner_id,
            },
        )

    async def begin_attempt(self, start: AttemptStart) -> None:
        """登记单次模型传输尝试的编号、身份与截止时间。"""
        await self._submit(
            lambda connection: model_storage.begin_attempt(connection, start),
            logical_bytes=len(serialization.encode(start).encode("utf-8")),
            control=True,
            writing=True,
        )

    async def finish_attempt(self, result: AttemptEvidence) -> None:
        """保存单次模型传输尝试的响应、错误分类与有限诊断。"""
        await self._submit(
            lambda connection: model_storage.finish_attempt(connection, result),
            logical_bytes=len(serialization.encode(result).encode("utf-8")),
            control=True,
            writing=True,
        )
        _logger.debug(
            "模型尝试已结束。",
            extra={
                "request_id": result.start.request_id,
                "attempt_id": result.start.attempt_id,
                "outcome": result.state,
                "error_type": None if result.failure is None else result.failure.error_type,
            },
        )

    async def finish_request(self, request_id: str, state: RequestOutcome) -> None:
        """记录逻辑模型请求的最终结论。"""
        self._validate_id(request_id)
        await self._submit(
            lambda connection: model_storage.finish_request(connection, request_id, state),
            logical_bytes=len(request_id.encode("utf-8")),
            control=True,
            writing=True,
        )

    async def get_request(self, request_id: str) -> RequestRecord | None:
        """按身份还原逻辑模型请求及其按序保存的传输尝试。"""
        self._validate_id(request_id)
        return await self._submit(
            lambda connection: model_storage.get_request(connection, request_id)
        )

    async def requests_for_turn(self, owner_id: str, turn_id: str) -> tuple[RequestRecord, ...]:
        """按所有者与轮次身份读取本批全部逻辑模型请求。"""

        def read(connection: sqlite3.Connection) -> tuple[RequestRecord, ...]:
            rows: list[tuple[object, ...]] = connection.execute(
                "SELECT request_id FROM model_requests "
                "WHERE json_extract(evidence_json,'$.scope.owner_id')=? "
                "AND json_extract(evidence_json,'$.scope.turn_id')=? "
                "ORDER BY created_at,request_id",
                (owner_id, turn_id),
            ).fetchall()
            records: list[RequestRecord] = []
            for row in rows:
                record = model_storage.get_request(connection, as_text(row[0]))
                if record is None:
                    raise StorageError("批次请求身份损坏。", code="invalid_record")
                records.append(record)
            return tuple(records)

        return await self._submit(read)

    async def activity_records(self) -> tuple[ActivityRecord, ...]:
        """读取全部未结束活动的当前状态快照。"""
        return await self._submit(activity_storage.activity_records)

    async def get_activity(self, activity_id: str) -> ActivityRecord | None:
        """按身份读取活动当前快照。"""
        self._validate_id(activity_id)
        return await self._submit(
            lambda connection: activity_storage.get_activity(connection, activity_id)
        )

    async def get_activity_command(self, command_id: str) -> ActivityCommand | None:
        """按身份读取活动控制命令及确认状态。"""
        self._validate_id(command_id)
        return await self._submit(
            lambda connection: activity_storage.get_activity_command(connection, command_id)
        )

    async def get_activity_feedback(self, feedback_id: str) -> ActivityFeedback | None:
        """按身份读取驱动上报的活动反馈记录。"""
        self._validate_id(feedback_id)
        return await self._submit(
            lambda connection: activity_storage.get_activity_feedback(connection, feedback_id)
        )

    async def commit_activity(
        self,
        record: ActivityRecord,
        *,
        expected_version: int | None,
        command: ActivityCommand | None = None,
        feedback: ActivityFeedback | None = None,
    ) -> None:
        """CAS 提交活动状态、控制命令与驱动反馈。"""
        size = len(serialization.encode((record, command, feedback)).encode("utf-8"))
        await self._submit(
            lambda connection: activity_storage.commit_activity(
                connection,
                record,
                expected_version=expected_version,
                command=command,
                feedback=feedback,
            ),
            logical_bytes=size,
            control=True,
            writing=True,
        )
        if command is not None:
            _logger.debug(
                "活动控制已入账，实际状态见确认。",
                extra={
                    "activity_id": record.activity_id,
                    "command_id": command.command_id,
                    "outcome": command.status,
                },
            )

    async def recover_activities(self) -> tuple[ActivityRecord, ...]:
        """在启动恢复阶段登记上次运行中断的活动。"""
        return await self._submit(
            lambda connection: activity_storage.recover_activities(
                connection, now=datetime.now(UTC).timestamp()
            ),
            control=True,
            writing=True,
        )

    async def pending_activity_feedback(
        self,
        consumer: Literal["foreground", "reflection"],
    ) -> tuple[ActivityFeedback, ...]:
        """读取指定消费者尚未确认的活动反馈。"""
        return await self._submit(
            lambda connection: activity_storage.pending_activity_feedback(connection, consumer)
        )

    async def acknowledge_activity_feedback(
        self,
        feedback_id: str,
        consumer: Literal["foreground", "reflection"],
    ) -> None:
        """确认指定消费者已完成该条活动反馈交接。"""
        self._validate_id(feedback_id)
        await self._submit(
            lambda connection: activity_storage.acknowledge_activity_feedback(
                connection, feedback_id, consumer
            ),
            control=True,
            writing=True,
        )

    async def begin_call(
        self,
        call_id: str,
        *,
        action_id: str,
        sequence: int,
        path: str,
        target_id: str,
        arguments_json: str,
    ) -> None:
        """在能力派发前记录完整调用意图。"""
        for identity in (call_id, action_id, path, target_id):
            self._validate_id(identity)
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("调用顺序必须是非负整数。")
        size = record_validation.payload_bytes(call_id, action_id, path, target_id, arguments_json)
        record_validation.validate_json(arguments_json, arguments=True)
        await self._submit(
            lambda connection: execution_storage.begin_call(
                connection, call_id, action_id, sequence, path, target_id, arguments_json
            ),
            control=True,
            writing=True,
            logical_bytes=size,
        )

    async def mark_call_started(self, call_id: str) -> None:
        """记录能力调用即将进入 Provider 执行的启动门槛。"""
        self._validate_id(call_id)
        size = record_validation.payload_bytes(call_id)
        await self._submit(
            lambda connection: execution_storage.mark_call_started(connection, call_id),
            control=True,
            writing=True,
            logical_bytes=size,
        )

    async def record_call_result(
        self,
        call_id: str,
        *,
        state: CallOutcome,
        result_json: str | None,
        error_type: str | None,
        error: str | None,
        media: tuple[MediaReference, ...] = (),
    ) -> CallRecord:
        """持久化能力调用回执、版本历史及可靠反馈待办。"""
        self._validate_id(call_id)
        if state not in ("confirmed", "failed", "unknown", "not_started"):
            raise ValueError("调用结果状态无效。")
        size = record_validation.payload_bytes(call_id, result_json, error_type, error)
        size += len(serialization.encode(media).encode())
        record_validation.validate_json(result_json)
        record = await self._submit(
            lambda connection: execution_storage.record_call_result(
                connection, call_id, state, result_json, error_type, error, media
            ),
            control=True,
            writing=True,
            logical_bytes=size,
        )
        _logger.debug(
            "能力回执已入账。",
            extra={
                "action_id": record.action_id,
                "call_id": record.call_id,
                "path": record.path,
                "outcome": record.state,
                "receipt_revision": record.receipt_revision,
                "error_type": record.error_type,
            },
        )
        return record

    async def finish_action(
        self,
        action_id: str,
        *,
        state: ActionOutcome,
        result_json: str | None,
        output: str,
        error: str | None,
    ) -> None:
        """保存行动结束状态与标准输出（不回滚已入账的逐次调用回执）。"""
        self._validate_id(action_id)
        if state not in ("completed", "failed", "timed_out", "cancelled"):
            raise ValueError("行动结果状态无效。")
        if not isinstance(output, str):
            raise ValueError("行动输出必须是字符串。")
        size = record_validation.payload_bytes(action_id, result_json, output, error)
        record_validation.validate_json(result_json)
        await self._submit(
            lambda connection: execution_storage.finish_action(
                connection, action_id, state, result_json, output, error
            ),
            control=True,
            writing=True,
            logical_bytes=size,
        )
        _logger.debug(
            "行动已结束；逐调用回执分别保留确认、未知与失败。",
            extra={"action_id": action_id, "outcome": state},
        )

    async def get_action(self, action_id: str) -> ActionRecord | None:
        """按身份读取完整行动记录及逐次调用回执历史。"""
        self._validate_id(action_id)
        return await self._submit(
            lambda connection: execution_storage.get_action(connection, action_id)
        )

    async def actions_for_inputs(self, input_ids: tuple[str, ...]) -> tuple[ActionRecord, ...]:
        """按因果输入身份集合查询关联行动记录。"""
        for input_id in input_ids:
            self._validate_id(input_id)

        def read(connection: sqlite3.Connection) -> tuple[ActionRecord, ...]:
            rows: list[tuple[object, ...]] = connection.execute(
                "SELECT action_id FROM actions WHERE input_id IN (SELECT value FROM json_each(?)) "
                "ORDER BY created_at,action_id",
                (serialization.encode(input_ids),),
            ).fetchall()
            result: list[ActionRecord] = []
            for row in rows:
                action = execution_storage.get_action(connection, as_text(row[0]))
                if action is None:
                    raise StorageError("因果行动引用损坏。", code="invalid_record")
                result.append(action)
            return tuple(result)

        return await self._submit(
            read, logical_bytes=len(serialization.encode(input_ids).encode("utf-8"))
        )

    async def pending_call_receipts(self, *, limit: int = 16) -> tuple[CallRecord, ...]:
        """读取待桥接交付的调用回执版本批次。"""
        require_positive(limit, "limit")
        bounded_limit = min(limit, self._max_inputs)
        return await self._submit(
            lambda connection: execution_storage.pending_call_receipts(connection, bounded_limit)
        )
