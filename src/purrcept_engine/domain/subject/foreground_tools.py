"""前台的六个 Core 工具及固定指令，不拥有模型循环或设备客户端。

调用方为一次连续处理绑定资格和有限执行入口；只有 play_action 产生世界
效果。其余工具检索原文/认识或提交计划/监管回应。最终模型文字保持内部
结果，不自动复制到控制台、QQ 或音频。认识修订只存在于独立反思工具集。
"""

from collections.abc import Callable
from typing import Protocol

from purrcept_core.models import ContentBlock, SystemInstruction, TextBlock
from purrcept_core.models.tools import FunctionTool, ToolContext, ToolResult, ToolSet

from purrcept_engine.domain.context.delivery import (
    ToolDelivery,
    ToolDeliveryGate,
    evidence_fallback,
    presented_evidence_calls,
)
from purrcept_engine.domain.context.presentation import action_timeline, render_material
from purrcept_engine.domain.context.tool_errors import model_input_boundary
from purrcept_engine.domain.execution.catalog import Catalog
from purrcept_engine.domain.execution.models import ActionRecord
from purrcept_engine.domain.inputs.models import MediaReference
from purrcept_engine.domain.knowledge.models import EvidenceSnapshot, Version, WakeupPlan
from purrcept_engine.domain.knowledge.ports import KnowledgeStore
from purrcept_engine.domain.knowledge.service import KnowledgeService, recall_payload
from purrcept_engine.domain.subject.prompts import (
    CATALOG_INSTRUCTION_PROMPT,
    CHARACTER_CARD_PROMPT,
    CHARACTER_DETAIL_PROMPT,
    CHARACTER_NOTES_PROMPT,
    FOREGROUND_PROMPT,
    HUMAN_FEEDBACK_PROMPT,
)
from purrcept_engine.serialization import encode
from purrcept_engine.values import decode_object


class ForegroundPorts(Protocol):
    """主体借用的真实行动与媒体端口，所有效果先于成功回执提交。

    play_action 在实际派发前复核本轮资格；取消工具等待不会撤销已开始行动。
    media_blocks 只展开已经接管的 BLOB，不访问网络或将媒体改写为说明。
    wakeup_committed 通知调度器重新检查已提交计划，不直接执行世界动作。
    """

    async def play_action(self, source: str) -> ActionRecord:
        """执行一段受限 Python，返回保存后的实际终态或未知调用事实。

        Args:
            source: 待执行的受限 Python 源码。

        Returns:
            已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。
        """
        ...

    async def media_blocks(self, media: tuple[MediaReference, ...]) -> tuple[ContentBlock, ...]:
        """展开完整原生内容，缺失/不支持明确报告。

        Args:
            media: 已接管媒体的引用集合，展开时必须取得原始 BLOB。

        Returns:
            按原始顺序展开的完整 Core 内容块，包括真实原生媒体。
        """
        ...

    def wakeup_committed(self) -> None:
        """唤醒有限调度器查询权威计划，不重放行动。"""
        ...


def format_character_card(character: dict[str, str | None]) -> str:
    """将固定身份配置格式化为结构化角色卡，凸显独立生活实感。

    Args:
        character: 固定身份卡的显式字段，缺省行为由指令构造器决定。
    """
    one_line = character.get("one_line")
    notes = character.get("notes")
    # 缺省身份由模板的默认绑定给出；只筛选显式非空配置，不清理字段空白。
    values: dict[str, object] = {
        key: value for key in CHARACTER_CARD_PROMPT.fields if (value := character.get(key))
    }
    values["one_line"] = CHARACTER_DETAIL_PROMPT.render({"text": one_line}) if one_line else None
    values["notes"] = CHARACTER_NOTES_PROMPT.render({"text": notes}) if notes else None
    return CHARACTER_CARD_PROMPT.render(values)


