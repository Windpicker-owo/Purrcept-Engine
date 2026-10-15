"""使用 LiteLLM 本地 tokenizer 计量完整请求，并补齐原生媒体参考量。

``count_litellm_capacity`` 由 Host 内置注册，仅在配置显式选择后调用。完整
JSON 的文字、工具 schema、回执及动态状态交给 LiteLLM；二进制媒体按独立
规则计量，不能把 base64 当正文分词。结果始终是本地 token 估计，不冒充
Gemini countTokens 或服务端 usage。模块导入不初始化 SDK 或访问网络。
"""

from __future__ import annotations

import base64
import binascii
import io
import math
import os
import wave
from collections.abc import Callable, Mapping
from copy import deepcopy
from importlib import import_module
from importlib.util import find_spec
from threading import RLock
from typing import cast

from PIL import Image, UnidentifiedImageError

from purrcept_engine.domain.context.capacity import (
    CapacityDeclaration,
    CapacityError,
    CapacityEstimate,
    GenerationCapacity,
)
from purrcept_engine.serialization import encode

RULE = "litellm-token-counter-v1"
GEMINI_MEDIA = "gemini-3-inline-v1"
_import_lock = RLock()


def count_litellm_capacity(
    payload: Mapping[str, object], declaration: CapacityDeclaration
) -> CapacityEstimate:
    """按显式规则计量请求副本，原始 SDK 请求及其媒体始终不变。

    Args:
        payload: 当前边界采用的业务 JSON 或完整模型请求材料。
        declaration: 待绑定或计量的不可变声明。

    Returns:
        包含计量方法与适用范围的估算，不是服务端实际 usage。

    Raises:
        CapacityError: 完整请求缺少适用计量依据、包含不支持材料或超出当前输入预算。

    Note:
        文本采用 LiteLLM 内置 cl100k tokenizer 的参考口径，适用于网关自定义
        模型别名；不声称拥有 Gemini 私有 tokenizer。完整 JSON 而非仅 messages
        参与计量，以保留任意工具 schema 字段。gemini-3-inline-v1 补入 WAV/MP3
        音频及单帧内联图片；其他媒体或分辨率覆盖明确拒绝，不能按零量放行。
    """
    if not isinstance(declaration, GenerationCapacity):
        raise CapacityError("LiteLLM 规则只适用于生成请求。", code="unsupported_rule")
    if declaration.media_rule not in ("unsupported", GEMINI_MEDIA):
        raise CapacityError("LiteLLM 规则没有此媒体依据。", code="unsupported_media")
    if "media_resolution" in payload:
        raise CapacityError("媒体分辨率覆盖需要独立计量依据。", code="unsupported_media")

    request = deepcopy(dict(payload))
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        raise CapacityError("生成请求缺少消息。", code="invalid_payload")
    media_tokens = 0
    for raw_message in cast(list[object], messages):
        message = _object(raw_message)
        content = message.get("content")
        if content is None or isinstance(content, str):
            continue
        if not isinstance(content, list):
            raise CapacityError("消息内容结构无效。", code="invalid_payload")
        for raw_part in cast(list[object], content):
            part = _object(raw_part)
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                continue
            if declaration.media_rule != GEMINI_MEDIA:
                raise CapacityError("当前规则未声明媒体计量依据。", code="unsupported_media")
            media_tokens += _gemini_media_tokens(part)

    counter = _local_counter()
    # 明确选择本地参考 tokenizer，而非让网关别名触发不透明的模型回退或
    # HuggingFace 下载。JSON 表示开销也计入，故不等同于远端精确 prompt tokens。
    tokens = counter(model="gpt-3.5-turbo", text=encode(request))
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
        raise CapacityError("LiteLLM tokenizer 未返回有效计量。", code="invalid_rule_result")
    return CapacityEstimate(
        tokens + media_tokens,
        RULE,
        "complete_sdk_json:litellm_cl100k_reference;" + declaration.media_rule,
        "token_estimate",
    )


