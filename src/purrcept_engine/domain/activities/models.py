"""定义活动、控制命令、专业步骤和反馈的不可变记录。

这些值连接活动服务、驱动与同库账本；运行代次和版本用于核验迟到确认。
本模块仅校验记录形状，不取得驱动、不提交状态或执行外部动作。
"""

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

    Attributes:
        activity_id: 已创建活动的稳定身份。
        definition: 新活动使用的已装配定义名。
        goal: 明确委托给专业活动的目标文本。
        state: 已提交的控制状态，包括请求、实际运行、暂停与结束阶段。
        version: 目标记录的确切版本，区别于可变的当前版本。
        run_generation: 活动当前运行代次；旧代次只能留下历史事实。
        granted_seconds: 此活动累计明确获得的秒数预算。
        used_seconds: 已确认累计运行的秒数，不因暂停或恢复清零。
        checkpoint_at: 最近一次持久保存运行累计的墙钟时刻。
        clock_running: 当前是否仍在累计活动运行时间。
        timing_incomplete: 上次进程中断导致末段计时不完整的事实标记。
        needs_reconciliation: 是否须先只读核对并由主体明确决策，不能直接恢复。
        last_command_id: 最近一次控制命令的稳定身份。
        work_json: 驱动报告的工作快照 JSON，不替代宿主控制状态。
        progress_json: 驱动实际报告的进展 JSON；None 表示尚无进展。
        created_at: 记录首次建立的 Unix 时间戳（秒）。
        updated_at: 此记录最近一次实际变更的 Unix 时间戳（秒）。
        control_version: 当前控制状态的版本，驱动确认须关联对应命令。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

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
        """已落盘的未来额度。

        Note:
            实时派发仍须核对服务的单调时钟累计。
        """
        return max(0.0, self.granted_seconds - self.used_seconds)


@dataclass(frozen=True, slots=True)
class ActivityCommand:
    """一次可信命令身份的接管与确认，不以驱动受理代替真实完成。

    Attributes:
        command_id: 控制命令的稳定身份；重复提交须沿用原身份和内容。
        activity_id: 已创建活动的稳定身份。
        command: 明确的活动控制种类，目标与预算字段须满足对应命令。
        definition: 新活动使用的已装配定义名。
        goal: 明确委托给专业活动的目标文本。
        additional_seconds: 本次明确增加的活动预算秒数。
        reason: 供后续核对的明确处置原因。
        cause: 本次控制的可信来源，区别于模型提供的原因文字。
        prior_state: 命令接管前已提交的活动状态。
        run_generation: 活动当前运行代次；旧代次只能留下历史事实。
        version: 目标记录的确切版本，区别于可变的当前版本。
        status: 这项命令的受理与确认阶段，不替代 actual_state 和 applied。
        admitted_at: 权威接管此工作的 Unix 时间戳（秒）。
        accepted_at: 驱动实际接管命令的时刻；None 表示尚未确认接管。
        confirmed_at: 实际确认返回的时刻；None 表示尚未取得确认。
        actual_state: 驱动报告的真实状态；None 保留未确认。
        applied: 此控制或反馈是否被当前版本实际采用，区别于历史已保存。
        error_type: 回执中稳定的错误类别，无错误时为 None。
        error: 当前流程已经取得的真实故障或失败说明。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

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
    """不可变业务反馈；原始材料保留，关键反馈分别等待前台与反思交接。

    Attributes:
        feedback_id: 驱动反馈的稳定身份，用于去重和查询实际确认。
        activity_id: 已创建活动的稳定身份。
        command_id: 控制命令的稳定身份；重复提交须沿用原身份和内容。
        run_generation: 活动当前运行代次；旧代次只能留下历史事实。
        version: 目标记录的确切版本，区别于可变的当前版本。
        kind: 此业务值所属的稳定种类。
        payload_json: 已编码的业务事实，不包含任意 Python 对象。
        occurred_at: 原始事实实际发生的 Unix 时间戳（秒）。
        applied: 此控制或反馈是否被当前版本实际采用，区别于历史已保存。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    feedback_id: str
    activity_id: str
    command_id: str | None
    run_generation: int
    version: int
    kind: FeedbackKind
    payload_json: str
    occurred_at: float
    applied: bool
