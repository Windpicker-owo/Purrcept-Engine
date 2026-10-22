"""核验完整请求的统一多模态 token 估算、预算与派发边界。"""

from __future__ import annotations

import pytest

from purrcept_engine.domain.context.capacity import (
    CapacityError,
    DecisionCapacity,
    GenerationCapacity,
    assess_capacity,
    check_capacity,
)
from purrcept_engine.serialization import encode


def payload() -> dict[str, object]:
    """固定目录、工具 schema、工具过程和动态尾部都参与同一次完整计量。"""
    return {
        "model": "explicit/model",
        "max_tokens": 100,
        "messages": [
            {"role": "system", "content": "catalog"},
            {"role": "user", "content": "original"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call", "function": {"name": "play_action", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "call", "content": "receipt"},
            {"role": "user", "content": "current state"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {"name": "play_action", "parameters": {"type": "object"}},
            }
        ],
    }


def test_complete_text_and_output_reserve_define_reference_budget() -> None:
    """完整文字和结构开销参与粗略估算，扣输出后的 10% 余量有明确分母。"""
    request = payload()
    declaration = GenerationCapacity(1000, 100)
    result = check_capacity(request, declaration)
    assert 0 < result.estimate.input_units < len(encode(request).encode("utf-8"))
    assert result.input_budget == 810
    assert result.estimate.unit == "token_estimate"
    assert result.estimate.method == "multimodal-heuristic-v1"
    assert result.ratio == result.estimate.input_units / 810
    extended = dict(request, tools=[{"type": "function", "description": "x" * 500}])
    assert assess_capacity(extended, declaration).estimate.input_units > result.estimate.input_units


def test_independent_input_limit_and_decision_protocol_have_no_hidden_output() -> None:
    """生成独立输入上限取较严格值，专用判断只使用自己的输入声明。"""
    result = assess_capacity(payload(), GenerationCapacity(1000, 100, input_limit=700))
    assert result.input_budget == 700
    decision = {
        "model": "typesafe/explicit",
        "input": "证据",
        "questions": [{"name": "attend", "type": "predicate", "instructions": "criteria"}],
    }
    result = check_capacity(decision, DecisionCapacity(1000))
    assert result.input_budget == 900
    assert 0 < result.estimate.input_units < len(encode(decision).encode("utf-8"))


def test_smaller_actual_output_uses_its_request_window_reserve() -> None:
    """借用模型的注意请求只预留实际额度，配置仍限制最大允许输出。"""
    request = payload()
    declaration = GenerationCapacity(1000, 500)
    assert assess_capacity(request, declaration).input_budget == 810
    request["max_completion_tokens"] = 200
    assert assess_capacity(request, declaration).input_budget == 720


def test_overflow_is_explicit_and_original_messages_are_not_trimmed() -> None:
    """超额结论能供维护观察，真正派发明确拒绝且不改原材料。"""
    request = payload()
    original = encode(request)
    declaration = GenerationCapacity(150, 100)
    assessed = assess_capacity(request, declaration)
    assert not assessed.within_budget and assessed.maintenance_due
    with pytest.raises(CapacityError) as caught:
        check_capacity(request, declaration)
    assert caught.value.code == "over_capacity" and caught.value.assessment == assessed
    assert encode(request) == original


def test_remote_image_has_a_fixed_planning_reserve_without_fetching() -> None:
    """没有本地尺寸的 URL 不按短引用文字当作几 token，而使用明确的媒体额度。"""
    request = payload()
    request["messages"] = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "https://never-fetch.invalid/image"}}
            ],
        }
    ]
    result = check_capacity(request, GenerationCapacity(100000, 100))
    assert result.estimate.input_units >= 2048
    assert result.estimate.scope == "complete_sdk_request"


def test_schema_and_literal_media_words_are_regular_text() -> None:
    """只检查内容槽位，不因正文/schema出现媒体字段名误判。"""
    request = payload()
    request["messages"] = [{"role": "user", "content": "image_url data:audio/wav;base64,AAAA"}]
    request["tools"] = [
        {"type": "function", "parameters": {"properties": {"image_url": {"type": "string"}}}}
    ]
    assert check_capacity(request, GenerationCapacity(1000, 100)).within_budget


def test_nonfinite_or_nonjson_payload_does_not_get_a_measurement() -> None:
    """真实 JSON 边界不能以丢字段或空值回退掩盖无法重建材料。"""
    request = payload()
    request["unknown"] = object()
    with pytest.raises(CapacityError) as caught:
        check_capacity(request, GenerationCapacity(1000, 100))
    assert caught.value.code == "invalid_payload"
    request["unknown"] = float("inf")
    with pytest.raises(CapacityError, match="有限 JSON"):
        check_capacity(request, GenerationCapacity(1000, 100))


@pytest.mark.parametrize("output", [None, 0, True, 101])
def test_actual_generation_output_limit_must_match_explicit_declaration(output: object) -> None:
    """不能只扣配置里的输出额度却让Provider使用更大的隐藏默认上限。"""
    request = payload()
    request["max_tokens"] = output
    with pytest.raises(CapacityError) as caught:
        check_capacity(request, GenerationCapacity(1000, 100))
    assert caught.value.code == "output_limit"


@pytest.mark.parametrize(
    "ratio,maintenance_due,target_met", [(0.5, False, True), (0.8, True, False), (1.0, True, False)]
)
def test_thresholds_share_the_input_budget_denominator(
    ratio: float, maintenance_due: bool, target_met: bool
) -> None:
    """触发、目标和派发上限都比较同一输入预算，并包含等号边界。"""
    request = payload()
    size = assess_capacity(request, GenerationCapacity(10000, 100)).estimate.input_units
    budget = int(size / ratio)
    result = check_capacity(request, GenerationCapacity(budget + 100, 100), reserve_fraction=0)
    assert result.maintenance_due is maintenance_due
    assert result.target_met is target_met
    assert result.within_budget
