"""核对真实工具正文的模型呈现资格，容量引用与待发结果不能代替阅读。

使用生产SDK纯编译与完整请求计量；只替代最终模型传输是否成功的通知。
活动完整入口另由实际Host/Core/Worker场景验证，本文件覆盖交付门槛的边界。
"""

import pytest
from purrcept_core.models import (
    Message,
    MessageRole,
    ModelRequest,
    ModelSettings,
    TextBlock,
    ToolCallBlock,
)
from purrcept_core.models.tools import ToolResult

from purrcept_engine.context.capacity import CapacityAssessment, GenerationCapacity, check_capacity
from purrcept_engine.execution.protocol import JsonObject
from purrcept_engine.storage.knowledge import EvidencePage, EvidenceSnapshot
from purrcept_engine.storage.model_requests import encode
from purrcept_engine.subject.delivery import (
    ToolDelivery,
    ToolDeliveryGate,
    presented_evidence_calls,
)


@pytest.mark.parametrize("large", [False, True])
def test_only_successfully_sent_complete_receipt_grants_next_action_reading(
    monkeypatch: pytest.MonkeyPatch, large: bool
) -> None:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    gate = ToolDeliveryGate()
    call = ToolCallBlock("model-tool", "play_action", arguments={"action_code": "pass"})
    result = gate.stage(
        call,
        ToolDelivery(
            ToolResult(content=(TextBlock("核对材料" + ("x" * 20_000 if large else "")),)),
            {"delivery": "reference_only", "stored_ref": {"id": "saved", "version": 1}},
            failure_is_error=False,
            presented_calls=frozenset({"actual-call"}),
        ),
    )
    request = ModelRequest(
        (
            Message(MessageRole.ASSISTANT, (call,)),
            Message(MessageRole.TOOL, (result.to_block(call.id),), name=call.name),
        ),
        settings=ModelSettings(max_output_tokens=100),
    )

    def capacity(payload: JsonObject) -> CapacityAssessment:
        return check_capacity(payload, GenerationCapacity(10_000, 100, "reference-text-only-v1"))

    assert gate.presented_call_ids == frozenset()
    prepared = gate.prepare(request, model="openai/test", capacity=capacity)
    # 请求材料已选定但还未取得成功响应，同响应另一工具不能取得读取资格。
    assert gate.presented_call_ids == frozenset()
    gate.sent(prepared)
    expected: frozenset[str] = frozenset() if large else frozenset({"actual-call"})
    assert gate.presented_call_ids == expected
    gate.sent(gate.prepare(prepared, model="openai/test", capacity=capacity))
    assert gate.presented_call_ids == expected


@pytest.mark.parametrize("kind", ["action_result", "call_receipt"])
def test_full_saved_receipt_can_be_expanded_but_partial_or_summary_cannot(kind: str) -> None:
    if kind == "action_result":
        source = {"source": "actual_action_result", "action_id": "actual-action"}
        payload = {
            "action_id": "actual-action",
            "calls": [{"call_id": "actual-call", "state": "confirmed", "result_json": "{}"}],
        }
    else:
        source = {"call_id": "actual-call", "action_id": "actual-action", "receipt_revision": 1}
        payload = {
            "source": "actual_call_receipt",
            "call": {"call_id": "actual-call", "state": "confirmed", "result_json": "{}"},
        }
    snapshot = EvidenceSnapshot(
        evidence_id="saved",
        version=1,
        kind=kind,
        text=encode(payload),
        source_json=encode(source),
        occurred_at=1,
    )
    page = EvidencePage(snapshot=snapshot, slice=None, text=snapshot.text, level="evidence")
    assert presented_evidence_calls(page) == frozenset({"actual-call"})
    partial = page.model_copy(update={"text": snapshot.text[:8]})
    assert presented_evidence_calls(partial) == frozenset()
    assert presented_evidence_calls(page.model_copy(update={"level": "summary"})) == frozenset()

    forged = snapshot.model_copy(update={"source_json": encode({"call_id": "other"})})
    with pytest.raises(RuntimeError):
        presented_evidence_calls(page.model_copy(update={"snapshot": forged}))


def test_unknown_receipt_identity_does_not_grant_later_observation_reading() -> None:
    snapshot = EvidenceSnapshot(
        evidence_id="saved",
        version=1,
        kind="call_receipt",
        text=encode(
            {"source": "actual_call_receipt", "call": {"call_id": "in-flight", "state": "unknown"}}
        ),
        source_json=encode({"call_id": "in-flight"}),
        occurred_at=1,
    )
    page = EvidencePage(snapshot=snapshot, slice=None, text=snapshot.text, level="evidence")
    assert presented_evidence_calls(page) == frozenset()
