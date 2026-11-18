"""呈现宿主已冻结的工作状态与完整回执，不解释具体输入来源协议。

render_attention_context 只整理注意的有限工作背景，render_host_state 投影当前
未决工作，render_material 为材料保留字段树，并将音频传输编码投影为引用。输入提供方负责渲染内容，
按来源分组与有序媒体交付由 materials.input_blocks 完成，本模块不再解析平台帧。
执行时间线只派生已保存记录的可读 UTC 时间，不采样当前时钟或推测平台送达。
"""

import json
import re
from datetime import UTC, datetime
from typing import cast

from purrcept_engine.domain.context.audio_receipts import audio_text_view
from purrcept_engine.domain.context.templates import (
    ATTENTION_CONTEXT_PROMPT,
    FENCED_TEXT_PROMPT,
    MATERIAL_PROMPT,
)
from purrcept_engine.domain.execution.models import ActionRecord
from purrcept_engine.serialization import render_field_tree, render_yaml
from purrcept_engine.values import JsonObject, JsonValue


def execution_times(record: JsonObject) -> JsonObject:
    """将执行记录的宿主时钟字段投影为可直接比较的 UTC 日期时间。

    Args:
        record: 行动或调用的明确结构化投影，仅读取已知执行时间字段。

    Returns:
        原字段名到 ISO 8601 时间的映射；未完成的 null 原样保留。

    Raises:
        TypeError: 已提供的执行时间不是 Unix 秒数或 null。
        ValueError: 时间戳超出可表达的日期范围。

    Note:
        只派生展示字段，不修改权威原文。started_at 是宿主派发记录，finished_at
        是宿主结果记录时间，均不证明平台用户在该时刻看到消息或外部效果精确发生。
    """
    times: JsonObject = {}
    for key in ("created_at", "prepared_at", "started_at", "finished_at"):
        if key not in record:
            continue
        value = record[key]
        if value is None:
            times[key] = None
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            times[key] = datetime.fromtimestamp(value, UTC).isoformat()
        else:
            raise TypeError("执行记录时间必须是 Unix 秒数或 null。")
    return times


def _utc_iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def action_receipt(action: ActionRecord) -> JsonObject:
    """生成面向模型的精简行动回执，去掉源码回显、重复历史与转义 JSON。"""
    calls: list[JsonValue] = []
    for call in action.calls:
        entry: JsonObject = {
            "call_id": call.call_id,
            "path": call.path,
            "state": call.state,
            "receipt_revision": call.receipt_revision,
            "started_at": _utc_iso(call.started_at),
            "finished_at": _utc_iso(call.finished_at),
        }
        if call.result_json is not None:
            entry["result"] = cast(JsonValue, json.loads(audio_text_view(call.result_json)))
        if call.error_type is not None:
            entry["error_type"] = call.error_type
        if call.error is not None:
            entry["error"] = call.error
        calls.append(entry)
    receipt: JsonObject = {
        "type": "ActionReceipt",
        "action_id": action.action_id,
        "state": action.state,
        "finished_at": _utc_iso(action.finished_at),
        "evidence_ref": f"action_result:{action.action_id}",
        "calls": calls,
    }
    if action.output:
        receipt["output"] = action.output
    if action.result_json is not None:
        action_result = cast(JsonValue, json.loads(audio_text_view(action.result_json)))
        if action_result is not None:
            receipt["result"] = action_result
    if action.error is not None:
        receipt["error"] = action.error
    return receipt


def action_timeline(action: ActionRecord) -> JsonObject:
    """为容量引用回执提供可读行动时间线。"""
    return {
        "action_id": action.action_id,
        **execution_times({"created_at": action.created_at, "finished_at": action.finished_at}),
        "calls": [
            {
                "call_id": call.call_id,
                "receipt_revision": call.receipt_revision,
                **execution_times(
                    {
                        "prepared_at": call.prepared_at,
                        "started_at": call.started_at,
                        "finished_at": call.finished_at,
                    }
                ),
            }
            for call in action.calls
        ],
    }


