"""ForegroundService 的固定工作流程；共享协调事实只借用同一领域运行时。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx
from purrcept_core.models import (
    ContentBlock,
    Message,
    MessageRole,
    ModelError,
    TextBlock,
    ToolLoopLimitError,
)

from purrcept_engine.domain.context.capacity import CapacityError
from purrcept_engine.domain.context.delivery import ToolDeliveryGate
from purrcept_engine.domain.context.materials import UnsupportedMaterial, input_message
from purrcept_engine.domain.context.presentation import render_material
from purrcept_engine.domain.execution.models import ActionRecord
from purrcept_engine.domain.inputs.models import InputRecord, MediaReference
from purrcept_engine.domain.knowledge.semantic import EmbeddingProtocolError
from purrcept_engine.domain.subject.attention import NativeMedia
from purrcept_engine.domain.subject.foreground_tools import ForegroundTools
from purrcept_engine.domain.subject.models import (
    TurnRecord,
)
from purrcept_engine.domain.subject.ports import ActionCallback
from purrcept_engine.errors import HostStateError, StorageError

if TYPE_CHECKING:
    from purrcept_engine.domain.subject.service import SubjectService


@dataclass(frozen=True, slots=True)
class ForegroundBinding:
    """本轮六工具的有限绑定，闭包只借出当前代次的行动资格。"""

    execute: ActionCallback
    media: NativeMedia
    wakeup: Callable[[], None]
    input_id: str
    eligible: Callable[[], bool]
    delivery: ToolDeliveryGate

    async def play_action(self, source: str) -> ActionRecord:
        """传入本轮资格，实际资源与迟到回执仍归 app 的执行器。"""
        return await self.execute(
            source,
            input_id=self.input_id,
            can_start=self.eligible,
            presented_call_ids=self.delivery.presented_call_ids,
        )

    async def media_blocks(self, media: tuple[MediaReference, ...]) -> tuple[ContentBlock, ...]:
        """仅展开已经接管的原生内容，缺口不会变为描述性预览。"""
        return await self.media(media)

    def wakeup_committed(self) -> None:
        """通知计划所属调度者查权威记录，不在这里执行计划。"""
        self.wakeup()


class ForegroundService:
    """领域运行时创建的工作所有者，启动一次；取消和汇总关闭由运行时统一协调。"""

    def __init__(self, runtime: SubjectService) -> None:
        """借用同一协调状态，不复制会话、生成资格或失败事实。"""
        self.runtime = runtime
        self.task: asyncio.Task[None] | None = None

    def start(self) -> asyncio.Task[None]:
        """创建唯一固定任务并交给运行时汇总关闭；重复启动明确拒绝。"""
        if self.task is not None:
            raise HostStateError("领域工作器已经启动。")
        self.task = asyncio.create_task(self.foreground(), name="subject-foreground")
        return self.task

    async def foreground(self) -> None:
        """唯一会话串行处理已经交接的输入；最终文字仅提交内部检查点。"""
        while True:
            self.runtime.check()
            revision = self.runtime.store.change_revision
            # 先只读检查唤起，避免空池写事务不断通知注意工作器。真正取批在
            # 单个事务中连同尚未到期/正在判断的普通刺激一并接管。
            ready = await self.runtime.store.staged_inputs("foreground", limit=1)
            records = await self.runtime.store.take_foreground_inputs() if ready else ()
            if not records:
                await self.runtime.wait(revision)
                continue
            try:
                await self.run_foreground(records)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if self.runtime.closing or task is None or task.cancelling():
                    raise
                # 匹配的 urgent 只取消本轮临时 Core 历史；新独立输入仍可接续。
            except (
                ModelError,
                CapacityError,
                UnsupportedMaterial,
                ToolLoopLimitError,
                TimeoutError,
                httpx.HTTPError,
                EmbeddingProtocolError,
            ):
                # Session 已保存失败并结案输入；没有新来源不会自动重启预算。
                pass

    async def foreground_message(
        self, records: tuple[InputRecord, ...], failures: tuple[TurnRecord, ...]
    ) -> Message:
        """完整交付当前输入；失败经历带原文、回执和可展开媒体引用。"""
        blocks: list[ContentBlock] = []
        evidence_ids: list[str] = []
        for failure in failures:
            blocks.append(
                TextBlock(
                    render_material(
                        "此前未完成的轮次与真实行动回执",
                        {
                            "material_kind": "prior_failed_turn",
                            "turn": failure,
                            "evidence_refs": failure.input_ids,
                            "actions": await self.runtime.store.actions_for_inputs(
                                failure.input_ids
                            ),
                        },
                    )
                )
            )
            original: list[InputRecord] = []
            for input_id in failure.input_ids:
                record = await self.runtime.store.get_input(input_id)
                if record is None:
                    raise StorageError("失败经历引用缺失原文。", code="invalid_record")
                original.append(record)
                evidence_ids.append(input_id)
            if original:
                # 失败媒体不自动再次进入请求：同一不可计量附件会让后续纯文本
                # 交互永久失败。保留原文和原件引用，明确未展开，由证据工具取回。
                blocks.extend(
                    (
                        await input_message(
                            self.runtime.store, tuple(original), media_as_references=True
                        )
                    ).content
                )
        blocks.extend((await input_message(self.runtime.store, records)).content)
        evidence_ids.extend(record.input_id for record in records)
        processing = await self.runtime.processing(records)
        assembled_at = time.time()
        blocks.append(
            TextBlock(
                render_material(
                    "本批输入的处理与延期情况",
                    {
                        "material_kind": "input_disposition",
                        "processing": processing,
                        "assembled_at": assembled_at,
                        "deferred_delivery": tuple(
                            {
                                "input_id": item.input_id,
                                "due_at": item.due_at,
                                "assembly_delay_seconds": max(0.0, assembled_at - item.due_at),
                            }
                            for item in processing
                            if item.handed_off and item.due_at is not None
                        ),
                    },
                )
            )
        )
        return Message(
            MessageRole.USER,
            tuple(blocks),
            metadata={
                "evidence": evidence_ids,
                "accepted_failures": [failure.turn_id for failure in failures],
            },
        )

    def turn_eligible(self, generation: int) -> bool:
        """执行器最后核验此闭包，旧代次不能借新会话继续派发脚本。"""
        if (
            self.runtime.closing
            or self.runtime.failure_error is not None
            or not self.runtime.session.eligible(generation)
        ):
            return False
        self.runtime.can_start()
        return True

    def check_turn(self, generation: int) -> None:
        """工具进入各业务端口前核验原轮次，权威故障保持原型。"""
        self.runtime.check()
        if not self.runtime.session.eligible(generation):
            raise HostStateError("前台工具已失去原轮次资格。")

    async def run_foreground(self, records: tuple[InputRecord, ...]) -> None:
        """建立本轮工具资格；后续拉取扩充来源但不刷新代次和主动预算。"""
        failures = await self.runtime.store.failed_turns("foreground")
        generation = self.runtime.session.generation + 1
        delivery = ToolDeliveryGate(compiler=self.runtime.subject.compiler)
        ports = ForegroundBinding(
            self.runtime.execute,
            self.runtime.media,
            self.runtime.wakeup_committed,
            records[0].input_id,
            lambda: self.turn_eligible(generation),
            delivery,
        )
        tools = ForegroundTools(
            self.runtime.knowledge,
            self.runtime.knowledge_store,
            ports,
            check=lambda: self.check_turn(generation),
            delivery=delivery,
        )

        async def receive_inputs(turn: TurnRecord) -> Message | None:
            """原子关联新输入后展开原文；失败仍由当前轮次统一结案。"""
            self.check_turn(generation)
            incoming = await self.runtime.store.take_foreground_inputs(turn.turn_id)
            if not incoming:
                return None
            return await self.foreground_message(incoming, ())

        await self.runtime.session.run(
            lambda: self.foreground_message(records, failures),
            turn_id=uuid4().hex,
            input_ids=tuple(record.input_id for record in records),
            accepted_failures=tuple(failure.turn_id for failure in failures),
            backend=self.runtime.subject.backend,
            evidence=self.runtime.store,
            model=self.runtime.subject.model,
            instructions=self.runtime.instructions,
            tools=tools.tools,
            output_tokens=self.runtime.subject.output_tokens,
            max_requests=self.runtime.settings.max_requests,
            turn_seconds=self.runtime.settings.turn_seconds,
            retry=self.runtime.subject.retry,
            diagnostic_bytes=self.runtime.settings.diagnostic_bytes,
            refresh=self.runtime.refresh,
            capacity=self.runtime.subject.capacity,
            delivery=delivery,
            prepare_history=self.runtime.prepare_history,
            receive_inputs=receive_inputs,
        )
