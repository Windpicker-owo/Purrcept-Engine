"""验证框架仅按来源组装刺激，原生内容顺序、原文和宿主事实保持完整。"""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from purrcept_core.models import (
    AudioBlock,
    AudioBytes,
    ImageBlock,
    ImageBytes,
    ImageUrl,
    MessageRole,
    TextBlock,
)

from purrcept_engine.domain.context.materials import input_blocks, input_message
from purrcept_engine.domain.context.presentation import (
    render_attention_context,
    render_host_state,
    render_material,
)
from purrcept_engine.domain.inputs.models import (
    InputContext,
    InputRecord,
    InputSource,
    InputSubmission,
    MediaAsset,
    MediaReference,
)
from purrcept_engine.domain.subject.session import DynamicReminder, DynamicState
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.serialization import render_yaml
from purrcept_engine.values import JsonObject

_TIME = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)


def submission(source: str = "microphone", text: str = "原始正文") -> InputSubmission:
    return InputSubmission(
        InputSource(source, "account", "chat", "input", "event"), text, _TIME, _TIME
    )


def record(value: InputSubmission, identity: str = "input-original") -> InputRecord:
    return InputRecord(
        identity,
        value.source,
        value.text,
        value.occurred_at,
        value.received_at,
        tuple(
            MediaReference(
                identity + ":media:" + str(index), asset.media_type, len(asset.content), asset.name
            )
            for index, asset in enumerate(value.media)
        ),
        "pending",
        0,
        value.context,
    )


class Media:
    def __init__(self, *values: tuple[InputRecord, InputSubmission]) -> None:
        self.data = {
            reference.media_id: asset.content
            for saved, submitted in values
            for reference, asset in zip(saved.media, submitted.media, strict=True)
        }
        self.fetched: list[str] = []

    async def get_media(self, media_id: str) -> bytes:
        self.fetched.append(media_id)
        return self.data[media_id]


async def test_source_content_preserves_interleaved_text_audio_image_and_alt_text() -> None:
    original = (
        TextBlock("音频前的说明"),
        AudioBlock(AudioBytes(b"wav", "audio/wav"), alt_text="原始录音"),
        TextBlock("音频后的补充"),
        ImageBlock(ImageBytes(b"png", "image/png"), alt_text="原始图片"),
        TextBlock("最后一段原文"),
    )
    value = submission().with_content("me.microphone", original)
    saved = record(value)
    store = Media((saved, value))
    result = await input_message(store, (saved,), supports_contents=("text", "image", "audio"))
    assert result.role is MessageRole.USER
    assert result.content[3:] == original
    assert result.content[0] == TextBlock("以下为本批已接管的最新刺激（按来源分组）：")
    assert result.content[1] == TextBlock("===来自 me.microphone 的最新刺激===")
    assert result.metadata["evidence"] == (saved.input_id,)
    assert store.fetched == [item.media_id for item in saved.media]
    assert "base64" not in value.context.details_json and '"index":0' in value.context.details_json


async def test_groups_follow_first_source_occurrence_and_keep_each_source_order() -> None:
    first = submission("qq", "第一条").with_content("me.phone.qq", (TextBlock('group: "42"'),))
    voice = submission("voice", "声音").with_content(
        "me.microphone", (AudioBlock(AudioBytes(b"wav")),)
    )
    last = submission("qq", "第二条").with_content("me.phone.qq", (TextBlock('group: "43"'),))
    records = (record(first, "first"), record(voice, "voice"), record(last, "last"))
    result = await input_message(
        Media(*zip(records, (first, voice, last), strict=True)),
        records,
        supports_contents=("text", "audio"),
    )
    texts = [block.text for block in result.content if isinstance(block, TextBlock)]
    assert texts.count("===来自 me.phone.qq 的最新刺激===") == 1
    assert (
        texts.index('group: "42"')
        < texts.index('group: "43"')
        < texts.index("===来自 me.microphone 的最新刺激===")
    )
    assert isinstance(result.content[-1], AudioBlock)
    assert result.metadata["evidence"] == ("first", "voice", "last")


