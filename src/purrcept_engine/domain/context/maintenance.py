"""冻结可压缩历史并验证摘要提案，将安装交给权威会话事务。

这里不调用模型、不循环重试摘要、不改 Core 工具循环。来源是一次带版本的
完整检查点，保护近期真实完整轮次、当前后缀及显式未决材料。摘要占位不新增
真实轮次；原文归档、候选检查点、摘要来源和版本更新由 MaintenanceStore
一次提交，成功后主体才能替换内存状态。失败、过期或忙碌保留旧检查点。
"""

from __future__ import annotations

import hashlib
from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from purrcept_core.models import ConversationCheckpoint, ConversationState, Message, TextBlock

from purrcept_engine.domain.context.capacity import CapacityAssessment
from purrcept_engine.errors import EngineError
from purrcept_engine.serialization import encode


class SummaryError(EngineError):
    """来源或候选不满足保护、缩小和容量约束，不能安装。"""


@dataclass(frozen=True, slots=True)
class SummarySource:
    """一个所有者/版本的完整冻结来源，覆盖一个连续、无保护材料的完整轮次区间。

    Attributes:
        owner_id: 发起本次工作并持有提交资格的所有者身份。
        source_version: 冻结来源的会话版本，安装摘要前必须仍然匹配。
        checkpoint: 完整冻结的源检查点，提交摘要时同时归档并核验保护范围。
        start_turn: 覆盖的首个完整轮次下标，保留此前消息；零表示从历史开头开始。
        end_turn: 摘要覆盖的轮次排他上界。
        end_message: 摘要覆盖的消息排他上界。
        coverage_id: 摘要实际覆盖范围的稳定关联身份。
        protected_turns: 历史尾部必须完整保留的轮次数。
        protected_message_indices: 不能被本次摘要覆盖的消息下标。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        coverage_id 只散列这次真正覆盖的消息与边界，不包含实时版本或提醒，避免
        状态变化却没有新增可压缩历史时重复摘要。checkpoint 仍完整保留，供事务
        归档与保护核验；messages 是摘要模型允许读取的旧完整轮次区间。
    """

    owner_id: str
    source_version: int
    checkpoint: ConversationCheckpoint
    end_turn: int
    end_message: int
    coverage_id: str
    protected_turns: int = 2
    protected_message_indices: tuple[int, ...] = ()
    start_turn: int = 0

    @property
    def start_message(self) -> int:
        """返回覆盖起点；轮次零包含历史开头尚无独立边界的摘要占位。"""
        return (
            self.checkpoint.history.turn_boundaries[self.start_turn - 1] if self.start_turn else 0
        )

    @property
    def turn_count(self) -> int:
        """返回真实覆盖轮数，缩小摘要输入和限制请求数量都使用区间长度。"""
        return self.end_turn - self.start_turn

    @property
    def messages(self) -> tuple[Message, ...]:
        """返回选定区间，近期、当前和显式未决材料没有混入可改写来源。"""
        return self.checkpoint.history.messages[self.start_message : self.end_message]


@dataclass(frozen=True, slots=True)
class SummaryProposal:
    """已经完成完整候选计量的提案，记录实际摘要请求引用和冻结来源。

    Attributes:
        source: 已冻结的历史范围及所属会话版本。
        summary_request_id: 生成此摘要的真实逻辑请求身份。
        summary: 已冻结的摘要文本或消息，不替代原文已读证明。
        candidate: 候选 Core 检查点，安装前仍须完成来源和容量核验。
        before: 摘要安装前完整请求的容量评估。
        after: 候选摘要安装后完整请求的容量评估。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

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
    在途；同覆盖不能并发重复计算。失败或取消须 release_summary 归还占位，
    后续独立维护可以按新预算恢复。install_summary 重验版本及 idle，
    同事务归档原检查点、保存候选与摘要来源关系并递增会话版本。返回新版本
    意味提交已完成，None 意味当前版本或空闲条件不再成立，旧状态不变。
    原始材料不删除；等待取消遵守 Store 的已接管事务规则。
    """

    async def reserve_summary(self, source: SummarySource) -> Reservation:
        """可靠登记一次覆盖的尝试资格，不调用模型或自动更换来源。

        Args:
            source: 已冻结的历史范围及所属会话版本。

        Returns:
            reserved 表示已取得独占占位；already_attempted 表示覆盖已有在途计算或已安装；stale 或
            busy 表示当前版本或空闲条件不允许维护。
        """
        ...

    async def install_summary(self, proposal: SummaryProposal) -> int | None:
        """原子安装经校验候选。

        Args:
            proposal: 已冻结来源和内容的候选提案，提交前仍须验证。

        Returns:
            实际安装后的新会话版本；None 表示来源版本或空闲条件已改变，没有安装候选。

        Note:
            提交失败原样传播，不先更新内存会话。
        """
        ...

    async def release_summary(self, source: SummarySource) -> None:
        """归还同一冻结来源的未安装占位，不移除成功归档或原请求证据。

        Args:
            source: 本次调用已成功保留的来源，不允许释放其它版本的在途计算。

        Note:
            维护所有者在失败、取消及候选过期后调用；成功安装后调用无效果。
            进程中断留下的占位由独占数据目录锁内的启动恢复归还。
        """
        ...


