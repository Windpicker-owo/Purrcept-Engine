"""核验完整 SDK payload 的纯文本参考容量及明确拒绝缺失媒体依据。"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from purrcept_engine.domain.context.capacity import (
    CapacityDeclaration,
    CapacityError,
    CapacityEstimate,
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


def test_complete_utf8_payload_and_output_reserve_define_reference_budget() -> None:
    """一字节一参考单位覆盖整个 JSON，扣输出后的 10% 余量有明确分母。"""
    request = payload()
    declaration = GenerationCapacity(1000, 100, "reference-text-only-v1")
    result = check_capacity(request, declaration)
    assert result.estimate.input_units == len(encode(request).encode("utf-8"))
    assert result.input_budget == 810
    assert result.estimate.unit == "utf8_byte_reference"
    assert result.estimate.method == "reference-text-only-v1"
    assert result.ratio == result.estimate.input_units / 810
    extended = dict(request, tools=[{"type": "function", "description": "x" * 500}])
    assert assess_capacity(extended, declaration).estimate.input_units > result.estimate.input_units


def test_independent_input_limit_and_decision_protocol_have_no_hidden_output() -> None:
    """生成独立输入上限取较严格值，专用判断只使用自己的输入声明。"""
    result = assess_capacity(
        payload(), GenerationCapacity(1000, 100, "reference-text-only-v1", input_limit=700)
    )
    assert result.input_budget == 700
    decision = {
        "model": "typesafe/explicit",
        "input": "证据",
        "questions": [{"name": "attend", "type": "predicate", "instructions": "criteria"}],
    }
    result = check_capacity(decision, DecisionCapacity(1000, "reference-text-only-v1"))
    assert result.input_budget == 900
    assert result.estimate.input_units == len(encode(decision).encode("utf-8"))


def test_smaller_actual_output_uses_its_request_window_reserve() -> None:
    """借用模型的注意请求只预留实际额度，配置仍限制最大允许输出。"""
    request = payload()
    declaration = GenerationCapacity(1000, 500, "reference-text-only-v1")
    assert assess_capacity(request, declaration).input_budget == 810
    request["max_completion_tokens"] = 200
    assert assess_capacity(request, declaration).input_budget == 720


def test_overflow_is_explicit_and_original_messages_are_not_trimmed() -> None:
    """超额结论能供维护观察，真正派发明确拒绝且不改原材料。"""
    request = payload()
    original = encode(request)
    declaration = GenerationCapacity(300, 100, "reference-text-only-v1")
    assessed = assess_capacity(request, declaration)
    assert not assessed.within_budget and assessed.maintenance_due
    with pytest.raises(CapacityError) as caught:
        check_capacity(request, declaration)
    assert caught.value.code == "over_capacity" and caught.value.assessment == assessed
    assert encode(request) == original


@pytest.mark.parametrize(
    "part",
    [
        {"type": "image_url", "image_url": {"url": "https://never-fetch.invalid/image"}},
        {"type": "input_audio", "input_audio": {"data": "large-base64", "format": "wav"}},
        {"type": "future-media", "data": "unknown"},
    ],
)
def test_reference_rule_never_treats_media_as_base64_text_or_zero(part: dict[str, object]) -> None:
    """未知媒体或缺规则/元数据不套统一常数，也不按 URL 文本长度放行。"""
    request = payload()
    request["messages"] = [{"role": "user", "content": [part]}]
    with pytest.raises(CapacityError) as caught:
        check_capacity(request, GenerationCapacity(100000, 100, "reference-text-only-v1"))
    assert caught.value.code == "unsupported_media" and caught.value.missing


def test_unknown_rule_is_not_inferred_from_model_name() -> None:
    """没有登记的计量标识不会选通用 tokenizer 或默认窗口。"""
    with pytest.raises(CapacityError) as caught:
        check_capacity(payload(), GenerationCapacity(1000, 100, "unknown-model-rule"))
    assert caught.value.code == "unsupported_rule" and caught.value.missing == (
        "unknown-model-rule",
    )


def test_schema_and_literal_media_words_are_regular_text() -> None:
    """只检查内容槽位，不因正文/schema出现媒体字段名误判。"""
    request = payload()
    request["messages"] = [{"role": "user", "content": "image_url data:audio/wav;base64,AAAA"}]
    request["tools"] = [
        {"type": "function", "parameters": {"properties": {"image_url": {"type": "string"}}}}
    ]
    assert check_capacity(
        request, GenerationCapacity(1000, 100, "reference-text-only-v1")
    ).within_budget


def test_nonfinite_or_nonjson_payload_does_not_get_a_measurement() -> None:
    """真实 JSON 边界不能以丢字段或空值回退掩盖无法重建材料。"""
    request = payload()
    request["unknown"] = object()
    with pytest.raises(CapacityError) as caught:
        check_capacity(request, GenerationCapacity(1000, 100, "reference-text-only-v1"))
    assert caught.value.code == "invalid_payload"
    request["unknown"] = float("inf")
    with pytest.raises(CapacityError, match="有限 JSON"):
        check_capacity(request, GenerationCapacity(1000, 100, "reference-text-only-v1"))


@pytest.mark.parametrize("output", [None, 0, True, 101])
def test_actual_generation_output_limit_must_match_explicit_declaration(output: object) -> None:
    """不能只扣配置里的输出额度却让Provider使用更大的隐藏默认上限。"""
    request = payload()
    request["max_tokens"] = output
    with pytest.raises(CapacityError) as caught:
        check_capacity(request, GenerationCapacity(1000, 100, "reference-text-only-v1"))
    assert caught.value.code == "output_limit"


def test_explicit_registered_rule_is_called_once_and_has_observable_basis() -> None:
    """规则通过明确登记选择，示例只证明端口，不宣称任何Provider公式。"""
    calls: list[CapacityDeclaration] = []

    def local_rule(
        payload: Mapping[str, object], declaration: CapacityDeclaration
    ) -> CapacityEstimate:
        assert payload["model"] == "explicit/model"
        calls.append(declaration)
        return CapacityEstimate(
            len(encode(payload).encode()), "test-local-v1", "synthetic_text", "utf8_reference"
        )

    declaration = GenerationCapacity(1000, 100, "test-local-rule")
    result = check_capacity(payload(), declaration, rules={"test-local-rule": local_rule})
    assert calls == [declaration] and result.estimate.method == "test-local-v1"


def test_registered_rule_internal_failure_keeps_exception_without_fallback() -> None:
    """计量规则的内部异常不转成零量、通用分词或另一条规则。"""
    failure = RuntimeError("synthetic rule failure")

    def broken_rule(
        payload: Mapping[str, object], declaration: CapacityDeclaration
    ) -> CapacityEstimate:
        raise failure

    with pytest.raises(RuntimeError) as caught:
        check_capacity(
            payload(), GenerationCapacity(1000, 100, "broken"), rules={"broken": broken_rule}
        )
    assert caught.value is failure