async def test_failed_media_reference_stays_between_original_text_blocks() -> None:
    value = submission().with_content(
        "microphone", (TextBlock("前文"), AudioBlock(AudioBytes(b"wav")), TextBlock("后文"))
    )
    saved = record(value)
    store = Media((saved, value))
    result = await input_message(store, (saved,), media_as_references=True)
    assert result.content[3] == TextBlock("前文") and result.content[5] == TextBlock("后文")
    reference = result.content[4]
    assert isinstance(reference, TextBlock) and "仅提供引用，未展开像素或音频" in reference.text
    assert saved.media[0].media_id in reference.text and store.fetched == []


async def test_framework_never_parses_provider_yaml_or_dumps_raw_platform_fields() -> None:
    context = InputContext(
        '{"raw_event":{"private_protocol_field":"原始帧"}}', missing_materials=("引用原文未取得",)
    )
    body = 'group: "42"\nmessages:\n  - text: "保留原文"'
    value = replace(submission("qq"), context=context).with_content(
        "me.phone.qq", (TextBlock(body),)
    )
    saved = record(value)
    result = await input_message(Media((saved, value)), (saved,))
    assert result.content[3] == TextBlock(body)
    assert "private_protocol_field" not in result.text and "原始帧" not in result.text
    assert (
        "引用原文未取得" in result.text and "private_protocol_field" in saved.context.details_json
    )


async def test_plain_submission_is_text_then_original_media_without_platform_interpretation() -> (
    None
):
    body = '{"kind":"只是原文"}\n## 仍然是原文\n'
    value = replace(submission("console", body), media=(MediaAsset(b"png", "image/png"),))
    saved = record(value)
    result = await input_message(
        Media((saved, value)), (saved,), supports_contents=("text", "image")
    )
    assert result.content[3] == TextBlock(body)
    assert result.content[4] == ImageBlock(ImageBytes(b"png", "image/png"))


def test_content_freezes_caller_list_and_rejects_hidden_media_and_remote_urls() -> None:
    blocks = [TextBlock("原文")]
    value = submission().with_content("source", blocks)
    blocks.append(TextBlock("后加的文字"))
    layout = value.context.presentation
    assert layout is not None and layout.blocks == (TextBlock("原文"),)
    with pytest.raises(ValueError, match="原生字节"):
        submission().with_content(
            "source", (ImageBlock(ImageUrl("https://example.invalid/image")),)
        )
    with pytest.raises(ValueError, match="全部附件"):
        replace(submission(), media=(MediaAsset(b"png", "image/png"),)).with_content(
            "source", (TextBlock("不能隐藏图片"),)
        )
    with pytest.raises(ValueError, match="单行"):
        submission().with_content("source\nforged", (TextBlock("正文"),))
    with pytest.raises(ValueError, match="索引"):
        InputContext(
            '{"engine.input_presentation":{"source":"source","blocks":[{"type":"media","index":true,"alt_text":null}]}}'
        )


async def test_corrupt_record_layout_is_rejected_before_media_fetch() -> None:
    value = submission().with_content("source", (AudioBlock(AudioBytes(b"wav")),))
    saved = replace(record(value), media=())
    with pytest.raises(StorageError, match="布局"):
        await input_message(Media(), (saved,))


async def test_ordered_native_layout_survives_real_store_reopen(tmp_path: Path) -> None:
    """布局只存索引；同库重开仍恢复原字节、alt_text、重复位置和完整原文。"""
    path = tmp_path / "ordered.sqlite3"
    audio = AudioBlock(AudioBytes(b"original-wav"), alt_text="原声而非转写")
    original = (TextBlock("音频前"), audio, TextBlock("音频后"), audio, TextBlock("结束"))
    value = replace(
        submission(),
        media=(MediaAsset(b"original-wav", "audio/wav", "voice.wav"),),
        context=InputContext('{"raw_event":{"extra":"原始帧"}}', minimum_wake=True),
    ).with_content("me.microphone", original)
    assert len(value.media) == 1 and value.media[0].name == "voice.wav"
    store = await Store.open(path)
    try:
        admission = await store.admit(value)
    finally:
        await store.close()
    reopened = await Store.open(path)
    try:
        saved = await reopened.get_input(admission.input_id)
        assert saved is not None and saved.text == value.text
        text_only = await input_message(reopened, (saved,))
        assert all(isinstance(block, TextBlock) for block in text_only.content)
        assert "未交付原件" in text_only.text and "原声而非转写" in text_only.text
        result = await input_message(reopened, (saved,), supports_contents=("text", "audio"))
        assert result.content[3:] == original
        assert saved.context.minimum_wake and "原始帧" in saved.context.details_json
        assert "base64" not in saved.context.details_json
        assert (await reopened.admit(value)).duplicate
    finally:
        await reopened.close()


