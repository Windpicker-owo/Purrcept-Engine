"""定义可靠反馈桥接、反思进度与有限来源候选快照。

事实按已提交游标交给前台或独立反思，未完成交接仍留在同库待办中。
这些值不持有原文的第二份权威副本，也不自行推进调度。
"""

from __future__ import annotations

from dataclasses import dataclass

import purrcept_engine.domain.activities.models as activities_models
import purrcept_engine.domain.execution.models as execution_models
import purrcept_engine.domain.knowledge.models as knowledge_models
from purrcept_engine.domain.subject.models import TurnRecord


@dataclass(frozen=True, slots=True)
class FeedbackWork:
    """有限的真实来源快照；只在 Store 队列两次交接之间持有。

    Attributes:
        calls: 当前行动已实际接管的逐次调用记录。
        activity: 待交接的真实专业活动反馈集合。
        failures: 尚未由后续成功处理承接的失败轮次事实。
        plans: 已到期或有新结论的可靠计划记录。
        supervision: 需要前台核验并回应的监管事实。
        deliveries: 已登记的完整原文交付记录，保存不等同于已呈现。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    calls: tuple[execution_models.CallRecord, ...]
    activity: tuple[activities_models.ActivityFeedback, ...]
    failures: tuple[TurnRecord, ...]
    plans: tuple[knowledge_models.WakeupPlan, ...]
    supervision: tuple[knowledge_models.SupervisionRecord, ...]
    deliveries: bool

    @property
    def available(self) -> bool:
        """空扫描不申请写队列，避免自己制造变化通知。"""
        return bool(
            self.calls
            or self.activity
            or self.failures
            or self.plans
            or self.supervision
            or self.deliveries
        )


@dataclass(frozen=True, slots=True)
class FeedbackBridge:
    """实际已提交的前台交接，blocked 表示输入额度仍须等待。

    Attributes:
        foreground_ids: 此次桥接实际交给前台的输入身份集合。
        changed: 此操作是否实际改变已提交的业务事实。
        blocked: 是否因正式接管容量不足而尚未完成可靠交接。
        urgent_generations: 已取得真实紧急依据且仍需核验的前台生成代次。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    foreground_ids: tuple[str, ...] = ()
    changed: bool = False
    blocked: bool = False
    urgent_generations: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ReflectionState:
    """独立来源序号和实际成功前台轮数，模型自己的输出不增加 epoch。

    Attributes:
        epoch: 后台可靠来源的已提交单调序号，不等同于模型输出次数。
        successful_turns: 已提交成功的前台轮数，用于后台调度判断。
        last_taken_successful_turns: 上次反思取批时已经观察的成功前台轮数。
        oldest_queued_at: 最早待复核来源入队时刻；None 表示当前没有待办。
        priority: 此来源是否属于需要优先复核的可靠事实。
        next_plan_at: 下一未完成计划的到期时刻；None 表示没有待到期计划。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    epoch: int
    successful_turns: int
    last_taken_successful_turns: int
    oldest_queued_at: float | None
    priority: bool
    next_plan_at: float | None
    next_attempt_at: float | None = None
    budget_remaining: int = 32
    budget_reset_at: float | None = None
    blocked_count: int = 0
    continuation: bool = False
    capacity_blocked_count: int = 0
    waiting_evidence_count: int = 0


@dataclass(frozen=True, slots=True)
class ReflectionSource:
    """一份有限候选的确切版本和已接管游标，不复制原文到调度队列。

    Attributes:
        sequence: 行动内从 1 开始的调用顺序号。
        evidence_id: 已保存原文快照的稳定身份。
        version: 当前反思批次的确切版本，旧代次不再拥有写入资格。
        queued_at: 当前来源加入可靠待办的 Unix 时间戳（秒）。
        cursor: 宿主签发并保存的不透明续读游标，续读同一快照版本。
        priority: 此来源是否属于需要优先复核的可靠事实。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    sequence: int
    evidence_id: str
    version: int
    queued_at: float
    cursor: str | None
    priority: bool
