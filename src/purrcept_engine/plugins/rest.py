"""内置床的明确休息能力，状态归TimePort的唯一主体，不创建自动唤醒任务。

rest保存明确睡眠开始/更新打算时长，leave明确起身；协调者据同库状态暂停注意
与主动思考，并在本轮结束后尝试一次历史压缩。预计到期不能自动清除状态或
发送消息。工厂纯声明稳定me.bed路径，真实值在调用与动态请求
准备中从同库读取。插件不建立主人身份、周期tick或世界组件兼容层。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from purrcept_engine.errors import ConfigError
from purrcept_engine.plugin_system import (
    CallContext,
    CallOutcome,
    CapabilityDeclaration,
    FunctionCapability,
    Minutes,
    ObjectDeclaration,
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
    typed_factory,
)
from purrcept_engine.values import JsonObject


class BedSettings(BaseModel):
    """只声明床的可见路径与标签，不保存另一份休息状态或人员归属。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    bed_id: str = Field(default="bed", pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    label: str = Field(default="床", min_length=1, pattern=r"\S")


class _RestParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_duration_minutes: Minutes = Field(
        description="预计休息分钟数，必须为1到20160（14天）之间的整数。"
    )


class _Empty(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _declare(instance_id: str, settings: BedSettings, base_dir: Path) -> PluginBlueprint:
    """保留rest分钟参数和无副作用leave，不读取数据库或调度模型。"""
    del base_dir
    object_id = instance_id + ":bed"
    path = "me." + settings.bed_id
    rest = CapabilityDeclaration(
        instance_id + ":bed.rest",
        object_id,
        "rest",
        "在床上躺下休息，记录预期的休息时长（到期不会自动唤醒）。",
        _RestParameters,
        5.0,
        f"await {path}.rest(expected_duration_minutes=480)",
        "需要睡眠、小憩或长时间休整时使用。分钟数为1..20160；"
        "入睡后普通刺激只记历史、不积压到醒来，不调用注意模型；"
        "只有高优先刺激能唤醒，主动思考暂停。"
        "当前轮结束后尝试一次历史压缩，保留近期轮次与未决事实。"
        "再次调用只更新打算时长，不重置开始时间或重复触发睡眠压缩。",
        concurrency="target_serial",
    )
    leave = CapabilityDeclaration(
        instance_id + ":bed.leave",
        object_id,
        "leave",
        "结束当前休息状态，从床上起身并恢复常规活动。",
        _Empty,
        5.0,
        f"await {path}.leave()",
        "休息充足或被高优先刺激唤醒后需要恢复活动时使用。"
        "起身恢复普通注意与主动思考计时；被唤醒本身不会自动起床。醒着时没有副作用。",
        concurrency="target_serial",
    )

    async def build(ports: PluginPorts) -> PluginRuntime:
        """借用本实例的有限端口，创建能力实现与插件资源所有者。"""
        port = ports.time
        if port is None:
            raise ConfigError("bed插件需要宿主时间业务端口。")

        async def run_rest(parameters: _RestParameters, context: CallContext) -> CallOutcome:
            """用真实调用身份保存本次明确休息变更。"""
            change = await port.rest(
                object_id, parameters.expected_duration_minutes, datetime.now(UTC), context.call_id
            )
            return CallOutcome("confirmed", change.to_json())

        async def run_leave(parameters: _Empty, context: CallContext) -> CallOutcome:
            """用真实调用身份明确结束当前休息状态。"""
            del parameters
            change = await port.leave(datetime.now(UTC), context.call_id)
            return CallOutcome("confirmed", change.to_json())

        async def prepare_request() -> JsonObject:
            """投影本插件当前业务状态，供下一逻辑请求构建动态尾部。"""
            state = await port.get_rest()
            return {
                "rest": {"resting": False} if state is None else state.to_json(datetime.now(UTC))
            }

        return PluginRuntime(
            (FunctionCapability(rest, run_rest), FunctionCapability(leave, run_leave)),
            prepare_request=prepare_request,
        )

    return PluginBlueprint(
        (ObjectDeclaration(object_id, settings.bed_id, settings.label.strip(), "subject"),),
        (rest, leave),
        build,
    )


factory: PluginFactory = typed_factory(BedSettings, _declare)
