"""独立反思的五个 Core 工具及实际材料呈现追踪，不另写模型循环。

工具创建原文片段与请求实际呈现分开：read_evidence 只登记完整工具候选，
容量 Gate 选择交付后，成功门槛通过 presented_refs 对照真实 ModelRequest，再由 Store
mark_presented 提交资格。同一响应先读后修订不能引用尚未送入下一请求的片段。
认识和监管逐次生效，批次完成只由 finish_reflection 的原子提交确认。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Protocol

from purrcept_core.models import (
    AudioBlock,
    ContentBlock,
    ImageBlock,
    ImageBytes,
    Message,
    MessageRole,
    ModelRequest,
    SystemInstruction,
    TextBlock,
    ToolResultBlock,
)
from purrcept_core.models.tools import FunctionTool, ToolContext, ToolResult, ToolSet
from pydantic import Field, StrictBool, StrictStr, ValidationError

from purrcept_engine.storage.knowledge import (
    Deferred,
    EvidenceDelivery,
    EvidencePage,
    FindingAssessment,
    FinishSubmission,
    Finite,
    KnowledgeInputError,
    MemoryConflictTarget,
    MemoryKind,
    MemoryPatch,
    ReflectionBatch,
    Review,
    SupervisionNotice,
    SupervisionRecord,
    Text,
)
from purrcept_engine.storage.models import MediaReference

from .delivery import ToolDelivery, ToolDeliveryGate, evidence_fallback
from .knowledge import KnowledgeService, KnowledgeStore, recall_payload

Nonnegative = Annotated[int, Field(ge=0, strict=True)]
Positive = Annotated[int, Field(gt=0, strict=True)]

REFLECTION_INSTRUCTIONS = (
    "你是同一主体的独立反思 Agent。持续复核前台实际上下文、行动回执与外部反馈。"
    "你没有世界行动权限，只可检索证据、修订认识、发送监管意见。"
    "长期认识必须引用原始来源和 base_version，区分 statement、observation、belief、"
    "preference、relationship、experience、self_understanding、document、episode。"
    "保留否定、条件、时间与反例，证据不足就保留未决。不能修改固定身份或权限。"
    "监督不是替代前台决定；urgent 仅请求重新思考。前台同意不构成独立证明。"
    "整合步骤：先按活动和因果关系分段为 episode，再提取保留否定、条件、对象和时间的候选；"
    "recall 查相关旧认识，逐项判断补充、重复、矛盾或替代。相似度和重复转述不是新证据。"
    "调用 revise_memory 时使用确切 base_version 与原始 sources；未解决矛盾并列保留。"
    "仅刷新受影响的 document 和 summary，保留反例及适用范围。用 finish_reflection 登记"
    "确实完成复核的问题，不能把扫描到的所有经历自动标为理解。"
    "容量维护由宿主优先调度的无工具计算承担，不需要自行展开完整会话才能启动压缩。"
    "材料已接管或已准备不表示模型运行成功或主动选择沉默；"
    "行动是否发生须查看实际请求和回执。"
    "unread_evidence 表示尚未纳入本轮材料，必须 read_evidence 后才能引用其具体内容。"
    "每批必须调用 finish_reflection 提交结构化完成记录，最终自然语言不确认复核。"
    "reviews 必须限定具体问题和证据；deferred 明确材料不足、未查看媒体等缺口。"
    "每项已复核材料都须给 memory_decision：临时闲聊可 no_change 并说明原因；"
    "明确偏好、被纠正的认识、可复用执行经验应先查重，再 revise_memory，"
    "最后以 written 报告已写入，真实修订 ID/版本由宿主关联。"
    "程序 findings 是本批可观察异常，必须逐项回应；前台没有发错消息不能证明感知或调度健康。"
    "对历史中无覆盖依据的正面评价保持审慎，它们不是新的外部证据。"
)


def reflection_instructions() -> tuple[SystemInstruction, ...]:
    """构造独立反思的固定规则，完成确认属于工具，原文仍是 user 材料。"""
    return (SystemInstruction.from_text(REFLECTION_INSTRUCTIONS),)


class ReflectionPorts(Protocol):
    """app 提供媒体及监管交接，不向工具借出模型客户端、执行器或通用数据库。

    media_blocks 返回与每个唯一入账媒体引用对应的原生字节内容，缺失/不支持
    原样失败，不用文字替代。supervision_committed 只交接已确认的新意见；
    urgent 请求重思考，不调用世界能力。资源关闭由 app 等待借用后完成。
    """

    async def media_blocks(self, media: tuple[MediaReference, ...]) -> tuple[ContentBlock, ...]:
        """按传入引用顺序返回全部原生媒体，字节应与持久 BLOB 摘要一致。"""
        ...

    def foreground_generation(self) -> int:
        """观察提交前的前台代次，不能在迟到通知中用新代次替换原信号。"""
        ...

    async def supervision_committed(
        self, record: SupervisionRecord, *, observed_generation: int
    ) -> None:
        """交接已经生效的新监管；展示失败不回滚其提交。"""
        ...


@dataclass(frozen=True, slots=True)
class _Delivery:
    content: tuple[ContentBlock, ...]
    slice_refs: tuple[str, ...]


class EvidenceDeliveries:
    """会话所有者持有的完整材料候选登记，连续反思批次可共享同一实例。

    只登记 Engine 真正创建的初始 Message 和 read_evidence ToolResult。对照
    完整 block 值而非字符串标记；普通外部文字、不同 tool_call_id、缺媒体或
    被改写正文不能取得资格。该缓存不是权威已读状态，Store 仍核验版本/范围。
    """

    def __init__(self) -> None:
        """为一个独立会话创建登记，不与前台可变历史共享。"""
        self._initial: list[tuple[Message, tuple[str, ...]]] = []
        self._tools: dict[str, list[_Delivery]] = {}

    def register_initial(self, message: Message, slice_refs: tuple[str, ...]) -> None:
        """登记宿主真正组装的完整输入，不从其中的外界正文解析权限标记。"""
        entry = (message, slice_refs)
        if entry not in self._initial:
            self._initial.append(entry)

    def register_result(
        self, tool_call_id: str, result: ToolResult, slice_refs: tuple[str, ...]
    ) -> None:
        """登记真实工具候选，复用调用 ID 时仍按完整内容区分原始交付。"""
        if result.is_error:
            raise ValueError("错误工具结果不能登记原文交付。")
        delivery = _Delivery(result.content, slice_refs)
        entries = self._tools.setdefault(tool_call_id, [])
        if delivery not in entries:
            entries.append(delivery)

    def restore_history(self, deliveries: tuple[EvidenceDelivery, ...]) -> None:
        """接回账本完整匹配的原交付；持久登记本身不代表本次已经呈现。

        账本返回原批次、完整 Message 或工具内容及确切范围。当前请求仍须通过
        presented_refs 再次匹配；不能用来源身份字符串恢复删去的正文或媒体。
        """
        for delivery in deliveries:
            if delivery.kind == "initial" and isinstance(delivery.material, Message):
                if delivery.tool_call_id is not None:
                    raise ValueError("初始交付不能携带工具调用身份。")
                self.register_initial(delivery.material, delivery.slice_refs)
            elif (
                delivery.kind == "tool"
                and isinstance(delivery.material, tuple)
                and delivery.tool_call_id is not None
            ):
                self.register_result(
                    delivery.tool_call_id,
                    ToolResult(content=delivery.material),
                    delivery.slice_refs,
                )
            else:
                raise ValueError("持久交付类别与完整材料不一致。")

    def retain_history(self, messages: tuple[Message, ...]) -> None:
        """只保留当前实际请求中完整存在的登记，摘要安装不扩展原文资格。

        每次请求成功门槛清理缓存，避免连续批次或失败临时历史无限堆积。
        权威交付记录仍在同库保留，以后恢复相同完整历史时可以重新读取。
        """
        self._initial = [entry for entry in self._initial if entry[0] in messages]
        results: dict[str, list[tuple[ContentBlock, ...]]] = {}
        for message in messages:
            if message.role is not MessageRole.TOOL or message.name != "read_evidence":
                continue
            for block in message.content:
                if isinstance(block, ToolResultBlock) and not block.is_error:
                    results.setdefault(block.tool_call_id, []).append(block.content)
        retained_tools: dict[str, list[_Delivery]] = {}
        for call_id, entries in self._tools.items():
            current_results = results.get(call_id, ())
            retained = [item for item in entries if item.content in current_results]
            if retained:
                retained_tools[call_id] = retained
        self._tools = retained_tools

    def presented_refs(self, request: ModelRequest) -> tuple[str, ...]:
        """从实际请求找出精确登记的 block 组，不扫描字符串相似或全量待办。"""
        references: dict[str, None] = {}
        for message in request.messages:
            if message.role is MessageRole.USER:
                for initial, identities in self._initial:
                    if message == initial:
                        references.update((reference, None) for reference in identities)
            if message.role is not MessageRole.TOOL or message.name != "read_evidence":
                continue
            for block in message.content:
                if not isinstance(block, ToolResultBlock) or block.is_error:
                    continue
                for delivery in self._tools.get(block.tool_call_id, ()):
                    if block.content == delivery.content:
                        references.update((reference, None) for reference in delivery.slice_refs)
        return tuple(references)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _input_error(error: KnowledgeInputError | ValidationError) -> ToolResult:
    """只将可纠正参数/范围错误交回模型，存储及程序故障不经此转换。"""
    if isinstance(error, ValidationError):
        issues = error.errors(include_input=False, include_context=False, include_url=False)
        description = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}" for issue in issues
        )
    else:
        description = str(error)
    return ToolResult(
        content=(TextBlock(_json({"error_type": type(error).__name__, "error": description})),),
        is_error=True,
    )


class ReflectionTools:
    """宿主为当前批次创建的工具实例，Core ToolSet 按固定五项顺序装配。

    批次身份与版本由宿主绑定，不要求模型复述。后端在有效响应、容量及权威
    请求提交完成之后调用 presented_refs，再提交 mark_presented，最后才将
    响应交给 Core 工具循环。模型普通收尾不确认复核；已成功知识提交不回滚。
    """

    def __init__(
        self,
        store: KnowledgeStore,
        batch: ReflectionBatch,
        ports: ReflectionPorts,
        *,
        page_chars: int = 8000,
        knowledge: KnowledgeService | None = None,
        deliveries: EvidenceDeliveries | None = None,
        delivery: ToolDeliveryGate | None = None,
    ) -> None:
        """借用账本和外围端口，连续会话应复用同一个材料登记实例。"""
        self.store = store
        self.batch = batch
        self.ports = ports
        self.knowledge = knowledge or KnowledgeService(store, page_chars=page_chars)
        self.deliveries = deliveries or EvidenceDeliveries()
        self.delivery = delivery
        self._toolset = ToolSet(
            (
                FunctionTool(self.recall),
                FunctionTool(self.read_evidence),
                FunctionTool(self.revise_memory),
                FunctionTool(self.supervise),
                FunctionTool(self.finish_reflection),
            )
        )

    def toolset(self) -> ToolSet:
        """返回固定五工具，不包含世界行动、手动压缩或旧 complete_review。"""
        return self._toolset

    def presented_refs(self, request: ModelRequest) -> tuple[str, ...]:
        """对照真正请求取得可信候选，调用本方法本身不修改账本。"""
        return self.deliveries.presented_refs(request)

    async def initial_message(self, pages: tuple[EvidencePage, ...]) -> Message:
        """组装本批原文及完整媒体，只登记候选而不提前声明模型已读。"""
        blocks: list[ContentBlock] = [
            TextBlock(
                _json(
                    {
                        "source": "engine_reflection_batch",
                        "batch_id": self.batch.batch_id,
                        "version": self.batch.version,
                        "required_slice_refs": self.batch.required_slice_refs,
                        "findings": [item.model_dump(mode="json") for item in self.batch.findings],
                    }
                )
            )
        ]
        references: list[str] = []
        for page in pages:
            content = await self._page_content(page)
            blocks.extend(content)
            if page.slice is not None:
                references.append(page.slice.slice_ref)
        message = Message(
            MessageRole.USER,
            tuple(blocks),
            metadata={
                "evidence": tuple(
                    dict.fromkeys(
                        page.snapshot.evidence_id for page in pages if page.snapshot is not None
                    )
                ),
                "source_slice_refs": tuple(references),
                "reflection_batch": self.batch.batch_id,
            },
        )
        if references:
            await self.store.record_delivery(
                self.batch.batch_id,
                self.batch.version,
                tool_call_id=None,
                material=message,
                slice_refs=tuple(references),
            )
            self.deliveries.register_initial(message, tuple(references))
        return message

    async def _page_content(self, page: EvidencePage) -> tuple[ContentBlock, ...]:
        """来源 JSON 与实际原生媒体共同构成一份不可拆的交付资格。"""
        snapshot = page.snapshot
        if snapshot is None:
            memory = page.memory
            if memory is None:
                raise RuntimeError("认识页面缺少对应版本。")
            patch = memory.patch
            payload = {
                "item": {
                    "id": patch.id,
                    "version": memory.version,
                    "kind": patch.kind,
                    "text": page.text,
                    "sources": patch.sources,
                    "active": memory.active,
                    "inactive_reason": memory.inactive_reason,
                    "conflicts": [
                        relation.model_dump(mode="json") for relation in memory.conflicts
                    ],
                },
                "derived": True,
                "evidence_ref": None,
                "slice_ref": None,
                "range": None,
                "level": page.level,
                "next_cursor": page.next_cursor,
            }
            return (TextBlock(_json(payload)),)
        payload = {
            "item": {
                "id": snapshot.evidence_id,
                "version": snapshot.version,
                "kind": snapshot.kind,
                "text": page.text,
                "source": json.loads(snapshot.source_json),
                "occurred_at": snapshot.occurred_at,
                "valid": snapshot.valid,
                "media": [
                    {
                        "media_id": reference.media_id,
                        "media_type": reference.media_type,
                        "byte_length": reference.byte_length,
                        "name": reference.name,
                    }
                    for reference in snapshot.media
                ],
            },
            "evidence_ref": {"id": snapshot.evidence_id, "version": snapshot.version},
            "slice_ref": None if page.slice is None else page.slice.slice_ref,
            "range": None
            if page.slice is None
            else {"unit": page.slice.unit, "start": page.slice.start, "end": page.slice.end},
            "level": page.level,
            "next_cursor": page.next_cursor,
        }
        media: tuple[MediaReference, ...] = ()
        if page.slice is not None:
            by_id: dict[str, MediaReference] = {}
            for reference in snapshot.media:
                if reference.media_id in page.slice.media_ids:
                    by_id.setdefault(reference.media_id, reference)
            media = tuple(by_id.values())
        native = await self.ports.media_blocks(media) if media else ()
        if len(native) != len(media):
            raise RuntimeError("原文媒体未完整展开。")
        for reference, block in zip(media, native, strict=True):
            if isinstance(block, ImageBlock) and isinstance(block.source, ImageBytes):
                source = block.source
            elif isinstance(block, AudioBlock):
                source = block.source
            else:
                raise TypeError("反思媒体必须是已入账的原生图像或音频字节。")
            if (
                source.media_type
                != ("audio/mpeg" if reference.media_type == "audio/mp3" else reference.media_type)
                or len(source.data) != reference.byte_length
                or hashlib.sha256(source.data).hexdigest() != reference.media_id
            ):
                raise RuntimeError("原生媒体与持久引用不一致。")
        return (TextBlock(_json(payload)), *native)

    async def recall(
        self,
        query: StrictStr,
        entities_json: str = "[]",
        start: Finite | None = None,
        end: Finite | None = None,
        at: Finite | None = None,
        include_inactive: StrictBool = False,
    ) -> str | ToolResult:
        """检索当前有效认识及原始来源；历史须明确 include_inactive，不把索引当权威。"""
        try:
            result = await self.knowledge.recall(
                query, entities_json, start, end, at, include_inactive
            )
        except KnowledgeInputError as error:
            return _input_error(error)
        return recall_payload(result)

    async def read_evidence(
        self,
        id: Text,
        level: str = "evidence",
        cursor: str | None = None,
        max_chars: Positive | None = None,
        version: Positive | None = None,
        *,
        context: ToolContext,
    ) -> ToolResult:
        """展开已有确切版本；续读核验原版本，认识与摘要不授予原文阅读资格。

        省略 version 首读当前版本；历史冲突引用可指定 version。cursor 始终
        绑定原快照，不能用新的版本改写后续页，也不访问外部服务。
        """
        try:
            page = await self.knowledge.read_evidence(id, level, cursor, max_chars, version)
            content = await self._page_content(page)
            result = ToolResult(content=content)
            if page.slice is not None:
                await self.store.record_delivery(
                    self.batch.batch_id,
                    self.batch.version,
                    tool_call_id=context.call.id,
                    material=result.content,
                    slice_refs=(page.slice.slice_ref,),
                )
        except KnowledgeInputError as error:
            return _input_error(error)
        if page.slice is not None:
            self.deliveries.register_result(context.call.id, result, (page.slice.slice_ref,))
        if self.delivery is not None:
            # 原候选登记不会赋予已读资格。Gate 若改为容量回执，实际请求与
            # 这里完整正文/媒体不匹配，presented_refs 就不会授予该范围。
            return self.delivery.stage(
                context.call, ToolDelivery(result, evidence_fallback(page, cursor=cursor))
            )
        return result

    async def revise_memory(
        self,
        id: Text,
        content: Text,
        sources: tuple[Text, ...],
        base_version: Nonnegative = 0,
        kind: MemoryKind = "belief",
        scope: Text = "subject",
        summary: str = "",
        overview: str = "",
        entities: tuple[Text, ...] = (),
        valid_from: Finite | None = None,
        valid_until: Finite | None = None,
        supersedes: Text | None = None,
        conflicts: tuple[MemoryConflictTarget, ...] = (),
    ) -> str | ToolResult:
        """原子提交有据认识与未决关系，sources 为已呈现原文片段。

        base_version 必须符合当前版本；conflicts 指定仍有效的对方确切认识
        版本，关系双向可见但不裁决、替代或授予对方原文的阅读资格。
        """
        try:
            patch = MemoryPatch(
                id=id,
                content=content,
                sources=sources,
                base_version=base_version,
                kind=kind,
                scope=scope,
                summary=summary,
                overview=overview,
                entities=entities,
                valid_from=valid_from,
                valid_until=valid_until,
                supersedes=supersedes,
                conflicts=conflicts,
            )
            record = await self.store.revise_memory(self.batch.batch_id, self.batch.version, patch)
        except (KnowledgeInputError, ValidationError) as error:
            return _input_error(error)
        return _json(record.model_dump(mode="json"))

    async def supervise(
        self,
        id: Text,
        issue: Text,
        evidence: tuple[Text, ...],
        suggestion: Text,
        level: str = "reminder",
        activity_id: Text | None = None,
        activity_version: Nonnegative | None = None,
        expires_at: Finite | None = None,
    ) -> str | ToolResult:
        """立即提交有据监管；urgent仅请求重思考，不暂停设备或代替前台决定。"""
        try:
            notice = SupervisionNotice.model_validate(
                {
                    "id": id,
                    "issue": issue,
                    "evidence": evidence,
                    "suggestion": suggestion,
                    "level": level,
                    "activity_id": activity_id,
                    "activity_version": activity_version,
                    "expires_at": expires_at,
                }
            )
            observed_generation = self.ports.foreground_generation()
            record = await self.store.supervise(self.batch.batch_id, self.batch.version, notice)
        except (KnowledgeInputError, ValidationError) as error:
            return _input_error(error)
        if record.active and not record.duplicate:
            await self.ports.supervision_committed(record, observed_generation=observed_generation)
        return _json(record.model_dump(mode="json"))

    async def finish_reflection(
        self,
        reviews: tuple[Review, ...],
        deferred: tuple[Deferred, ...],
        findings: tuple[FindingAssessment, ...],
    ) -> str | ToolResult:
        """原子提交本批复核、暂缓和异常判断；written必须对应本批真实有效修订。"""
        try:
            submission = FinishSubmission(reviews=reviews, deferred=deferred, findings=findings)
            receipt = await self.store.finish_reflection(
                self.batch.batch_id, self.batch.version, submission
            )
        except (KnowledgeInputError, ValidationError) as error:
            return _input_error(error)
        return _json(receipt.model_dump(mode="json"))


__all__ = ["EvidenceDeliveries", "ReflectionPorts", "ReflectionTools"]
