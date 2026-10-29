"""后台借用的可靠反馈端口，事务确认先于前台交接。"""

from typing import Literal, Protocol

import purrcept_engine.domain.subject.feedback_models as subject_feedback_models
from purrcept_engine.domain.knowledge.models import EvidencePage, SupervisionRecord
from purrcept_engine.values import JsonObject


class FeedbackPort(Protocol):
    """应用借出的同库反馈与反思调度事实，不授予任意 SQL。"""

    async def postpone_preparation(
        self,
        sources: tuple[subject_feedback_models.ReflectionSource, ...],
        *,
        purpose: Literal["memory", "behavior"],
        reason: str,
    ) -> None:
        """准备阶段耗尽批次预算时保留未领取范围，按三次有界退避等待。

        Args:
            sources: 本轮有限候选的确切版本，不采用候选领取游标确认理解。
            purpose: 相互隔离的分析责任。
            reason: 稳定失败分类，不包含第三方异常原文。
        """
        ...

    async def bridge(
        self, *, now: float, limit: int = 16
    ) -> subject_feedback_models.FeedbackBridge:
        """桥接实际回执、活动双消费者、前台失败及到期计划和有效监管。

        Args:
            now: 本次规则计算采用的时刻，便于同一操作保持一致。
            limit: 本次读取或更新的最大条目数。 默认 16。

        Returns:
            实际提交的消费者交接与仍需等待容量的状态。
        """
        ...

    async def dispatch_supervision(
        self, record: SupervisionRecord, *, observed_generation: int
    ) -> subject_feedback_models.FeedbackBridge:
        """即时交接提交前已观察代次的新意见，迟到扫描不能重绑当前代次。

        Args:
            record: 带原始依据与版本约束的监管记录。
            observed_generation: 接管或判断开始时观察到的前台生成代次。

        Returns:
            实际提交的消费者交接与仍需等待容量的状态。
        """
        ...

    async def reflection_state(
        self, *, purpose: Literal["memory", "behavior"] = "memory"
    ) -> subject_feedback_models.ReflectionState:
        """读取独立来源和真正成功轮数，不改变复核资格。

        Returns:
            反思来源序号与实际成功前台轮数。
        """
        ...

    async def reflection_sources(
        self,
        *,
        epoch: int,
        limit: int = 32,
        after: int = 0,
        purpose: Literal["memory", "behavior"] = "memory",
    ) -> tuple[subject_feedback_models.ReflectionSource, ...]:
        """分页读取具体版本。

        Args:
            epoch: 后台可靠来源的已提交单调序号，不等同于模型输出次数。
            limit: 本次读取或更新的最大条目数。 默认 32。
            after: 已观察的变更序号。 默认 0。

        Returns:
            条目集合：待复核来源的确切版本与可靠游标。

        Note:
            已耗尽的相同覆盖不会反复排到第一批。
        """
        ...

    async def reserve_reflection(
        self,
        *,
        batch_id: str,
        epoch: int,
        pages: tuple[EvidencePage, ...],
        successful_turns: int,
        purpose: Literal["memory", "behavior"] = "memory",
    ) -> bool:
        """在实际模型请求前持久接管原文范围。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            epoch: 后台可靠来源的已提交单调序号，不等同于模型输出次数。
            pages: 当前批次实际取得的完整原文页面。
            successful_turns: 已提交成功的前台轮数，用于后台调度判断。

        Note:
            失败不退回自动重跑。
        """
        ...

    async def block_reflection(
        self,
        *,
        source_id: str,
        version: int,
        reason: str,
        purpose: Literal["memory", "behavior"] = "memory",
    ) -> None:
        """标记单项容量阻塞，保留原文且让其他可容纳来源继续排队。"""
        ...

    async def behavior_context(self, identities: tuple[str, ...]) -> JsonObject:
        """读取有限关联刺激和真实行动，不授予额外原文呈现资格。"""
        ...

    async def configure_capacity(self, capacity_key: str) -> None:
        """容量策略变更后重新开放被该策略阻塞的来源；不刷新失败次数或小时额度。"""
        ...
