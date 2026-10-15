"""保存单一活动服务拥有的运行时身份、时钟起点与恢复证据；不提交状态。"""

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
    """单调起点仅存于本次宿主运行，不跨崩溃推算未记录时段。"""

    run_generation: int
    used_at_start: float
    started_at: float
    checkpoint_at: float


@dataclass(frozen=True, slots=True)
class ActivityRequest:
    """宿主已校验的控制意图；身份和恢复资格不由模型补写。"""

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
    """只对未改变记录有效的核对，后续行动须有原调用材料的真实呈现证据。"""

    version: int
    run_generation: int
    action_id: str
    call_id: str
    observation: RecoveryObservation
