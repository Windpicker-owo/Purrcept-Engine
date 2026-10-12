"""冻结可压缩历史并验证摘要提案，将安装交给权威会话事务。

这里不调用模型、不循环重试摘要、不改 Core 工具循环。来源是一次带版本的
完整检查点，保护近期真实完整轮次、当前后缀及显式未决材料。摘要占位不新增
真实轮次；原文归档、候选检查点、摘要来源和版本更新由 MaintenanceStore
一次提交，成功后主体才能替换内存状态。失败、过期或忙碌保留旧检查点。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, Protocol

from purrcept_core.models import ConversationCheckpoint, ConversationState, Message, TextBlock

from purrcept_engine.errors import EngineError
from purrcept_engine.storage.model_requests import encode

from .capacity import CapacityAssessment


class SummaryError(EngineError):
    """来源或候选不满足保护、缩小和容量约束，不能安装。"""


@dataclass(frozen=True, slots=True)
class SummarySource:
    """一个所有者/版本的完整冻结来源，end_turn/end_message 为排他覆盖边界。

    coverage_id 只散列这次真正覆盖的消息与边界，不包含实时版本或提醒，避免
    状态变化却没有新增可压缩历史时重复摘要。checkpoint 仍完整保留，供事务
    归档与保护核验；messages 是摘要模型允许读取的旧完整轮次前缀。
    """

    owner_id: str
    source_version: int
    checkpoint: ConversationCheckpoint
    end_turn: int
    end_message: int
    coverage_id: str
    protected_turns: int = 2
    protected_message_indices: tuple[int, ...] = ()

    @property
    def messages(self) -> tuple[Message, ...]:
        """返回覆盖前缀，近期、当前和显式未决材料没有混入可改写来源。"""
        return self.checkpoint.history.messages[: self.end_message]


@dataclass(frozen=True, slots=True)
class SummaryProposal:
    """已经完成完整候选计量的提案，记录实际摘要请求引用和冻结来源。

    before/after 必须使用同一计量依据、分母及完整请求布局；target_met 只用于
    展示，不阻止一个真实缩小且在预算内的候选。candidate 尚未成为正式会话。
    """

    source: SummarySource
    summary_request_id: str
    summary: Message
    candidate: ConversationCheckpoint
    before: CapacityAssessment
    after: CapacityAssessment


Reservation = Literal["reserved", "already_attempted", "stale", "busy"]


class MaintenanceStore(Protocol):
    """主体借用的有限历史维护提交面，生产实现复用 Store 专属连接。

    reserve_summary 在摘要模型启动前核验版本和 idle，并可靠记录该 coverage
    已尝试；同覆盖不因模型失败自动重跑。install_summary 重验版本及 idle，
    同事务归档原检查点、保存候选与摘要来源关系并递增会话版本。返回新版本
    意味提交已完成，None 意味当前版本或空闲条件不再成立，旧状态不变。
    原始材料不删除；等待取消遵守 Store 的已接管事务规则。
    """

    async def reserve_summary(self, source: SummarySource) -> Reservation:
        """可靠登记一次覆盖的尝试资格，不调用模型或自动更换来源。"""
        ...

    async def install_summary(self, proposal: SummaryProposal) -> int | None:
        """原子安装经校验候选；提交失败原样传播，不先更新内存会话。"""
        ...


def freeze_summary_source(
    checkpoint: ConversationCheckpoint,
    *,
    owner_id: str,
    source_version: int,
    protected_turns: int = 2,
    protected_message_indices: Iterable[int] = (),
) -> SummarySource | None:
    """冻结最长可压缩完整前缀；未决保护会提前截断，不能跨过它补压缩。

    Core turn_boundaries 只表示真实完整轮次，当前未提交后缀自然排除。调用方
    从实际未决记录给出受保护消息位置，不靠正文猜测完成或退役。没有旧完整
    前缀返回 None；能否开始由权威 reserve_summary 的同覆盖/版本/idle 判定。
    """
    if not owner_id or isinstance(source_version, bool) or source_version < 0:
        raise ValueError("摘要来源需要明确所有者及非负版本。")
    if isinstance(protected_turns, bool) or protected_turns < 0:
        raise ValueError("保护轮数不能为负。")
    history = checkpoint.history
    protected_positions = tuple(protected_message_indices)
    cutoff = _eligible_turns(checkpoint, protected_turns, protected_positions)
    if cutoff == 0:
        return None
    end_message = history.turn_boundaries[cutoff - 1]
    coverage = _coverage_id(checkpoint, cutoff, end_message)
    return SummarySource(
        owner_id,
        source_version,
        checkpoint,
        cutoff,
        end_message,
        coverage,
        protected_turns,
        protected_positions,
    )


def _eligible_turns(
    checkpoint: ConversationCheckpoint,
    protected_turns: int,
    positions: tuple[int, ...],
) -> int:
    """保护材料把可压缩前缀提前截止，消息位置不改变完整轮次的计数。"""
    history = checkpoint.history
    cutoff = max(0, len(history.turn_boundaries) - protected_turns)
    for position in positions:
        if (
            isinstance(position, bool)
            or not isinstance(position, int)
            or not 0 <= position < len(history.messages)
        ):
            raise ValueError("未决保护位置不属于冻结历史。")
        for index, end in enumerate(history.turn_boundaries):
            if position < end:
                cutoff = min(cutoff, index)
                break
    return cutoff


def _coverage_id(checkpoint: ConversationCheckpoint, end_turn: int, end_message: int) -> str:
    encoded = encode(
        {
            "messages": checkpoint.history.messages[:end_message],
            "turn_boundaries": checkpoint.history.turn_boundaries[:end_turn],
        }
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def candidate_checkpoint(source: SummarySource, summary: Message) -> ConversationCheckpoint:
    """用一条文本摘要替换覆盖前缀，完整保留受保护后缀、提醒和 continuation。

    摘要内容与模型可见来源标注由调用方按已确认提示构造，本模块不改写文字。
    占位没有单独 turn boundary，retained boundaries 仍只计真实完整轮次。
    """
    if not summary.content or not all(isinstance(block, TextBlock) for block in summary.content):
        raise SummaryError("摘要候选必须只包含实际文本。")
    if not any(block.text.strip() for block in summary.content if isinstance(block, TextBlock)):
        raise SummaryError("空摘要不能替换旧历史。")
    history = source.checkpoint.history
    if (
        not 0 < source.end_turn <= len(history.turn_boundaries)
        or history.turn_boundaries[source.end_turn - 1] != source.end_message
    ):
        raise SummaryError("摘要覆盖不是冻结来源的完整轮次边界。")
    if (
        source.protected_turns < 0
        or source.end_turn
        > _eligible_turns(
            source.checkpoint, source.protected_turns, source.protected_message_indices
        )
        or source.coverage_id
        != _coverage_id(source.checkpoint, source.end_turn, source.end_message)
    ):
        raise SummaryError("摘要来源覆盖身份或保护条件被改变。")
    current = ConversationState(
        (summary, *history.messages[source.end_message :]),
        continuation=history.continuation,
        turn_index=history.turn_index - source.end_turn,
        turn_boundaries=tuple(
            1 + end - source.end_message for end in history.turn_boundaries[source.end_turn :]
        ),
    )
    return ConversationCheckpoint(current, source.checkpoint.reminders)


def build_summary_proposal(
    source: SummarySource,
    summary: Message,
    *,
    summary_request_id: str,
    before: CapacityAssessment,
    after: CapacityAssessment,
) -> SummaryProposal:
    """核验真实缩小且在输入预算内，目标未达不能变成反复摘要的理由。"""
    proposal = SummaryProposal(
        source, summary_request_id, summary, candidate_checkpoint(source, summary), before, after
    )
    validate_summary_proposal(proposal)
    return proposal


def validate_summary_proposal(proposal: SummaryProposal) -> None:
    """提交边界核验候选保护及计量依据，不信任任意手工构造的 dataclass。"""
    if not proposal.summary_request_id:
        raise SummaryError("摘要必须关联实际模型请求。")
    if proposal.candidate != candidate_checkpoint(proposal.source, proposal.summary):
        raise SummaryError("摘要候选改写了保护历史或会话状态。")
    before, after = proposal.before, proposal.after
    for assessment in (before, after):
        units = assessment.estimate.input_units
        budget = assessment.input_budget
        if (
            isinstance(units, bool)
            or not isinstance(units, int)
            or units < 0
            or isinstance(budget, bool)
            or not isinstance(budget, int)
            or budget <= 0
        ):
            raise SummaryError("摘要计量需要非负整数用量及正整数输入分母。")
    if (
        before.input_budget != after.input_budget
        or before.estimate.method != after.estimate.method
        or before.estimate.unit != after.estimate.unit
        or before.estimate.scope != after.estimate.scope
    ):
        raise SummaryError("摘要候选前后不是同一完整请求计量依据。")
    if after.estimate.input_units >= before.estimate.input_units:
        raise SummaryError("摘要候选没有真正缩小完整请求。")
    if before.payload_sha256 == after.payload_sha256:
        raise SummaryError("相同实际请求不能声称已经缩小。")
    if not after.within_budget:
        raise SummaryError("摘要后必要材料仍超出输入预算。")


async def install_summary(store: MaintenanceStore, proposal: SummaryProposal) -> int | None:
    """验证后委托原子安装，调用方只在返回新版本后替换内存检查点。

    None 不是模型成功，也不自动重试；原提案可保存为过期证据。事务失败保持
    异常原型，旧检查点仍由调用方和 Store 持有。
    """
    validate_summary_proposal(proposal)
    return await store.install_summary(proposal)
