"""用 Core 完整轮次和临时 SQLite 验证历史媒体窗口，不调用真实模型或设备。

覆盖图片/音频混合输入、嵌套工具媒体、轮内工具调用与重试、失败回滚及重启。
证据原件与模型可见窗口分别断言，不能把历史过期伪装成已删除、识图或转写。
"""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from purrcept_core.models import (
    AudioBlock,
    AudioBytes,
    ContentBlock,
    ConversationCheckpoint,
    ConversationState,
    FunctionTool,
    ImageBlock,
    ImageBytes,
    ImageUrl,
    Message,
    MessageRole,
    ModelContinuation,
    ModelResponse,
    ModelSettings,
    ModelUnavailableError,
    ReasoningBlock,
    ReminderState,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolSet,
)
from test_subject_session import ScriptedBackend, refresh

from purrcept_engine.app.config import RetrySettings
from purrcept_engine.domain.context.capacity import GenerationCapacity, check_capacity
from purrcept_engine.domain.context.history import summary_request
from purrcept_engine.domain.context.history_preparation import preview_request
from purrcept_engine.domain.context.maintenance import freeze_summary_source
from purrcept_engine.domain.context.media_history import expire_history_media
from purrcept_engine.domain.inputs.models import (
    InputContext,
    InputSource,
    InputSubmission,
    MediaAsset,
)
from purrcept_engine.domain.subject.models import SessionScope
from purrcept_engine.domain.subject.session import ContinuousSession, DynamicState
from purrcept_engine.infrastructure.audio.wav import encode_wav
from purrcept_engine.infrastructure.models.capacity import RULE, count_litellm_capacity
from purrcept_engine.infrastructure.models.compiler import compile_materials
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.values import JsonObject


def _blocks(content: tuple[ContentBlock, ...]) -> Iterator[ContentBlock]:
    """遍历 Core 原生嵌套内容，不从 JSON/文字猜测媒体。"""
    for block in content:
        yield block
        if isinstance(block, ToolResultBlock):
            yield from _blocks(block.content)


def _audio(messages: tuple[Message, ...]) -> list[AudioBlock]:
    return [
        block
        for message in messages
        for block in _blocks(message.content)
        if isinstance(block, AudioBlock)
    ]


def _images(messages: tuple[Message, ...]) -> list[ImageBlock]:
    return [
        block
        for message in messages
        for block in _blocks(message.content)
        if isinstance(block, ImageBlock)
    ]


def _checkpoint() -> ConversationCheckpoint:
    """每轮混合图片、音频与多层工具回执，另带一个当前后缀。"""
    messages: list[Message] = []
    ends: list[int] = []
    for index in range(5):
        messages.extend(
            (
                Message(
                    MessageRole.USER,
                    (
                        TextBlock(f"input:{index}"),
                        AudioBlock(AudioBytes(b"input")),
                        ImageBlock(ImageBytes(b"input-image", "image/png")),
                    ),
                ),
                Message(MessageRole.ASSISTANT, (ToolCallBlock(f"call:{index}", "read_evidence"),)),
                Message(
                    MessageRole.TOOL,
                    (
                        ToolResultBlock(
                            f"call:{index}",
                            content=(
                                TextBlock("真实回执未变"),
                                AudioBlock(AudioBytes(b"receipt")),
                                ImageBlock(ImageBytes(b"image", "image/png")),
                                ImageBlock(ImageUrl("https://example.invalid/old.png")),
                                ToolResultBlock(
                                    f"nested:{index}",
                                    content=(
                                        TextBlock("嵌套回执未变"),
                                        ImageBlock(ImageBytes(b"nested-image", "image/jpeg")),
                                    ),
                                ),
                            ),
                            is_error=True,
                        ),
                    ),
                    name="read_evidence",
                    metadata={"evidence": (f"evidence:{index}",)},
                ),
                Message(
                    MessageRole.ASSISTANT,
                    (ReasoningBlock("隐藏推理保持原协议"), TextBlock("内部结束")),
                ),
            )
        )
        ends.append(len(messages))
    messages.append(
        Message(
            MessageRole.USER,
            (
                AudioBlock(AudioBytes(b"current")),
                ImageBlock(ImageBytes(b"current-image", "image/png")),
            ),
        )
    )
    return ConversationCheckpoint(
        ConversationState(
            tuple(messages),
            turn_index=5,
            turn_boundaries=tuple(ends),
            continuation=ModelContinuation("test", {"opaque": "unchanged"}),
        ),
        ReminderState(),
    )


