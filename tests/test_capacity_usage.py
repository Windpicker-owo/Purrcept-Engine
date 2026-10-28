"""用真实请求账本验证实际输入锚点、增量材料和重启隔离，模型均为离线替身。"""

from __future__ import annotations

import asyncio
import io
from collections import deque
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image
from purrcept_core.models import (
    ImageBlock,
    ImageBytes,
    Message,
    MessageRole,
    ModelEventSink,
    ModelRequest,
    ModelResponse,
    ModelSettings,
    ModelUnavailableError,
    SystemReminder,
    TextBlock,
    TokenUsage,
)

from purrcept_engine.app.config import RetrySettings
from purrcept_engine.domain.context.capacity import (
    CapacityError,
    GenerationCapacity,
    assess_capacity,
    check_capacity,
)
from purrcept_engine.domain.context.usage import UsageMeter
from purrcept_engine.domain.model_requests.models import RequestScope
from purrcept_engine.domain.model_requests.service import ModelGenerationBackend, RequestBudget
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.models.compiler import compile_materials, compiler_identity
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.values import JsonObject, decode_object


class Backend:
    """只提供明确响应，不计算或改写服务端输入用量。"""

    def __init__(self, responses: tuple[ModelResponse | Exception, ...]) -> None:
        self.responses = deque(responses)
        self.calls = 0

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        self.calls += 1
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def request() -> ModelRequest:
    """旧正文故意明显低估，实际输入由替身端点明确报告。"""
    return ModelRequest(
        (Message.user("结构化旧材料" + 'a1b2c3d4\\"' * 1000),),
        settings=ModelSettings(max_output_tokens=1000),
    )


def payload(value: ModelRequest) -> JsonObject:
    raw, _ = compile_materials(value, model="openai/offline")
    sdk = decode_object(raw.encode("utf-8"))["payload"]
    assert isinstance(sdk, dict)
    return sdk


async def meter(store: Store, binding: str = "foreground-endpoint-a") -> UsageMeter:
    return UsageMeter(
        store, binding, compiler_identity("generation"), await store.get_usage_anchor(binding)
    )


async def invoke(store: Store, usage: UsageMeter, backend: Backend, current: ModelRequest) -> None:
    wrapped = ModelGenerationBackend(
        backend,
        store,
        compiler=compile_materials,
        scope=RequestScope("foreground", "usage-test"),
        budget=RequestBudget(max_requests=1, deadline=asyncio.get_running_loop().time() + 30),
        policy=RetrySettings(attempts=1),
        diagnostic_bytes=0,
        observe_usage=usage.observe,
    )
    try:
        await wrapped.generate(current, model="openai/offline")
    finally:
        await wrapped.close()


async def test_usage_survives_restart_and_prevents_underestimated_history(tmp_path: Path) -> None:
    """缓存命中不重复加计；重启后只估新增消息，实际旧输入仍能阻止超窗。"""
    path = tmp_path / "usage.sqlite3"
    store = await Store.open(path)
    original = request()
    usage = await meter(store)
    backend = Backend(
        (
            ModelResponse(
                Message.assistant("已处理"),
                usage=TokenUsage(90_000, 200, cached_input_tokens=80_000),
            ),
        )
    )
    try:
        await invoke(store, usage, backend, original)
        assert usage.estimate(payload(original)).input_tokens == 90_000
    finally:
        await store.close()

    reopened = await Store.open(path)
    try:
        restored = await meter(reopened)
        candidate = replace(
            original,
            messages=(*original.messages, Message.assistant("已处理"), Message.user("继续")),
        )
        assessment = assess_capacity(
            payload(candidate), GenerationCapacity(100_000, 1000), usage=restored
        )
        assert assessment.estimate.method == "provider-usage-plus-delta-v1"
        assert assessment.estimate.observed_input_tokens == 90_000
        assert assessment.estimate.new_input_units is not None
        assert 0 < assessment.estimate.new_input_units < 100
        assert 90_000 < assessment.estimate.input_units < 90_100
        assert not assessment.within_budget
        with pytest.raises(CapacityError, match="超过"):
            check_capacity(payload(candidate), GenerationCapacity(100_000, 1000), usage=restored)
        assert check_capacity(payload(candidate), GenerationCapacity(100_000, 1000)).within_budget
        assert (await meter(reopened, "another-endpoint")).anchor is None
        wrong_sdk = UsageMeter(reopened, restored.binding, "different-compiler", restored.anchor)
        assert wrong_sdk.anchor is None
    finally:
        await reopened.close()


