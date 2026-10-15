"""把纯能力声明与类型明确的插件函数绑定，不向插件借出主脑或通用数据库。

FunctionCapability 在真实调用边界仅解析一次参数，再调用插件函数。执行服务
拥有截止时间、并发、资格和持久化；插件函数只拥有它实际完成的业务及结果。
错误由负责结果的边界分类，程序异常不能成为让模型重填参数的建议。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, ValidationError

from purrcept_engine.domain.execution.catalog import CapabilityDeclaration
from purrcept_engine.domain.inputs.models import MediaReference
from purrcept_engine.errors import EngineError
from purrcept_engine.values import JsonObject, JsonValue


class CapabilityInputError(EngineError):
    """调用参数不符合已声明 schema，可在原有行动预算内纠正。"""


@dataclass(frozen=True, slots=True)
class CallContext:
    """宿主分配的逐次调用身份及绝对单调期限，插件不能自行重置预算。

    Attributes:
        call_id: 实际能力调用的唯一身份，用于保存启动门槛及结果版本。
        action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
        deadline: 所属事件循环的绝对单调截止时间，不是 Unix 时间戳。
        input_id: 正式接管返回的输入身份，用于关联原文与后续处理。
        presented_call_ids: 主体交付门槛确认已完整呈现的调用身份；空集合不授予恢复资格。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        插件可以把 call_id 传给原生幂等协议或关联播放/控制；它不意味着平台
        本身保证恰好一次。来源用于因果追溯，不能被解释为人员权限等级。
        presented_call_ids 来自本轮成功模型请求的完整确认正文，Worker及模型
        参数不能填写；UNKNOWN空回执及同响应尚未交付的核对不在此集合中。
    """

    call_id: str
    action_id: str
    deadline: float
    input_id: str | None = None
    presented_call_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class CallOutcome:
    """插件返回的真实业务结论和 JSON 材料，执行服务保存后再交 Worker。

    Attributes:
        state: 当前所有者持有的状态或待提交状态值。
        value: 真实业务结果，仅支持 JSON 值；部分成功或未知字段须由插件明确保留。
        error_type: 回执中稳定的错误类别，无错误时为 None。
        error: 当前流程已经取得的真实故障或失败说明。
        media: 已接管媒体的引用集合，展开时必须取得原始 BLOB。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        confirmed 表示插件按具体协议取得确认；unknown 保留无法确认的效果，
        failed 表示明确失败。value 中仍应分别表达部分效果和未知字段，不以
        请求参数补成实际结果。此值不依赖后续 Worker 或会话提交是否成功。
    """

    state: Literal["confirmed", "failed", "unknown"]
    value: JsonValue
    error_type: str | None = None
    error: str | None = None
    media: tuple[MediaReference, ...] = field(default=(), kw_only=True)


class CapabilityImplementation(Protocol):
    """由插件 build 创建、由执行服务调用的冻结能力实现。

    declaration 必须与纯声明阶段的同一能力契约对应。invoke 只在宿主
    完成实际启动资格、锁及期限检查之后调用；取消不构成外部副作用回滚。
    宿主等待在途使用结束后才关闭插件资源。内置函数使用 FunctionCapability，
    外部实现可直接遵循此契约，不取得主脑会话或 Worker 的真实对象。
    """

    @property
    def declaration(self) -> CapabilityDeclaration[BaseModel]:
        """提供该实现绑定的纯契约，不以设备实时状态修改目录。"""
        ...

    async def invoke(self, arguments: JsonObject, context: CallContext) -> CallOutcome:
        """在真实边界校验参数并报告具体结果，内部异常原样传播。

        Args:
            arguments: 按已装配参数模型校验的能力关键字参数。
            context: 宿主或 Core 注入的可信调用上下文，不由模型自行填写。

        Returns:
            实际确认、失败或未知结论，以及可 JSON 化的业务值和原生媒体引用。

        Raises:
            CapabilityInputError: 实际参数不满足声明中的参数模型。
        """
        ...


@dataclass(frozen=True, slots=True)
class FunctionCapability[Parameters: BaseModel]:
    """用一个 Pydantic 参数模型连接声明与插件函数，避免重复解析或强制 cast。

    Attributes:
        declaration: 待绑定或计量的不可变声明。
        function: 待包装的工具入口，保留原有参数和调用身份注入。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    declaration: CapabilityDeclaration[Parameters]
    function: Callable[[Parameters, CallContext], Awaitable[CallOutcome]]

    async def invoke(self, arguments: JsonObject, context: CallContext) -> CallOutcome:
        """按声明参数模型调用插件函数，返回真实业务回执。

        Args:
            arguments: 按已装配参数模型校验的能力关键字参数。
            context: 宿主或 Core 注入的可信调用上下文，不由模型自行填写。

        Returns:
            实际确认、失败或未知结论，以及可 JSON 化的业务值和原生媒体引用。

        Raises:
            CapabilityInputError: 参数、材料或当前状态不满足此入口的约束。

        Note:
            参数不合法时只报告字段原因，不把插件程序错误翻译成输入错误。
        """
        try:
            parameters = self.declaration.parameters.model_validate(arguments)
        except ValidationError as error:
            issues = error.errors(include_input=False, include_context=False, include_url=False)
            descriptions = [
                f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
                for issue in issues
            ]
            raise CapabilityInputError("能力参数无效：" + "; ".join(descriptions)) from None
        return await self.function(parameters, context)


def bind_implementations(
    implementations: tuple[CapabilityImplementation, ...],
) -> Mapping[str, CapabilityImplementation]:
    """按能力身份收敛实际实现，重复绑定在启动期失败。

    Args:
        implementations: 与冻结目录逐项对应的宿主能力实现。

    Returns:
        按能力身份索引的只读实现快照。

    Raises:
        ValueError: 同一能力有重复实现绑定。
    """
    from types import MappingProxyType

    by_id: dict[str, CapabilityImplementation] = {}
    for implementation in implementations:
        capability_id = implementation.declaration.capability_id
        if capability_id in by_id:
            raise ValueError("同一能力有重复实现绑定。")
        by_id[capability_id] = implementation
    return MappingProxyType(by_id)
