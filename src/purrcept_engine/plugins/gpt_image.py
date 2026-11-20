"""在显式行动中调用 OpenAI Images API，并把确认完成的图像交给同库媒体和附件通道。

目录将能力挂在指定 Computer 实例下。generate 发送文本，edit 用已接管的 media_id
或 OpenAI 可解析的图片来源发起编辑。
每次编辑显式指定当前底图，历史和后续发送由 Engine 管理。客户端由插件实例独占，
执行器等待实际调用结束后才归还。只有收到完整图像并接管原件才确认生图成功。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import re
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import uuid4
from xml.etree import ElementTree

import httpx
from PIL import Image
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from purrcept_engine.errors import ConfigError
from purrcept_engine.infrastructure.logging import redact_secrets
from purrcept_engine.plugin_system import (
    CallContext,
    CallOutcome,
    CapabilityDeclaration,
    CapabilityInputError,
    EvidencePort,
    EvidenceSnapshot,
    FunctionCapability,
    InputPort,
    MediaAsset,
    MediaReference,
    ObjectDeclaration,
    PluginBlueprint,
    PluginPorts,
    PluginRuntime,
    typed_factory,
)
from purrcept_engine.values import JsonObject, JsonValue, encode_json

_MEDIA_ID = re.compile(r"[0-9a-f]{64}")
_MIME = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "svg": "image/svg+xml",
}
_MIB = 1024 * 1024
# JSON 编辑入口限制单个 data URL 的长度。为 Base64 和 MIME 前缀预留空间。
_MAX_DATA_URL = 20 * _MIB
_MAX_REQUEST_IMAGE_BYTES = 64 * _MIB
_MAX_RESULT_BYTES = 128 * _MIB
_OutputFormat = Literal["png", "jpeg", "webp", "gif", "svg"]


class _Settings(BaseModel):
    """实例配置由宿主解析，凭据只进入该实例的 HTTP 客户端。"""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)

    api_key: SecretStr = Field(min_length=1)
    computer_id: str = Field(default="computer", pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    api_base: str = "https://api.openai.com/v1"
    model: str = Field(default="gpt-image-2.6-flare", min_length=1, pattern=r"\S")
    output_dir: str = Field(default=".purrcept/image-generation", min_length=1, pattern=r"\S")
    timeout_seconds: float = Field(default=150.0, gt=0, allow_inf_nan=False)
    moderation: Literal["auto", "low"] = "auto"
    safety_identifier: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("api_base")
    @classmethod
    def valid_base(cls, value: str) -> str:
        """只向显式 HTTP(S) API 基址发送客户端凭据。"""
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.query
            or url.fragment
        ):
            raise ValueError("api_base 必须是无凭据、查询或片段的 HTTP(S) API 基址。")
        return value.rstrip("/")

    @field_validator("api_key")
    @classmethod
    def nonblank_key(cls, value: SecretStr) -> SecretStr:
        """启动前识别未设置或全空白的密钥。"""
        key = value.get_secret_value().strip()
        if not key:
            raise ValueError("api_key 不能为空。")
        return SecretStr(key)


class _Options(BaseModel):
    """生成和编辑共用的 GPT Image 输出选项，省略字段时沿用服务端默认值。"""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    prompt: str = Field(min_length=1, max_length=32000, pattern=r"\S")
    model: str | None = Field(
        default=None,
        pattern=r"^\S+$",
        description="本次调用的 GPT Image 模型 ID，省略时使用插件配置。"
        "例如 gpt-image-2.6-flare 或 gpt-image-2.6-sunburst。",
    )
    size: str | None = Field(default=None, pattern=r"^(auto|[1-9][0-9]{0,3}x[1-9][0-9]{0,3})$")
    quality: Literal["auto", "low", "medium", "high", "xhigh", "max"] | None = None
    complexity: Literal["low", "medium", "high"] | None = None
    background: Literal["auto", "opaque", "transparent"] | None = None
    output_format: _OutputFormat = "png"
    output_compression: int | None = Field(default=None, ge=0, le=100)
    n: int = Field(default=1, ge=1, le=10)
    frame_count: int | None = Field(default=None, ge=2, le=16)
    frame_duration_ms: int | None = Field(default=None, ge=20, le=2000)
    partial_images: int = Field(
        default=0, ge=0, le=3, description="大于 0 时使用流式 API 并保存可取得的中间预览。"
    )
    include_media: bool = Field(
        default=False, description="主脑支持图片输入且需要检查成图时，设为 True 交付原生图像。"
    )

    @model_validator(mode="after")
    def compatible_output(self) -> Self:
        """把跨字段冲突挡在付费调用之前；模型专属限制由 API 校验。"""
        if self.output_compression is not None and self.output_format not in {"jpeg", "webp"}:
            raise ValueError("仅 JPEG 和 WebP 支持 output_compression。")
        if self.background == "transparent" and self.output_format not in {"png", "webp"}:
            raise ValueError("透明背景请使用 PNG 或 WebP。")
        if (self.frame_count is not None or self.frame_duration_ms is not None) and (
            self.output_format != "gif"
        ):
            raise ValueError("仅 GIF 支持帧数与帧时长。")
        if self.partial_images and (self.n != 1 or self.output_format in {"gif", "svg"}):
            raise ValueError("流式预览只支持单张 PNG、JPEG 或 WebP。")
        if self.output_format == "svg" and self.n != 1:
            raise ValueError("SVG 每次只支持生成一张。")
        return self


class _Generate(_Options):
    """由文字提示直接生成一张或多张图像。"""

    svg_model: str | None = Field(default=None, min_length=1, max_length=128)
    svg_reasoning: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None

    @model_validator(mode="after")
    def svg_options(self) -> Self:
        """SVG 绘制模型及推理设置仅用于矢量输出。"""
        if (self.svg_model or self.svg_reasoning) and self.output_format != "svg":
            raise ValueError("svg_model 和 svg_reasoning 仅用于 SVG 输出。")
        return self


class _InputImage(BaseModel):
    """引用一次入站消息中的图片，按原顺序从 0 开始编号。"""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    input_id: str = Field(min_length=1, max_length=128, pattern=r"\S")
    image_index: int = Field(default=0, ge=0)


type _ImageSource = str | _InputImage


class _Edit(_Options):
    """首张图提供底图，后续图片按参数顺序提供参考。"""

    images: list[_ImageSource] = Field(
        min_length=1,
        max_length=16,
        description="入站 {input_id, image_index}、media_id、HTTPS URL 或 OpenAI file ID。",
    )
    mask: _ImageSource | None = Field(default=None, description="首张图的透明 PNG 蒙版。")
    markup: _ImageSource | None = Field(default=None, description="首张图的透明 PNG 标注层。")
    input_fidelity: Literal["low", "high"] | None = Field(
        default=None, description="只在所配置的图像模型支持该参数时设置。"
    )

    @model_validator(mode="after")
    def compatible_edit(self) -> Self:
        """编辑 API 对矢量输出和区域引导组合有独立限制。"""
        if self.output_format == "svg":
            raise ValueError("请用 generate 生成 SVG，编辑输入需为位图。")
        if self.mask is not None and (self.markup is not None or self.output_format == "gif"):
            raise ValueError("蒙版不能与标注层或 GIF 输出同时使用。")
        return self


class _ImageAPIError(ValueError):
    """OpenAI 明确拒绝请求，或返回了无法确认为最终图像的响应。"""


class _ImageData(BaseModel):
    """API 图片条目只通过 Base64 交付实际成图。"""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    b64_json: str = Field(min_length=1)


class _Completion(BaseModel):
    """统一承载普通 JSON 结果和 SSE 最终事件的图片与用量。"""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    data: list[_ImageData] = Field(min_length=1, max_length=10)
    usage: JsonObject | None = None


class _RemoteError(BaseModel):
    """提取稳定错误码和可向调用方交代的原因，避免回显整个响应体。"""

    model_config = ConfigDict(hide_input_in_errors=True)

    code: str | None = None
    message: str = "远端未返回错误原因。"


class _Event(BaseModel):
    """保留流式事件中完整图片、可选用量和显式错误。"""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    type: str
    b64_json: str | None = None
    usage: JsonObject | None = None
    error: _RemoteError | None = None


def _content_type(content: bytes) -> tuple[str, int, int]:
    """核验位图的实际格式与尺寸，供编辑上传和生成结果共用。"""
    try:
        with Image.open(io.BytesIO(content)) as image:
            mime = _MIME.get((image.format or "").lower())
            if mime is None or mime == _MIME["svg"]:
                raise ValueError("图片编码必须为 PNG、JPEG、WebP 或 GIF。")
            width, height = image.size
            image.verify()
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError("读取不到完整有效的图片。") from error
    return mime, width, height


def _result_bytes(
    completion: _Completion, previews: list[str], output_format: _OutputFormat
) -> tuple[tuple[MediaAsset, ...], list[JsonObject]]:
    """验证整个回包再接管，按最终结果、预览的顺序命名；Worker 只拿引用。"""
    assets: list[MediaAsset] = []
    entries: list[JsonObject] = []
    total_bytes = 0
    encoded_images = [item.b64_json for item in completion.data] + previews
    for index, encoded in enumerate(encoded_images, start=1):
        if len(encoded) > 4 * ((_MAX_RESULT_BYTES - total_bytes + 2) // 3):
            raise _ImageAPIError("OpenAI 图片结果超过单次 128 MiB 交付上限。")
        try:
            content = base64.b64decode(encoded, validate=True)
            if not content:
                raise ValueError("图片内容为空。")
            entry: JsonObject = {}
            if output_format == "svg":
                # 矢量原件只按附件导出，不能通过主脑原生位图路径解释。
                if ElementTree.fromstring(content).tag != "{http://www.w3.org/2000/svg}svg":
                    raise ValueError("远端返回的根元素不是 SVG。")
            else:
                actual, width, height = _content_type(content)
                if actual != _MIME[output_format]:
                    raise ValueError(f"实际图片格式 {actual} 与请求格式 {output_format} 不符。")
                entry.update(width=width, height=height)
        except (ValueError, binascii.Error, ElementTree.ParseError) as error:
            raise _ImageAPIError(f"OpenAI 返回了无效图片（第 {index} 项）：{error}") from error
        total_bytes += len(content)
        if total_bytes > _MAX_RESULT_BYTES:
            raise _ImageAPIError("OpenAI 图片结果超过单次 128 MiB 交付上限。")
        if index <= len(completion.data):
            name = f"image-{index}.{output_format}"
        else:
            name = f"preview-{index - len(completion.data)}.{output_format}"
        assets.append(MediaAsset(content, _MIME[output_format], name))
        entries.append(entry)
    return tuple(assets), entries


def _export(directory: Path, assets: tuple[MediaAsset, ...]) -> None:
    """同一目录内暂存完整批次后改名，避免通道看到部分写出的图像。"""
    directory.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".writing-", dir=directory.parent) as temp_dir:
        staging = Path(temp_dir) / "images"
        staging.mkdir()
        for asset in assets:
            assert asset.name is not None
            (staging / asset.name).write_bytes(asset.content)
        staging.rename(directory)


class _ImageGeneration:
    """build 创建的调用实现，借用同库媒体并持有一个无自动重试的 HTTP 客户端。"""

    def __init__(
        self,
        settings: _Settings,
        output_dir: Path,
        evidence: EvidencePort,
        inputs: InputPort,
        client: httpx.AsyncClient,
    ) -> None:
        """保存已校验配置、媒体借用和本实例的客户端。"""
        self.settings = settings
        self.output_dir = output_dir
        self.evidence = evidence
        self.inputs = inputs
        self.client = client

    async def _image_reference(self, source: _ImageSource) -> tuple[JsonObject, int]:
        """把已接管的位图作为 data URL 上传；上游 URL 由 OpenAI 获取。"""
        if isinstance(source, _InputImage):
            record = await self.inputs.query_input(source.input_id)
            if record is None:
                raise CapabilityInputError(f"找不到入站消息 {source.input_id}。")
            photos = [
                reference for reference in record.media if reference.media_type.startswith("image/")
            ]
            if source.image_index >= len(photos):
                raise CapabilityInputError(f"入站消息没有第 {source.image_index + 1} 张图片。")
            source = photos[source.image_index].media_id
        if _MEDIA_ID.fullmatch(source):
            content = await self.evidence.get_media(source)
            if content is None:
                raise CapabilityInputError(f"同库中找不到图片 media_id {source}。")
            if 4 * ((len(content) + 2) // 3) + 30 > _MAX_DATA_URL:
                raise CapabilityInputError("单张图片超过 OpenAI JSON 编辑入口的 data URL 上限。")
            try:
                mime, _, _ = await asyncio.to_thread(_content_type, content)
            except ValueError as error:
                raise CapabilityInputError(f"媒体 {source} 无法作为编辑输入：{error}") from error
            if mime == "image/gif":
                raise CapabilityInputError("编辑输入必须为 PNG、JPEG 或 WebP。")
            encoded = await asyncio.to_thread(base64.b64encode, content)
            return {"image_url": f"data:{mime};base64,{encoded.decode('ascii')}"}, len(content)
        if re.fullmatch(r"file-[\w-]+", source, flags=re.ASCII):
            return {"file_id": source}, 0
        try:
            url = urlsplit(source)
            if url.scheme == "https" and url.hostname and url.username is None:
                return {"image_url": source}, 0
        except ValueError as error:
            raise CapabilityInputError("图片来源 URL 无效。") from error
        raise CapabilityInputError(
            "图片来源须为入站图片引用、同库 media_id、HTTPS URL 或 OpenAI file ID。"
        )

    async def _request(
        self, endpoint: str, payload: JsonObject, parameters: _Options
    ) -> tuple[_Completion, list[str], str | None]:
        """消费单次 HTTP 响应；中间帧、断流与最终完成分别判定。"""
        async with self.client.stream("POST", endpoint, json=payload) as response:
            request_id = response.headers.get("x-request-id")
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                await response.aread()
                try:
                    failure = _RemoteError.model_validate(response.json()["error"])
                except (ValueError, TypeError, KeyError):
                    raise _ImageAPIError(f"{error}（请求 ID：{request_id or '未知'}）") from error
                detail = failure.message[:2000]
                raise _ImageAPIError(
                    f"HTTP {response.status_code} {failure.code or ''}：{detail}"
                    f"（请求 ID：{request_id or '未知'}）"
                ) from error

            if not parameters.partial_images:
                # HTTPX 在此持有流；先检查边界，再将完整 JSON 交给响应模型。
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_RESULT_BYTES * 4 // 3 + _MIB:
                        raise _ImageAPIError("OpenAI 图片响应超过单次交付上限。")
                return _Completion.model_validate_json(body), [], request_id

            if response.headers.get("content-type", "").split(";")[0] != "text/event-stream":
                raise _ImageAPIError("OpenAI 未返回图片 SSE 流。")
            prefix = "image_edit" if endpoint == "images/edits" else "image_generation"
            previews: list[str] = []
            event_data: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    event_data.append(line[5:].lstrip(" "))
                if line or not event_data:
                    continue
                data = "\n".join(event_data)
                event_data.clear()
                if data == "[DONE]":
                    break
                event = _Event.model_validate_json(data)
                if event.error is not None:
                    raise _ImageAPIError(f"{event.error.code or 'SSE 错误'}：{event.error.message}")
                if event.type == prefix + ".partial_image" and event.b64_json:
                    if len(previews) < parameters.partial_images:
                        previews.append(event.b64_json)
                if event.type == prefix + ".completed" and event.b64_json:
                    complete = _Completion(
                        data=[_ImageData(b64_json=event.b64_json)], usage=event.usage
                    )
                    return complete, previews, request_id
            raise _ImageAPIError(
                f"图片流已结束，未取得最终成图（请求 ID：{request_id or '未知'}）。"
            )

    async def run(self, parameters: _Generate | _Edit, context: CallContext) -> CallOutcome:
        """发送声明的生成或编辑请求，接管图像后返回供查看和发送的稳定身份。"""
        editing = isinstance(parameters, _Edit)
        model = parameters.model or self.settings.model
        payload = parameters.model_dump(
            mode="json",
            exclude_none=True,
            exclude={"include_media", "images", "mask", "markup", "svg_model", "svg_reasoning"},
        )
        payload.update(model=model, moderation=self.settings.moderation)
        if self.settings.safety_identifier:
            payload["safety_identifier"] = self.settings.safety_identifier
        if parameters.partial_images:
            payload["stream"] = True
        else:
            del payload["partial_images"]
        if isinstance(parameters, _Edit):
            total_bytes = 0
            images: list[JsonValue] = []
            for source in parameters.images:
                reference, size = await self._image_reference(source)
                total_bytes += size
                if total_bytes > _MAX_REQUEST_IMAGE_BYTES:
                    raise CapabilityInputError("一次编辑最多上传 64 MiB 的本地图片原件。")
                images.append(reference)
            payload["images"] = images
            for key, layer in (("mask", parameters.mask), ("markup", parameters.markup)):
                if layer is not None:
                    reference, size = await self._image_reference(layer)
                    if total_bytes + size > _MAX_REQUEST_IMAGE_BYTES:
                        raise CapabilityInputError("一次编辑最多上传 64 MiB 的本地图片原件。")
                    image_url = reference.get("image_url")
                    if (
                        isinstance(image_url, str)
                        and image_url.startswith("data:")
                        and not image_url.startswith("data:image/png;")
                    ):
                        raise CapabilityInputError(f"{key} 需要 PNG。")
                    payload[key] = reference
        elif parameters.svg_model or parameters.svg_reasoning:
            svg: JsonObject = {}
            if parameters.svg_model:
                svg["model"] = parameters.svg_model
            if parameters.svg_reasoning:
                svg["reasoning"] = {"effort": parameters.svg_reasoning}
            payload["svg"] = svg

        # 不自动重试有费用的 POST。连接或整体期限耗尽后仍可能已在远端执行。
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                completion, previews, request_id = await self._request(
                    "images/edits" if editing else "images/generations", payload, parameters
                )
            if len(completion.data) != parameters.n:
                raise _ImageAPIError(
                    f"请求 {parameters.n} 张成图，OpenAI 返回 {len(completion.data)} 张。"
                )
            assets, entries = await asyncio.to_thread(
                _result_bytes, completion, previews, parameters.output_format
            )
        except (TimeoutError, httpx.TransportError) as error:
            return CallOutcome(
                "unknown", None, type(error).__name__, f"OpenAI 图片请求未确认完成：{error}"
            )
        except (_ImageAPIError, ValidationError) as error:
            key = self.settings.api_key.get_secret_value()
            reason = redact_secrets(str(error).replace(key, "<redacted>"))
            return CallOutcome("failed", None, "OpenAIImageError", reason)

        identity = "image-generation-" + uuid4().hex
        directory = self.output_dir / identity
        references = tuple(
            MediaReference(
                hashlib.sha256(asset.content).hexdigest(),
                asset.media_type,
                len(asset.content),
                asset.name,
            )
            for asset in assets
        )
        for entry, reference in zip(entries, references, strict=True):
            entry.update(
                media_id=reference.media_id,
                media_type=reference.media_type,
                byte_length=reference.byte_length,
            )
        outcome: JsonObject = {
            "images": [entry for entry in entries[: parameters.n]],
            "previews": [entry for entry in entries[parameters.n :]],
            "model": model,
            "request_id": request_id,
            "usage": completion.usage,
        }
        saved = await self.evidence.capture(
            EvidenceSnapshot(
                evidence_id=identity,
                version=1,
                kind="image.edit" if editing else "image.generate",
                text=encode_json(outcome).decode("utf-8"),
                source_json=encode_json(
                    {"call_id": context.call_id, "action_id": context.action_id}
                ).decode("utf-8"),
                occurred_at=time.time(),
                media=references,
            ),
            assets,
        )
        outcome["evidence_ref"] = saved.evidence_id
        visible = (
            saved.media[: parameters.n]
            if parameters.include_media and parameters.output_format != "svg"
            else ()
        )
        # 同库成图已经可靠；导出失败须保留 media_id，避免为取得同一张图重复计费。
        try:
            await asyncio.to_thread(_export, directory, assets)
        except OSError as error:
            outcome["export_error"] = str(error)
        else:
            for entry, asset in zip(entries, assets, strict=True):
                assert asset.name is not None
                entry["path"] = str(directory / asset.name)
        return CallOutcome("confirmed", outcome, media=visible)


def _declare(instance_id: str, settings: _Settings, base_dir: Path) -> PluginBlueprint:
    """在指定电脑实例下注册生成和编辑，并在 build 时借到同库原件。"""
    object_id = f"{instance_id}:gpt_image"
    output_dir = (base_dir / settings.output_dir).resolve()
    generate = CapabilityDeclaration(
        f"{object_id}.generate",
        object_id,
        "generate",
        "用 OpenAI GPT Image 从文字生成图像，返回同库媒体 ID 和可发送的文件路径。",
        _Generate,
        settings.timeout_seconds + 10,
        'result = await {path}(prompt="一只趴在窗边晒太阳的橘猫", size="1536x1024")',
        "可用 model 选择本次模型；对比多个模型时用相同参数分别调用。"
        "按需要指定尺寸、质量、背景、PNG/JPEG/WebP/GIF/SVG 格式或张数。"
        "用 images[i].path 发送文件，images[i].media_id 可继续编辑位图。"
        "需要让支持图片输入的主脑检查成图时，设置 include_media=True。",
    )
    edit = CapabilityDeclaration(
        f"{object_id}.edit",
        object_id,
        "edit",
        "依据一张底图和可选参考图编辑或重新生成图像。",
        _Edit,
        settings.timeout_seconds + 10,
        'result = await {path}(images=[media_id], prompt="保留橘猫，把背景换成星空")',
        "images 首张为底图，后续为参考。入站附件用 {'input_id': 输入 ID, 'image_index': 0} 引用；"
        "也接受已接管的 media_id、HTTPS 图片 URL、OpenAI file ID。"
        "本机图片可先用 me.view_image 取得 media_id。mask 或 markup 支持局部编辑；"
        "把本次 images[i].media_id 作为下次底图即可迭代。仅在用户需要时显式发送成图。",
    )

    async def build(ports: PluginPorts) -> PluginRuntime:
        """创建实例专用的 HTTP 客户端，凭据只用于此 API 地址。"""
        if ports.evidence is None:
            raise ConfigError("gpt_image 插件需要宿主原文/媒体端口。")
        client = httpx.AsyncClient(
            base_url=settings.api_base + "/",
            headers={"Authorization": "Bearer " + settings.api_key.get_secret_value()},
            timeout=settings.timeout_seconds,
            follow_redirects=False,
        )
        runtime = _ImageGeneration(settings, output_dir, ports.evidence, ports.inputs, client)
        return PluginRuntime(
            (FunctionCapability(generate, runtime.run), FunctionCapability(edit, runtime.run)),
            close=client.aclose,
        )

    return PluginBlueprint(
        (
            ObjectDeclaration(
                object_id, "gpt_image", "OpenAI 生图", f"{settings.computer_id}:computer"
            ),
        ),
        (generate, edit),
        build,
    )


factory = typed_factory(_Settings, _declare)
