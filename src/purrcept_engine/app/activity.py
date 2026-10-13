"""将活动专用状态提交者绑定到保留的 me.activity 模型路径。

调用身份和核对材料的实际呈现证据来自执行服务，模型不填写它们。重启首次
resume/end 只交付只读核对，收到材料后的新行动才可明确决定；普通页面控制
不取得该资格。返回受理及当前事实，不把专业驱动受理等同于已经运行。
"""

from pydantic import BaseModel, ConfigDict

from purrcept_engine.execution.catalog import CapabilityDeclaration
from purrcept_engine.execution.protocol import decode_object, encode_json
from purrcept_engine.execution.providers import (
    CallContext,
    CallOutcome,
    CapabilityImplementation,
    CapabilityInputError,
    FunctionCapability,
)
from purrcept_engine.storage.activities import CommandKind
from purrcept_engine.storage.model_requests import public_value
from purrcept_engine.subject.activities import ActivityError, ActivityRecovery, ActivityService


class _ActivityParameters(BaseModel):
    """已批准的有效组合由活动真实边界校验，不悄悄忽略多余参数。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    command: CommandKind
    activity_id: str | None = None
    definition: str | None = None
    goal: str | None = None
    additional_seconds: float | None = None
    reason: str | None = None


ACTIVITY_DECLARATION = CapabilityDeclaration(
    "activity.control",
    "subject",
    "activity",
    "创建、暂停、恢复、结束或调整局部活动；额度追加不会自动恢复。",
    _ActivityParameters,
    10.0,
    'await me.activity(command="start", definition="已登记定义", goal="局部目标")',
    usage=(
        "start 使用 definition/goal；其余控制引用 activity_id，受理与真实确认分别查询。"
        "重启待核对活动首次 resume/end 只返回 reconciliation、decision_required=true、"
        "command=null；收到完整核对材料后，用新的行动明确 resume 或 end。"
        "未知停止状态不能 resume，明确 end 仍需驱动实际确认。"
    ),
    control=True,
)


def activity_capability(service: ActivityService) -> CapabilityImplementation:
    """把同一实际服务绑定到目录声明，内部/权威错误保留原型。"""

    async def invoke(parameters: _ActivityParameters, context: CallContext) -> CallOutcome:
        try:
            control = await service.subject_command(
                context.call_id,
                parameters.command,
                action_id=context.action_id,
                presented_call_ids=context.presented_call_ids,
                activity_id=parameters.activity_id,
                definition=parameters.definition,
                goal=parameters.goal,
                additional_seconds=parameters.additional_seconds,
                reason=parameters.reason,
            )
        except ActivityError as error:
            if error.__cause__ is not None:
                raise
            raise CapabilityInputError(str(error)) from error
        record = await service.activity(control.activity_id)
        if isinstance(control, ActivityRecovery):
            result = decode_object(
                encode_json(
                    {
                        "command": None,
                        "activity": public_value(record),
                        "remaining_seconds": record.remaining_seconds,
                        "reconciliation": {
                            "stopped": control.observation.stopped,
                            "evidence_json": control.observation.evidence_json,
                        },
                        "decision_required": True,
                    }
                )
            )
            return CallOutcome("confirmed", result)
        result = decode_object(
            encode_json(
                {
                    "command": public_value(control),
                    "activity": public_value(record),
                    "remaining_seconds": record.remaining_seconds,
                }
            )
        )
        return CallOutcome("confirmed", result)

    return FunctionCapability(ACTIVITY_DECLARATION, invoke)
