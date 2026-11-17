"""用 Core 完整轮次和临时 SQLite 验证按大小清理历史媒体，不调用真实模型或设备。

覆盖图片/音频混合输入、嵌套工具媒体、轮内工具调用与重试、失败回滚及重启。
证据原件与模型可见媒体分别断言，不能把历史清理伪装成已删除、识图或转写。
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
    ModelRequest,
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
from test_subject_session import ScriptedBackend, admitted, refresh

from purrcept_engine.app.config import RetrySettings
from purrcept_engine.domain.context.capacity import GenerationCapacity, check_capacity
from purrcept_engine.domain.context.checkpoints import decode_checkpoint, encode_checkpoint
from purrcept_engine.domain.context.delivery import ToolDeliveryGate
from purrcept_engine.domain.context.history import prepare_summary_request, summary_request
from purrcept_engine.domain.context.history_preparation import preview_request
from purrcept_engine.domain.context.maintenance import freeze_summary_source
from purrcept_engine.domain.context.media_history import HistoryMedia, expire_history_media
from purrcept_engine.domain.inputs.models import (
    InputContext,
    InputSource,
    InputSubmission,
    MediaAsset,
)
from purrcept_engine.domain.subject.models import SessionScope
from purrcept_engine.domain.subject.session import ContinuousSession, DynamicState
from purrcept_engine.infrastructure.audio.wav import encode_wav
from purrcept_engine.infrastructure.models.compiler import compile_materials
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.serialization import encode
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
    assert len(_audio(projected.history.messages)) == 1
    assert len(_images(original.history.messages)) == 21
    assert len(_images(projected.history.messages)) == 1
    assert projected.history.messages[20:] == original.history.messages[20:]
    assert projected.history.turn_boundaries == original.history.turn_boundaries
    assert projected.history.turn_index == original.history.turn_index
    assert projected.history.continuation == original.history.continuation
    assert projected.reminders == original.reminders
    for index in (0, 4, 8, 12, 16):
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


@pytest.mark.parametrize("completed", [0, 1, 2, 5])
def test_under_threshold_preserves_media_regardless_of_age(completed: int) -> None:
    message = Message(
        MessageRole.USER,
        (ImageBlock(ImageBytes(b"image", "image/png")), AudioBlock(AudioBytes(b"audio"))),
    )
    request = ModelRequest(
        (message,) * (completed + 1), settings=ModelSettings(max_output_tokens=1000)
    )
    media = HistoryMedia(completed, compiler=compile_materials)
    assert media.prepare(request, model="openai/test") is request


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
    assert not _images(projected.history.messages)
    assert projected.history.messages[0].content == (TextBlock("历史图片已过期"),)


def test_summary_and_core_preview_preserve_full_media_before_size_check() -> None:
    original = _checkpoint()
    source = freeze_summary_source(original, owner_id="foreground", source_version=1)
    assert source is not None
    request = summary_request(source, 1000)
    assert len(_audio(request.messages)) == 6
    assert len(_images(request.messages)) == 12
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
    assert len(_audio(preview.messages)) == 11
    assert len(_images(preview.messages)) == 21


def test_cleanup_uses_full_utf8_payload_and_freezes_the_completed_prefix() -> None:
    """达到阈值不清理，工具续问超限才批量清理；之后的新媒体始终保留。"""
    original = _checkpoint()
    messages = (*original.history.messages[0:20:4], original.history.messages[-1])
    request = ModelRequest(messages, settings=ModelSettings(max_output_tokens=1000))
    raw, _ = compile_materials(request, model="openai/test")
    request_bytes = len(encode(json.loads(raw)["payload"]).encode("utf-8"))
    media = HistoryMedia(5, compiler=compile_materials, threshold_bytes=request_bytes)
    assert media.prepare(request, model="openai/test") is request

    larger = replace(request, messages=(*request.messages, Message.assistant("新结果" * 100)))
    cleaned = media.prepare(larger, model="openai/test")
    assert len(_images(cleaned.messages)) == 1 and len(_audio(cleaned.messages)) == 1
    assert cleaned.messages[5:] == larger.messages[5:]
    # 再次收到同一份原 Core 历史也不能恢复旧媒体；新轮内媒体不受清理决定影响。
    smaller = media.prepare(request, model="openai/test")
    assert smaller.messages[:5] == cleaned.messages[:5]
    assert smaller.messages[5:] == request.messages[5:]
    assert len(_images(original.history.messages)) == 21


@pytest.mark.parametrize("threshold", [1, 30_000_000])
async def test_summary_applies_the_same_request_size_threshold(threshold: int) -> None:
    image = io.BytesIO()
    Image.new("RGB", (8, 8)).save(image, "PNG")
    messages = tuple(
        Message(MessageRole.USER, (ImageBlock(ImageBytes(image.getvalue(), "image/png")),))
        for _ in range(3)
    )
    checkpoint = ConversationCheckpoint(
        ConversationState(messages, turn_index=3, turn_boundaries=(1, 2, 3)), ReminderState()
    )
    source = freeze_summary_source(checkpoint, owner_id="foreground", source_version=1)
    assert source is not None
    _, request = await prepare_summary_request(
        source,
        output_tokens=1000,
        model="openai/test",
        capacity=_capacity,
        compiler=compile_materials,
        check_budget=lambda: None,
        media_cleanup_threshold_bytes=threshold,
    )
    assert len(_images(request.messages)) == (0 if threshold == 1 else 1)
    assert len(_images(source.messages)) == 1


@pytest.mark.parametrize("failed", [False, True])
async def test_cleanup_during_tool_continuation_is_committed_only_on_success(
    tmp_path: Path, failed: bool
) -> None:
    """真实 Core 首问保留媒体，工具结果令请求超限，只有成功轮次安装清理后的历史。"""
    store = await Store.open(tmp_path / "size.sqlite3")
    session = ContinuousSession(
        store, "foreground", can_start=lambda: None, compiler=compile_materials
    )
    image = io.BytesIO()
    Image.new("RGB", (8, 8)).save(image, "PNG")
    old = Message(MessageRole.USER, (ImageBlock(ImageBytes(image.getvalue(), "image/png")),))
    seed = ConversationCheckpoint(
        ConversationState((old, Message.assistant("已看图")), turn_index=1, turn_boundaries=(2,)),
        ReminderState(),
    )

    async def read() -> str:
        """返回足以触发字节阈值的正文，不产生设备效果。"""
        return "x" * 8000

    tools = ToolSet((FunctionTool(read),))
    backend = ScriptedBackend(
        (
            ModelResponse(Message(MessageRole.ASSISTANT, (ToolCallBlock("read", "read"),))),
            RuntimeError("清理后模型失败") if failed else ModelResponse(Message.assistant("完成")),
        )
    )
    try:
        identity = await admitted(store, "seed", "旧图")
        turn = await store.begin_turn("seed", "foreground", 0, 1, (identity,))
        await store.finish_turn(turn, "succeeded", checkpoint_json=encode_checkpoint(seed))
        before = await store.get_session("foreground")
        current_id = await admitted(store, "current", "新图")
        current = replace(old, content=(*old.content, TextBlock("当前输入")))
        preview = preview_request(
            backend,
            "openai/test",
            seed,
            current,
            instructions=(),
            tools=tools,
            settings=ModelSettings(max_output_tokens=1000),
            dynamic=DynamicState("宿主已提交状态（user 材料）：已使用请求 0"),
            max_requests=8,
        )
        raw, _ = compile_materials(preview, model="openai/test")
        session.media_cleanup_threshold_bytes = (
            len(encode(json.loads(raw)["payload"]).encode()) + 1000
        )
        operation = session.run(
            current,
            turn_id="current",
            input_ids=(current_id,),
            backend=backend,
            evidence=store,
            model="openai/test",
            instructions=(),
            tools=tools,
            output_tokens=1000,
            max_requests=8,
            turn_seconds=20,
            retry=RetrySettings(attempts=1),
            diagnostic_bytes=0,
            refresh=refresh,
            capacity=_capacity,
            delivery=ToolDeliveryGate(compiler=compile_materials),
        )
        if failed:
            with pytest.raises(RuntimeError, match="清理后模型失败"):
                await operation
        else:
            await operation
        assert len(_images(backend.requests[0].messages)) == 2
        assert len(_images(backend.requests[1].messages)) == 1
        saved = await store.get_session("foreground")
        if failed:
            assert saved.checkpoint_json == before.checkpoint_json
        else:
            assert saved.checkpoint_json is not None
            assert len(_images(decode_checkpoint(saved.checkpoint_json).history.messages)) == 1
    finally:
        await session.close()
        await store.close()


def _capacity(payload: JsonObject):
    return check_capacity(
        payload,
        GenerationCapacity(100_000, 1000),
    )


@pytest.mark.parametrize("scope", ["foreground", "behavior_review"])
async def test_under_threshold_history_survives_tool_rounds_retry_failure_and_reopen(
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
        backend.responses.append(RuntimeError("失败轮次不提交历史"))
        with pytest.raises(RuntimeError, match="不提交历史"):
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
            4,
            4,
            4,
            4,
            5,
        ]
        assert backend.requests[4].messages == backend.requests[5].messages
        assert [len(_images(request.messages)) for request in backend.requests] == [
            1,
            2,
            3,
            4,
            4,
            4,
            4,
            5,
        ]
        assert all(
            request.messages[0] == backend.requests[0].messages[0] for request in backend.requests
        )
        await session.close()
        await store.close()
        store = await Store.open(database)
        session = ContinuousSession(
            store, scope, can_start=lambda: None, compiler=compile_materials
        )
        backend.responses.append(ModelResponse(Message.assistant("重开后完成")))
        await run(6)
        assert len(_audio(backend.requests[-1].messages)) == 6
        assert len(_images(backend.requests[-1].messages)) == 6
        for identity, wav, png in originals:
            record = await store.get_input(identity)
            assert record is not None
            assert await store.get_media(record.media[0].media_id) == wav
            assert await store.get_media(record.media[1].media_id) == png
        evidence = await store.requests_for_turn(scope, "turn:0")
        assert len(evidence) == 1
        assert evidence[0].request.materials_json is not None
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
