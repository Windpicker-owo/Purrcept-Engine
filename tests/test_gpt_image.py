"""离线验证生图、编辑、失败和文件交付的真实插件路径。"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from purrcept_core.models import ImageBlock, ImageBytes, TextBlock
from test_images import Inputs, close, context, offline_transport, pixels, ports, store

from purrcept_engine.app.assembly import PreparedPlugins, prepare_plugins
from purrcept_engine.app.config import ExecutionSettings, PluginSettings
from purrcept_engine.domain.context.materials import media_blocks
from purrcept_engine.domain.execution.catalog import Catalog
from purrcept_engine.domain.execution.providers import CapabilityInputError
from purrcept_engine.domain.inputs.models import (
    InputRecord,
    InputSource,
    InputSubmission,
    MediaAsset,
)
from purrcept_engine.infrastructure.execution.service import ExecutionService
from purrcept_engine.infrastructure.sqlite.store import Store

# 这些测试与看图共用真实临时库 fixture 及离线 HTTP 接缝。
__all__ = ["store"]


def _configured(tmp_path: Path) -> PreparedPlugins:
    """使用对用户开放的内置插件入口，一并检验 Catalog 装配。"""
    return prepare_plugins(
        (
            PluginSettings(instance_id="computer", entry="computer"),
            PluginSettings.model_validate(
                {
                    "instance_id": "gpt_image",
                    "entry": "gpt_image",
                    "config": {"api_key": "test-only-image-key"},
                }
            ),
        ),
        base_dir=tmp_path,
        factories={},
    )


@pytest.mark.parametrize("entry", ["gpt_image", "image_generation"])
def test_mounts_under_configured_computer_instance(tmp_path: Path, entry: str) -> None:
    """按 Computer 实例绑定父节点，由目录采用该设备的可见路径。"""
    computer = PluginSettings.model_validate(
        {
            "instance_id": "work_device",
            "entry": "computer",
            "config": {"computer_id": "desktop"},
        }
    )
    image = PluginSettings.model_validate(
        {
            "instance_id": "gpt_image",
            "entry": entry,
            "config": {"computer_id": "work_device", "api_key": "test-only-image-key"},
        }
    )
    catalog = prepare_plugins((image, computer), base_dir=tmp_path, factories={}).catalog
    assert "me.desktop.gpt_image.generate" in catalog.bindings
    assert "me.desktop.gpt_image.edit" in catalog.bindings
    assert "me.image_generation.generate" not in catalog.bindings


@pytest.mark.asyncio
async def test_worker_generates_and_edits_with_persisted_image(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker 可对比默认和指定模型，再复用指定模型及首张图的媒体 ID。"""
    data = pixels()
    encoded = base64.b64encode(data).decode("ascii")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"data": [{"b64_json": encoded}], "usage": {"total_tokens": 32}},
            headers={"x-request-id": "req-test"},
        )

    offline_transport(monkeypatch, httpx.MockTransport(respond))
    prepared = _configured(tmp_path)
    blueprint = dict(prepared.blueprints)["gpt_image"]
    runtime = await blueprint.build(ports(store))
    execution = ExecutionService(
        Catalog(tuple(prepared.catalog.objects.values()), blueprint.capabilities),
        runtime.capabilities,
        store,
        ExecutionSettings(),
        scratch_root=tmp_path / "scratch",
    )
    try:
        result = await execution.execute(
            "from world import me\n"
            "original = await me.computer.gpt_image.generate("
            'prompt="橘猫", quality="low", include_media=True)\n'
            "comparison = await me.computer.gpt_image.generate("
            'prompt="橘猫", quality="low", model="gpt-image-2.6-sunburst")\n'
            'return await me.computer.gpt_image.edit(prompt="换成星空背景", images=['
            'original["images"][0]["media_id"], "https://example.com/style.png", "file-guide"'
            '], markup="file-annotation", include_media=False, model=comparison["model"])'
        )
        assert result.state == "completed", result
        assert [call.state for call in result.calls] == ["confirmed", "confirmed", "confirmed"]
        assert [request.url.path for request in requests] == [
            "/v1/images/generations",
            "/v1/images/generations",
            "/v1/images/edits",
        ]
        assert requests[0].headers["Authorization"] == "Bearer test-only-image-key"
        generated = json.loads(requests[0].content)
        assert generated["model"] == "gpt-image-2.6-flare" and generated["quality"] == "low"
        compared = json.loads(requests[1].content)
        assert compared == {**generated, "model": "gpt-image-2.6-sunburst"}
        edited = json.loads(requests[2].content)
        assert edited["model"] == "gpt-image-2.6-sunburst"
        assert edited["images"] == [
            {"image_url": "data:image/png;base64," + encoded},
            {"image_url": "https://example.com/style.png"},
            {"file_id": "file-guide"},
        ]
        assert edited["markup"] == {"file_id": "file-annotation"}
        assert "include_media" not in edited
        assert result.result_json is not None and encoded not in result.result_json
        value = json.loads(result.result_json)
        assert value["model"] == "gpt-image-2.6-sunburst"
        image = value["images"][0]
        assert Path(image["path"]).is_relative_to(tmp_path / ".purrcept" / "image-generation")
        assert await asyncio.to_thread(Path(image["path"]).read_bytes) == data
        assert await store.get_media(image["media_id"]) == data
        assert value["request_id"] == "req-test" and value["usage"] == {"total_tokens": 32}
        assert result.calls[1].media == ()
        assert await media_blocks(store, result.calls[0].media) == (
            ImageBlock(ImageBytes(data, "image/png")),
        )
    finally:
        await execution.close()
        await close(runtime)


