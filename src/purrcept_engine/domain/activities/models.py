"""保存公开值及其语义校验，不打开数据库或调用设备。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ActivityState = Literal[
    "starting",
    "running",
    "pause_requested",
    "paused",
    "resume_requested",
    "end_requested",
    "ended",
    "completed",
    "adjust_requested",
    "budget_exhausted",
    "interrupted",
    "awaiting_decision",
]


CommandKind = Literal["start", "pause", "resume", "end", "adjust", "extend"]


CommandStatus = Literal["admitted", "accepted", "confirmed", "rejected", "unknown"]


CommandCause = Literal["explicit", "budget", "shutdown"]


FeedbackKind = Literal["progress", "completed", "failed", "needs_decision", "reconciled"]


@dataclass(frozen=True, slots=True)
class ActivityRecord:
    """宿主持有的活动快照，业务工作状态与控制状态拥有不同含义。

    granted_seconds 是历次授予总量，used_seconds 是已持久化实际运行累计；
    两者不因暂停、恢复或追加清零。clock_running 表示尚需结清的运行时段，
    checkpoint_at 为成功保存累计所对应的观察时刻，落盘完成可能更晚。
    崩溃后的 timing_incomplete 明确末段缺口，
    needs_reconciliation 阻止仅凭旧快照恢复。work_json 是驱动工作检查点。
    control_version 指向最近控制接管，version 包含累计和反馈提交；同控制代次
    内的计时提交不会使合法专业反馈失效，新控制则撤销旧观察的状态采用资格。
    """

    activity_id: str
    definition: str
    goal: str
    state: ActivityState
    version: int
    run_generation: int
    granted_seconds: float
    used_seconds: float
    checkpoint_at: float | None
    clock_running: bool
    timing_incomplete: bool
    needs_reconciliation: bool
    last_command_id: str
    work_json: str
    progress_json: str | None
    created_at: float
    updated_at: float
    control_version: int = 1

    @property
    def remaining_seconds(self) -> float:
        """已落盘的未来额度；实时派发仍须核对服务的单调时钟累计。"""
        return max(0.0, self.granted_seconds - self.used_seconds)


@dataclass(frozen=True, slots=True)
class ActivityCommand:
    """一次可信命令身份的接管与确认，不以驱动受理代替真实完成。

    version 与 run_generation 固定为接管身份，不随计时检查点改写。actual_state
    是驱动确认的事实；applied 表示它是否仍有资格改变当前活动。旧控制结论
    即使不再适用也保留。unknown 可以被真实迟到确认澄清，不重发旧命令。
    accepted_at 只记录显式受理返回的观察；确认先到时可以为空，不补造时序。
    """

    command_id: str
    activity_id: str
    command: CommandKind
    definition: str | None
    goal: str | None
    additional_seconds: float | None
    reason: str | None
    cause: CommandCause
    prior_state: ActivityState
    run_generation: int
    version: int
    status: CommandStatus
    admitted_at: float
    accepted_at: float | None = None
    confirmed_at: float | None = None
    actual_state: ActivityState | None = None
    applied: bool | None = None
    error_type: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ActivityFeedback:
    """不可变业务反馈；原始材料保留，关键反馈分别等待前台与反思交接。"""

    feedback_id: str
    activity_id: str
    command_id: str | None
    run_generation: int
    version: int
    kind: FeedbackKind
    payload_json: str
    occurred_at: float
    applied: bool
