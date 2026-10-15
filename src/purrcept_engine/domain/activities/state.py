"""保存活动服务的运行计时、在途步骤及恢复呈现证明。

内存事实辅助唯一提交者 ActivityService 计算下一状态；恢复证明绑定真实
调用和完整材料交付。这里不独立提交活动状态，也不创建第二份预算。
"""

from __future__ import annotations

from dataclasses import dataclass

from purrcept_engine.domain.activities.contracts import (
    RecoveryObservation,
)
from purrcept_engine.domain.activities.models import (
    CommandCause,
    CommandKind,
)


@dataclass(slots=True)
class ActivityClock:
    """单调起点仅存于本次宿主运行，不跨崩溃推算未记录时段。

    Attributes:
        run_generation: 活动当前运行代次；旧代次只能留下历史事实。
        used_at_start: 当前计时段开始时已经提交的累计秒数。
        started_at: 本次真实工作开始的时刻，未开始时保留 None。
        checkpoint_at: 最近一次持久保存运行累计的墙钟时刻。
    """

    run_generation: int
    used_at_start: float
    started_at: float
    checkpoint_at: float


@dataclass(frozen=True, slots=True)
class ActivityRequest:
    """宿主已校验的控制意图；身份和恢复资格不由模型补写。

    Attributes:
        command_id: 控制命令的稳定身份；重复提交须沿用原身份和内容。
        command: 明确的活动控制种类，目标与预算字段须满足对应命令。
        activity_id: 已创建活动的稳定身份。
        definition: 新活动使用的已装配定义名。
        goal: 明确委托给专业活动的目标文本。
        additional_seconds: 本次明确增加的活动预算秒数。
        reason: 供后续核对的明确处置原因。
        cause: 本次控制的可信来源，区别于模型提供的原因文字。
        expected_version: 提交前观察到的版本，用于拒绝并发覆盖。
        recovery_version: 取得只读核对材料时冻结的活动版本。
        action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
        presented_call_ids: 主体交付门槛确认已完整呈现的调用身份；空集合不授予恢复资格。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    command_id: str
    command: CommandKind
    activity_id: str | None
    definition: str | None
    goal: str | None
    additional_seconds: float | None
    reason: str | None
    cause: CommandCause = "explicit"
    expected_version: int | None = None
    recovery_version: int | None = None
    action_id: str | None = None
    presented_call_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class RecoveryProof:
    """只对未改变记录有效的核对，后续行动须有原调用材料的真实呈现证据。

    Attributes:
        version: 目标记录的确切版本，区别于可变的当前版本。
        run_generation: 活动当前运行代次；旧代次只能留下历史事实。
        action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
        call_id: 实际能力调用的唯一身份，用于保存启动门槛及结果版本。
        observation: 显式核对取得的实际观察，不能通过重放旧写操作生成。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    version: int
    run_generation: int
    action_id: str
    call_id: str
    observation: RecoveryObservation