def _local_counter() -> Callable[..., object]:
    """首次导入只读随包价格表，恢复调用者环境，不下载分词或价格资源。

    不同会话的预检在线程中进行；锁串行化临时环境设置与首次导入。
    已导入的 SDK 由 Python 复用，不改变其全局开关或其他宿主的计量配置。
    """
    with _import_lock:
        key = "LITELLM_LOCAL_MODEL_COST_MAP"
        previous = os.environ.get(key)
        os.environ[key] = "True"
        try:
            module = import_module("litellm")
        finally:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
    return cast(Callable[..., object], module.__dict__["token_counter"])


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CapacityError("媒体或消息结构无效。", code="invalid_payload")
    return cast(dict[str, object], value)


def _decode(value: object) -> bytes:
    """只解码请求内已有字节，不读取文件路径或远端 URL。"""
    if not isinstance(value, str) or not value:
        raise CapacityError("内联媒体缺少内容。", code="invalid_payload")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise CapacityError("内联媒体编码无效。", code="invalid_payload") from error


def _gemini_media_tokens(part: dict[str, object]) -> int:
    """替换计量副本中的二进制数据，以免重复计算 base64 文本。

    Gemini 3 的媒体分辨率文档规定音频每秒 25 token，默认图片 1120 token：
    https://ai.google.dev/gemini-api/docs/media-resolution 。规则版本固定此口径；
    它不改变模型能力、原始请求、网关协议或配置声明的上下文窗口。
    """
    if part.get("type") == "input_audio":
        audio = _object(part.get("input_audio"))
        data = _decode(audio.get("data"))
        duration = _audio_seconds(data, audio.get("format"))
        audio["data"] = "[audio bytes counted by duration]"
        return math.ceil(duration * 25)
    if part.get("type") == "image_url":
        image = _object(part.get("image_url"))
        url = image.get("url")
        if not isinstance(url, str) or not url.startswith("data:image/") or ";base64," not in url:
            raise CapacityError("图片计量只接受已接管的内联图片。", code="unsupported_media")
        data = _decode(url.split(";base64,", 1)[1])
        try:
            with Image.open(io.BytesIO(data)) as picture:
                if getattr(picture, "n_frames", 1) != 1:
                    raise CapacityError("动态图片需要视频计量依据。", code="unsupported_media")
                picture.verify()
        except (UnidentifiedImageError, OSError, ValueError) as error:
            raise CapacityError("内联图片损坏。", code="invalid_payload") from error
        image["url"] = "[image bytes counted by Gemini 3 default resolution]"
        return 1120
    raise CapacityError("此媒体类型尚无本地计量依据。", code="unsupported_media")


def _audio_seconds(data: bytes, audio_format: object) -> float:
    """从实际音频读取时长；损坏或缺少解码依赖不能变成零时长。

    WebUI 收音产出 PCM WAV，直接验证帧数与完整数据。MP3 使用 voice-input
    已声明的 PyAV 解码，累计有效样本；不信任可缺省的容器时长或调用 ffprobe。
    """
    try:
        if audio_format == "wav":
            with wave.open(io.BytesIO(data)) as audio:
                frames = audio.getnframes()
                frame_size = audio.getsampwidth() * audio.getnchannels()
                if len(audio.readframes(frames)) != frames * frame_size:
                    raise CapacityError("WAV 数据不完整。", code="invalid_payload")
                duration = frames / audio.getframerate()
        elif audio_format == "mp3":
            if find_spec("av") is None:
                raise CapacityError("MP3 计量需要 voice-input extra。", code="unsupported_media")
            import av
            from av.error import FFmpegError

            try:
                with av.open(io.BytesIO(data), mode="r", format="mp3") as container:
                    duration = sum(
                        frame.samples / frame.sample_rate for frame in container.decode(audio=0)
                    )
            except FFmpegError as error:
                raise CapacityError("MP3 解码失败。", code="invalid_payload") from error
        else:
            raise CapacityError("未知音频格式无法计量。", code="unsupported_media")
    except (wave.Error, EOFError, ValueError, ZeroDivisionError) as error:
        raise CapacityError("音频内容不能取得有效时长。", code="invalid_payload") from error
    if not math.isfinite(duration) or duration <= 0:
        raise CapacityError("音频时长必须为有限正数。", code="invalid_payload")
    return duration
