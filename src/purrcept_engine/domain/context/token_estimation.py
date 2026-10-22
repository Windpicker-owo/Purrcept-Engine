"""为全部模型提供同一文本、图片和音频 token 启发式估算。

estimate_tokens 只读取已编译 SDK 请求：从内容槽位分离媒体，以字符数估算
剩余 JSON，用图片分块和音频时长补入媒体占用。文件编码、压缩率和模型别名
不决定估计值，base64 不参与文本计数。Pillow 与 PyAV 只读取内存中的原件；
本模块不下载 URL、不读取文件路径、不拥有传输能力或容量预算。

这些系数是本地规划用的统一假设，不是任一 Provider 的精确 tokenizer，也
不承诺上界。无法读取的媒体或未知内容明确失败；远端图片无法离线取得尺寸，采用
固定 2048 token 规划额度。所有临时容器在本次同步调用内释放，无共享缓存。
"""

from __future__ import annotations

import base64
import binascii
import io
import math
from collections.abc import Mapping
from typing import cast

from PIL import Image, UnidentifiedImageError

from purrcept_engine.serialization import encode

# 采用固定、易解释的规划尺度，不维护模型版本表。图片基础额度覆盖视觉
# 编码开销，分块数保留分辨率增长；音频参考常见每秒数十 token 的数量级。
# 实际服务端口径见 https://ai.google.dev/gemini-api/docs/tokens 与
# https://developers.openai.com/api/docs/guides/images-vision ，本地系数不承诺上界。
_IMAGE_TILE_SIZE = 512
_IMAGE_BASE_TOKENS = 256
_IMAGE_TILE_TOKENS = 256
_REMOTE_IMAGE_TOKENS = 2048
_AUDIO_TOKENS_PER_SECOND = 32


def estimate_tokens(payload: Mapping[str, object]) -> int:
    """用固定的多模态启发式估算完整请求，不修改原始材料。

    Args:
        payload: 编译完成并已验证为有限 JSON 的 SDK 请求，含生成 messages
            或专用判断 input；工具 schema 与其他公开请求字段也计入文本估算。

    Returns:
        文本与媒体合计的参考 token 数。ASCII 每四字符约一 token，其他字符
        各约一 token；图片为 256 基础加每个 512 像素方块 256 token；音频
        每秒约 32 token。JSON 结构开销保留，但媒体数据改为不计正文的空串。

    Raises:
        ValueError: 内容槽位、媒体编码或实际文件不能取得有效的估算依据。

    Note:
        所有模型使用同一组系数，不查询模型信息或使用特定 Provider 公式。
        函数可在线程中并发调用；调用期间调用者不得修改请求。
    """
    request = dict(payload)
    media_tokens = 0
    for field in ("messages", "input"):
        if field in request:
            request[field], tokens = _project_messages(request[field])
            media_tokens += tokens

    text = encode(request)
    ascii_characters = sum(character.isascii() for character in text)
    other_characters = len(text) - ascii_characters
    text_tokens = math.ceil(ascii_characters / 4) + other_characters
    return text_tokens + media_tokens


def _project_messages(value: object) -> tuple[object, int]:
    """只复制实际消息的内容槽位，避免把工具 schema 中的同名字段当媒体。"""
    if isinstance(value, str):
        return value, 0
    if not isinstance(value, list):
        raise ValueError("SDK 请求的消息或输入结构无效。")
    messages: list[object] = []
    tokens = 0
    for raw in cast(list[object], value):
        message = dict(_object(raw))
        content = message.get("content")
        if isinstance(content, list):
            parts: list[object] = []
            for raw_part in cast(list[object], content):
                part, measured = _project_part(_object(raw_part))
                parts.append(part)
                tokens += measured
            message["content"] = parts
        elif content is not None and not isinstance(content, str):
            raise ValueError("SDK 消息内容结构无效。")
        messages.append(message)
    return messages, tokens