def foreground_instructions(
    character: dict[str, str | None], catalog: Catalog
) -> tuple[SystemInstruction, ...]:
    """构造独立生活主体的具身前台系统指令与能力目录说明。

    Args:
        character: 固定身份卡的显式字段，缺省行为由指令构造器决定。
        catalog: 全部已启用插件共同生成的冻结目录。

    Returns:
        按固定顺序组成 system 前缀的完整指令与能力目录。

    Note:
        确立“过自己的日子”之核心主旨：拥有独立生活、节奏与作息，交流仅为日常
        的一部分；彻底祛除客服味、过度热情与表演型人格；追求真实、松弛、克制
        的生活质感；同时严格恪守 play_action 唯一外发等底层执行契约。
    """
    return (
        FOREGROUND_PROMPT.instruction({"profile": format_character_card(character)}),
        CATALOG_INSTRUCTION_PROMPT.instruction({"catalog": catalog.instructions}),
        HUMAN_FEEDBACK_PROMPT.instruction(),
    )


class ForegroundTools:
    """一次前台批次的工具绑定，固定顺序与参数名，不携带认识写入权限。"""

    def __init__(
        self,
        knowledge: KnowledgeService,
        store: KnowledgeStore,
        ports: ForegroundPorts,
        *,
        check: Callable[[], None],
        delivery: ToolDeliveryGate | None = None,
    ) -> None:
        """绑定现有子系统和本轮资格，Core 在其自身工具循环中调用。

        Args:
            knowledge: 原文展开与认识召回服务，修订仍经过反思批次契约。
            store: 应用借出的领域存储端口，实际连接和关闭归资源所有者。
            ports: 应用显式借出的有限端口，不包含设备客户端或通用服务定位器。
            check: 在提交或派发前复核本轮资格的同步函数。
            delivery: 本轮工具原文交付门槛；None 不执行候选交付替换。
        """
        self.knowledge = knowledge
        self.store = store
        self.ports = ports
        self.check = check
        self.delivery = delivery
        # Core 只向注册入口注入调用身份，公开直接方法及模型参数保持原契约。
        self.tools = ToolSet(
            (
                FunctionTool(
                    self._deliver_action, name="play_action", description=self.play_action.__doc__
                ),
                FunctionTool(self.recall),
                FunctionTool(
                    self._deliver_evidence,
                    name="read_evidence",
                    description=self.read_evidence.__doc__,
                ),
                FunctionTool(self.plan_wakeup),
                FunctionTool(self.resolve_plan),
                FunctionTool(self.respond_supervision),
            )
        )

    # 维护者契约（工具说明参与模型请求，保持下方 docstring 原文）。
    # 执行 Python 行动代码。
    #
    # Args:
    #     action_code: 待隔离执行的受限 Python 源码。
    #
    # Returns:
    #     完整工具内容和错误标记；引用回执可能要求继续展开原文。
    #
    # Note:
    #     只调用本轮目录中的世界路径，返回真实回执。
    #
    #     你对外部世界的全部表达与控制（说话、发QQ、调神态动作、记笔记、
    #     定闹铃）必须通过此工具进行。
    #     每次调用都在全新的隔离环境中运行，不保留上次变量或导入，me 不自动存在。
    #     使用路径前必须写 from world import me, world；能力必须 await 并传 schema 关键字。
    #     可使用顶层 await 和 return，不要包进函数或 asyncio.run。dotted_path 是调用路径。
    #     return 返回继续查看的结果，print 只输出调试。禁止反射、文件、网络、函数、类和 lambda。
    #     失败后按回执纠正，不猜测已经完成，也不重放 UNKNOWN。
    @model_input_boundary
    async def play_action(self, action_code: str) -> ToolResult:
        """执行 Python 行动代码；只调用本轮目录中的世界路径，返回真实回执。
        你对外部世界的全部表达与控制（说话、发QQ、调神态动作、记笔记、定闹铃）必须通过此工具进行。
        每次调用都在全新的隔离环境中运行，不保留上次变量或导入，me 不自动存在。
        使用路径前必须写 from world import me, world；能力必须 await 并传 schema 关键字。
        可使用顶层 await 和 return，不要包进函数或 asyncio.run。dotted_path 是调用路径。
        return 返回继续查看的结果，print 只输出调试。禁止反射、文件、网络、函数、类和 lambda。
        失败后按回执纠正，不猜测已经完成，也不重放 UNKNOWN。
        """
        return (await self._action_result(action_code)).result

    async def _action_result(self, action_code: str) -> ToolDelivery:
        """取得真实效果后保存即时可展开原文；容量失败不回写行动状态。"""
        self.check()
        result = await self.ports.play_action(action_code)
        media = tuple(dict.fromkeys(reference for call in result.calls for reference in call.media))
        original = encode(result)
        # 来源身份：这里冻结实际返回的行动版本，不冒充尚待反馈桥接创建的
        # call 原文身份；同一引用后来展开也只能看到这个不可变快照。
        identity = f"action_result:{result.action_id}"
        if self.delivery is not None:
            if result.finished_at is None:
                raise RuntimeError("行动端口没有返回已保存的结束事实。")
            # 生命周期：行动/调用已经独立提交。此快照只冻结本次返回版本；
            # 迟到回执不改写原快照，后续展开也不会重放设备效果。
            await self.store.put_evidence(
                EvidenceSnapshot(
                    evidence_id=identity,
                    version=1,
                    kind="action_result",
                    text=original,
                    source_json=encode(
                        {"source": "actual_action_result", "action_id": result.action_id}
                    ),
                    occurred_at=result.finished_at,
                    media=media,
                ),
                needs_reflection=False,
            )
        native = await self.ports.media_blocks(media)
        # 原文仍逐字保存；可读时间是派生材料，完整/引用两条交付路径都要保留。
        timeline = action_timeline(result)
        return ToolDelivery(
            ToolResult(
                content=(
                    TextBlock(original),
                    *native,
                    TextBlock(render_material("行动时间线（宿主记录，非平台送达证明）", timeline)),
                )
            ),
            {
                "delivery": "reference_only",
                "original_content": "not_delivered",
                "stored_ref": {"id": identity, "version": 1},
                "action_id": result.action_id,
                "state": result.state,
                "timing": timeline,
                "calls": tuple(
                    {
                        "call_id": call.call_id,
                        "state": call.state,
                        "receipt_revision": call.receipt_revision,
                    }
                    for call in result.calls
                ),
                "retry": {
                    "tool": "read_evidence",
                    "id": identity,
                    "instruction": "正文和平台详细字段尚未交付；依赖它们时用更小的 max_chars "
                    "先 read_evidence，不要重放原行动。",
                },
            },
            failure_is_error=False,
            presented_calls=frozenset(
                call.call_id
                for call in result.calls
                if call.state == "confirmed" and call.result_json is not None
            ),
        )

    # 维护者契约（工具说明参与模型请求，保持下方 docstring 原文）。
    # 让注册工具借用 Core 调用身份，容量选择留到完整下一请求门槛。
    @model_input_boundary
    async def _deliver_action(self, action_code: str, *, context: ToolContext) -> ToolResult:
        """让注册工具借用 Core 调用身份，容量选择留到完整下一请求门槛。"""
        proposal = await self._action_result(action_code)
        if self.delivery is None:
            return proposal.result
        return self.delivery.stage(context.call, proposal)

    # 维护者契约（工具说明参与模型请求，保持下方 docstring 原文）。
    # 检索长期记忆与事实经历。
    #
    # Args:
    #     query: 用于关键词或语义检索的意图文本。
    #     entities_json: 已编码为 JSON 字符串数组的实体过滤条件。 默认 '[]'。
    #     start: 按发生时间筛选的起点；None 表示不限制此端。
    #     end: 按发生时间筛选的终点；None 表示不限制此端。
    #     at: 核验记录有效性的 Unix 时间戳；None 使用当前时间。
    #     include_inactive: 是否同时返回已失效的历史记录，并保留失效原因。 默认 False。
    #
    # Note:
    #     在回答前主动唤醒关于用户的偏好习惯、过往承诺、情感羁绊或特定经历，深化陪伴默契。
    #     query 为检索意图，entities_json 是实体数组，start/end 为发生时间，at 为事实有效时间。
    @model_input_boundary
    async def recall(
        self,
        query: str,
        entities_json: str = "[]",
        start: float | None = None,
        end: float | None = None,
        at: float | None = None,
        include_inactive: bool = False,
    ) -> str:
        """检索长期记忆与事实经历。
        在回答前主动唤醒关于用户的偏好习惯、过往承诺、情感羁绊或特定经历，深化陪伴默契。
        query 为检索意图，entities_json 是实体数组，start/end 为发生时间，at 为事实有效时间。
        """
        self.check()
        result = await self.knowledge.recall(
            query, entities_json, start, end, at, include_inactive=include_inactive
        )
        return recall_payload(result)

    # 维护者契约（工具说明参与模型请求，保持下方 docstring 原文）。
    # 展开已存原文或认识的确切版本，深入查阅未展开细节与原生媒体。
    #
    # Args:
    #     id: 当前工具定位的认识、原文或计划身份。
    #     level: 需要展开的材料层级，具体允许值由认识服务校验。 默认 'evidence'。
    #     cursor: 宿主签发并保存的不透明续读游标，续读同一快照版本。 None 表示首次读取。
    #     max_chars: 本页正文的字符上限；分页游标继续绑定原快照。 None
    #         使用本实例配置的统一页长。
    #     version: 目标记录的确切版本，区别于可变的当前版本。 None 首读当前版本，
    #         续读仍绑定游标的原版本。
    #
    # Returns:
    #     完整工具内容和错误标记；引用回执可能要求继续展开原文。
    #
    # Note:
    #     在需要准确核验用户原始输入、对话细节或特定记忆版本时调用，省略 version 首读当前版本。
    #
    #     历史冲突引用可指定 version；cursor 绑定同一版本，不能用新的版本
    #     改写后续页。认识、summary 和 overview 均不授予原文阅读资格。
    @model_input_boundary
    async def read_evidence(
        self,
        id: str,
        level: str = "evidence",
        cursor: str | None = None,
        max_chars: int | None = None,
        version: Version | None = None,
    ) -> ToolResult:
        """展开已存原文或认识的确切版本，深入查阅未展开细节与原生媒体。
        在需要准确核验用户原始输入、对话细节或特定记忆版本时调用，省略 version 首读当前版本。

        历史冲突引用可指定 version；cursor 绑定同一版本，不能用新的版本
        改写后续页。认识、summary 和 overview 均不授予原文阅读资格。
        """
        return (await self._evidence_result(id, level, cursor, max_chars, version)).result

    async def _evidence_result(
        self,
        id: str,
        level: str,
        cursor: str | None,
        max_chars: int | None,
        version: int | None,
    ) -> ToolDelivery:
        """构造不可拆的正文/媒体候选，有限回执始终定位原页而非下一页。"""
        self.check()
        page = await self.knowledge.read_evidence(id, level, cursor, max_chars, version)
        snapshot = page.snapshot
        memory = page.memory
        if snapshot is not None:
            item = {
                "id": snapshot.evidence_id,
                "version": snapshot.version,
                "kind": snapshot.kind,
                "text": page.text,
                "source": decode_object(snapshot.source_json.encode("utf-8")),
                "occurred_at": snapshot.occurred_at,
                "valid": snapshot.valid,
                "media": snapshot.media,
            }
        elif memory is not None:
            item = {
                "id": memory.patch.id,
                "version": memory.version,
                "kind": memory.patch.kind,
                "text": page.text,
                "sources": memory.patch.sources,
                "active": memory.active,
                "inactive_reason": memory.inactive_reason,
                "conflicts": [relation.model_dump(mode="json") for relation in memory.conflicts],
            }
        else:
            raise RuntimeError("原文页面缺少权威快照或认识版本。")
        # 页长约束作用于真实交付正文，不能再把 snapshot.text 全文塞回元数据。
        payload = {
            "item": item,
            "derived": memory is not None,
            "evidence_ref": None
            if snapshot is None
            else {"id": snapshot.evidence_id, "version": snapshot.version},
            "slice_ref": None if page.slice is None else page.slice.slice_ref,
            "range": None
            if page.slice is None
            else {"unit": page.slice.unit, "start": page.slice.start, "end": page.slice.end},
            "level": page.level,
            "next_cursor": page.next_cursor,
        }
        blocks: list[ContentBlock] = [TextBlock(encode(payload))]
        if page.slice is not None and page.snapshot is not None:
            selected = tuple(
                media for media in page.snapshot.media if media.media_id in page.slice.media_ids
            )
            blocks.extend(await self.ports.media_blocks(selected))
        return ToolDelivery(
            ToolResult(content=tuple(blocks)),
            evidence_fallback(page, cursor=cursor),
            presented_calls=presented_evidence_calls(page),
        )

    # 维护者契约（工具说明参与模型请求，保持下方 docstring 原文）。
    # 登记当前工具完整结果，直接调用 read_evidence 仍返回取得的原页。
    @model_input_boundary
    async def _deliver_evidence(
        self,
        id: str,
        level: str = "evidence",
        cursor: str | None = None,
        max_chars: int | None = None,
        version: Version | None = None,
        *,
        context: ToolContext,
    ) -> ToolResult:
        """登记当前工具完整结果，直接调用 read_evidence 仍返回取得的原页。"""
        proposal = await self._evidence_result(id, level, cursor, max_chars, version)
        if self.delivery is None:
            return proposal.result
        return self.delivery.stage(context.call, proposal)

    # 维护者契约（工具说明参与模型请求，保持下方 docstring 原文）。
    # 为未来的约定、关怀或承诺保存定时唤醒计划。
    #
    # Args:
    #     id: 当前工具定位的认识、原文或计划身份。
    #     due: 明确安排的到期 Unix 时间戳（秒）。
    #     reason: 供后续核对的明确处置原因。
    #     evidence: 结束计划或确认事实所依据的实际证据文本。
    #
    # Note:
    #     到期后调度器将主动唤起前台，使你能守时履行对用户的承诺与提醒。口头约定在此落入计划。
    @model_input_boundary
    async def plan_wakeup(self, id: str, due: float, reason: str, evidence: str) -> str:
        """为未来的约定、关怀或承诺保存定时唤醒计划。
        到期后调度器将主动唤起前台，使你能守时履行对用户的承诺与提醒。口头约定在此落入计划。
        """
        self.check()
        result = await self.store.plan_wakeup(
            WakeupPlan(id=id, due=due, reason=reason, evidence=evidence)
        )
        self.ports.wakeup_committed()
        return result.model_dump_json()

    # 维护者契约（工具说明参与模型请求，保持下方 docstring 原文）。
    # 以真实行动事实与闭环证据关闭已履行的计划，守护信任，告别口头应付。
    #
    # Args:
    #     id: 当前工具定位的认识、原文或计划身份。
    #     outcome: 此次操作的真实结论，不由期望效果推断成功。
    #     evidence: 结束计划或确认事实所依据的实际证据文本。
    @model_input_boundary
    async def resolve_plan(self, id: str, outcome: str, evidence: str) -> str:
        """以真实行动事实与闭环证据关闭已履行的计划，守护信任，告别口头应付。"""
        self.check()
        result = await self.store.resolve_plan(id, outcome, evidence)
        self.ports.wakeup_committed()
        return result.model_dump_json()

    # 维护者契约（工具说明参与模型请求，保持下方 docstring 原文）。
    # 审阅独立反思提出的监管意见并记录回应。
    #
    # Args:
    #     id: 当前工具定位的认识、原文或计划身份。
    #     decision: 本次对监管意见的明确回应选择。
    #     reason: 供后续核对的明确处置原因。
    #
    # Note:
    #     decision 使用 accept（接受）、partial（部分接受）或 reject（拒绝）。
    #     说明充分理由，促使主体在自我审视与复盘中持续成长。回应不成为新的客观事实证明。
    @model_input_boundary
    async def respond_supervision(self, id: str, decision: str, reason: str) -> str:
        """审阅独立反思提出的监管意见并记录回应。
        decision 使用 accept（接受）、partial（部分接受）或 reject（拒绝）。
        说明充分理由，促使主体在自我审视与复盘中持续成长。回应不成为新的客观事实证明。
        """
        self.check()
        result = await self.store.respond_supervision(id, decision, reason)
        return result.model_dump_json()
