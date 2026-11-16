"""定义原文快照、精确片段、行为评审批次、监管与计划的不可变值。

原文获取、实际呈现和行为结案分别表达；这些值不取得数据库或模型资源。
Markdown 长期记忆和整理提案位于 domain.notebook，不在此保存第二套认识。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Literal, Self, cast

from purrcept_core.models import ContentBlock, Message
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from purrcept_engine.domain.execution.record_validation import validate_json
from purrcept_engine.domain.inputs.models import MediaReference

Text = Annotated[str, Field(min_length=1, pattern=r"\S", strict=True)]


Version = Annotated[int, Field(ge=1, strict=True)]


Finite = Annotated[float, Field(allow_inf_nan=False, strict=True)]


RangeUnit = Literal["characters", "items"]


class EvidenceInputError(ValueError):
    """来源、范围或参数不符合公开阅读契约，可在原批次预算内纠正。"""


class EvidenceConflict(EvidenceInputError):
    """来源、版本或批次已变化，必须重读实际状态，不能盲目重放修订。"""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @model_validator(mode="before")
    @classmethod
    def frozen_mapping(cls, value: object) -> object:
        """Core 以只读 Mapping 冻结工具对象。

        Args:
            value: 此校验边界接收的原始值，校验失败时保留明确错误。

        Note:
            展开形状仍由各字段严格校验。

            Pydantic 的 strict model 边界只接受 dict。复制真实 Mapping 可接受
            Core 的不可变 JSON 对象，同时不对字符串、数值或未知对象作猜测转换。
        """
        if isinstance(value, Mapping):
            return dict(cast(Mapping[object, object], value))
        return value


# 维护者属性契约（类说明参与 schema，保持原 docstring）。
# Attributes:
#     evidence_id: 原文稳定身份，配合 version 定位不可变快照。
#     source_json: 完整来源对象的 JSON；外界正文不能自称可信来源。
#     text: 完整原文，不因摘要或分页裁剪已保存内容。
#     media: 同库完整媒体的引用，取得页面不等同于模型已阅。
class EvidenceSnapshot(_Record):
    """一份不可变原文；source_json 保存来源信封，不把它解释为系统权限。

    items_json 仅供明确声明的结构化列表；分页不拆开其中一个条目。媒体引用
    指向 Store BLOB，valid 是查询时的来源资格，不覆盖已经保存的发生事实。
    """

    evidence_id: Text
    version: Version
    kind: Text
    text: str
    source_json: str
    occurred_at: Finite
    summary: str = ""
    overview: str = ""
    media: tuple[MediaReference, ...] = Field(default=(), strict=False)
    items_json: str | None = None
    valid: bool = True

    @field_validator("source_json")
    @classmethod
    def source_is_json(cls, value: str) -> str:
        """来源必须是完整标准 JSON 对象，不从 repr 猜测事实。

        Args:
            value: 此校验边界接收的原始值，校验失败时保留明确错误。
        """
        validate_json(value, arguments=True)
        return value

    @field_validator("items_json")
    @classmethod
    def items_are_json(cls, value: str | None) -> str | None:
        """结构化分页只接受完整数组，普通人类正文仍保留为 text。

        Args:
            value: 此校验边界接收的原始值，校验失败时保留明确错误。

        Raises:
            ValueError: 结构化材料必须是 JSON 数组。
        """
        if value is not None:
            validate_json(value)
            decoded: object = json.loads(value)
            if not isinstance(decoded, list):
                raise ValueError("结构化材料必须是 JSON 数组。")
        return value


@dataclass(frozen=True, slots=True)
class EvidenceDelivery:
    """一次真实初始或工具交付，存在不代表已经向模型呈现或复核完成。

    Attributes:
        delivery_id: 完整材料交付登记的稳定身份。
        batch_id: 独立反思批次的身份，须与批次版本配对使用。
        version: 目标记录的确切版本，区别于可变的当前版本。
        kind: 此业务值所属的稳定种类。
        tool_call_id: Core 分配的工具调用身份，用于匹配完整工具结果。
        material: 真正交付的完整消息或工具内容，供后续请求逐项匹配。
        slice_refs: 当前材料实际覆盖的原文片段身份。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        material 保存完整公开 Message 或原工具内容；恢复仅匹配同批次、调用身份
        和完整值，不能根据模型或外界正文中的来源字符串创建资格。
    """

    delivery_id: str
    batch_id: str
    version: int
    kind: Literal["initial", "tool"]
    tool_call_id: str | None
    material: Message | tuple[ContentBlock, ...]
    slice_refs: tuple[str, ...]


# 维护者属性契约（类说明参与 schema，保持原 docstring）。
# Attributes:
#     slice_ref: 宿主保存的不可变原文范围身份。
#     start: 范围包含的起始字符或条目下标。
#     end: 范围的排他结束下标，单位由 unit 决定。
#     media_ids: 本片段完整覆盖的原生媒体身份。
class EvidenceSlice(_Record):
    """宿主生成的可引用范围，文本范围为半开区间，媒体另按真实引用记录。"""

    slice_ref: Text
    evidence_id: Text
    version: Version
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(ge=0)]
    unit: RangeUnit = "characters"
    media_ids: tuple[Text, ...] = Field(default=(), strict=False)

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        """空正文仍可携带媒体或来源。

        Returns:
            校验完成的当前值或解析后的新配置，具体是否复制由实现说明。

        Raises:
            ValueError: 来源片段范围倒置。

        Note:
            倒置区间不能成为阅读证明。
        """
        if self.end < self.start:
            raise ValueError("来源片段范围倒置。")
        return self


# 维护者属性契约（类说明参与 schema，保持原 docstring）。
# Attributes:
#     snapshot: 这一页对应的确切原文快照。
#     slice: 真正原文范围的凭据；概要展开不凭空生成此值。
#     next_cursor: 继续读取同一版本的宿主游标；None 表示已读到末尾。
class EvidencePage(_Record):
    """一次完整阅读结果；摘要没有 slice，创建原文 slice 也不表示模型已读。"""

    snapshot: EvidenceSnapshot
    slice: EvidenceSlice | None
    text: str
    level: Literal["summary", "overview", "evidence"]
    next_cursor: str | None = None


class EvidenceReference(_Record):
    """权威材料的确切版本引用，异步排名后仍须核验当前版本与来源资格。"""

    kind: Literal["evidence"]
    id: Text
    version: Version


class EvidenceCandidate(_Record):
    """一条权威检索候选及其有限路线排名，排名不能授予来源资格。"""

    reference: EvidenceReference
    record: EvidenceSnapshot
    ranks: tuple[tuple[str, int], ...] = Field(default=(), strict=False)


class OperationalFinding(_Record):
    """宿主列出的具体异常事实；模型只评估它，不根据正文 status 自造异常。"""

    id: Text
    fact: Text
    evidence: tuple[Text, ...] = Field(default=(), strict=False)


class Review(_Record):
    """一项有明确范围的行为复核结论，不手抄修订 ID/版本。"""

    issue: Annotated[str, Field(min_length=1, max_length=300)]
    evidence: tuple[Text, ...] = Field(min_length=1, strict=False)
    conclusion: Annotated[str, Field(min_length=1, max_length=1200)]
    reason: Annotated[str, Field(min_length=1, max_length=600)]


class Deferred(_Record):
    """尚未完成复核的明确范围与理由，不安排无新事实的自动循环。"""

    evidence: tuple[Text, ...] = Field(min_length=1, strict=False)
    reason: Annotated[str, Field(min_length=1, max_length=600)]


class FindingAssessment(_Record):
    """对本批程序异常逐项说明确定或未决，不声称全局系统健康。"""

    id: Text
    status: Literal["confirmed", "uncertain"]
    reason: Annotated[str, Field(min_length=1, max_length=600)]


class ObservationAssessment(_Record):
    """对确切后续轮次的改善观察，不能用主脑接受意见冒充结果。"""

    notice_id: Text
    turn_id: Text
    outcome: Literal["improved", "recurred", "insufficient_samples"]
    evidence: tuple[Text, ...] = Field(min_length=1, strict=False)


class BehaviorConclusion(_Record):
    """局部评审的明确结果；不要求为了结案制造批评。"""

    decision: Literal["no_intervention", "suggestion", "insufficient_evidence"]
    conclusion: Text
    evidence: tuple[Text, ...] = Field(min_length=1, strict=False)
    observations: tuple[ObservationAssessment, ...] = Field(default=(), strict=False)


class FinishSubmission(_Record):
    """行为评审的完成输入，范围核验与结案由存储事务完成。"""

    reviews: tuple[Review, ...] = Field(strict=False)
    deferred: tuple[Deferred, ...] = Field(strict=False)
    findings: tuple[FindingAssessment, ...] = Field(strict=False)
    behavior: BehaviorConclusion | None = None


class ReflectionReceipt(_Record):
    """已成功完成的不可变回执，后续会话失败或重复确认不改写它。"""

    batch_id: Text
    version: Version
    completed_at: Finite
    submission: FinishSubmission
    coverage: tuple[Text, ...] = Field(default=(), strict=False)


# 维护者属性契约（类说明参与 schema，保持原 docstring）。
# Attributes:
#     version: 当前批次的写资格版本，恢复不能借用旧代次。
#     required_slice_refs: 本批冻结的必需复核范围。
#     receipt: 结构化结案已提交时存在的真实回执。
class ReflectionBatch(_Record):
    """宿主冻结的一批范围和异常，version 区分实际处理代次。"""

    batch_id: Text
    version: Version
    required_slice_refs: tuple[Text, ...] = Field(min_length=1, strict=False)
    purpose: Literal["behavior"] = "behavior"
    created_at: Finite | None = None
    findings: tuple[OperationalFinding, ...] = Field(default=(), strict=False)
    state: Literal["running", "completed", "interrupted"] = "running"
    interrupted_reason: Text | None = None
    receipt: ReflectionReceipt | None = None


class PendingEvidence(_Record):
    """可靠待复核来源与首次入队时刻，扫描或分页读取不确认处理。"""

    evidence_id: Text
    version: Version
    queued_at: Finite
    reviewed_slice_refs: tuple[Text, ...] = Field(default=(), strict=False)
    deferred_slice_refs: tuple[Text, ...] = Field(default=(), strict=False)


# 维护者属性契约（类说明参与 schema，保持原 docstring）。
# Attributes:
#     evidence: 监管所依据的已呈现原文范围。
#     activity_id: 可选活动约束，须与 activity_version 同时填写。
#     activity_version: 监管针对的确切活动版本，迟到意见不能控制新状态。
#     level: reminder、review 或 urgent；紧急意见仍须宿主交接。
class ObservationScope(_Record):
    """用明确会话或能力限定后续观察机会，不依靠自然语言猜匹配范围。"""

    adapter_id: Text | None = None
    conversation_id: Text | None = None
    capability_path: Text | None = None

    @model_validator(mode="after")
    def bounded(self) -> Self:
        """至少声明一项实际匹配条件，避免把所有未来行为都当观察样本。"""
        if not any((self.adapter_id, self.conversation_id, self.capability_path)):
            raise ValueError("观察范围至少需要会话、适配器或能力路径。")
        return self


class SupervisionNotice(_Record):
    """有原文依据的监管意见，urgent 只请求重思考，不取得行动权限。"""

    id: Text
    issue: Text
    evidence: tuple[Text, ...] = Field(min_length=1, strict=False)
    suggestion: Text
    level: Literal["reminder", "review", "urgent"] = "reminder"
    activity_id: Text | None = None
    activity_version: Annotated[int, Field(ge=0)] | None = None
    expires_at: Finite | None = None
    scope: ObservationScope | None = None
    expected_change: Text | None = None
    generalized: bool = False

    @model_validator(mode="after")
    def activity_pair(self) -> Self:
        """活动约束须同时给出身份与版本，缺项不能默认作用到当前活动。

        Returns:
            校验完成的当前值或解析后的新配置，具体是否复制由实现说明。

        Raises:
            ValueError: 活动监管必须包含确切身份和版本。
        """
        if (self.activity_id is None) != (self.activity_version is None):
            raise ValueError("活动监管必须包含确切身份和版本。")
        return self


class SupervisionRecord(_Record):
    """监管的保存事实及当前交付资格，前台回应不成为独立事实证明。"""

    notice: SupervisionNotice
    state: Literal["open", "accept", "partial", "reject"]
    created_at: Finite
    active: bool
    response: str | None = None
    duplicate: bool = False


class WakeupPlan(_Record):
    """前台显式计划，到期只进入决策待办，不等同于注意 defer。"""

    id: Text
    due: Finite
    reason: Text
    evidence: Text
    state: Literal["pending", "completed"] = "pending"
    outcome: str | None = None
    result_evidence: str | None = None
