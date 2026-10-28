"""被协调者唤醒一次就处理一份冻结快照，连接前台工具与连续会话。

入口 run 只处理调用方已经取出的输入；刺激选择、合批和下一次唤醒归协调者。
ContinuousSession 拥有连续历史和生成资格，Core 拥有轮内模型/工具循环。
本模块不轮询新刺激，也不根据前台最终文字自动对外发送。
"""

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
    """本轮六工具的有限绑定，闭包只借出当前代次的行动资格。

    Attributes:
        execute: 当前主体执行受限行动的有限入口。
        media: 已接管媒体的引用集合，展开时必须取得原始 BLOB。
        wakeup: 提醒固定调度器重新查询权威事实的同步回调。
        input_id: 正式接管返回的输入身份，用于关联原文与后续处理。
        eligible: 在实际行动前复核当前生成代次的同步入口或其冻结结论。
        delivery: 本轮工具原文交付门槛；None 不执行候选交付替换。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    execute: ActionCallback
    media: NativeMedia
    wakeup: Callable[[], None]
    input_id: str
    eligible: Callable[[], bool]
    delivery: ToolDeliveryGate

    async def play_action(self, source: str) -> ActionRecord:
        """传入本轮资格，实际资源与迟到回执仍归 app 的执行器。

        Args:
            source: 待执行的受限 Python 源码。

        Returns:
            已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。
        """
        return await self.execute(
            source,
            input_id=self.input_id,
            can_start=self.eligible,
            presented_call_ids=self.delivery.presented_call_ids,
        )

    async def media_blocks(self, media: tuple[MediaReference, ...]) -> tuple[ContentBlock, ...]:
        """仅展开已经接管的原生内容，缺口不会变为描述性预览。

        Args:
            media: 已接管媒体的引用集合，展开时必须取得原始 BLOB。

        Returns:
            按原始顺序展开的完整 Core 内容块，包括真实原生媒体。
        """
        return await self.media(media)

    def wakeup_committed(self) -> None:
        """通知计划所属调度者查权威记录，不在这里执行计划。"""
        self.wakeup()


class ForegroundService:
    """按外部唤醒推进一次连续前台，连接固定输入、工具与历史维护。

    Note:
        SubjectService 创建并持有工作任务；ContinuousSession 拥有会话历史，
        Core 拥有工具循环。抢占只撤销生成资格，真实行动由执行器独立收尾。
    """

    def __init__(self, runtime: SubjectService) -> None:
        """借用同一协调状态，不复制会话、生成资格或失败事实。

        Args:
            runtime: 创建此协作者的领域或执行运行实例，不跨实例共享状态。
        """
        self.runtime = runtime

    async def run(self, records: tuple[InputRecord, ...]) -> None:
        """只推进调用方唤醒时已取走的输入快照。

        Args:
            records: 协调者原子取走的非空收件箱快照。

        Note:
            一次调用对应一次前台轮次；Core 继续拥有轮内模型/工具循环。
            模型暂时失败由 Session 留下失败经历，新的唤醒由外部协调者安排。
        """
        try:
            await self.run_foreground(records)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if self.runtime.closing or task is None or task.cancelling():
                raise
            # urgent 取消的是 Session 的本轮任务，不能误取消协调者后续唤醒。
        except (
            ModelError,
            CapacityError,
            UnsupportedMaterial,
            ToolLoopLimitError,
            TimeoutError,
            httpx.HTTPError,
            EmbeddingProtocolError,
        ):
            # Session 已完成失败结案；没有独立新输入就不重新发起同轮预算。
            pass

    async def foreground_message(
        self, records: tuple[InputRecord, ...], failures: tuple[TurnRecord, ...]
    ) -> Message:
        """组装完整当前输入及尚未承接的失败引用目录。

        Args:
            records: 按接管顺序取得的完整输入记录。
            failures: 尚未由后续成功处理承接的失败轮次事实。

        Returns:
            当前输入保留完整正文和媒体位置；旧失败只携带事实及去重的原文定位。

        Raises:
            StorageError: 失败经历引用缺失原文。

        Note:
            失败不安装临时 Core 历史，不能把此前请求再次包进当前输入。旧输入
            正文和媒体通过 read_evidence 按需展开；行动只保留一次状态索引，
            当前回执引用由宿主动态状态提供。成功承接只确认失败事实已交接，不表示
            旧原文已读、旧输入处理成功或世界行动需要重放。
        """
        blocks: list[ContentBlock] = []
        current_ids = {record.input_id for record in records}
        failed_ids = tuple(
            dict.fromkeys(identity for turn in failures for identity in turn.input_ids)
        )
        evidence_ids = list(dict.fromkeys((*failed_ids, *(record.input_id for record in records))))
        if failures:
            originals: list[dict[str, object]] = []
            for input_id in failed_ids:
                if input_id in current_ids:
                    continue
                record = await self.runtime.store.get_input(input_id)
                if record is None:
                    raise StorageError("失败经历引用缺失原文。", code="invalid_record")
                # 原文只在库中查询以核验来源；不能因新的轮次再次失败，就将同一
                # 长文本、附件或原生回包递归复制进下一份初始消息。
                originals.append(
                    {
                        "id": record.input_id,
                        "source": record.source,
                        "occurred_at": record.occurred_at.isoformat(),
                        "received_at": record.received_at.isoformat(),
                    }
                )
            # 失败原文与行动各按真实身份汇总一次。只将效果状态留进成功历史，
            # 不复制脚本、输出、调用正文或其历次回包；动态提醒本身不进入历史。
            actions = await self.runtime.store.actions_for_inputs(failed_ids)
            blocks.append(
                TextBlock(
                    render_material(
                        "此前未完成的轮次（原文按需读取）",
                        {
                            "material_kind": "prior_failed_turn",
                            "turns": failures,
                            "inputs": originals,
                            "actions": [
                                {
                                    "action_id": action.action_id,
                                    "input_id": action.input_id,
                                    "state": action.state,
                                    "calls": [
                                        {
                                            "call_id": call.call_id,
                                            "path": call.path,
                                            "target_id": call.target_id,
                                            "state": call.state,
                                            "receipt_revision": call.receipt_revision,
                                        }
                                        for call in action.calls
                                    ],
                                }
                                for action in actions
                            ],
                            "delivery": "reference_only",
                            "instruction": "这里只交接失败事实与原文引用，未展开旧正文或媒体；"
                            "需要细节时用 read_evidence(id=输入身份) 分页读取。"
                            "真实调用状态及回执版本见宿主当前状态；不要自动重放旧行动。",
                        },
                    )
                )
            )
        current = await input_message(
            self.runtime.store, records, supports_contents=self.runtime.subject.supports_contents
        )
        blocks.extend(current.content)
        processing = await self.runtime.processing(records)
        assembled_at = time.time()
        blocks.append(
            TextBlock(
                render_material(
                    "本轮唤醒时冻结的输入状态",
                    {
                        "material_kind": "input_disposition",
                        "processing": processing,
                        "assembled_at": assembled_at,
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
        """执行器最后核验此闭包，旧代次不能借新会话继续派发脚本。

        Args:
            generation: 当前会话或设备的代次，用于拒绝迟到结果更新新状态。
        """
        if (
            self.runtime.closing
            or self.runtime.failure_error is not None
            or not self.runtime.session.eligible(generation)
        ):
            return False
        self.runtime.can_start()
        return True

    def check_turn(self, generation: int) -> None:
        """工具进入各业务端口前核验原轮次，权威故障保持原型。

        Args:
            generation: 当前会话或设备的代次，用于拒绝迟到结果更新新状态。

        Raises:
            HostStateError: 前台工具已失去原轮次资格。
        """
        self.runtime.check()
        if not self.runtime.session.eligible(generation):
            raise HostStateError("前台工具已失去原轮次资格。")

    async def run_foreground(self, records: tuple[InputRecord, ...]) -> None:
        """建立本轮工具资格。

        Args:
            records: 按接管顺序取得的完整输入记录。

        Note:
            本轮不再查询新刺激；随后到达的输入由协调者留给下一次唤醒。
        """
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
            observe_usage=self.runtime.subject.observe_usage,
            max_requests=self.runtime.settings.max_requests,
            turn_seconds=self.runtime.settings.turn_seconds,
            retry=self.runtime.subject.retry,
            diagnostic_bytes=self.runtime.settings.diagnostic_bytes,
            refresh=self.runtime.refresh,
            capacity=self.runtime.subject.capacity,
            delivery=delivery,
            prepare_history=self.runtime.prepare_history,
        )