def _fenced_text(text: str) -> str:
    """完整保留提供方版式；围栏长于正文内所有反引号，外部文字不能提前闭合。"""
    longest = max((len(match.group()) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return FENCED_TEXT_PROMPT.render({"fence": fence, "text": text})


def render_material(title: str, payload: object) -> str:
    """按公开编码展开对象，音频传输编码以引用交付。

    Args:
        title: 宿主提供的固定节标题，不从外界正文提取指令。
        payload: 已按公开材料编码协议冻结的完整对象。

    Returns:
        固定标题与字段树，音频编码替换为含摘要及大小的引用；新输入使用 materials.input_blocks。

    Raises:
        TypeError: 对象不支持公开编码，或包含非字符串映射键。
        ValueError: 材料包含标准 JSON 无法表达的非有限数值。
        ActionProtocolError: 编码结果不是合法的严格 JSON 对象。

    Note:
        固定标题不从外部正文取得。

        数据类沿用权威编码的类型标签与字段顺序，不遍历任意实例属性。显示层不
        改写存储格式；音频引用只省略传输字节，不冒充原生音频已被感知。
    """
    return audio_text_view(
        MATERIAL_PROMPT.render({"title": title, "fields": render_field_tree(payload)})
    )


def render_attention_context(background: JsonObject) -> str:
    """把注意判断的有限工作背景呈现为明确的状态，避免重复包装和空字段树。

    Args:
        background: 宿主已冻结的前台身份、有限计划与活动；额外工作材料单独完整呈现。

    Returns:
        带明确覆盖范围的文本；空闲、无计划和无活动分别表述，不把未展示当作不存在。

    Raises:
        TypeError: 已声明的前台状态、计划或活动不符合对象/数组契约。

    Note:
        不读取主脑历史，也不重新查询存储。计划及活动非空时保留全部已交付字段，
        外部值按 YAML 字符串引用；未知扩展不按字段名猜业务或静默丢弃。
    """
    current = background.get("current_turn")
    plans = background.get("plans", [])
    activities = background.get("activities", [])
    if current is not None and not isinstance(current, dict):
        raise TypeError("注意背景的当前前台必须是对象或 null。")
    if not isinstance(plans, list) or not isinstance(activities, list):
        raise TypeError("注意背景的计划与活动必须是数组。")
    sections: JsonObject = {}
    if "coverage" in background:
        sections["coverage"] = background["coverage"]
    if "current_turn" in background:
        sections["foreground"] = current if current is not None else "idle"
    if "plans" in background:
        sections["plans"] = plans
    if "activities" in background:
        sections["activities"] = activities
    extensions: JsonObject = {
        key: value
        for key, value in background.items()
        if key not in ("coverage", "current_turn", "plans", "activities")
    }
    if extensions:
        sections["extra_work"] = extensions
    return ATTENTION_CONTEXT_PROMPT.render({"body": _fenced_text(render_yaml(sections))})


def render_host_state(payload: JsonObject) -> str:
    """投影判断与行动状态，内部版本仍留在权威快照供执行和回执核验。

    Args:
        payload: 宿主明确提供的判断、行动、活动与设备动态状态。

    Raises:
        TypeError: 宿主工作材料必须是对象；宿主预算材料必须是对象。

    Note:
        不遍历删除所有叫 version/id 的字段：证据版本、活动身份和回执版本是
        行动契约的一部分。calls 采用交付 Gate 的原文引用投影，额外派生可读时间。
        插件材料由各自 prepare_request 明确选择；诊断 snapshot 不获默认注入资格。
    """
    work = payload["work"]
    if not isinstance(work, dict):
        raise TypeError("宿主工作材料必须是对象。")
    current: JsonObject = {
        key: work[key] for key in ("plans", "supervision", "activities", "calls") if key in work
    }
    turn = work.get("current_turn")
    if isinstance(turn, dict):
        current["current_inputs"] = turn["input_ids"]
    activities = work.get("activities")
    if isinstance(activities, list):
        # 运行代次只供迟到结果隔离；活动版本用于精确关联监管证据，仍须保留。
        current["activities"] = [
            {
                key: activity[key]
                for key in (
                    "activity_id",
                    "version",
                    "goal",
                    "state",
                    "remaining_seconds",
                    "reconciliation_required",
                )
                if key in activity
            }
            if isinstance(activity, dict)
            else activity
            for activity in activities
        ]
    assembled_at = work.get("assembled_at")
    if isinstance(assembled_at, (int, float)):
        current["observed_at"] = datetime.fromtimestamp(assembled_at, UTC).isoformat()
    calls = work.get("calls")
    if isinstance(calls, list):
        # 动态回执改为原文引用后仍保留时间，迟到回执也带其自身版本的记录时间。
        current["calls"] = [
            {**call, "recorded_times_utc": execution_times(call)}
            if isinstance(call, dict)
            else call
            for call in calls
        ]
    budget = payload.get("budget", {})
    if not isinstance(budget, dict):
        raise TypeError("宿主预算材料必须是对象。")
    return render_material(
        "宿主当前状态（全部未决工作；设备状态是观察事实，不是行动许可）",
        {
            "request_time": payload.get("request_time", {}),
            "work": current,
            "capabilities": payload.get("request_materials", {}),
            "budget": {
                key: budget[key]
                for key in (
                    "used_requests",
                    "max_requests",
                    "remaining_seconds_at_assembly",
                )
                if key in budget
            },
        },
    )
