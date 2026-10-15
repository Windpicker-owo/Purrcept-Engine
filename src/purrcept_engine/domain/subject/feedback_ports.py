"""后台借用的可靠反馈端口，事务确认先于前台交接。"""

from typing import Protocol

import purrcept_engine.domain.subject.feedback_models as subject_feedback_models
from purrcept_engine.domain.knowledge.models import EvidencePage, SupervisionRecord


class FeedbackPort(Protocol):
    """应用借出的同库反馈与反思调度事实，不授予任意 SQL。"""

    async def bridge(
        self, *, now: float, limit: int = 16
    ) -> subject_feedback_models.FeedbackBridge:
        """桥接实际回执、活动双消费者、前台失败及到期计划和有效监管。"""
        ...

    async def dispatch_supervision(
        self, record: SupervisionRecord, *, observed_generation: int
    ) -> subject_feedback_models.FeedbackBridge:
        """即时交接提交前已观察代次的新意见，迟到扫描不能重绑当前代次。"""
        ...

    async def reflection_state(self) -> subject_feedback_models.ReflectionState:
        """读取独立来源和真正成功轮数，不改变复核资格。"""
        ...

    async def reflection_sources(
        self, *, epoch: int, limit: int = 32, after: int = 0
    ) -> tuple[subject_feedback_models.ReflectionSource, ...]:
        """分页读取具体版本；已耗尽的相同覆盖不会反复排到第一批。"""
        ...

    async def reserve_reflection(
        self, *, batch_id: str, epoch: int, pages: tuple[EvidencePage, ...], successful_turns: int
    ) -> bool:
        """在实际模型请求前持久接管原文范围；失败不退回自动重跑。"""
        ...
