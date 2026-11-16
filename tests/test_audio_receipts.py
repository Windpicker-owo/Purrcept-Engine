"""合成音频验证模型回执、旧历史和原件分页；不调用模型或声音设备。"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from purrcept_core.models import (
    ConversationCheckpoint,
    ConversationState,
    Message,
    MessageRole,
    ReminderState,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from test_audio_files import audio

from purrcept_engine.domain.context.audio_receipts import audio_text_view, evidence_text_view
from purrcept_engine.domain.context.media_history import expire_history_media
from purrcept_engine.domain.evidence.models import EvidencePage, EvidenceSlice, EvidenceSnapshot
from purrcept_engine.infrastructure.audio.wav import encode_wav


@pytest.mark.parametrize("prefix", ["", "base64://", "data:audio/wav;base64,"])
@pytest.mark.parametrize("format", ["wav", "mp3"])
def test_receipt_audio_is_referenced_without_changing_plain_text_or_json_shape(
    prefix: str,
    format: str,
) -> None:
    data = audio(format)
    encoded = base64.b64encode(data).decode()
    original = json.dumps({"voice": prefix + encoded, "text": "明确发送成功", "code": "a" * 1000})
    projected = audio_text_view(original)
    value = json.loads(projected)
    assert encoded not in projected and hashlib.sha256(data).hexdigest() in projected
    assert value["text"] == "明确发送成功" and value["code"] == "a" * 1000
    assert "音频编码未交付" in value["voice"]
    assert encoded in original and audio_text_view(projected) == projected


@pytest.mark.parametrize("offset", [200, 5000, 20000])
def test_evidence_page_inside_audio_does_not_leak_partial_binary(offset: int) -> None:
    encoded = base64.b64encode(encode_wav(bytes(32000), 16000)).decode()
    original = json.dumps({"voice": encoded, "status": "confirmed"})
    snapshot = EvidenceSnapshot(
        evidence_id="receipt",
        version=1,
        kind="call_receipt",
        text=original,
        source_json="{}",
        occurred_at=1,
    )
    page = EvidencePage(
        snapshot=snapshot,
        slice=EvidenceSlice(
            slice_ref="page", evidence_id="receipt", version=1, start=offset, end=offset + 100
        ),
        text=original[offset : offset + 100],
        level="evidence",
    )
    projected = evidence_text_view(page)
    assert "音频编码未交付" in projected and page.text not in projected
    assert page.slice is not None and page.slice.start == offset
    assert snapshot.text == original


def test_recent_history_projects_tool_audio_but_preserves_user_input_and_tool_pairing() -> None:
    encoded = base64.b64encode(encode_wav(bytes(32000), 16000)).decode()
    messages = (
        Message.user("用户原始音频编码示例 " + encoded),
        Message(MessageRole.ASSISTANT, (ToolCallBlock("call", "play_action"),)),
        Message(
            MessageRole.TOOL,
            (
                ToolResultBlock(
                    "call", content=(TextBlock(json.dumps({"result_json": json.dumps(encoded)})),)
                ),
            ),
        ),
        Message.assistant("已结束"),
    )
    original = ConversationCheckpoint(
        ConversationState(messages, turn_index=1, turn_boundaries=(4,)), ReminderState()
    )
    projected = expire_history_media(original)
    assert projected.history.messages[0] == original.history.messages[0]
    tool = projected.history.messages[2].content[0]
    assert isinstance(tool, ToolResultBlock) and tool.tool_call_id == "call"
    text = tool.content[0]
    assert isinstance(text, TextBlock) and encoded not in text.text
    assert projected.history.turn_boundaries == (4,)
    assert original.history.messages == messages and expire_history_media(projected) == projected