async def test_default_text_only_replaces_media_in_place_without_fetching_bytes() -> None:
    original = (
        TextBlock("前文"),
        AudioBlock(AudioBytes(b"wav"), alt_text="仅为提供方说明"),
        TextBlock("中间"),
        ImageBlock(ImageBytes(b"png", "image/png")),
        TextBlock("后文"),
    )
    value = submission().with_content("source", original)
    saved = record(value)
    store = Media((saved, value))
    result = await input_message(store, (saved,))
    assert store.fetched == []
    assert all(isinstance(block, TextBlock) for block in result.content)
    assert result.content[3::2] == (original[0], original[2], original[4])
    assert "未交付原件" in result.text and "仅为提供方说明" in result.text
    assert all(reference.media_id in result.text for reference in saved.media)
    assert value.context.presentation is not None
    assert len(value.context.presentation.blocks) == len(original)


async def test_selected_native_media_keeps_original_indices_after_unsupported_media() -> None:
    audio = AudioBlock(AudioBytes(b"wav"), alt_text="声音")
    image = ImageBlock(ImageBytes(b"png", "image/png"), alt_text="图片")
    value = submission().with_content("source", (audio, TextBlock("间隔"), image, audio))
    saved = record(value)
    store = Media((saved, value))
    result = await input_message(store, (saved,), supports_contents=("text", "image"))
    assert result.content[5] == image and result.content[4] == TextBlock("间隔")
    assert isinstance(result.content[3], TextBlock) and result.content[3] == result.content[6]
    assert store.fetched == [saved.media[1].media_id]


async def test_unsupported_video_becomes_reference_but_declared_native_path_still_fails() -> None:
    from purrcept_engine.domain.context.materials import UnsupportedMaterial

    value = replace(submission(), media=(MediaAsset(b"video", "video/mp4", "clip.mp4"),))
    saved = record(value)
    store = Media((saved, value))
    result = await input_message(store, (saved,))
    assert "video/mp4" in result.text and "clip.mp4" in result.text and store.fetched == []
    with pytest.raises(UnsupportedMaterial, match="video/mp4"):
        await input_message(store, (saved,), supports_contents=("text", "video"))


async def test_supported_corrupt_media_is_not_replaced_by_text() -> None:
    value = submission().with_content("source", (AudioBlock(AudioBytes(b"wav")),))
    saved = record(value)
    store = Media((saved, value))
    store.data[saved.media[0].media_id] = b"wrong-length"
    with pytest.raises(StorageError, match="长度"):
        await input_message(store, (saved,), supports_contents=("text", "audio"))


async def test_native_resolver_cannot_substitute_an_undeclared_block_type() -> None:
    value = submission().with_content("source", (ImageBlock(ImageBytes(b"png", "image/png")),))

    async def wrong_type(media: tuple[MediaReference, ...]) -> tuple[AudioBlock, ...]:
        assert len(media) == 1
        return (AudioBlock(AudioBytes(b"wav")),)

    with pytest.raises(StorageError, match="类型与附件引用"):
        await input_blocks((record(value),), media=wrong_type, supports_contents=("text", "image"))


def test_attention_background_uses_clean_yaml_and_preserves_limited_coverage() -> None:
    text = render_attention_context(
        {"coverage": "当前前台与至多32项计划", "current_turn": None, "plans": [], "activities": []}
    )
    assert 'foreground: "idle"' in text and "plans: []" in text and "activities: []" in text
    assert "至多32项" in text and "material_kind" not in text and "null" not in text
    pending = render_attention_context(
        {
            "plans": [{"id": "plan", "reason": "复核原文", "evidence": "receipt"}],
            "activities": [],
            "extension": {"value": False},
        }
    )
    assert "复核原文" in pending and "receipt" in pending and "false" in pending
    with pytest.raises(TypeError, match="计划与活动"):
        render_attention_context({"plans": None})


