"""保存公开值及其语义校验，不打开数据库或调用设备。"""

from __future__ import annotations

from dataclasses import dataclass

import purrcept_engine.domain.activities.models as activities_models
import purrcept_engine.domain.execution.models as execution_models
import purrcept_engine.domain.knowledge.models as knowledge_models
from purrcept_engine.domain.subject.models import TurnRecord


@dataclass(frozen=True, slots=True)
class FeedbackWork:
    """有限的真实来源快照；只在 Store 队列两次交接之间持有。"""

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
    """实际已提交的前台交接，blocked 表示输入额度仍须等待。"""

    foreground_ids: tuple[str, ...] = ()
    changed: bool = False
    blocked: bool = False
    urgent_generations: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ReflectionState:
    """独立来源序号和实际成功前台轮数，模型自己的输出不增加 epoch。"""

    epoch: int
    successful_turns: int
    last_taken_successful_turns: int
    oldest_queued_at: float | None
    priority: bool
    next_plan_at: float | None


@dataclass(frozen=True, slots=True)
class ReflectionSource:
    """一份有限候选的确切版本和已接管游标，不复制原文到调度队列。"""

    sequence: int
    evidence_id: str
    version: int
    queued_at: float
    cursor: str | None
    priority: bool
