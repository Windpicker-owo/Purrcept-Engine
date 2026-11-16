"""为行为评审装配原文阅读、监管建议与结构化结案工具。

完整材料登记和实际呈现分开：真正成功请求核对内容后才提交阅读资格。
本批工具不能执行世界行动，也没有记忆修订或 Notebook 写入权限。
原始媒体与文本作为不可拆的交付候选，容量不足保留确切原页引用。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

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
from pydantic import Field, ValidationError

from purrcept_engine.domain.context.audio_receipts import evidence_text_view
from purrcept_engine.domain.context.delivery import (
    ToolDelivery,
    ToolDeliveryGate,
    evidence_fallback,
)
from purrcept_engine.domain.evidence.models import (
    BehaviorConclusion,
    Deferred,
    EvidenceDelivery,
    EvidenceInputError,
    EvidencePage,
    FindingAssessment,
    FinishSubmission,
    Finite,
    ObservationAssessment,
    ObservationScope,
    ReflectionBatch,
    Review,
    SupervisionNotice,
    SupervisionRecord,
    Text,
)
from purrcept_engine.domain.evidence.ports import EvidenceStore
from purrcept_engine.domain.evidence.service import EvidenceService
from purrcept_engine.domain.inputs.models import MediaReference
from purrcept_engine.domain.subject.prompts import (
    BEHAVIOR_REVIEW_PROMPT,
    HUMAN_FEEDBACK_PROMPT,
)
from purrcept_engine.values import JsonObject

Nonnegative = Annotated[int, Field(ge=0, strict=True)]
Positive = Annotated[int, Field(gt=0, strict=True)]


def reflection_instructions(
    purpose: Literal["behavior"] = "behavior",
) -> tuple[SystemInstruction, ...]:
    """构造当前分析用途的固定规则，完成确认属于工具，原文仍是 user 材料。

    Args:
        purpose: 固定为 behavior，与持久化行为评审批次使用同一用途标记。

    Returns:
        按固定顺序组成 system 前缀的完整指令与能力目录。
    """
    return (BEHAVIOR_REVIEW_PROMPT.instruction(), HUMAN_FEEDBACK_PROMPT.instruction())


class ReflectionPorts(Protocol):
    """app 提供媒体及监管交接，不向工具借出模型客户端、执行器或通用数据库。

    media_blocks 返回与每个唯一入账媒体引用对应的原生字节内容，缺失/不支持
    原样失败，不用文字替代。supervision_committed 只交接已确认的新意见；
    urgent 请求重思考，不调用世界能力。资源关闭由 app 等待借用后完成。
    """

    async def media_blocks(self, media: tuple[MediaReference, ...]) -> tuple[ContentBlock, ...]:
        """按传入引用顺序返回全部原生媒体，字节应与持久 BLOB 摘要一致。

        Args:
            media: 已接管媒体的引用集合，展开时必须取得原始 BLOB。

        Returns:
            按原始顺序展开的完整 Core 内容块，包括真实原生媒体。
        """
        ...

    def foreground_generation(self) -> int:
        """观察提交前的前台代次，不能在迟到通知中用新代次替换原信号。"""
        ...

    async def supervision_committed(
        self, record: SupervisionRecord, *, observed_generation: int
    ) -> None:
        """交接已经生效的新监管。

        Args:
            record: 带原始依据与版本约束的监管记录。
            observed_generation: 接管或判断开始时观察到的前台生成代次。

        Note:
            展示失败不回滚其提交。
        """
        ...


@dataclass(frozen=True, slots=True)
class _Delivery:
    """保存当前会话的完整交付候选与精确范围，供真实请求逐项匹配。"""

    content: tuple[ContentBlock, ...]
    slice_refs: tuple[str, ...]


class EvidenceDeliveries:
    """批次所有者持有的完整材料候选登记；同批工具请求复用，下一批重新建立。

    只登记 Engine 真正创建的初始 Message 和 read_evidence ToolResult。对照
    完整 block 值而非字符串标记；普通外部文字、不同 tool_call_id、缺媒体或
    被改写正文不能取得资格。该缓存不是权威已读状态，Store 仍核验版本/范围。
    """

    def __init__(self) -> None:
        """为一个独立会话创建登记，不与前台可变历史共享。"""
        self._initial: list[tuple[Message, tuple[str, ...]]] = []
        self._tools: dict[str, list[_Delivery]] = {}

    def register_initial(self, message: Message, slice_refs: tuple[str, ...]) -> None:
        """登记宿主真正组装的完整输入，不从其中的外界正文解析权限标记。

        Args:
            message: 当前请求中实际存在的完整 Core 消息。
            slice_refs: 当前材料实际覆盖的原文片段身份。
        """
        entry = (message, slice_refs)
        if entry not in self._initial:
            self._initial.append(entry)

    def register_result(
        self, tool_call_id: str, result: ToolResult, slice_refs: tuple[str, ...]
    ) -> None:
        """登记真实工具候选，复用调用 ID 时仍按完整内容区分原始交付。

        Args:
            tool_call_id: Core 分配的工具调用身份，用于匹配完整工具结果。
            result: Core 工具的完整内容与错误标记。
            slice_refs: 当前材料实际覆盖的原文片段身份。

        Raises:
            ValueError: 错误工具结果不能登记原文交付。
        """
        if result.is_error:
            raise ValueError("错误工具结果不能登记原文交付。")
        delivery = _Delivery(result.content, slice_refs)
        entries = self._tools.setdefault(tool_call_id, [])
        if delivery not in entries:
            entries.append(delivery)

    def restore_history(self, deliveries: tuple[EvidenceDelivery, ...]) -> None:
        """接回账本完整匹配的原交付。

        Args:
            deliveries: 已登记的完整原文交付记录，保存不等同于已呈现。

        Raises:
            ValueError: 初始交付不能携带工具调用身份；持久交付类别与完整材料不一致。

        Note:
            持久登记本身不代表本次已经呈现。

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

        Args:
            messages: 按实际先后排列的完整 Core 消息，不从正文猜测交付身份。

        Note:
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
        """从实际请求找出精确登记的 block 组，不扫描字符串相似或全量待办。

        Args:
            request: 已经按 Core 协议组装的完整生成请求。

        Returns:
            与本次完整请求逐项匹配的原文片段身份，不能由正文标记伪造。
        """
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


