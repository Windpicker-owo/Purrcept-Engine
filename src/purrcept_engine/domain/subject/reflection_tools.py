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

from purrcept_engine.domain.context.delivery import (
    ToolDelivery,
    ToolDeliveryGate,
    evidence_fallback,
)
from purrcept_engine.domain.inputs.models import MediaReference
from purrcept_engine.domain.knowledge.models import (
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
from purrcept_engine.domain.knowledge.ports import KnowledgeStore
from purrcept_engine.domain.knowledge.service import KnowledgeService, recall_payload
from purrcept_engine.domain.subject.prompts import REFLECTION_PROMPT

Nonnegative = Annotated[int, Field(ge=0, strict=True)]
Positive = Annotated[int, Field(gt=0, strict=True)]


def reflection_instructions() -> tuple[SystemInstruction, ...]:
    """构造独立反思的固定规则，完成确认属于工具，原文仍是 user 材料。

    Returns:
        按固定顺序组成 system 前缀的完整指令与能力目录。
    """
    return (REFLECTION_PROMPT.instruction(),)


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
        """借用账本和外围端口，连续会话应复用同一个材料登记实例。

        Args:
            store: 应用借出的领域存储端口，实际连接和关闭归资源所有者。
            batch: 当前独立反思批次及其冻结范围。
            ports: 应用显式借出的有限端口，不包含设备客户端或通用服务定位器。
            page_chars: 本实例统一的正文分页字符上限。 默认 8000。
            knowledge: 原文展开与召回服务；None 创建同一账本上的 KnowledgeService。 None
                表示未借入认识服务。
            deliveries: 连续反思会话复用的完整材料登记；None 创建本实例登记。
            delivery: 本轮工具原文交付门槛；None 不执行候选交付替换。
        """
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
        """返回固定五工具，不包含世界行动、手动压缩或旧 complete_review。

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

    async def initial_message(self, pages: tuple[EvidencePage, ...]) -> Message:
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

    # 维护者契约（工具说明参与模型请求，保持下方 docstring 原文）。
    # 检索长时记忆库中有效认识及原始来源。
    #
    # Args:
    #     query: 用于关键词或语义检索的意图文本。
    #     entities_json: 已编码为 JSON 字符串数组的实体过滤条件。 默认 '[]'。
    #     start: 按发生时间筛选的起点；None 表示不限制此端。
    #     end: 按发生时间筛选的终点；None 表示不限制此端。
    #     at: 核验记录有效性的 Unix 时间戳；None 使用当前时间。
    #     include_inactive: 是否同时返回已失效的历史记录，并保留失效原因。 默认 False。
    #
    # Returns:
    #     成功时的编码业务结果，或含原生内容与错误标记的完整工具结果。
    #
    # Note:
    #     在更新认识前查重，判断新材料是补充、确认还是推翻旧认识；历史记录须明确
    #     include_inactive。
    async def recall(
        self,
        query: StrictStr,
        entities_json: str = "[]",
        start: Finite | None = None,
        end: Finite | None = None,
        at: Finite | None = None,
        include_inactive: StrictBool = False,
    ) -> str | ToolResult:
        """检索长时记忆库中有效认识及原始来源。
        在更新认识前查重，判断新材料是补充、确认还是推翻旧认识；历史记录须明确 include_inactive。
        """
        try:
            result = await self.knowledge.recall(
                query, entities_json, start, end, at, include_inactive
            )
        except KnowledgeInputError as error:
            return _input_error(error)
        return recall_payload(result)

    # 维护者契约（工具说明参与模型请求，保持下方 docstring 原文）。
    # 展开本批候选或引用的确切原文版本，查阅完整对话上下文与原生媒体。
    #
    # Args:
    #     id: 当前工具定位的认识、原文或计划身份。
    #     level: 需要展开的材料层级，具体允许值由认识服务校验。 默认 'evidence'。
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

    # 维护者契约（工具说明参与模型请求，保持下方 docstring 原文）。
    # 原子沉淀或修订有据认识（preference/relationship/episode/experience/belief等）。
    #
    # Args:
    #     id: 当前工具定位的认识、原文或计划身份。
    #     content: 本次明确写入的完整正文。
    #     sources: 为本次修订或监管提供依据的原文片段身份。
    #     base_version: 本次认识修订所基于的版本，首次创建使用 0。 默认 0。
    #     kind: 此业务值所属的稳定种类。 默认 'belief'。
    #     scope: 本次监管意见所约束的明确业务范围。 默认 'subject'。
    #     summary: 已冻结的摘要文本或消息，不替代原文已读证明。 默认 ''。
    #     overview: 用于召回的简短概览，不替代完整正文。 默认 ''。
    #     entities: 需要共同检索或关联的实体名称集合。 默认不附加此类条目。
    #     valid_from: 认识事实的生效时间；None 表示未指定下界。
    #     valid_until: 认识事实的失效时间；None 表示未指定上界。
    #     supersedes: 本次修订明确替代的旧认识身份。
    #     conflicts: 与本次认识保持未决反证关系的目标及确切版本。 默认不附加此类条目。
    #
    # Returns:
    #     成功时的编码业务结果，或含原生内容与错误标记的完整工具结果。
    #
    # Note:
    #     sources 必须是本轮已呈现的原文片段引用，base_version 严格对齐当前版本；
    #     保留时间范围与反例。
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
        """原子沉淀或修订有据认识（preference/relationship/episode/experience/belief等）。
        sources 必须是本轮已呈现的原文片段引用，base_version 严格对齐当前版本；保留时间范围与反例。
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

    # 维护者契约（工具说明参与模型请求，保持下方 docstring 原文）。
    # 向前台提交有据监管意见，纠偏客服腔、未履行的约定或认知冲突。
    #
    # Args:
    #     id: 当前工具定位的认识、原文或计划身份。
    #     issue: 监管意见指出的具体问题。
    #     evidence: 本次工具给出的原文片段身份集合。
    #     suggestion: 基于实际证据提出的修正建议。
    #     level: 需要展开的材料层级，具体允许值由认识服务校验。 默认 'reminder'。
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
                }
            )
            observed_generation = self.ports.foreground_generation()
            record = await self.store.supervise(self.batch.batch_id, self.batch.version, notice)
        except (KnowledgeInputError, ValidationError) as error:
            return _input_error(error)
        if record.active and not record.duplicate:
            await self.ports.supervision_committed(record, observed_generation=observed_generation)
        return _json(record.model_dump(mode="json"))

    # 维护者契约（工具说明参与模型请求，保持下方 docstring 原文）。
    # 原子提交本批反思结案、暂缓和程序异常评估。
    #
    # Args:
    #     reviews: 对本批已复核原文范围给出的结论。
    #     deferred: 本批未完成范围及明确的延期理由。
    #     findings: 本批需要核验的实际异常或运行观察。
    #
    # Returns:
    #     成功时的编码业务结果，或含原生内容与错误标记的完整工具结果。
    #
    # Note:
    #     written 必须真实对应本批 revise_memory 产生的有效修订，每项材料明确交代处置决定。
    async def finish_reflection(
        self,
        reviews: tuple[Review, ...],
        deferred: tuple[Deferred, ...],
        findings: tuple[FindingAssessment, ...],
    ) -> str | ToolResult:
        """原子提交本批反思结案、暂缓和程序异常评估。
        written 必须真实对应本批 revise_memory 产生的有效修订，每项材料明确交代处置决定。
        """
        try:
            submission = FinishSubmission(reviews=reviews, deferred=deferred, findings=findings)
            receipt = await self.store.finish_reflection(
                self.batch.batch_id, self.batch.version, submission
            )
        except (KnowledgeInputError, ValidationError) as error:
            return _input_error(error)
        return _json(receipt.model_dump(mode="json"))


__all__ = ["EvidenceDeliveries", "ReflectionPorts", "ReflectionTools"]
