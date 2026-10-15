"""校验活动命令并构造控制候选，不持有锁、驱动、时钟或数据库。

ActivityService 在同一控制锁内提供当前事实；这里只决定候选，不提交状态，
也不把受理请求当作实际停止确认。
"""

from __future__ import annotations

from dataclasses import replace

from purrcept_engine.domain.activities.contracts import (
    ActivityError,
)
from purrcept_engine.domain.activities.models import (
    ActivityRecord,
    ActivityState,
    CommandKind,
)
from purrcept_engine.domain.activities.state import ActivityRequest
from purrcept_engine.domain.activities.validation import positive_seconds, validate_text


def validate_request(
    command_id: str,
    command: CommandKind,
    activity_id: str | None,
    definition: str | None,
    goal: str | None,
    additional_seconds: float | None,
    reason: str | None,
) -> ActivityRequest:
    """校验活动控制的命令种类与字段组合，构造明确请求。

    Args:
        command_id: 控制命令的稳定身份；重复提交须沿用原身份和内容。
        command: 明确的活动控制种类，目标与预算字段须满足对应命令。
        activity_id: 已创建活动的稳定身份。
        definition: 新活动使用的已装配定义名。
        goal: 明确委托给专业活动的目标文本。
        additional_seconds: 本次明确增加的活动预算秒数。
        reason: 供后续核对的明确处置原因。

    Returns:
        字段与命令用途一致的明确活动控制请求。

    Raises:
        ActivityError: 命令不属于 start/pause/resume/end/adjust/extend，字段组合不匹配，
            或所需身份、目标与预算无效。

    Note:
        真实边界精确验证组合，不把无关参数忽略为默认值。
    """
    validate_text(command_id, "command_id")
    if command not in ("start", "pause", "resume", "end", "adjust", "extend"):
        raise ActivityError("活动命令无效。", code="activity_arguments")
    if command == "start":
        if activity_id is not None or additional_seconds is not None or reason is not None:
            raise ActivityError("start 只接受 definition 与 goal。", code="activity_arguments")
        validate_text(definition, "definition")
        validate_text(goal, "goal")
    else:
        validate_text(activity_id, "activity_id")
        if definition is not None:
            raise ActivityError("后续命令不接受 definition。", code="activity_arguments")
        if command == "adjust":
            validate_text(goal, "goal")
            if additional_seconds is not None or reason is not None:
                raise ActivityError("adjust 只接受新 goal。", code="activity_arguments")
        elif command == "extend":
            if goal is not None:
                raise ActivityError("extend 不接受 goal。", code="activity_arguments")
            additional_seconds = positive_seconds(additional_seconds, "additional_seconds")
            validate_text(reason, "reason")
        elif goal is not None or additional_seconds is not None or reason is not None:
            raise ActivityError("该控制只接受 activity_id。", code="activity_arguments")
    return ActivityRequest(
        command_id, command, activity_id, definition, goal, additional_seconds, reason
    )


def control_record(
    current: ActivityRecord,
    request: ActivityRequest,
    now: float,
    *,
    in_flight: bool,
    maximum: float,
) -> ActivityRecord:
    """根据当前活动与明确控制，构造下一提交候选。

    Args:
        current: 规则计算前已确认的活动状态。
        request: 包含命令身份、目标活动与预算参数的明确控制请求。
        now: 本次规则计算采用的时刻，便于同一操作保持一致。
        in_flight: 当前是否仍有尚未归还的专业步骤。
        maximum: 当前业务规则允许的最大预算值。

    Returns:
        活动的控制、预算、运行代次与工作快照。

    Raises:
        ActivityError: 活动已结束、尚有在途步骤、未完成恢复核对，或命令不符合当前状态与预算。

    Note:
        接管控制立即撤销下一步许可；实际运行计时到真实停止确认才结清。
    """
    if current.state in ("ended", "completed"):
        raise ActivityError("已结束活动不能恢复或控制。")
    if request.command == "extend":
        if (
            current.state not in ("paused", "budget_exhausted", "interrupted")
            or current.clock_running
            or in_flight
        ):
            raise ActivityError("追加仅允许已停止且无在途收尾的活动。")
        assert request.additional_seconds is not None
        if current.remaining_seconds + request.additional_seconds > maximum:
            raise ActivityError("追加后剩余额度超过上限。", code="activity_budget")
        return replace(
            current,
            version=current.version + 1,
            last_command_id=request.command_id,
            granted_seconds=current.granted_seconds + request.additional_seconds,
            updated_at=now,
        )
    if request.command == "resume":
        if current.state not in (
            "paused",
            "budget_exhausted",
            "interrupted",
            "awaiting_decision",
        ):
            raise ActivityError("当前状态不能恢复。")
        if current.needs_reconciliation or current.clock_running or in_flight:
            raise ActivityError("恢复前需要明确核对停止状态与在途效果。")
        if current.remaining_seconds <= 0:
            raise ActivityError("活动预算耗尽，须明确追加后再恢复。", code="activity_budget")
        state: ActivityState = "resume_requested"
    elif request.command == "pause":
        if current.state in (
            "pause_requested",
            "end_requested",
            "paused",
            "budget_exhausted",
            "interrupted",
        ):
            raise ActivityError("当前状态不能再次暂停。")
        state = "pause_requested"
    elif request.command == "end":
        if current.needs_reconciliation:
            raise ActivityError("结束前需要主脑核对停止状态与在途效果。")
        if current.state == "end_requested":
            raise ActivityError("活动正在结束。")
        state = "end_requested"
    else:
        if current.state not in ("running", "paused", "awaiting_decision"):
            raise ActivityError("当前状态不能调整目标。")
        state = "adjust_requested"
    return replace(
        current,
        state=state,
        version=current.version + 1,
        run_generation=current.run_generation + (1 if request.command == "resume" else 0),
        last_command_id=request.command_id,
        goal=request.goal if request.goal is not None else current.goal,
        updated_at=now,
    )