@pytest.mark.asyncio
async def test_edit_selects_image_from_an_admitted_input(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """图片序号跳过其他类型附件，按真实入站记录提取底图。"""
    data = pixels()
    encoded = base64.b64encode(data).decode("ascii")
    now = datetime.now(UTC)
    incoming = await store.admit(
        InputSubmission(
            InputSource("test", "account", "chat", "message"),
            "",
            now,
            now,
            (MediaAsset(b"audio", "audio/wav"), MediaAsset(data, "image/png")),
        )
    )

    class StoredInputs(Inputs):
        """测试借用真实输入查询，避免依赖端口替身构造媒体引用。"""

        async def query_input(self, input_id: str) -> InputRecord | None:
            """从正式接管记录取得待编辑图片的稳定引用。"""
            return await store.get_input(input_id)

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-image-2.6-flare"
        assert payload["images"] == [{"image_url": "data:image/png;base64," + encoded}]
        assert payload["mask"] == {"file_id": "file-mask"}
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    offline_transport(monkeypatch, httpx.MockTransport(respond))
    blueprint = dict(_configured(tmp_path).blueprints)["gpt_image"]
    runtime = await blueprint.build(replace(ports(store), inputs=StoredInputs()))
    try:
        result = await runtime.capabilities[1].invoke(
            {
                "images": [{"input_id": incoming.input_id}],
                "prompt": "重绘高亮区域",
                "mask": "file-mask",
            },
            context(),
        )
        assert result.state == "confirmed"
    finally:
        await close(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False])
async def test_stream_preview_does_not_replace_final(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, complete: bool
) -> None:
    """只有 completed 事件能确认流结果，中间图在成功时独立交付。"""
    encoded = base64.b64encode(pixels()).decode("ascii")

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["partial_images"] == 2 and payload["stream"] is True
        events = [
            {"type": "image_generation.partial_image", "b64_json": encoded},
        ]
        if complete:
            events.append({"type": "image_generation.completed", "b64_json": encoded})
        sse = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    offline_transport(monkeypatch, httpx.MockTransport(respond))
    runtime = await dict(_configured(tmp_path).blueprints)["gpt_image"].build(ports(store))
    try:
        result = await runtime.capabilities[0].invoke(
            {"prompt": "猫", "partial_images": 2, "include_media": True}, context()
        )
        if complete:
            assert result.state == "confirmed" and isinstance(result.value, dict)
            previews = result.value["previews"]
            assert isinstance(previews, list) and len(previews) == 1
            assert isinstance(previews[0], dict)
            assert await asyncio.to_thread(Path(str(previews[0]["path"])).read_bytes) == pixels()
            assert len(result.media) == 1
        else:
            assert result.state == "failed" and "最终成图" in (result.error or "")
            assert not (tmp_path / ".purrcept").exists()
    finally:
        await close(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["gif", "svg"])
async def test_new_output_formats_are_delivered_as_complete_files(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, format: str
) -> None:
    """SVG 只作为附件交付，GIF 可沿用现有原生位图通道。"""
    data = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path d="M0 0"/></svg>'
        if format == "svg"
        else pixels("GIF")
    )
    arguments = (
        {"svg_reasoning": "low", "svg_model": "gpt-6.2-astra"}
        if format == "svg"
        else {"frame_count": 3, "frame_duration_ms": 200}
    )

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["output_format"] == format
        if format == "svg":
            assert payload["svg"] == {"model": "gpt-6.2-astra", "reasoning": {"effort": "low"}}
        else:
            assert payload["frame_count"] == 3 and payload["frame_duration_ms"] == 200
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(data).decode()}]})

    offline_transport(monkeypatch, httpx.MockTransport(respond))
    runtime = await dict(_configured(tmp_path).blueprints)["gpt_image"].build(ports(store))
    try:
        result = await runtime.capabilities[0].invoke(
            {"prompt": "猫", "output_format": format, "include_media": True, **arguments}, context()
        )
        assert result.state == "confirmed" and isinstance(result.value, dict)
        images = result.value["images"]
        assert isinstance(images, list) and isinstance(images[0], dict)
        path = Path(str(images[0]["path"]))
        assert path.suffix == "." + format
        assert await asyncio.to_thread(path.read_bytes) == data
        assert bool(result.media) == (format == "gif")
        if format == "svg":
            page = await store.evidence.read_evidence(str(result.value["evidence_ref"]))
            blocks = await media_blocks(store, page.snapshot.media)
            assert isinstance(blocks[0], TextBlock)
            assert str(images[0]["media_id"]) in blocks[0].text
    finally:
        await close(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["reject", "timeout", "missing"])
async def test_failure_has_no_output_and_no_automatic_retry(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """缺图先于请求失败，远端错误保留详情，超时保留未确认状态。"""
    requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if scenario == "timeout":
            raise httpx.ReadTimeout("mock timeout", request=request)
        return httpx.Response(
            403,
            json={"error": {"code": "verification_required", "message": "Verify project"}},
            headers={"x-request-id": "req-denied"},
        )

    offline_transport(monkeypatch, httpx.MockTransport(respond))
    runtime = await dict(_configured(tmp_path).blueprints)["gpt_image"].build(ports(store))
    try:
        if scenario == "missing":
            with pytest.raises(CapabilityInputError, match="找不到"):
                await runtime.capabilities[1].invoke(
                    {"prompt": "换背景", "images": ["0" * 64]}, context()
                )
            assert requests == 0
        else:
            result = await runtime.capabilities[0].invoke({"prompt": "猫"}, context())
            assert requests == 1 and not result.media
            if scenario == "reject":
                assert result.state == "failed"
                assert result.error is not None
                assert "Verify project" in result.error and "req-denied" in result.error
            else:
                assert result.state == "unknown"
        assert not (tmp_path / ".purrcept").exists()
    finally:
        await close(runtime)