def freeze_summary_source(
    checkpoint: ConversationCheckpoint,
    *,
    owner_id: str,
    source_version: int,
    protected_turns: int = 2,
    protected_message_indices: Iterable[int] = (),
) -> SummarySource | None:
    """冻结最长连续可压缩轮次区间，等长时优先较早区间。

    Args:
        checkpoint: 完整 Core 检查点；None 表示尚无已提交历史。
        owner_id: 发起本次工作并持有提交资格的所有者身份。
        source_version: 冻结来源的会话版本，安装摘要前必须仍然匹配。
        protected_turns: 历史尾部必须完整保留的轮次数。 默认 2。
        protected_message_indices: 不能被本次摘要覆盖的消息下标。 默认不附加此类条目。

    Returns:
        可维护历史的完整冻结范围、保护边界与来源版本。

    Raises:
        ValueError: 摘要来源需要明确所有者及非负版本；保护轮数不能为负。

    Note:
        未决保护保留其所在的整个真实轮次，不阻断其后的独立历史区间。

        Core turn_boundaries 只表示真实完整轮次，当前未提交后缀自然排除。调用方
        从实际未决记录给出受保护消息位置，不靠正文猜测完成或退役。没有旧完整
        区间返回 None；能否开始由权威 reserve_summary 的同覆盖/版本/idle 判定。
    """
    if not owner_id or isinstance(source_version, bool) or source_version < 0:
        raise ValueError("摘要来源需要明确所有者及非负版本。")
    if isinstance(protected_turns, bool) or protected_turns < 0:
        raise ValueError("保护轮数不能为负。")
    history = checkpoint.history
    protected_positions = tuple(protected_message_indices)
    ranges = _eligible_ranges(checkpoint, protected_turns, protected_positions)
    if not ranges:
        return None
    start_turn, end_turn = max(ranges, key=lambda bounds: bounds[1] - bounds[0])
    end_message = history.turn_boundaries[end_turn - 1]
    coverage = _coverage_id(checkpoint, start_turn, end_turn)
    return SummarySource(
        owner_id,
        source_version,
        checkpoint,
        end_turn,
        end_message,
        coverage,
        protected_turns,
        protected_positions,
        start_turn,
    )


def _eligible_ranges(
    checkpoint: ConversationCheckpoint,
    protected_turns: int,
    positions: tuple[int, ...],
) -> tuple[tuple[int, int], ...]:
    """按完整轮次切开保护位置，返回近期轮次之前的全部可压缩连续区间。

    消息位置只能保护其所在的整个轮次，不能拆开工具调用和回执。当前未提交
    后缀不属于任何完整轮次；前置旧摘要随其后的首轮一起选择或保留。
    """
    history = checkpoint.history
    cutoff = max(0, len(history.turn_boundaries) - protected_turns)
    blocked: set[int] = set()
    for position in positions:
        if (
            isinstance(position, bool)
            or not isinstance(position, int)
            or not 0 <= position < len(history.messages)
        ):
            raise ValueError("未决保护位置不属于冻结历史。")
        blocked.add(bisect_right(history.turn_boundaries, position))

    ranges: list[tuple[int, int]] = []
    start = 0
    for index in sorted(blocked):
        if index >= cutoff:
            break
        if start < index:
            ranges.append((start, index))
        start = index + 1
    if start < cutoff:
        ranges.append((start, cutoff))
    return tuple(ranges)


