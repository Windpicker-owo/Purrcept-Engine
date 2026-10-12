"""将活动专用状态提交者绑定到保留的 me.activity 模型路径。

调用身份来自执行服务，模型不填写 command_id。返回命令受理及当前活动事实，
不把专业驱动已经受理等同于已经运行。Web/嵌入控制借用同一 ActivityService。
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
from purrcept_engine.subject.activities import ActivityError, ActivityService


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
    usage="start 使用 definition/goal；其余控制引用 activity_id，受理与真实确认分别查询。",
    control=True,
)


def activity_capability(service: ActivityService) -> CapabilityImplementation:
    """把同一实际服务绑定到目录声明，内部/权威错误保留原型。"""

    async def invoke(parameters: _ActivityParameters, context: CallContext) -> CallOutcome:
        try:
            command = await service.command(
                context.call_id,
                parameters.command,
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
        record = await service.activity(command.activity_id)
        result = decode_object(
            encode_json(
                {
                    "command": public_value(command),
                    "activity": public_value(record),
                    "remaining_seconds": record.remaining_seconds,
                }
            )
        )
        return CallOutcome("confirmed", result)

    return FunctionCapability(ACTIVITY_DECLARATION, invoke)
