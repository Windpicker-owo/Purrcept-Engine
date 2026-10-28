"""以已确认输入用量和新增材料估算核验完整请求容量。

应用只声明窗口与输出上限；每次派发前提交真实 SDK 布局的完整材料。
usage 复用同用途、端点及 SDK 布局的实际输入前缀；缺失基线或前缀重写时，
token_estimation 以固定启发式分别估算文字、图片尺寸和音频时长，不按模型
选择规则，不把媒体 base64 算成文本。assess_capacity 为历史维护返回可超额
结论，check_capacity 在派发前执行硬性预算检查；本模块不裁剪材料或发起网络。
原始 JSON 的字节编码仅用于完整性校验和材料指纹，不能代表媒体上下文占用。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass

from purrcept_engine.domain.context.token_estimation import estimate_tokens
from purrcept_engine.domain.context.usage import UsageMeter
from purrcept_engine.errors import EngineError
from purrcept_engine.serialization import encode
from purrcept_engine.values import decode_object


@dataclass(frozen=True, slots=True)
class GenerationCapacity:
    """配置声明的共享窗口与实际输出上限，不由模型名推断。

    Attributes:
        context_window: 模型显式声明的共享上下文窗口容量。
        max_output_tokens: 模型明确允许的最大输出 token 数。
        input_limit: 可选的独立输入参考上限，与共享窗口预算取较小值。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        input_limit 可表达独立输入上限；存在时与扣输出并预留后的共享窗口取较小值。
    """

    context_window: int
    max_output_tokens: int
    input_limit: int | None = None


@dataclass(frozen=True, slots=True)
class DecisionCapacity:
    """专用判断协议的明确输入上限，没有生成采样或输出预算。

    Attributes:
        input_limit: 专用判断协议明确允许的输入容量。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    input_limit: int


CapacityDeclaration = GenerationCapacity | DecisionCapacity


@dataclass(frozen=True, slots=True)
class CapacityEstimate:
    """完整容量规划值，分别说明实际输入基线和新 payload 的估算。

    Attributes:
        input_units: 完整请求的参考输入占用，不能冒充服务端 usage。
        method: 内置估算方法的版本标识。
        scope: 此估算实际覆盖的材料范围，不代表服务端 usage。
        unit: 估算采用的参考计数单位。
        usage_request_id: 可复用前缀实际用量的请求身份；None 表示没有已知基线。
        observed_input_tokens: 服务端确认的旧输入总量，缓存命中不另行相加。
        new_input_units: 相同布局前缀以外的新增材料估算；没有基线时为 None。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        input_units 是实际基线加增量估算，不能作为整份新请求的服务端 usage。
        没有匹配锚点时，文本与媒体采用同一内置
        估算器，媒体编码大小不参与正文计数；系数不依赖模型名，也不承诺是任意
        Provider 实际占用的误差上界。请求的 SHA-256 仍覆盖完整原始材料。
    """

    input_units: int
    method: str
    scope: str
    unit: str
    usage_request_id: str | None = None
    observed_input_tokens: int | None = None
    new_input_units: int | None = None

    @property
    def policy(self) -> str:
        """返回可比较的规划策略，实际输入基线与新布局估算属于同一内置策略。

        摘要重写前缀会使实际用量锚点不再匹配，前后数据来源因此可以不同；
        两者仍使用同一 token 单位、增量公式和输入分母。未知方法不合并策略。
        """
        if self.method in ("multimodal-heuristic-v1", "provider-usage-plus-delta-v1"):
            return "builtin-multimodal-v1"
        return self.method


