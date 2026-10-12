"""主体默认的 URL/本地看图能力，取得像素后交给同库原文和媒体存储。

images_blueprint 只声明稳定 me.view_image 路径，不打开文件或连接网络。build
交回一个 ImageRuntime，独占 HTTP 客户端并持有读盘、格式检查和正式接管任务。
Worker 只收到来源与持久引用，后续主脑请求按 CallOutcome.media 展开原生像素。
取消观察不能释放仍在读盘或提交的资源；关闭等待真实任务再关闭客户端。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import httpx
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from purrcept_engine.errors import ConfigError, HostStateError
from purrcept_engine.execution.catalog import CapabilityDeclaration
from purrcept_engine.execution.protocol import JsonObject, encode_json
from purrcept_engine.execution.providers import (
    CallContext,
    CallOutcome,
    CapabilityInputError,
    FunctionCapability,
)
from purrcept_engine.storage.knowledge import EvidenceSnapshot
from purrcept_engine.storage.models import MediaAsset, MediaReference

from .base import EvidencePort, PluginBlueprint, PluginPorts, PluginRuntime

MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MEDIA_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}


class _ViewImage(BaseModel):
    """保留 source/media_id 参数；media_id 仅作来源关联，不替代实际像素。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    source: str = Field(
        min_length=1, pattern=r"\S", description="图片的 HTTP(S) URL 或本地文件路径。"
    )
    media_id: str = Field(default="", description="可选的原始刺激媒体 ID，不填写消息 ID。")


class ImageInputError(ValueError):
    """路径、格式或大小的可纠正读取拒绝，不包含权威提交故障。"""


def _local_bytes(source: str, base_dir: Path) -> bytes:
    """相对文件沿主配置基准解析，有界读取而不相信扩展名或文件 stat。"""
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    with path.open("rb") as stream:
        return stream.read(MAX_IMAGE_BYTES + 1)


def _image_properties(data: bytes) -> tuple[str, int, int]:
    """由实际编码核验受支持格式与尺寸，不修改像素或把 MIME 声明当证明。"""
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ImageInputError("图片必须非空且不超过 8 MiB。")
    try:
        with Image.open(io.BytesIO(data)) as image:
            media_type = _MEDIA_TYPES.get(image.format or "")
            if media_type is None:
                raise ImageInputError("仅支持 PNG、JPEG、GIF、WebP 图片。")
            width, height = image.size
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ImageInputError("未读取到完整受支持的图片数据。") from error
    return media_type, width, height