def _coverage_id(checkpoint: ConversationCheckpoint, start_turn: int, end_turn: int) -> str:
    """身份覆盖确切区间、消息与边界；不因来源版本或动态提醒变化而重算身份。"""
    history = checkpoint.history
    start = history.turn_boundaries[start_turn - 1] if start_turn else 0
    end = history.turn_boundaries[end_turn - 1]
    encoded = encode(
        {
            "start_turn": start_turn,
            "messages": history.messages[start:end],
            "turn_boundaries": tuple(
                bound - start for bound in history.turn_boundaries[start_turn:end_turn]
            ),
        }
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def shorten_summary_source(source: SummarySource, end_turn: int) -> SummarySource:
    """在原冻结区间内缩短结尾，覆盖起点和区间外的原文始终保持不变。

    Args:
        source: 已冻结的历史范围及所属会话版本。
        end_turn: 摘要覆盖的轮次排他上界。

    Returns:
        可维护历史的完整冻结范围、保护边界与来源版本。

    Raises:
        ValueError: 摘要只能缩短到原覆盖内的正整数轮次。

    Note:
        只改变本次摘要覆盖及其身份，不截断消息或工具配对，也不扩大允许压缩范围。
        调用方在模型派发前按实际摘要容量选择，候选安装仍须核验整个主脑请求。
    """
    if isinstance(end_turn, bool) or not source.start_turn < end_turn <= source.end_turn:
        raise ValueError("摘要只能缩短到原覆盖内的正整数轮次。")
    end_message = source.checkpoint.history.turn_boundaries[end_turn - 1]
    return replace(
        source,
        end_turn=end_turn,
        end_message=end_message,
        coverage_id=_coverage_id(source.checkpoint, source.start_turn, end_turn),
    )


def candidate_checkpoint(source: SummarySource, summary: Message) -> ConversationCheckpoint:
    """在原位置用文本摘要替换冻结区间，完整保留区间外消息、提醒与 continuation。

    Args:
        source: 已冻结的历史范围及所属会话版本。
        summary: 已冻结的摘要文本或消息，不替代原文已读证明。

    Returns:
        可供 Core 无损恢复的完整检查点，未知格式或损坏媒体会明确失败。

    Raises:
        SummaryError: 典型拒绝原因：摘要候选必须只包含实际文本；空摘要不能替换旧历史；
            摘要覆盖不是冻结来源的完整轮次边界；摘要来源覆盖身份或保护条件被改变。

    Note:
        摘要内容与模型可见来源标注由调用方按已确认提示构造，本模块不改写文字。
        占位没有单独 turn boundary，retained boundaries 仍只计真实完整轮次。
    """
    if not summary.content or not all(isinstance(block, TextBlock) for block in summary.content):
        raise SummaryError("摘要候选必须只包含实际文本。")
    if not any(block.text.strip() for block in summary.content if isinstance(block, TextBlock)):
        raise SummaryError("空摘要不能替换旧历史。")
    history = source.checkpoint.history
    if (
        isinstance(source.start_turn, bool)
        or not isinstance(source.start_turn, int)
        or not 0 <= source.start_turn < source.end_turn <= len(history.turn_boundaries)
        or history.turn_boundaries[source.end_turn - 1] != source.end_message
    ):
        raise SummaryError("摘要覆盖不是冻结来源的完整轮次边界。")
    if (
        source.protected_turns < 0
        or not any(
            start <= source.start_turn < source.end_turn <= end
            for start, end in _eligible_ranges(
                source.checkpoint, source.protected_turns, source.protected_message_indices
            )
        )
        or source.coverage_id != _coverage_id(source.checkpoint, source.start_turn, source.end_turn)
    ):
        raise SummaryError("摘要来源覆盖身份或保护条件被改变。")

    # Ordering: 摘要回到原区间位置，不能移到受保护轮次之前或打断工具配对。
    # 它不新增真实轮次，前置保留轮次的边界不变，后置边界只按消息差量平移。
    shift = source.start_message + 1 - source.end_message
    current = ConversationState(
        (
            *history.messages[: source.start_message],
            summary,
            *history.messages[source.end_message :],
        ),
        continuation=history.continuation,
        turn_index=history.turn_index - source.turn_count,
        turn_boundaries=(
            *history.turn_boundaries[: source.start_turn],
            *(end + shift for end in history.turn_boundaries[source.end_turn :]),
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
    """核验真实缩小且在输入预算内，目标未达不能变成反复摘要的理由。

    Args:
        source: 已冻结的历史范围及所属会话版本。
        summary: 已冻结的摘要文本或消息，不替代原文已读证明。
        summary_request_id: 生成此摘要的真实逻辑请求身份。
        before: 摘要安装前完整请求的容量评估。
        after: 候选摘要安装后完整请求的容量评估。

    Returns:
        已经通过完整请求容量检查的摘要候选及真实请求引用。
    """
    proposal = SummaryProposal(
        source, summary_request_id, summary, candidate_checkpoint(source, summary), before, after
    )
    validate_summary_proposal(proposal)
    return proposal


def validate_summary_proposal(proposal: SummaryProposal) -> None:
    """提交边界核验候选保护及计量依据，不信任任意手工构造的 dataclass。

    Args:
        proposal: 已冻结来源和内容的候选提案，提交前仍须验证。

    Raises:
        SummaryError: 典型拒绝原因：摘要必须关联实际模型请求；摘要候选改写了保护历史或会话状态；
            摘要计量需要非负整数用量及正整数输入分母；摘要候选前后不是同一完整请求计量依据。
    """
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
        or before.estimate.policy != after.estimate.policy
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

    Args:
        store: 应用借出的领域存储端口，实际连接和关闭归资源所有者。
        proposal: 已冻结来源和内容的候选提案，提交前仍须验证。

    Returns:
        实际安装的新会话版本；None 表示来源过期或会话忙碌。

    Note:
        None 不是模型成功，也不自动重试；原提案可保存为过期证据。事务失败保持
        异常原型，旧检查点仍由调用方和 Store 持有。
    """
    validate_summary_proposal(proposal)
    return await store.install_summary(proposal)
