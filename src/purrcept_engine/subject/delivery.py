"""在首次派发前选择 Engine 工具原文的实际交付，不拥有 Core 工具循环。

工具先取得并保存真实结果，再登记完整结果与有限容量回执。Core 按原顺序
执行全部工具后，请求门槛用真实 SDK 编译完整下一请求；只有尚未派发的
登记结果可以整体改为容量回执。正文不缩短、媒体不变为描述，权威效果不变。
已采用的交付按消息位置和完整值冻结，随后请求与最终检查点使用同一投影。
宿主结构化尾部还按权威原文引用、确切回执版本和完整工具值省去重复结果
正文；容量引用不被尾部重新展开，迟到新版本与完整实际参数仍明确保留。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal

from purrcept_core.models import (
    Message,
    MessageRole,
    ModelRequest,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from purrcept_core.models.tools import ToolResult

from purrcept_engine.context.capacity import CapacityAssessment, CapacityError
from purrcept_engine.execution.protocol import JsonObject, JsonValue, decode_object
from purrcept_engine.storage.knowledge import EvidencePage
from purrcept_engine.storage.model_requests import encode

from .model_requests import compile_materials

type CapacityCheck = Callable[[JsonObject], CapacityAssessment]
type DeliveryMode = Literal["full_tool_message", "reference_only"]

# 这是 subject_work 已声明的 CallRecord 表示，不是从任意 Provider JSON
# 猜测关键字段。原文引用另由 Store 核验完整 CallRecord（包括历史和媒体）。
_CALL_CONTEXT_FIELDS = (
    "call_id",
    "path",
    "target_id",
    "arguments_json",
    "state",
    "receipt_revision",
    "started_at",
    "finished_at",
    "result_json",
    "error_type",
    "error",
)


@dataclass(frozen=True, slots=True)
class ToolDelivery:
    """工具创建的完整候选及有限回执，不包含推测的后续工具结果。

    fallback 只保存明确的来源引用、真实关键效果和重试指引；不可放入正文、
    媒体描述或任意调试输出。failure_is_error 区分阅读未交付与已发生行动。
    presented_calls 由工具的权威完整确认正文提供，Gate只在成功的实际模型
    请求保留该正文后授予；它不进入模型参数，也不授权容量引用或未知结果。
    """

    result: ToolResult
    fallback: dict[str, object]
    failure_is_error: bool = True
    presented_calls: frozenset[str] = field(default=frozenset(), kw_only=True)


@dataclass(frozen=True, slots=True)
class _PendingDelivery:
    call: ToolCallBlock
    original: Message
    proposal: ToolDelivery


@dataclass(frozen=True, slots=True)
class _FrozenDelivery:
    position: int
    original: Message
    delivered: Message


def presented_evidence_calls(page: EvidencePage) -> frozenset[str]:
    """只给完整真实回执正文登记调用身份，派生认识与摘要不能授予此资格。

    原文由权威账本生成，来源与正文身份必须一致。分页片段不证明完整核对
    材料已呈现；容量引用也没有此登记。这里不从模型文字或ID前缀猜来源。
    """
    snapshot = page.snapshot
    if snapshot is None or page.level != "evidence" or page.text != snapshot.text:
        return frozenset()
    source = decode_object(snapshot.source_json.encode("utf-8"))
    if snapshot.kind == "action_result":
        if source.get("source") != "actual_action_result":
            raise RuntimeError("行动结果缺少真实内部来源。")
        payload = decode_object(snapshot.text.encode("utf-8"))
        if payload.get("action_id") != source.get("action_id"):
            raise RuntimeError("行动结果正文与来源身份不一致。")
        calls = payload.get("calls")
        if not isinstance(calls, list):
            raise RuntimeError("行动结果没有完整调用记录。")
    elif snapshot.kind == "call_receipt":
        payload = decode_object(snapshot.text.encode("utf-8"))
        call = payload.get("call")
        if payload.get("source") != "actual_call_receipt" or not isinstance(call, dict):
            raise RuntimeError("调用原文缺少真实内部来源。")
        if call.get("call_id") != source.get("call_id"):
            raise RuntimeError("调用原文与来源身份不一致。")
        calls = [call]
    else:
        return frozenset()
    identities: set[str] = set()
    for call in calls:
        if not isinstance(call, dict):
            raise RuntimeError("真实回执缺少明确调用身份。")
        identity = call.get("call_id")
        if not isinstance(identity, str):
            raise RuntimeError("真实回执调用身份无效。")
        # UNKNOWN只有在途身份，不能让之后迟到的核对结果借旧空回执获得已读资格。
        if call.get("state") == "confirmed" and call.get("result_json") is not None:
            identities.add(identity)
    return frozenset(identities)


def evidence_fallback(page: EvidencePage, *, cursor: str | None) -> dict[str, object]:
    """返回已保存材料的确切引用；重试定位原页起点，不跳过未交付内容。"""
    snapshot, memory = page.snapshot, page.memory
    if snapshot is not None:
        reference = {"id": snapshot.evidence_id, "version": snapshot.version}
        derived = False
    elif memory is not None:
        reference = {"id": memory.patch.id, "version": memory.version}
        derived = True
    else:
        raise RuntimeError("原文页面缺少权威快照或认识版本。")
    return {
        "delivery": "not_delivered",
        "stored_ref": reference,
        "derived": derived,
        "level": page.level,
        "retry": {
            "tool": "read_evidence",
            "id": reference["id"],
            "version": reference["version"],
            "cursor": cursor,
            "level": "evidence",
            "instruction": "使用更小的 max_chars 重读原页；正文与媒体均未获得阅读覆盖。",
        },
    }


def _capacity_details(error: CapacityError) -> dict[str, object]:
    """只公开真实计量依据及缺口，不复制触发错误的完整请求。"""
    result: dict[str, object] = {
        "code": error.code,
        "reason": str(error),
        "scope": "complete_next_request",
    }
    if error.missing:
        result["missing"] = error.missing
    if error.assessment is not None:
        assessment = error.assessment
        result.update(
            input_units=assessment.estimate.input_units,
            input_budget=assessment.input_budget,
            method=assessment.estimate.method,
            unit=assessment.estimate.unit,
        )
    return result


def _correctable(error: CapacityError) -> bool:
    # 只有材料超额或缺少媒体计量依据可以撤回尚未交付的原文。配置、SDK
    # 形状、规则实现及其它内部故障不能被包装为让模型反复尝试的输入问题。
    return error.code in ("over_capacity", "unsupported_media")


def _full_action_matches(payload: JsonObject, current: JsonObject) -> bool:
    """只匹配真实 ActionRecord 的当前同版本调用，不以身份字符串代替内容。"""
    if payload.get("type") != "ActionRecord" or (
        payload.get("action_id") != current.get("action_id")
        or payload.get("state") != current.get("action_state")
        or payload.get("input_id") != current.get("input_id")
    ):
        return False
    calls = payload.get("calls")
    if not isinstance(calls, list):
        raise RuntimeError("Engine 完整行动工具结果缺少调用列表。")
    for original in calls:
        if not isinstance(original, dict) or original.get("type") != "CallRecord":
            raise RuntimeError("Engine 完整行动调用结果形状无效。")
        if all(
            key in original and key in current and original[key] == current[key]
            for key in _CALL_CONTEXT_FIELDS
        ):
            return True
    return False


def _full_call_receipt_matches(payload: JsonObject, source: JsonValue, current: JsonObject) -> bool:
    """核对真实反馈原文的来源、因果输入和同版本调用，不靠 call 前缀猜关系。"""
    if source != {
        "call_id": current.get("call_id"),
        "action_id": current.get("action_id"),
        "receipt_revision": current.get("receipt_revision"),
    } or (
        payload.get("source") != "actual_call_receipt"
        or payload.get("causal_input_id") != current.get("input_id")
    ):
        return False
    call = payload.get("call")
    if not isinstance(call, dict):
        raise RuntimeError("Engine 完整调用回执原文缺少调用事实。")
    return call.get("action_id") == current.get("action_id") and all(
        key in call and key in current and call[key] == current[key] for key in _CALL_CONTEXT_FIELDS
    )


def _tool_payloads(messages: tuple[Message, ...]) -> tuple[tuple[str, JsonObject], ...]:
    """每个候选请求只解析一次完整工具 JSON，避免按当前调用重复解析长正文。"""
    payloads: list[tuple[str, JsonObject]] = []
    for message in messages:
        if message.role is not MessageRole.TOOL or message.name not in (
            "play_action",
            "read_evidence",
        ):
            continue
        for block in message.content:
            if not isinstance(block, ToolResultBlock) or block.is_error or not block.content:
                continue
            text = block.content[0]
            if isinstance(text, TextBlock):
                payloads.append((message.name, decode_object(text.text.encode("utf-8"))))
    return tuple(payloads)


def _matching_delivery(
    deliveries: tuple[tuple[str, JsonObject], ...], current: JsonObject
) -> DeliveryMode | None:
    """在实际工具消息中定位同一权威回执，旧版本不隐藏迟到新事实。

    Store 提供的 original_ref 先证明原文实际存在且当前调用完整匹配。完整
    play_action 再逐值匹配；容量回执匹配确切已保存引用、行动及调用版本。
    read_evidence 只有交付整份 action_result 或可靠 call_receipt 原文时才可
    复用，片段不冒充全文；反馈原文还须核对来源和实际因果输入。
    普通用户文字、工具描述和 Provider 自报 ID 都不参与此授权。
    """
    reference = current.get("original_ref")
    if reference is None:
        return None
    if not isinstance(reference, dict):
        raise RuntimeError("宿主当前调用的原文引用不是明确对象。")
    for name, payload in deliveries:
        if name == "play_action":
            if _full_action_matches(payload, current):
                return "full_tool_message"
            if (
                payload.get("delivery") != "reference_only"
                or payload.get("original_content") != "not_delivered"
                or payload.get("stored_ref") != reference
                or payload.get("action_id") != current.get("action_id")
                or payload.get("state") != current.get("action_state")
            ):
                continue
            calls = payload.get("calls")
            if not isinstance(calls, list):
                raise RuntimeError("Engine 容量回执缺少真实调用状态。")
            for call in calls:
                if isinstance(call, dict) and all(
                    key in call and key in current and call[key] == current[key]
                    for key in ("call_id", "receipt_revision", "state")
                ):
                    return "reference_only"
        else:
            item, span = payload.get("item"), payload.get("range")
            if (
                payload.get("evidence_ref") != reference
                or payload.get("level") != "evidence"
                or payload.get("next_cursor") is not None
                or not isinstance(item, dict)
                or item.get("kind") not in ("action_result", "call_receipt")
                or not isinstance(span, dict)
                or span.get("unit") != "characters"
                or span.get("start") != 0
            ):
                continue
            body = item.get("text")
            if not isinstance(body, str):
                raise RuntimeError("完整行动原文缺少正文。")
            original = decode_object(body.encode("utf-8"))
            if (
                _full_action_matches(original, current)
                if item["kind"] == "action_result"
                else _full_call_receipt_matches(original, item.get("source"), current)
            ):
                return "full_tool_message"
    return None


class ToolDeliveryGate:
    """一次 Core 连续处理的交付所有者，不跨轮共享可变待交付状态。

    工具通过 Core 注入的调用身份 stage；PreparedGenerationBackend 在真实
    尾部刷新后 prepare，使用同一个完整 SDK 编译与容量端口。采用顺序就是
    请求中的工具消息顺序。先验证全部有限回执，再逐项尝试完整原文，避免
    第一项占满窗口使同响应其它真实回执失去纠正机会。回执仍放不下时明确
    终止，既不删历史也不重新执行已发生工具。
    """

    def __init__(self) -> None:
        """创建尚未派发的登记；会话结束时由本轮调用方释放。"""
        self._pending: list[_PendingDelivery] = []
        self._frozen: list[_FrozenDelivery] = []
        self._sent_prefix = 0
        self._awaiting_presentation: list[tuple[Message, frozenset[str]]] = []
        self._presented_call_ids: set[str] = set()

    @property
    def presented_call_ids(self) -> frozenset[str]:
        """返回本轮成功模型请求已完整接收的真实回执身份，不含引用或待发结果。"""
        return frozenset(self._presented_call_ids)

    def stage(self, call: ToolCallBlock, proposal: ToolDelivery) -> ToolResult:
        """登记工具真正生成的完整候选，登记本身不赋予原文呈现资格。"""
        if proposal.result.is_error:
            raise ValueError("输入错误不能登记为原文交付候选。")
        original = Message(MessageRole.TOOL, (proposal.result.to_block(call.id),), name=call.name)
        self._pending.append(_PendingDelivery(call, original, proposal))
        return proposal.result

    def project(self, messages: tuple[Message, ...]) -> tuple[Message, ...]:
        """还原已冻结的真实交付，用于后续请求及最终提交的检查点。"""
        projected = list(messages)
        for decision in self._frozen:
            position = decision.position
            if position >= len(projected) or projected[position] not in (
                decision.original,
                decision.delivered,
            ):
                raise RuntimeError("Core 工具历史与已冻结交付位置不一致。")
            # Provider 可以复用 call_id。位置与完整 Message 共同绑定本次结果，
            # 不能把另一次同名调用或较早容量回执重新扩展为正文。
            projected[position] = decision.delivered
        return tuple(projected)

    def sent(self, request: ModelRequest) -> None:
        """成功派发之后冻结已存在前缀，不从未来响应猜测新的结果位置。"""
        self._sent_prefix = len(request.messages)
        # Ordering: 后端成功且请求证据提交后才授予下一工具行动读取资格。
        # 同一响应的全部工具在 prepare/sent 前执行，不能消费尚未呈现的核对。
        for message, identities in self._awaiting_presentation:
            if message not in request.messages:
                raise RuntimeError("真实回执呈现证明不在实际模型请求中。")
            self._presented_call_ids.update(identities)
        self._awaiting_presentation.clear()

    def prepare(
        self,
        request: ModelRequest,
        *,
        model: str,
        capacity: CapacityCheck,
        host_state: JsonObject | None = None,
        reminder_key: str | None = None,
    ) -> ModelRequest:
        """计量完整实际请求，仅选择本轮尚未发出的 Engine 原文结果。"""
        current = replace(request, messages=self.project(request.messages))
        if not self._pending:
            return self._project_tail(current, host_state, reminder_key)

        positions: list[int] = []
        search_from = self._sent_prefix
        for pending in self._pending:
            for position in range(search_from, len(current.messages)):
                if current.messages[position] == pending.original:
                    positions.append(position)
                    search_from = position + 1
                    break
            else:
                raise RuntimeError("待交付工具结果不在 Core 的真实下一请求中。")

        try:
            self._check(
                self._project_tail(current, host_state, reminder_key),
                model=model,
                capacity=capacity,
            )
        except CapacityError as error:
            if not _correctable(error):
                raise
            reason = _capacity_details(error)
        else:
            self._remember_presentations(current, positions)
            self._pending.clear()
            return self._project_tail(current, host_state, reminder_key)

        # Ordering: 先为同响应全部工具保留已保存引用和真实效果，再尝试原文。
        # 这些是已经取得的完整回执，而非为未知未来结果猜测固定容量余量。
        selected = list(current.messages)
        for position, pending in zip(positions, self._pending, strict=True):
            result = ToolResult(
                content=(TextBlock(encode({**pending.proposal.fallback, "capacity": reason})),),
                is_error=pending.proposal.failure_is_error,
            )
            selected[position] = Message(
                MessageRole.TOOL,
                (result.to_block(pending.call.id),),
                name=pending.original.name,
            )
        current = replace(current, messages=tuple(selected))
        self._check(
            self._project_tail(current, host_state, reminder_key), model=model, capacity=capacity
        )

        for position, pending in zip(positions, self._pending, strict=True):
            candidate_messages = list(current.messages)
            candidate_messages[position] = pending.original
            candidate = replace(current, messages=tuple(candidate_messages))
            try:
                self._check(
                    self._project_tail(candidate, host_state, reminder_key),
                    model=model,
                    capacity=capacity,
                )
            except CapacityError as error:
                if not _correctable(error):
                    raise
            else:
                current = candidate

        for position, pending in zip(positions, self._pending, strict=True):
            delivered = current.messages[position]
            if delivered != pending.original:
                self._frozen.append(_FrozenDelivery(position, pending.original, delivered))
        self._remember_presentations(current, positions)
        self._pending.clear()
        return self._project_tail(current, host_state, reminder_key)

    def _remember_presentations(self, request: ModelRequest, positions: list[int]) -> None:
        """登记容量选择后的完整正文，失败派发不会使它成为已读事实。"""
        for position, pending in zip(positions, self._pending, strict=True):
            if request.messages[position] == pending.original and pending.proposal.presented_calls:
                self._awaiting_presentation.append(
                    (pending.original, pending.proposal.presented_calls)
                )

    @staticmethod
    def _project_tail(
        request: ModelRequest, host_state: JsonObject | None, reminder_key: str | None
    ) -> ModelRequest:
        """消除同回执在宿主尾部的正文副本，不隐藏新的权威回执版本。

        宿主显式传入结构化状态；普通提醒文字不参与此投影。Store 的
        original_ref 已核验确切当前 CallRecord 与持久原文一致，工具历史还
        必须完整值匹配或携带相同已冻结容量引用，才能移除该 result_json。
        参数、当前状态、错误、目标及时间原值保留；所有引用均不计为已读。
        """
        if host_state is None:
            return request
        if reminder_key is None:
            raise RuntimeError("结构化宿主尾部缺少明确提醒身份。")
        work = host_state.get("work")
        if not isinstance(work, dict) or not isinstance(work.get("calls"), list):
            raise RuntimeError("宿主结构化状态缺少明确当前调用列表。")
        calls = work["calls"]
        if not isinstance(calls, list):
            raise RuntimeError("当前调用列表形状不一致。")
        if not calls:
            return request
        selected: list[JsonValue] = []
        deliveries = _tool_payloads(request.messages)
        for raw in calls:
            if not isinstance(raw, dict):
                raise RuntimeError("宿主当前调用记录不是对象。")
            mode = _matching_delivery(deliveries, raw)
            if mode is None:
                selected.append(raw)
                continue
            projected = {key: value for key, value in raw.items() if key != "result_json"}
            projected["result_delivery"] = {
                "mode": mode,
                "original_ref": raw["original_ref"],
                "details_presented": mode == "full_tool_message",
                "instruction": "引用不表示已读；依赖未交付正文或平台详细字段时先 read_evidence。",
            }
            selected.append(projected)
        projected_state = {**host_state, "work": {**work, "calls": selected}}
        original_text = encode(host_state)
        projected_text = encode(projected_state)
        reminders = list(request.reminders)
        for index, reminder in enumerate(reminders):
            if reminder.key == reminder_key:
                if reminder.content != (TextBlock(original_text),):
                    raise RuntimeError("当前 Engine 提醒与结构化宿主快照不一致。")
                reminders[index] = replace(reminder, content=(TextBlock(projected_text),))
                break
        else:
            raise RuntimeError("实际请求缺少待投影的 Engine 提醒。")
        return replace(request, reminders=tuple(reminders))

    @staticmethod
    def _check(request: ModelRequest, *, model: str, capacity: CapacityCheck) -> CapacityAssessment:
        """调用与真实派发相同的 SDK 编译器，完整 schema、媒体及尾部均入账。"""
        materials, _ = compile_materials(request, model=model)
        payload = decode_object(materials.encode("utf-8"))["payload"]
        if not isinstance(payload, dict):
            raise TypeError("SDK 编译材料必须包含 payload 对象。")
        return capacity(payload)
