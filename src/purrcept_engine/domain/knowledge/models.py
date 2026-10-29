"""定义原文、片段、认识版本与独立反思的不可变记录。

原文阅读范围、认识有效性和反思写资格分别表达，并保留确切证据与版本。
这里只校验值，不查询存储、不调用检索模型或提交认识修订。
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


MemoryKind = Literal[
    "statement",
    "observation",
    "belief",
    "preference",
    "relationship",
    "experience",
    "self_understanding",
    "document",
    "episode",
]


RangeUnit = Literal["characters", "items"]


class KnowledgeInputError(ValueError):
    """来源、范围或参数不符合公开阅读契约，可在原批次预算内纠正。"""


class KnowledgeConflict(KnowledgeInputError):
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
#     snapshot: 原文页的确切快照，与 memory 只能存在一项。
#     memory: 认识页的确切版本，不生成新的原文阅读资格。
#     slice: 真正原文范围的凭据；概要或认识展开不凭空生成此值。
#     next_cursor: 继续读取同一版本的宿主游标；None 表示已读到末尾。
class EvidencePage(_Record):
    """一次完整阅读结果；摘要没有 slice，创建原文 slice 也不表示模型已读。"""

    snapshot: EvidenceSnapshot | None = None
    memory: MemoryRecord | None = None
    slice: EvidenceSlice | None
    text: str
    level: Literal["summary", "overview", "evidence"]
    next_cursor: str | None = None

    @model_validator(mode="after")
    def one_source(self) -> Self:
        """认识展开是派生页面，不能伪装成新的原文阅读资格。

        Returns:
            校验完成的当前值或解析后的新配置，具体是否复制由实现说明。

        Raises:
            ValueError: 页面必须且只能对应原文或认识版本；认识正文不能成为独立原文片段。
        """
        if (self.snapshot is None) == (self.memory is None):
            raise ValueError("页面必须且只能对应原文或认识版本。")
        if self.memory is not None and self.slice is not None:
            raise ValueError("认识正文不能成为独立原文片段。")
        return self


class KnowledgeReference(_Record):
    """权威材料的确切版本引用，异步排名后仍须核验当前版本与来源资格。"""

    kind: Literal["evidence", "memory"]
    id: Text
    version: Version


class MemoryConflictTarget(_Record):
    """反思明确提出的对方认识版本，原文来源仍由修订的 sources 单独核验。"""

    kind: Literal["memory"]
    id: Text
    version: Version


class MemoryConflict(MemoryConflictTarget):
    """保留的未决关系及对方确切版本当前资格，不推断冲突已经消解。"""

    status: Literal["unresolved"] = "unresolved"
    active: bool
    inactive_reason: str | None = None


# 维护者属性契约（类说明参与 schema，保持原 docstring）。
# Attributes:
#     base_version: 当前修订基于的认识版本；首次创建为 0。
#     sources: 已实际呈现且仍可引用的原文片段身份。
#     conflicts: 保留未决反证的目标和确切版本，不自动替代对方。
#     valid_from: 事实有效时间下界，不是认识写入时间。
#     valid_until: 事实有效时间上界；None 不设显式上界。
class MemoryPatch(_Record):
    """本批有据修订；原文来源、替代和未决版本关系由同一事务确认。

    sources 只接受实际呈现的原文片段。conflicts 指向当前有效的确切认识版本，
    不授予对方原文的阅读资格，也不将未决反证解释为自动替代。
    """

    id: Text
    content: Text
    sources: tuple[Text, ...] = Field(min_length=1, strict=False)
    base_version: Annotated[int, Field(ge=0)] = 0
    kind: MemoryKind = "belief"
    scope: Text = "subject"
    summary: str = ""
    overview: str = ""
    entities: tuple[Text, ...] = Field(default=(), strict=False)
    valid_from: Finite | None = None
    valid_until: Finite | None = None
    supersedes: Text | None = None
    conflicts: tuple[MemoryConflictTarget, ...] = Field(default=(), strict=False)

    @model_validator(mode="after")
    def consistent_revision(self) -> Self:
        """有效期、明确替代与未决关系必须表达相容且无重复的修订。

        Returns:
            校验完成的当前值或解析后的新配置，具体是否复制由实现说明。

        Raises:
            ValueError: 参数值或组合不满足本入口的明确约束，错误正文指出具体字段。
        """
        if self.valid_from is not None and self.valid_until is not None:
            if self.valid_until <= self.valid_from:
                raise ValueError("认识有效时间区间为空。")
        if self.supersedes == self.id:
            raise ValueError("认识不能替代自身。")
        if len(set(self.conflicts)) != len(self.conflicts):
            raise ValueError("冲突认识版本不能重复。")
        if any(item.id == self.id for item in self.conflicts):
            raise ValueError("认识不能与自身登记未决冲突。")
        if any(item.id == self.supersedes for item in self.conflicts):
            raise ValueError("同一认识不能同时被替代并登记为未决反证。")
        return self


class MemoryRecord(_Record):
    """保留的认识版本及双向未决关系，失效历史不会自动转移到新版本。

    active 由版本、时间、替代和来源共同计算；conflicts 显示对方确切版本的
    同时点资格，不包含对方正文，也不撤销双方已经保存的来源。
    """

    patch: MemoryPatch
    version: Version
    learned_at: Finite
    active: bool
    inactive_reason: str | None = None
    conflicts: tuple[MemoryConflict, ...] = Field(default=(), strict=False)


class KnowledgeCandidate(_Record):
    """一条权威检索候选及其有限路线排名，排名不能授予来源资格。"""

    reference: KnowledgeReference
    record: EvidenceSnapshot | MemoryRecord
    ranks: tuple[tuple[str, int], ...] = Field(default=(), strict=False)


class IndexJob(_Record):
    """可重建索引的版本任务，完成旧任务不得复活失效认识。"""

    reference: KnowledgeReference
    queued_at: Finite
    route: Literal["keyword", "semantic"]


class OperationalFinding(_Record):
    """宿主列出的具体异常事实；模型只评估它，不根据正文 status 自造异常。"""

    id: Text
    fact: Text
    evidence: tuple[Text, ...] = Field(default=(), strict=False)


class Review(_Record):
    """一项有明确范围的复核与知识处理取舍，不手抄修订 ID/版本。"""

    issue: Annotated[str, Field(min_length=1, max_length=300)]
    evidence: tuple[Text, ...] = Field(min_length=1, strict=False)
    conclusion: Annotated[str, Field(min_length=1, max_length=1200)]
    memory_decision: Literal["written", "no_change", "defer"]
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


class AbandonedMemoryWrite(_Record):
    """明确撤回一次失败的记忆写入意图；不能代替成功写入或伪造原文资格。"""

    id: Text = Field(description="本批失败的 revise_memory 调用中使用的记忆 id。")
    reason: Annotated[str, Field(min_length=1, max_length=600, pattern=r"\S")] = Field(
        description="重新判断不应写入的具体理由；来源错误本身不等于没有记忆价值。"
    )


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
    """按用途提交的完成输入，范围核验和真实修订关联由事务完成。"""

    reviews: tuple[Review, ...] = Field(strict=False)
    deferred: tuple[Deferred, ...] = Field(strict=False)
    findings: tuple[FindingAssessment, ...] = Field(strict=False)
    abandoned_writes: tuple[AbandonedMemoryWrite, ...] = Field(default=(), strict=False)
    behavior: BehaviorConclusion | None = None


class ReflectionReceipt(_Record):
    """已成功完成的不可变回执，后续会话失败或重复确认不改写它。"""

    batch_id: Text
    version: Version
    completed_at: Finite
    submission: FinishSubmission
    written: tuple[KnowledgeReference, ...] = Field(default=(), strict=False)
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
    purpose: Literal["memory", "behavior"] = "memory"
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