async def test_changed_history_and_tools_do_not_borrow_old_usage(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "changed.sqlite3")
    try:
        usage = await meter(store)
        original = request()
        await invoke(
            store,
            usage,
            Backend((ModelResponse(Message.assistant("完成"), usage=TokenUsage(90_000, 5)),)),
            original,
        )
        rewritten = replace(original, messages=(Message.user("新的摘要历史"),))
        measured = assess_capacity(
            payload(rewritten), GenerationCapacity(100_000, 1000), usage=usage
        )
        assert measured.estimate.method == "multimodal-heuristic-v1"
        assert measured.estimate.input_units < 100
        changed = payload(original)
        changed["tools"] = [{"type": "function", "function": {"name": "new-tool"}}]
        assert usage.estimate(changed).request_id is None
    finally:
        await store.close()


async def test_only_new_media_are_estimated_after_valid_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await Store.open(tmp_path / "media.sqlite3")
    image = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(image, "PNG")
    original = replace(
        request(),
        messages=(
            Message(MessageRole.USER, (ImageBlock(ImageBytes(image.getvalue(), "image/png")),)),
        ),
    )
    try:
        usage = await meter(store)
        await invoke(
            store,
            usage,
            Backend((ModelResponse(Message.assistant("已看图"), usage=TokenUsage(12_345, 4)),)),
            original,
        )

        def cannot_reestimate_old_image(url: object) -> int:
            raise AssertionError("已有实际用量的旧图片不应再次解码估算。")

        monkeypatch.setattr(
            "purrcept_engine.domain.context.token_estimation._image_tokens",
            cannot_reestimate_old_image,
        )
        candidate = replace(original, messages=(*original.messages, Message.user("继续看上一张图")))
        measured = usage.estimate(payload(candidate))
        assert measured.observed_tokens == 12_345 and measured.new_tokens < 100
    finally:
        await store.close()


async def test_missing_zero_and_failed_usage_never_replace_valid_anchor(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "missing.sqlite3")
    try:
        usage = await meter(store)
        original = request()
        await invoke(
            store,
            usage,
            Backend((ModelResponse(Message.assistant("完成"), usage=TokenUsage(12_000, 3)),)),
            original,
        )
        previous = usage.anchor
        for response in (
            ModelResponse(Message.assistant("无用量")),
            ModelResponse(Message.assistant("零用量"), usage=TokenUsage(0, 2)),
        ):
            await invoke(store, usage, Backend((response,)), original)
            assert usage.anchor == previous
        with pytest.raises(ModelUnavailableError):
            await invoke(store, usage, Backend((ModelUnavailableError("offline"),)), original)
        assert usage.anchor == previous
    finally:
        await store.close()


async def test_dynamic_tail_is_new_material_and_does_not_accumulate_old_estimates(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "tail.sqlite3")
    original = replace(
        request(),
        reminders=(SystemReminder((TextBlock("旧动态状态"),), key="engine.committed_state"),),
    )
    try:
        usage = await meter(store)
        await invoke(
            store,
            usage,
            Backend((ModelResponse(Message.assistant("完成"), usage=TokenUsage(12_000, 3)),)),
            original,
        )
        updated = replace(
            original,
            reminders=(SystemReminder((TextBlock("新动态状态"),), key="engine.committed_state"),),
        )
        measured = usage.estimate(payload(updated))
        assert measured.observed_tokens == 12_000 and 0 < measured.new_tokens < 100
        await invoke(
            store,
            usage,
            Backend((ModelResponse(Message.assistant("完成"), usage=TokenUsage(12_020, 3)),)),
            updated,
        )
        again = usage.estimate(payload(original))
        assert again.observed_tokens == 12_020
        assert again.new_tokens < 100
    finally:
        await store.close()


async def test_usage_storage_failure_does_not_retry_successful_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await Store.open(tmp_path / "storage-failure.sqlite3")
    try:
        usage = await meter(store)

        async def refuse(binding: str, anchor: object) -> None:
            raise StorageError("用量保存失败", code="synthetic")

        monkeypatch.setattr(store, "put_usage_anchor", refuse)
        backend = Backend(
            (ModelResponse(Message.assistant("真实成功"), usage=TokenUsage(1234, 2)),)
        )
        with pytest.raises(StorageError, match="用量保存失败"):
            await invoke(store, usage, backend, request())
        records = await store.requests_for_turn("foreground", "usage-test")
        assert len(records) == 1 and records[0].state == "succeeded"
        assert backend.calls == 1 and usage.anchor is None
    finally:
        await store.close()