@dataclass(frozen=True, slots=True)
class CapacityAssessment:
    """完整请求的不可变计量结论，触发阈值与目标均使用 input_budget 分母。

    Attributes:
        estimate: 当前完整请求的计量结果与适用范围。
        input_budget: 扣除输出与预留空间后的实际输入预算。
        maintenance_due: 当前占用是否达到历史维护触发比例。
        target_met: 当前占用是否已满足摘要维护目标比例。
        payload_sha256: 完整实际 payload 的内容摘要，用于关联计量材料。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    estimate: CapacityEstimate
    input_budget: int
    maintenance_due: bool
    target_met: bool
    payload_sha256: str

    @property
    def ratio(self) -> float:
        """返回明确分母下的本地比例，不能当作远端模型占用率。"""
        return self.estimate.input_units / self.input_budget

    @property
    def within_budget(self) -> bool:
        """只有完整材料的参考估算不超过输入额度时才具备派发容量。"""
        return self.estimate.input_units <= self.input_budget


class CapacityError(EngineError):
    """请求材料无法估算或超过容量，携带明确原因而不是返回零占用。"""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        assessment: CapacityAssessment | None = None,
    ) -> None:
        """保留可查询的失败原因与已有结论，不包含凭据或完整模型正文。

        Args:
            message: 对调用方公开的原因或错误说明。
            code: 供调用方分类处理的稳定错误码。
            assessment: 已取得并需按原版本核验的判断结果。
        """
        super().__init__(message)
        self.code = code
        self.assessment = assessment


def assess_capacity(
    payload: Mapping[str, object],
    declaration: CapacityDeclaration,
    *,
    reserve_fraction: float = 0.1,
    trigger: float = 0.8,
    target: float = 0.5,
    usage: UsageMeter | None = None,
) -> CapacityAssessment:
    """计量完整候选，允许返回超额结论供维护使用。

    Args:
        payload: 按真实 SDK 布局编译的完整公开请求，包含消息、工具 schema 和媒体表示。
        declaration: 待绑定或计量的不可变声明。
        reserve_fraction: 从输入容量中扣除的预留比例。 默认 0.1。
        trigger: 触发摘要维护的输入预算占用比例。 默认 0.8。
        target: 摘要维护后期望达到的输入预算占用比例。 默认 0.5。
        usage: 当前用途的已确认输入锚点；None 时整份新布局使用内置估算。

    Returns:
        对完整请求的参考占用、输入预算、阈值和材料指纹。

    Raises:
        ValueError: 参数值或组合不满足本入口的明确约束，错误正文指出具体字段。
        CapacityError: 输出上限不符合声明、请求不是有限 JSON，或媒体无法取得有效依据。

    Note:
        输入分母在扣除输出后预留，再与独立输入上限取更严格值。预警阈值不是
        停止线，0.5 目标也不是摘要安装硬门槛；实际派发使用 check_capacity。
    """
    if not 0 <= reserve_fraction < 1 or not 0 < target < trigger < 1:
        raise ValueError("容量余量及摘要阈值关系无效。")
    _positive(
        declaration.input_limit
        if isinstance(declaration, DecisionCapacity)
        else declaration.context_window
    )
    if isinstance(declaration, GenerationCapacity):
        _positive(declaration.max_output_tokens)
        actual_output = payload.get("max_tokens")
        if (
            isinstance(actual_output, bool)
            or not isinstance(actual_output, int)
            or not 0 < actual_output <= declaration.max_output_tokens
        ):
            raise CapacityError("实际生成请求没有符合声明的显式输出上限。", code="output_limit")
        completion_output = payload.get("max_completion_tokens")
        if completion_output is not None and (
            isinstance(completion_output, bool)
            or not isinstance(completion_output, int)
            or not 0 < completion_output <= declaration.max_output_tokens
        ):
            raise CapacityError("请求级输出参数超过模型声明。", code="output_limit")
        # 相同模型配置可被注意/摘要借用较小的实际输出额度。配置是允许上限，
        # 完整请求里的已映射输出才占本次共享窗口；不能把256输出当4096预留。
        output_reserve = max(actual_output, completion_output or actual_output)
        available = declaration.context_window - output_reserve
        if available <= 0:
            raise ValueError("共享窗口必须为输入留下正额度。")
        budget = math.floor(available * (1 - reserve_fraction))
        if declaration.input_limit is not None:
            _positive(declaration.input_limit)
            budget = min(budget, declaration.input_limit)
    else:
        budget = math.floor(declaration.input_limit * (1 - reserve_fraction))
    if budget <= 0:
        raise ValueError("预留后的输入额度必须为正。")

    try:
        encoded = encode(payload).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as error:
        raise CapacityError(
            "实际 SDK 请求不能表示为完整有限 JSON。", code="invalid_payload"
        ) from error
    # 指纹必须覆盖原始媒体；估算只在独立副本中剥离编码数据，不能把
    # 降低估算成本误写成实际请求裁剪，或让相同尺寸/时长随压缩率变化。
    try:
        measured = usage.estimate(decode_object(encoded)) if usage is not None else None
        input_tokens = measured.input_tokens if measured is not None else estimate_tokens(payload)
    except ValueError as error:
        raise CapacityError(str(error), code="invalid_material") from error
    anchored = measured is not None and measured.request_id is not None
    estimate = CapacityEstimate(
        input_tokens,
        "provider-usage-plus-delta-v1" if anchored else "multimodal-heuristic-v1",
        "complete_sdk_request",
        "token_estimate",
        usage_request_id=measured.request_id if measured is not None else None,
        observed_input_tokens=measured.observed_tokens
        if measured is not None and anchored
        else None,
        new_input_units=measured.new_tokens if measured is not None and anchored else None,
    )
    ratio = estimate.input_units / budget
    return CapacityAssessment(
        estimate, budget, ratio >= trigger, ratio <= target, hashlib.sha256(encoded).hexdigest()
    )


def check_capacity(
    payload: Mapping[str, object],
    declaration: CapacityDeclaration,
    *,
    reserve_fraction: float = 0.1,
    trigger: float = 0.8,
    target: float = 0.5,
    usage: UsageMeter | None = None,
) -> CapacityAssessment:
    """真正派发前核验完整材料。

    Args:
        payload: 按真实 SDK 布局编译的完整公开请求，包含消息、工具 schema 和媒体表示。
        declaration: 待绑定或计量的不可变声明。
        reserve_fraction: 从输入容量中扣除的预留比例。 默认 0.1。
        trigger: 触发摘要维护的输入预算占用比例。 默认 0.8。
        target: 摘要维护后期望达到的输入预算占用比例。 默认 0.5。
        usage: 当前用途的已确认输入锚点；None 时整份新布局使用内置估算。

    Returns:
        对完整请求的参考占用、输入预算、阈值和材料指纹。

    Raises:
        CapacityError: 完整请求超额、输出声明无效、材料无法估算或不是有限 JSON。

    Note:
        超额明确拒绝，不删历史或工具后重发。
    """
    result = assess_capacity(
        payload,
        declaration,
        reserve_fraction=reserve_fraction,
        trigger=trigger,
        target=target,
        usage=usage,
    )
    if not result.within_budget:
        raise CapacityError("完整请求超过可用输入额度。", code="over_capacity", assessment=result)
    return result


def _positive(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("模型容量声明必须是正整数。")