def _input_error(
    error: EvidenceInputError | ValidationError,
    *,
    memory_recovery: dict[str, object] | None = None,
) -> ToolResult:
    """只将可纠正参数/范围错误交回模型，存储及程序故障不经此转换。"""
    if isinstance(error, ValidationError):
        issues = error.errors(include_input=False, include_context=False, include_url=False)
        description = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}" for issue in issues
        )
    else:
        description = str(error)
    payload: dict[str, object] = {"error_type": type(error).__name__, "error": description}
    if memory_recovery is not None:
        payload["memory_recovery"] = memory_recovery
    return ToolResult(content=(TextBlock(_json(payload)),), is_error=True)


class ReflectionTools:
    """宿主为当前用途和批次创建的工具实例，Core 按用途装配四项工具。

    批次身份与版本由宿主绑定，不要求模型复述。后端在有效响应、容量及权威
    请求提交完成之后调用 presented_refs，再提交 mark_presented，最后才将
    响应交给 Core 工具循环。模型普通收尾不确认复核；已成功知识提交不回滚。
    """

    def __init__(
        self,
        store: EvidenceStore,
        batch: ReflectionBatch,
        ports: ReflectionPorts,
        *,
        page_chars: int = 8000,
        evidence: EvidenceService | None = None,
        deliveries: EvidenceDeliveries | None = None,
        delivery: ToolDeliveryGate | None = None,
    ) -> None:
        """借用账本和外围端口，材料登记仅在当前批次的请求间复用。

        Args:
            store: 应用借出的领域存储端口，实际连接和关闭归资源所有者。
            batch: 当前独立反思批次及其冻结范围。
            ports: 应用显式借出的有限端口，不包含设备客户端或通用服务定位器。
            page_chars: 本实例统一的正文分页字符上限。 默认 8000。
            evidence: 原文版本阅读服务；None 创建同一证据端口上的 EvidenceService。
            deliveries: 当前批次复用的完整材料登记；None 创建本实例登记。
            delivery: 本轮工具原文交付门槛；None 不执行候选交付替换。
        """
        self.store = store
        self.batch = batch
        self.ports = ports
        self.evidence = evidence or EvidenceService(store, page_chars=page_chars)
        self.deliveries = deliveries or EvidenceDeliveries()
        self.delivery = delivery
        # 这是本批提示与结案状态，不是持久已读凭据。旧批次不会跨进程恢复运行；
        # 未结案时原文待办仍由账本保留，新批次重新复核，不依赖内存继续旧预算。
        self._source_catalog: dict[str, tuple[str, int]] = {}
        operations = [
            FunctionTool(self.read_evidence),
            FunctionTool(self.supervise),
            FunctionTool(self.submit_behavior_review),
        ]
        self._toolset = ToolSet(tuple(operations))

    def toolset(self) -> ToolSet:
        """返回当前用途的四项工具，不包含世界行动或跨用途写入。

        Returns:
            当前会话按固定顺序装配的 Core 工具集合。
        """
        return self._toolset

    def presented_refs(self, request: ModelRequest) -> tuple[str, ...]:
        """对照真正请求取得可信候选，调用本方法本身不修改账本。

        Args:
            request: 已经按 Core 协议组装的完整生成请求。

        Returns:
            本次真实请求完整覆盖的原文片段身份，不在此提前提交已读资格。
        """
        return self.deliveries.presented_refs(request)

    async def initial_message(
        self,
        pages: tuple[EvidencePage, ...],
        *,
        record: bool = True,
        context: JsonObject | None = None,
    ) -> Message:
        """组装本批原文及完整媒体，只登记候选而不提前声明模型已读。

        Args:
            pages: 当前批次实际取得的完整原文页面。

        Returns:
            供后续 Core 请求使用的完整消息。
        """
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
        if context is not None:
            blocks.append(
                TextBlock(
                    _json(
                        {
                            "related_context": context,
                            "qualification": (
                                "关联背景不代替原文片段；新增结论仍须引用真实呈现范围。"
                            ),
                        }
                    )
                )
            )
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
                "evidence": tuple(dict.fromkeys(page.snapshot.evidence_id for page in pages)),
                "source_slice_refs": tuple(references),
                "reflection_batch": self.batch.batch_id,
            },
        )
        if references and record:
            await self.store.record_delivery(
                self.batch.batch_id,
                self.batch.version,
                tool_call_id=None,
                material=message,
                slice_refs=tuple(references),
            )
            self.deliveries.register_initial(message, tuple(references))
        return message

    async def submit_behavior_review(
        self,
        decision: Literal["no_intervention", "suggestion", "insufficient_evidence"],
        conclusion: str,
        evidence: tuple[str, ...],
        observations: tuple[ObservationAssessment, ...] = (),
        deferred: tuple[Deferred, ...] = (),
    ) -> str | ToolResult:
        """提交局部评审结果；suggestion 必须已有本批实际保存的监管建议。

        证据必须真实呈现，未覆盖材料仍需逐项明确复核；意见是否接受与后续行为是否改善分别保存。
        """
        try:
            behavior = (
                None
                if not evidence and deferred
                else BehaviorConclusion(
                    decision=decision,
                    conclusion=conclusion,
                    evidence=evidence,
                    observations=observations,
                )
            )
            submission = FinishSubmission(
                reviews=()
                if behavior is None
                else (
                    Review(
                        issue="局部行为评审",
                        evidence=evidence,
                        conclusion=conclusion,
                        reason=decision,
                    ),
                ),
                deferred=deferred,
                findings=tuple(
                    FindingAssessment(id=item.id, status="uncertain", reason=conclusion)
                    for item in self.batch.findings
                ),
                behavior=behavior,
            )
            receipt = await self.store.finish_reflection(
                self.batch.batch_id, self.batch.version, submission
            )
            return _json(receipt.model_dump(mode="json"))
        except (EvidenceInputError, ValidationError) as error:
            return _input_error(error)

    async def _page_content(self, page: EvidencePage) -> tuple[ContentBlock, ...]:
        """来源 JSON 与实际原生媒体共同构成一份不可拆的交付资格。"""
        snapshot = page.snapshot
        payload: dict[str, object]
        payload = {
            "item": {
                "id": snapshot.evidence_id,
                "version": snapshot.version,
                "kind": snapshot.kind,
                "text": evidence_text_view(page),
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
            "source_slice_refs": [] if page.slice is None else [page.slice.slice_ref],
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
        if page.slice is not None:
            # 目录只说明该页提供了哪个可复制引用。真实请求是否完整呈现仍由
            # EvidenceDeliveries 与 Store 核验，错误反馈不能替媒体缺口补授资格。
            self._source_catalog[page.slice.slice_ref] = (snapshot.evidence_id, snapshot.version)
        return (TextBlock(_json(payload)), *native)

    # 维护者契约（工具说明参与模型请求，保持下方 docstring 原文）。
    # 展开本批候选或引用的确切原文版本，查阅完整对话上下文与原生媒体。
    #
    # Args:
    #     id: 当前工具定位的原文身份。
    #     level: 原文阅读层级，由证据服务校验。 默认 'evidence'。
    #     cursor: 宿主签发并保存的不透明续读游标，续读同一快照版本。 None 表示首次读取。
    #     max_chars: 本页正文的字符上限；分页游标继续绑定原快照。 None
    #         使用本实例配置的统一页长。
    #     version: 当前反思批次的确切版本，旧代次不再拥有写入资格。 None 首读当前版本，
    #         续读仍绑定游标的原版本。
    #     context: 宿主或 Core 注入的可信调用上下文，不由模型自行填写。
    #
    # Returns:
    #     完整工具内容和错误标记；引用回执可能要求继续展开原文。
    #
    # Note:
    #     省略 version 首读当前版本；历史冲突引用可指定 version。cursor 始终绑定原快照。
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
        """展开本批候选或引用的确切原文版本，查阅完整对话上下文与原生媒体。
        省略 version 首读当前版本；历史冲突引用可指定 version。cursor 始终绑定原快照。
        """
        try:
            page = await self.evidence.read_evidence(id, level, cursor, max_chars, version)
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
        except EvidenceInputError as error:
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

    # 维护者契约（工具说明参与模型请求，保持下方 docstring 原文）。
    # 向前台提交有据监管意见，纠偏客服腔、未履行的约定或认知冲突。
    #
    # Args:
    #     id: 本次监管意见的稳定身份。
    #     issue: 监管意见指出的具体问题。
    #     evidence: 本次工具给出的原文片段身份集合。
    #     suggestion: 基于实际证据提出的修正建议。
    #     level: 监管等级，urgent 请求主体重新思考。 默认 'reminder'。
    #     activity_id: 已创建活动的稳定身份。 None 只适用于不需要已有活动身份的控制。
    #     activity_version: 监管意见所约束的活动版本，须与活动身份同时提供。
    #     expires_at: 监管意见的有效截止时刻；None 表示无显式期限。
    #
    # Returns:
    #     成功时的编码业务结果，或含原生内容与错误标记的完整工具结果。
    #
    # Note:
    #     urgent 仅请求重新思考当前轮次，不代替前台做世界决定，也不直接调用世界能力。
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
        scope: ObservationScope | None = None,
        expected_change: Text | None = None,
        generalized: bool = False,
    ) -> str | ToolResult:
        """向前台提交有据监管意见，纠偏客服腔、未履行的约定或认知冲突。
        urgent 仅请求重新思考当前轮次，不代替前台做世界决定，也不直接调用世界能力。
        """
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
                    "scope": scope,
                    "expected_change": expected_change,
                    "generalized": generalized,
                }
            )
            observed_generation = self.ports.foreground_generation()
            record = await self.store.supervise(self.batch.batch_id, self.batch.version, notice)
        except (EvidenceInputError, ValidationError) as error:
            return _input_error(error)
        if record.active and not record.duplicate:
            await self.ports.supervision_committed(record, observed_generation=observed_generation)
        return _json(record.model_dump(mode="json"))


__all__ = ["EvidenceDeliveries", "ReflectionPorts", "ReflectionTools"]