def _project_part(part: Mapping[str, object]) -> tuple[dict[str, object], int]:
    """从计量副本移除媒体编码；原请求中的媒体和顺序始终不变。"""
    projected = dict(part)
    kind = part.get("type")
    if kind in ("text", "input_text") and isinstance(part.get("text"), str):
        return projected, 0
    if kind == "image_url":
        image = dict(_object(part.get("image_url")))
        tokens = _image_tokens(image.get("url"))
        image["url"] = ""
        projected["image_url"] = image
        return projected, tokens
    if kind == "input_image":
        tokens = _image_tokens(part.get("image_url"))
        projected["image_url"] = ""
        return projected, tokens
    if kind == "input_audio":
        audio = dict(_object(part.get("input_audio")))
        tokens = _audio_tokens(_decode(audio.get("data")))
        # 格式名属于传输表示，同一内容换编码后不应改变文本开销。
        projected["input_audio"] = {"data": ""}
        return projected, tokens
    raise ValueError("SDK 内容没有可估算的文字、图片或音频表示。")


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("SDK 消息或媒体结构无效。")
    return cast(Mapping[str, object], value)


def _decode(value: object) -> bytes:
    """只解码请求已有的 base64，不把字符串当路径或待下载资源。"""
    if not isinstance(value, str) or not value:
        raise ValueError("内联媒体缺少内容。")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("内联媒体的 base64 编码无效。") from error
    if not data:
        raise ValueError("内联媒体不能为空。")
    return data


def _image_tokens(url: object) -> int:
    """图片按尺寸和帧数估算，压缩质量、元信息和 data URL 头部不计正文。"""
    if not isinstance(url, str):
        raise ValueError("图片缺少 URL 表示。")
    if url.startswith(("https://", "http://")):
        # 无尺寸的远端图片保留明确的固定规划额度，不能按短 URL 计成几 token。
        # 此值不代表已读取像素，也不保证覆盖远端模型任意分辨率的实际用量。
        return _REMOTE_IMAGE_TOKENS
    if not url.startswith("data:image/") or ";base64," not in url:
        raise ValueError("图片必须是内联数据或 HTTP(S) 引用。")
    data = _decode(url.split(";base64,", 1)[1])
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            frames = getattr(image, "n_frames", 1)
            image.verify()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
    ) as error:
        raise ValueError("内联图片损坏或无法读取尺寸。") from error
    tiles = math.ceil(width / _IMAGE_TILE_SIZE) * math.ceil(height / _IMAGE_TILE_SIZE)
    return (_IMAGE_BASE_TOKENS + tiles * _IMAGE_TILE_TOKENS) * frames


def _audio_tokens(data: bytes) -> int:
    """自动识别容器并流式累计音频样本，不按码率或压缩后文件大小猜测时长。

    PyAV 统一覆盖 WAV、MP3 等编码，不维护模型或音频格式映射。逐帧解码避免
    容器缺少时长或 MP3 填充造成错误估计；不把完整 PCM 再存入内存。首次需要
    音频时才导入解码器，准备仍在宿主的请求准备线程中完成。
    """
    import av
    from av.error import FFmpegError

    try:
        # Security: 内存容器也可能引用播放列表外部资源。禁用全部底层 URL
        # 协议，确保媒体探测和解码不能发起网络或打开容器引用的本地路径。
        with av.open(io.BytesIO(data), mode="r", options={"protocol_whitelist": ""}) as container:
            if not container.streams.audio:
                raise ValueError("媒体不包含可解码音频。")
            duration = sum(frame.samples / frame.sample_rate for frame in container.decode(audio=0))
    except (FFmpegError, OSError, ZeroDivisionError) as error:
        raise ValueError("音频无法解码为有效时长。") from error
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("音频时长必须是有限正数。")
    return math.ceil(duration * _AUDIO_TOKENS_PER_SECOND)