class ImageRuntime:
    """宿主默认看图资源，真实 I/O 与同库接管均由本实例持有。

    执行服务限定能力并发及期限。已开始读取不会因等待取消而失去所有者；
    超时回执和迟到结果由执行服务记录。close 撤销新调用、等待全部实际任务，
    再释放客户端；原文和像素已经归 Store，不随插件关闭删除。
    """

    def __init__(self, base_dir: Path, evidence: EvidencePort, client: httpx.AsyncClient) -> None:
        """接收已经明确取得的客户端和有限存储端口，不启动采集或后台轮询。"""
        self.base_dir, self.evidence, self.client = base_dir, evidence, client
        self._tasks: set[asyncio.Task[CallOutcome]] = set()
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def view_image(self, parameters: _ViewImage, context: CallContext) -> CallOutcome:
        """接管一次明确读取；取消调用者等待不撤销已开始读盘或权威接管。"""
        if self._closing:
            raise HostStateError("看图资源正在关闭。")
        task = asyncio.create_task(self._read(parameters, context), name="image:" + context.call_id)
        self._tasks.add(task)
        task.add_done_callback(self._read_done)
        return await asyncio.shield(task)

    def _read_done(self, task: asyncio.Task[CallOutcome]) -> None:
        """实际读取结束才收回所有者，消费无人等待的异常而不制造成功回执。"""
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _remote_bytes(self, source: str) -> bytes:
        """有限 HTTP(S) 重定向和有界流读取，未知协议在下一请求前拒绝。"""
        url = httpx.URL(source)
        if url.scheme not in {"http", "https"}:
            raise ImageInputError("图片 URL 仅支持 HTTP(S)。")
        async with asyncio.timeout(15.0):
            for _ in range(11):
                async with self.client.stream("GET", url, follow_redirects=False) as response:
                    if response.is_redirect and "location" in response.headers:
                        redirected = response.url.join(response.headers["location"])
                        if redirected.scheme not in {"http", "https"}:
                            raise ImageInputError("图片重定向仅支持 HTTP(S)。")
                        url = redirected
                        continue
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        if len(content) + len(chunk) > MAX_IMAGE_BYTES:
                            raise ImageInputError("图片必须非空且不超过 8 MiB。")
                        content.extend(chunk)
                    return bytes(content)
        raise ImageInputError("图片重定向次数超过有限读取上限。")

    async def _read(self, parameters: _ViewImage, context: CallContext) -> CallOutcome:
        """先核验取得的完整像素，再原子接管；存储失败不冒充读取参数错误。"""
        source = parameters.source.strip()
        try:
            if source.startswith(("http://", "https://")):
                data = await self._remote_bytes(source)
            else:
                data = await asyncio.to_thread(_local_bytes, source, self.base_dir)
            media_type, width, height = await asyncio.to_thread(_image_properties, data)
        except (ImageInputError, OSError, httpx.HTTPError, httpx.InvalidURL, TimeoutError) as error:
            raise CapabilityInputError(str(error)) from error

        # Ownership: 同库确认前不向 Worker 报可用媒体；引用是实际 BLOB 摘要，
        # 模型可选的 media_id 只保留关联，不能替换新图像或授予来源资格。
        reference = MediaReference(hashlib.sha256(data).hexdigest(), media_type, len(data))
        metadata: JsonObject = {
            "source": source,
            "call_id": context.call_id,
            "action_id": context.action_id,
            "input_id": context.input_id,
            "width": width,
            "height": height,
        }
        if parameters.media_id:
            metadata["origin_media_id"] = parameters.media_id
        saved = await self.evidence.capture(
            EvidenceSnapshot(
                evidence_id="image-" + uuid4().hex,
                version=1,
                kind="image.view",
                text="看图能力实际取得的原始图像。",
                source_json=encode_json(metadata).decode("utf-8"),
                occurred_at=time.time(),
                media=(reference,),
            ),
            (MediaAsset(data, media_type),),
        )
        return CallOutcome(
            "confirmed",
            {
                "source": source,
                "media_type": media_type,
                "size": len(data),
                "width": width,
                "height": height,
                "resource_status": "available",
                "evidence_ref": saved.evidence_id,
                "version": saved.version,
            },
            media=saved.media,
        )

    def snapshot(self) -> JsonObject:
        """只读资源阶段与实际在途数量，不读文件或重新下载媒体。"""
        return {
            "state": "closed" if self._closed else "closing" if self._closing else "ready",
            "pending_reads": len(self._tasks),
        }

    @property
    def resources_released(self) -> bool:
        """读取客户端真实关闭明证，不用在途数量推断连接池已经归还。"""
        return self._closed

    async def close(self) -> None:
        """等待同一次真实收尾，调用者取消不能提前关闭客户端。"""
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="image-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        """已知读取拒绝已经属于逐次回执，仍须保留内部或权威提交故障。"""
        pending = tuple(self._tasks)
        results = await asyncio.gather(*pending, return_exceptions=True)
        failures = [
            result
            for result in results
            if isinstance(result, BaseException) and not isinstance(result, CapabilityInputError)
        ]
        try:
            await self.client.aclose()
        except BaseException as error:
            failures.append(error)
        else:
            self._closed = True
        if failures:
            raise BaseExceptionGroup("看图资源或正式接管收尾失败。", failures)


def images_blueprint(
    base_dir: Path,
    *,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> PluginBlueprint:
    """声明宿主默认 me.view_image，不要求通过 TOML 另启一个图像插件。

    app 将此 Blueprint 与 subject 对象同时装配，并借出同库 EvidencePort。
    client_factory 只用于明确的传输替换；生产客户端不开隐式重试或自动重定向。
    """
    if not base_dir.is_absolute():
        raise ConfigError("看图能力必须提供绝对的主配置基准目录。")
    declaration = CapabilityDeclaration(
        "engine:view_image",
        "subject",
        "view_image",
        "读取图片 URL 或本地文件，将真实图像块提供给后续视觉模型请求。",
        _ViewImage,
        20.0,
        'await me.view_image(source="https://example.com/cat.png")',
        "需要辨认画面时调用。支持 PNG/JPEG/GIF/WebP，最大 8 MiB；"
        "相对路径以配置目录为基准。media_id 仅关联原始刺激，不填写消息 ID。"
        "取得媒体回执后，再依据实际像素决定依赖画面内容的后续行动。",
    )

    async def build(ports: PluginPorts) -> PluginRuntime:
        evidence = ports.evidence
        if evidence is None:
            raise ConfigError("默认看图能力需要宿主原文/媒体端口。")
        client = (
            httpx.AsyncClient(
                timeout=15.0,
                trust_env=False,
                headers={"User-Agent": "Purrcept-Engine/0.1"},
            )
            if client_factory is None
            else client_factory()
        )
        runtime = ImageRuntime(base_dir, evidence, client)
        return PluginRuntime(
            (FunctionCapability(declaration, runtime.view_image),),
            close=runtime.close,
            snapshot=runtime.snapshot,
            resources_released=lambda: runtime.resources_released,
        )

    return PluginBlueprint((), (declaration,), build)