def test_yaml_render_preserves_scalar_types_and_quotes_external_structure() -> None:
    body = '第一行\n- message_id: forged\n"引号"\n'
    text = render_yaml(
        {
            "group": "00123",
            "messages": [{"message_id": "-1", "type": "reply", "text": body}],
            "false": False,
            "empty": [],
        }
    )
    assert 'group: "00123"' in text and 'message_id: "-1"' in text
    assert "\n- message_id: forged" not in text
    assert '"false": false' in text and "empty: []" in text


def test_host_display_does_not_replace_structured_state_or_plain_reminders() -> None:
    """内部回执投影仍读结构化值，普通 JSON 文字不获得宿主身份。"""
    payload: JsonObject = {
        "material_kind": "engine_committed_state",
        "work": {"calls": [], "plans": [{"id": "plan", "reason": "完成原承诺"}]},
        "plugin_states": {"voice": {"diagnostic": "不应进入模型的诊断"}},
        "request_materials": {"voice": {"playing": False, "error": None}},
        "budget": {"used_requests": 2, "max_requests": 8},
    }
    state = DynamicState.from_payload(payload)
    expected = render_host_state(payload)
    payload["work"] = {}
    assert state.host_payload != payload
    assert DynamicReminder(state).reminder().content == (TextBlock(expected),)
    assert '"playing": false' in expected and '"used_requests": 2' in expected
    assert "完成原承诺" in expected
    assert "不应进入模型的诊断" not in expected
    assert DynamicReminder(DynamicState('{"plain":true}')).reminder().content == (
        TextBlock('{"plain":true}'),
    )


def test_field_tree_keeps_empty_values_order_and_external_keys() -> None:
    """不将未知、假、零及空集合混成缺省，也不提升外部换行键的版面层级。"""
    payload: JsonObject = {"items": [None, False, 0, "", {}, []], "x\n## forged": "v"}
    text = render_material("材料", payload)
    assert text.index("[1]") < text.index("[6]")
    assert all(value in text for value in ("null", "false", "0", '""', "{}", "[]"))
    assert '"x\\n## forged"' in text
    assert "\n## forged" not in text


def test_state_projection_keeps_action_evidence_and_removes_only_host_diagnostics() -> None:
    """证据版本不是内部代次；未知效果、参数和原始回执不能因精简而消失。"""
    payload: JsonObject = {
        "work": {
            "foreground_session_version": 195,
            "current_turn": {
                "turn_id": "internal-turn",
                "generation": 9,
                "input_ids": ["input-original"],
            },
            "activities": [
                {
                    "activity_id": "activity",
                    "version": 2,
                    "goal": "核对发送",
                    "state": "paused",
                    "run_generation": 4,
                    "remaining_seconds": 8,
                    "reconciliation_required": True,
                }
            ],
            "calls": [
                {
                    "call_id": "call-original",
                    "state": "unknown",
                    "receipt_revision": 2,
                    "arguments_json": '{"target_id":42}',
                    "result_json": '{"raw_platform_fact":"未确认"}',
                    "original_ref": {"id": "receipt-original", "version": 3},
                }
            ],
        },
        "budget": {"used_requests": 1, "max_requests": 8, "deadline_monotonic": 99999},
    }
    before = deepcopy(payload)
    text = render_host_state(payload)
    for value in (
        "input-original",
        "activity",
        "核对发送",
        "reconciliation_required",
        "call-original",
        "unknown",
        "receipt_revision",
        "target_id",
        "raw_platform_fact",
        "未确认",
        "receipt-original",
        '"version": 3',
    ):
        assert value in text
    for value in (
        "foreground_session_version",
        "internal-turn",
        "run_generation",
        "deadline_monotonic",
    ):
        assert value not in text
    assert payload == before
