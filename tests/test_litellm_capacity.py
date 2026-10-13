"""以真实 LiteLLM 和合成媒体验证本地容量预检，不调用模型或设备。"""

from __future__ import annotations

import base64
import io
import os
import socket
import wave
from copy import deepcopy
from typing import TYPE_CHECKING, Protocol, cast

import pytest
from PIL import Image

from purrcept_engine.context.capacity import CapacityError, GenerationCapacity, check_capacity
from purrcept_engine.context.litellm_capacity import (
    GEMINI_MEDIA,
    RULE,
    count_litellm_capacity,
)

if TYPE_CHECKING:
    from av.audio.stream import AudioStream


class _AudioWriter(Protocol):
    """限定测试借用的 PyAV 音频构造接缝，资源寿命仍由原容器上下文负责。"""

    def add_stream(self, codec_name: str, *, rate: int) -> AudioStream:
        """创建本测试显式选择的 MP3 编码流，解码计量使用其实际输出。"""
        ...


def _request(*parts: dict[str, object]) -> dict[str, object]:
    return {
        "model": "openai/gemini-3.8-flash-high",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": list(parts)}],
    }


def _audio(seconds: int) -> dict[str, object]:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(bytes(32000 * seconds))
    return {
        "type": "input_audio",
        "input_audio": {"data": base64.b64encode(buffer.getvalue()).decode(), "format": "wav"},
    }


def _declaration(window: int = 10000) -> GenerationCapacity:
    return GenerationCapacity(window, 100, RULE, GEMINI_MEDIA)


def test_actual_litellm_is_offline_and_counts_complete_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """禁止网络连接，首次实际 SDK 导入、分词及完整 schema 计量均须成功。"""

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("本地 tokenizer 不得访问网络")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "False")
    request = _request({"type": "text", "text": "你好，检查一下本机状态。"})
    first = count_litellm_capacity(request, _declaration())
    # 连 JSON 表示也分词；不只计算 messages 或遗漏 LiteLLM 工具格式化未覆盖的字段。
    request["tools"] = [{"type": "function", "function": {"description": "完整工具说明 " * 200}}]
    second = count_litellm_capacity(request, _declaration())
    assert second.input_units > first.input_units
    assert first.unit == "token_estimate"
    assert "cl100k_reference" in first.scope
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "False"


def test_native_audio_counts_duration_without_mutating_or_tokenizing_base64() -> None:
    """时长增加一秒只增加 25 token，模型最终接收的音频和全部文本保持不变。"""
    short = _request(_audio(1), {"type": "text", "text": "请听原声"})
    original = deepcopy(short)
    long = _request(_audio(2), {"type": "text", "text": "请听原声"})
    first = count_litellm_capacity(short, _declaration())
    second = count_litellm_capacity(long, _declaration())
    assert second.input_units - first.input_units == 25
    assert first.input_units < 200
    assert short == original
    with pytest.raises(CapacityError, match="超过"):
        check_capacity(long, _declaration(200), rules={RULE: count_litellm_capacity})


def test_inline_image_uses_gemini_resolution_and_never_downloads() -> None:
    """真实内联 PNG 纳入媒体预算，远端图片不能在同步预检时下载。"""
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32)).save(buffer, format="PNG")
    url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
    request = _request({"type": "image_url", "image_url": {"url": url}})
    original = deepcopy(request)
    assert count_litellm_capacity(request, _declaration()).input_units >= 1120
    assert request == original
    remote = _request({"type": "image_url", "image_url": {"url": "https://invalid/image.png"}})
    with pytest.raises(CapacityError, match="内联"):
        count_litellm_capacity(remote, _declaration())


@pytest.mark.parametrize(
    "part",
    [
        {"type": "input_audio", "input_audio": {"data": "!bad", "format": "wav"}},
        {"type": "input_audio", "input_audio": {"data": "aGVsbG8=", "format": "wav"}},
        {"type": "input_audio", "input_audio": {"data": "aGVsbG8=", "format": "mp3"}},
        {"type": "video_url", "video_url": {"url": "https://invalid/video.mp4"}},
        {"type": "text", "text": 123},
    ],
)
def test_unknown_or_invalid_media_never_becomes_zero_tokens(part: dict[str, object]) -> None:
    """无法验证的媒体必须保留明确失败，而非用零占用继续派发。"""
    with pytest.raises(CapacityError):
        count_litellm_capacity(_request(part), _declaration())


def test_media_requires_explicit_profile() -> None:
    """选择 tokenizer 不等同于自动授予模型媒体计量依据。"""
    with pytest.raises(CapacityError, match="媒体计量"):
        count_litellm_capacity(_request(_audio(1)), GenerationCapacity(10000, 100, RULE))


def test_mp3_duration_is_read_from_decoded_samples() -> None:
    """压缩音频不按文件大小推断时长，也不会被遗漏为零 token。"""
    import av
    from av.audio.stream import AudioStream

    buffer = io.BytesIO()
    with av.open(buffer, mode="w", format="mp3") as container:
        # PyAV 的可变参数 stub 未标注；限定本测试唯一调用的音频编码入口。
        stream = cast(_AudioWriter, container).add_stream("libmp3lame", rate=16000)
        assert isinstance(stream, AudioStream)
        frame = av.AudioFrame(format="s16", layout="mono", samples=16000)
        frame.sample_rate = 16000
        frame.planes[0].update(bytes(32000))
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    request = _request(
        {
            "type": "input_audio",
            "input_audio": {"data": base64.b64encode(buffer.getvalue()).decode(), "format": "mp3"},
        }
    )
    estimate = count_litellm_capacity(request, _declaration())
    wav_estimate = count_litellm_capacity(_request(_audio(1)), _declaration())
    assert abs(estimate.input_units - wav_estimate.input_units) < 10
