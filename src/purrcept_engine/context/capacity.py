"""对完整公开 SDK payload 做纯容量预检，保持参考估算与真实用量的区别。

应用显式声明模型窗口、输出及计量规则；主体在每次真正派发前提交完整消息、
目录、工具 schema、过程和动态尾部。这里不删材料、不猜模型、不开网络或加载
分词资源。内置规则只支持纯文本的完整 JSON UTF-8 字节参考；媒体必须使用
明确注册并持有适用元数据的规则，缺少依据就报告不能预检。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from purrcept_engine.errors import EngineError
from purrcept_engine.storage.model_requests import encode


@dataclass(frozen=True, slots=True)
class GenerationCapacity:
    """配置声明的共享窗口与实际输出上限，不由模型名推断。

    input_limit 可表达独立输入上限；存在时与扣输出并预留后的共享窗口取较小值。
    media_rule 是适用媒体依据的标识，不代表文本参考规则就具备该媒体能力。
    """

    context_window: int
    max_output_tokens: int
    capacity_rule: str
    media_rule: str = "unsupported"
    input_limit: int | None = None


@dataclass(frozen=True, slots=True)
class DecisionCapacity:
    """专用判断协议的明确输入上限，没有生成采样或输出预算。"""

    input_limit: int
    capacity_rule: str
    media_rule: str = "unsupported"


CapacityDeclaration = GenerationCapacity | DecisionCapacity


@dataclass(frozen=True, slots=True)
class CapacityEstimate:
    """纯规则的估计值、方法版本与适用范围，不能冒充服务端 token usage。

    input_units 是与声明输入额度比较的参考量。unit 明确规则口径；内置
    utf8_byte_reference 表示一字节一参考单位，包含完整 JSON 开销，不是
    Provider 的精确 tokenizer，也不承诺 10% 余量是理论误差上界。
    """

    input_units: int
    method: str
    scope: str
    unit: str


@dataclass(frozen=True, slots=True)
class CapacityAssessment:
    """完整请求的不可变计量结论，触发阈值与目标均使用 input_budget 分母。"""

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
    """无法计量或超过容量，携带具体缺失原因而不是返回零占用。"""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        missing: tuple[str, ...] = (),
        assessment: CapacityAssessment | None = None,
    ) -> None:
        """保留可查询的计量缺口与已有结论，不包含凭据或完整模型正文。"""
        super().__init__(message)
        self.code = code
        self.missing = missing
        self.assessment = assessment


class CapacityRule(Protocol):
    """启动期显式登记的纯计量扩展，应用负责适用性及所需元数据。

    调用发生在实时请求预检，不能联网、下载、改材料或返回 awaitable。
    实现必须持有已经取得的有限媒体元数据及本地分词资源，声明方法版本与
    适用范围；未知模型/媒体必须抛 CapacityError，不用默认公式或零量回退。
    注册映射由应用冻结，本模块不扫描或自动选择 Provider 规则。
    """

    def __call__(
        self,
        payload: Mapping[str, object],
        declaration: CapacityDeclaration,
    ) -> CapacityEstimate:
        """估计完整 SDK 公开请求，保留消息与 schema 的所有表示开销。"""
        ...


def assess_capacity(
    payload: Mapping[str, object],
    declaration: CapacityDeclaration,
    *,
    reserve_fraction: float = 0.1,
    trigger: float = 0.8,
    target: float = 0.5,
    rules: Mapping[str, CapacityRule] | None = None,
) -> CapacityAssessment:
    """计量完整候选，允许返回超额结论供维护使用；未知依据立即失败。

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
    method = declaration.capacity_rule
    if method == "reference-text-only-v1":
        _require_text_payload(payload, isinstance(declaration, GenerationCapacity))
        estimate = CapacityEstimate(
            len(encoded), method, "complete_text_sdk_json", "utf8_byte_reference"
        )
    else:
        rule = None if rules is None else rules.get(method)
        if rule is None:
            raise CapacityError(
                "模型没有可用的明确容量规则。", code="unsupported_rule", missing=(method,)
            )
        estimate = rule(payload, declaration)
        if not isinstance(estimate, CapacityEstimate) or (
            isinstance(estimate.input_units, bool)
            or not isinstance(estimate.input_units, int)
            or estimate.input_units < 0
            or not estimate.method
            or not estimate.scope
            or not estimate.unit
        ):
            raise CapacityError("计量扩展没有返回完整有效结论。", code="invalid_rule_result")
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
    rules: Mapping[str, CapacityRule] | None = None,
) -> CapacityAssessment:
    """真正派发前核验完整材料；超额明确拒绝，不删历史或工具后重发。"""
    result = assess_capacity(
        payload,
        declaration,
        reserve_fraction=reserve_fraction,
        trigger=trigger,
        target=target,
        rules=rules,
    )
    if not result.within_budget:
        raise CapacityError("完整请求超过可用输入额度。", code="over_capacity", assessment=result)
    return result


def _positive(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("模型容量声明必须是正整数。")


def _require_text_payload(payload: Mapping[str, object], generation: bool) -> None:
    """只检查真实内容槽位，不把工具 schema 或普通正文中的字段当成媒体。"""
    messages = payload.get("messages") if generation else payload.get("input")
    if not generation and isinstance(messages, str):
        return
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)) or not messages:
        raise CapacityError("完整 SDK 请求缺少已识别消息或输入。", code="invalid_payload")
    for raw in cast(Sequence[object], messages):
        if not isinstance(raw, Mapping):
            raise CapacityError("SDK 消息结构无效。", code="invalid_payload")
        content = cast(Mapping[str, object], raw).get("content")
        if content is None or isinstance(content, str):
            continue
        if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
            raise CapacityError("SDK 内容表示没有适用计量。", code="unsupported_content")
        for raw_part in cast(Sequence[object], content):
            if not isinstance(raw_part, Mapping):
                raise CapacityError("SDK 内容结构无效。", code="invalid_payload")
            part = cast(Mapping[str, object], raw_part)
            if part.get("type") not in ("text", "input_text") or not isinstance(
                part.get("text"), str
            ):
                raise CapacityError(
                    "文本参考规则不支持本次媒体或未知内容。",
                    code="unsupported_media",
                    missing=("applicable_media_rule_and_metadata",),
                )