def test_expiration_uses_complete_turns_and_preserves_receipts_and_current_suffix() -> None:
    original = _checkpoint()
    projected = expire_history_media(original)
    assert len(_audio(original.history.messages)) == 11
    assert len(_audio(projected.history.messages)) == 5
    assert len(_images(original.history.messages)) == 21
    assert len(_images(projected.history.messages)) == 9
    assert projected.history.messages[12:] == original.history.messages[12:]
    assert projected.history.turn_boundaries == original.history.turn_boundaries
    assert projected.history.turn_index == original.history.turn_index
    assert projected.history.continuation == original.history.continuation
    assert projected.reminders == original.reminders
    for index in (0, 4, 8):
        assert projected.history.messages[index].content[1] == TextBlock("历史语音输入已过期")
        before = original.history.messages[index + 2]
        after = projected.history.messages[index + 2]
        receipt = after.content[0]
        assert isinstance(receipt, ToolResultBlock)
        assert receipt.content[1] == TextBlock("历史语音输入已过期")
        assert receipt.tool_call_id == f"call:{index // 4}" and receipt.is_error
        assert after.name == before.name and after.metadata == before.metadata
        assert receipt.content[0] == TextBlock("真实回执未变")
        assert receipt.content[2:4] == (TextBlock("历史图片已过期"),) * 2
        nested = receipt.content[4]
        assert isinstance(nested, ToolResultBlock)
        assert nested.content == (TextBlock("嵌套回执未变"), TextBlock("历史图片已过期"))
        assert projected.history.messages[index].content[2] == TextBlock("历史图片已过期")
        assert projected.history.messages[index + 1] is original.history.messages[index + 1]
        assert projected.history.messages[index + 3] is original.history.messages[index + 3]
    assert expire_history_media(projected) is projected


@pytest.mark.parametrize("completed", [0, 1, 2])
def test_recent_turns_do_not_expire_media(completed: int) -> None:
    original = _checkpoint()
    history = original.history
    boundary = history.turn_boundaries[completed - 1] if completed else 0
    checkpoint = replace(
        original,
        history=replace(
            history,
            messages=(*history.messages[:boundary], history.messages[-1]),
            turn_boundaries=history.turn_boundaries[:completed],
            turn_index=completed,
        ),
    )
    assert expire_history_media(checkpoint) is checkpoint


def test_image_only_history_expires_without_audio() -> None:
    messages = tuple(
        Message(MessageRole.USER, (ImageBlock(ImageBytes(b"image", "image/png")),))
        for _ in range(3)
    )
    checkpoint = ConversationCheckpoint(
        ConversationState(messages, turn_index=3, turn_boundaries=(1, 2, 3)), ReminderState()
    )
    projected = expire_history_media(checkpoint)
    assert not _audio(projected.history.messages)
    assert len(_images(projected.history.messages)) == 2
    assert projected.history.messages[0].content == (TextBlock("历史图片已过期"),)


def test_summary_does_not_reintroduce_expired_media_and_keeps_its_original_source() -> None:
    original = _checkpoint()
    source = freeze_summary_source(original, owner_id="foreground", source_version=1)
    assert source is not None
    request = summary_request(source, 1000)
    assert not _audio(request.messages)
    assert not _images(request.messages)
    assert "历史语音输入已过期" in request.messages[0].text
    assert "历史图片已过期" in request.messages[0].text
    assert source.checkpoint is original and len(_audio(source.messages)) == 6
    assert len(_images(source.messages)) == 12
    preview = preview_request(
        ScriptedBackend(()),
        "openai/test",
        original,
        Message.user("下一轮"),
        instructions=(),
        tools=ToolSet(),
        settings=ModelSettings(max_output_tokens=1000),
        dynamic=DynamicState("真实状态"),
        max_requests=8,
    )
    assert len(_audio(preview.messages)) == 5
    assert len(_images(preview.messages)) == 9


def _capacity(payload: JsonObject):
    return check_capacity(
        payload,
        GenerationCapacity(100_000, 1000, RULE, "gemini-3-inline-v1"),
        rules={RULE: count_litellm_capacity},
    )


