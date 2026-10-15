"""用合成图像、离线 HTTP 和真实临时 Store/Worker验证默认看图交接。

不访问外部网站或模型。重点核对字节、持久引用和原生材料，而不是把图片
路径或 MIME 响应当成功；取消测试证明读盘未结束时客户端和关闭仍有所有者。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from PIL import Image
from purrcept_core.models import ImageBlock, ImageBytes

from purrcept_engine.app.config import ExecutionSettings
from purrcept_engine.app.evidence import EvidenceAccess
from purrcept_engine.context.materials import media_blocks
from purrcept_engine.errors import ConfigError, StorageError
from purrcept_engine.execution.catalog import Catalog, ObjectDeclaration
from purrcept_engine.execution.providers import CallContext, CapabilityInputError
from purrcept_engine.execution.service import ExecutionService
from purrcept_engine.plugins import images
from purrcept_engine.plugins.base import PluginPorts, PluginRuntime
from purrcept_engine.storage.knowledge import EvidenceSnapshot
from purrcept_engine.storage.models import Admission, InputRecord, InputSubmission, MediaAsset
from purrcept_engine.storage.store import Store
from purrcept_engine.storage.subject import InputProcessing


def pixels(format: str = "PNG") -> bytes:
    """小型有效编码用于验证原样交付，不依赖用户文件。"""
    with Image.new("RGB", (17, 9), (10, 20, 30)) as image, io.BytesIO() as output:
        image.save(output, format=format)
        return output.getvalue()


class Inputs:
    """看图不得制造新外界输入；真实媒体只通过 EvidencePort 接管。"""

    async def submit(self, submission: InputSubmission) -> Admission:
        """显式工具读取不属于新的外界发言。"""
        raise AssertionError("显式看图不得提交前台输入。")

    async def query_input(self, input_id: str) -> InputRecord | None:
        """可选媒体来源只作归因，不需要访问输入状态。"""
        raise AssertionError("显式看图无需查询外界输入。")

    async def query_processing(self, input_id: str) -> InputProcessing | None:
        """取得像素不依赖主体先前处理结论。"""
        raise AssertionError("显式看图无需查询主体处理。")


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    """每场景独占真实临时库，在看图资源归还后收尾。"""
    ledger = await Store.open(tmp_path / "data")
    try:
        yield ledger
    finally:
        await ledger.close()


def ports(store: Store) -> PluginPorts:
    """仅借同库原文/媒体门面，不给看图能力通用可变数据库。"""
    return PluginPorts(Inputs(), lambda: {}, EvidenceAccess(store.knowledge, 8000))


def context() -> CallContext:
    """创建有限离线调用身份，不以路径或模型参数猜测因果关系。"""
    return CallContext("test-call", "test-action", asyncio.get_running_loop().time() + 20)


def offline_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    """替换测试基线的全 HTTP 拒绝钩子，只允许本场景合成响应。"""

    def selected(client: httpx.AsyncClient, url: httpx.URL) -> httpx.MockTransport:
        return transport

    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", selected)


async def close(runtime: PluginRuntime) -> None:
    """等待插件真实收尾，不能在仍持有字节任务时关闭共享库。"""
    assert runtime.close is not None
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["PNG", "JPEG", "GIF", "WEBP"])
async def test_local_images_keep_original_bytes_and_native_content(
    store: Store, tmp_path: Path, format: str
) -> None:
    """四种实际编码均原样保存，本地路径沿主配置目录解释。"""
    data = pixels(format)
    (tmp_path / "picture.untrusted-extension").write_bytes(data)
    runtime = await images.images_blueprint(tmp_path).build(ports(store))
    try:
        result = await runtime.capabilities[0].invoke(
            {"source": "picture.untrusted-extension", "media_id": "original-stimulus"}, context()
        )
        assert result.state == "confirmed"
        assert isinstance(result.value, dict)
        assert result.value["width"] == 17 and result.value["height"] == 9
        assert result.value["resource_status"] == "available"
        assert len(result.media) == 1
        assert await store.get_media(result.media[0].media_id) == data
        blocks = await media_blocks(store, result.media)
        assert blocks == (ImageBlock(ImageBytes(data, result.media[0].media_type)),)
        saved = await store.knowledge.read_evidence(str(result.value["evidence_ref"]))
        assert saved.snapshot is not None
        assert '"origin_media_id":"original-stimulus"' in saved.snapshot.source_json
        assert not await store.pending()
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_http_redirect_keeps_pixels_and_ignores_claimed_mime(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP 只决定如何取得字节，实际编码决定媒体类型。"""
    data = pixels()
    urls: list[str] = []

    def response(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/pixel"})
        return httpx.Response(200, content=data, headers={"content-type": "text/plain"})

    transport = httpx.MockTransport(response)
    offline_transport(monkeypatch, transport)
    runtime = await images.images_blueprint(tmp_path).build(ports(store))
    try:
        result = await runtime.capabilities[0].invoke(
            {"source": "https://image.test/start"}, context()
        )
        assert result.state == "confirmed"
        assert urls == ["https://image.test/start", "https://image.test/pixel"]
        assert result.media[0].media_type == "image/png"
        assert await store.get_media(result.media[0].media_id) == data
    finally:
        await close(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["redirect", "large", "html", "http_error", "loop"])
async def test_remote_failures_do_not_publish_media(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """协议、大小、格式和平台拒绝都不能产生可用媒体回执。"""
    requests = 0

    def response(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if scenario == "redirect":
            return httpx.Response(302, headers={"location": "file:///private.png"})
        if scenario == "loop":
            return httpx.Response(302, headers={"location": "/loop"})
        if scenario == "large":
            return httpx.Response(200, content=b"x" * (images.MAX_IMAGE_BYTES + 1))
        if scenario == "http_error":
            return httpx.Response(404)
        return httpx.Response(
            200, content=b"<html>not pixels</html>", headers={"content-type": "image/png"}
        )

    transport = httpx.MockTransport(response)
    offline_transport(monkeypatch, transport)
    runtime = await images.images_blueprint(tmp_path).build(ports(store))
    try:
        with pytest.raises(CapabilityInputError):
            await runtime.capabilities[0].invoke({"source": "https://image.test/start"}, context())
        assert requests == (11 if scenario == "loop" else 1)
        assert await store.get_media(hashlib.sha256(pixels()).hexdigest()) is None
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_capture_failure_is_not_translated_to_input_error(
    store: Store, tmp_path: Path
) -> None:
    """权威故障保留其类型，不能诱使模型重填已合法参数。"""

    class BrokenEvidence(EvidenceAccess):
        async def capture(
            self, snapshot: EvidenceSnapshot, assets: tuple[MediaAsset, ...]
        ) -> EvidenceSnapshot:
            """注入真实接管失败，不以内存媒体仓库补齐。"""
            raise StorageError("synthetic authority failure", code="invalid_record")

    (tmp_path / "local.png").write_bytes(pixels())
    runtime = await images.images_blueprint(tmp_path).build(
        PluginPorts(Inputs(), lambda: {}, BrokenEvidence(store.knowledge, 8000))
    )
    try:
        with pytest.raises(StorageError, match="synthetic authority"):
            await runtime.capabilities[0].invoke({"source": "local.png"}, context())
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_cancelled_read_keeps_client_until_actual_capture_finishes(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """观察取消后，关闭仍等待线程读盘和同库接管的真实结束。"""
    entered, release = threading.Event(), threading.Event()
    data = pixels()

    def delayed_read(source: str, base_dir: Path) -> bytes:
        entered.set()
        assert release.wait(5)
        return data

    monkeypatch.setattr(images, "_local_bytes", delayed_read)
    client = httpx.AsyncClient(trust_env=False)
    runtime = await images.images_blueprint(tmp_path, client_factory=lambda: client).build(
        ports(store)
    )
    reading = asyncio.create_task(runtime.capabilities[0].invoke({"source": "slow.png"}, context()))
    closing: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
        closing = asyncio.create_task(close(runtime))
        await asyncio.sleep(0)
        assert not closing.done() and not client.is_closed
        assert runtime.snapshot is not None and runtime.snapshot()["pending_reads"] == 1
        assert runtime.prepare_request is not None
        assert await runtime.prepare_request() == {"state": "closing"}
        release.set()
        await closing
        assert client.is_closed
        assert runtime.snapshot()["pending_reads"] == 0
        assert await store.get_media(hashlib.sha256(data).hexdigest()) == data
        await close(runtime)
    finally:
        release.set()
        if closing is not None:
            await closing
        else:
            await close(runtime)


@pytest.mark.asyncio
async def test_real_worker_receipt_has_no_pixels_but_media_expands_from_same_store(
    store: Store, tmp_path: Path
) -> None:
    """生产 Worker 只收 JSON，主脑原生像素从同一真实 BLOB 展开。"""
    data = pixels()
    (tmp_path / "local.png").write_bytes(data)
    blueprint = images.images_blueprint(tmp_path)
    runtime = await blueprint.build(ports(store))
    catalog = Catalog((ObjectDeclaration("subject", "me", "主体"),), blueprint.capabilities)
    execution = ExecutionService(
        catalog, runtime.capabilities, store, ExecutionSettings(), scratch_root=tmp_path / "scratch"
    )
    try:
        result = await execution.execute(
            'from world import me\nreturn await me.view_image(source="local.png")'
        )
        assert result.state == "completed" and result.calls[0].state == "confirmed"
        assert result.result_json is not None and len(result.result_json) < 1000
        assert "base64" not in result.result_json
        assert result.calls[0].media
        assert await media_blocks(store, result.calls[0].media) == (
            ImageBlock(ImageBytes(data, "image/png")),
        )
    finally:
        await execution.close()
        await close(runtime)


def test_default_blueprint_requires_config_base_and_does_not_open_clients(
    tmp_path: Path,
) -> None:
    """完整目录可在配置检查中生成，客户端只在 build 取得。"""

    def forbidden_client() -> httpx.AsyncClient:
        raise AssertionError("纯声明不得创建客户端。")

    blueprint = images.images_blueprint(tmp_path, client_factory=forbidden_client)
    catalog = Catalog((ObjectDeclaration("subject", "me", "主体"),), blueprint.capabilities)
    assert catalog.resolve("me.view_image").declaration.timeout_seconds == 20
    assert '"media_id"' in catalog.instructions
    with pytest.raises(ConfigError, match="绝对"):
        images.images_blueprint(Path("relative"))