@pytest.mark.parametrize("scope", ["foreground", "reflection"])
async def test_real_core_media_window_survives_tool_rounds_retry_failure_and_reopen(
    tmp_path: Path,
    scope: SessionScope,
) -> None:
    database = tmp_path / "audio-history.sqlite3"
    store = await Store.open(database)
    session = ContinuousSession(store, scope, can_start=lambda: None, compiler=compile_materials)
    backend = ScriptedBackend(tuple(ModelResponse(Message.assistant("完成")) for _ in range(3)))
    originals: list[tuple[str, bytes, bytes]] = []

    async def echo() -> str:
        """本地工具只返回文字，不产生世界效果。"""
        return "工具已结束"

    async def run(index: int) -> None:
        now = datetime.now(UTC)
        wav = encode_wav(bytes([index + 1, 0]) * 512, 16000)
        with Image.new("RGB", (8, 8), (index, 0, 0)) as image, io.BytesIO() as output:
            image.save(output, format="PNG")
            png = output.getvalue()
        admission = await store.admit(
            InputSubmission(
                InputSource("test", "local", "audio", "utterance", str(index)),
                str(index),
                now,
                now,
                media=(MediaAsset(wav, "audio/wav"), MediaAsset(png, "image/png")),
                context=InputContext(minimum_wake=True),
            )
        )
        originals.append((admission.input_id, wav, png))
        await session.run(
            Message(
                MessageRole.USER,
                (
                    TextBlock(f"current:{index}"),
                    AudioBlock(AudioBytes(wav)),
                    ImageBlock(ImageBytes(png, "image/png")),
                ),
            ),
            turn_id=f"turn:{index}",
            input_ids=(admission.input_id,),
            backend=backend,
            evidence=store,
            model="openai/test",
            instructions=(),
            tools=ToolSet((FunctionTool(echo),)),
            output_tokens=1000,
            max_requests=8,
            turn_seconds=20,
            retry=RetrySettings(attempt_seconds=5, total_seconds=10),
            diagnostic_bytes=4096,
            refresh=refresh,
            capacity=_capacity,
        )

    try:
        for index in range(3):
            await run(index)
        before = await store.get_session(scope)
        backend.responses.append(RuntimeError("失败轮次不推进窗口"))
        with pytest.raises(RuntimeError, match="不推进窗口"):
            await run(3)
        assert (await store.get_session(scope)).checkpoint_json == before.checkpoint_json
        backend.responses.extend(
            (
                ModelUnavailableError("传输替身暂不可用", provider="offline"),
                ModelResponse(Message(MessageRole.ASSISTANT, (ToolCallBlock("echo:4", "echo"),))),
                ModelResponse(Message.assistant("完成")),
                ModelResponse(Message.assistant("完成")),
            )
        )
        await run(4)
        await run(5)
        assert [len(_audio(request.messages)) for request in backend.requests] == [
            1,
            2,
            3,
            3,
            3,
            3,
            3,
            3,
        ]
        assert backend.requests[4].messages == backend.requests[5].messages
        assert [len(_images(request.messages)) for request in backend.requests] == [
            1,
            2,
            3,
            3,
            3,
            3,
            3,
            3,
        ]
        assert all(
            "历史语音输入已过期" in request.messages[0].text for request in backend.requests[3:]
        )
        await session.close()
        await store.close()
        store = await Store.open(database)
        session = ContinuousSession(
            store, scope, can_start=lambda: None, compiler=compile_materials
        )
        backend.responses.append(ModelResponse(Message.assistant("重开后完成")))
        await run(6)
        assert len(_audio(backend.requests[-1].messages)) == 3
        assert len(_images(backend.requests[-1].messages)) == 3
        for identity, wav, png in originals:
            record = await store.get_input(identity)
            assert record is not None
            assert await store.get_media(record.media[0].media_id) == wav
            assert await store.get_media(record.media[1].media_id) == png
        evidence = await store.requests_for_turn(scope, "turn:0")
        assert len(evidence) == 1
        payload = json.loads(evidence[0].request.materials_json)["payload"]
        audio_parts = [
            part
            for message in payload["messages"]
            if isinstance(message["content"], list)
            for part in message["content"]
            if part["type"] == "input_audio"
        ]
        assert len(audio_parts) == 1
        assert base64.b64decode(audio_parts[0]["input_audio"]["data"]) == originals[0][1]
        image_parts = [
            part
            for message in payload["messages"]
            if isinstance(message["content"], list)
            for part in message["content"]
            if part["type"] == "image_url"
        ]
        assert len(image_parts) == 1
        assert (
            base64.b64decode(image_parts[0]["image_url"]["url"].split(",", 1)[1]) == originals[0][2]
        )
    finally:
        await session.close()
        await store.close()
