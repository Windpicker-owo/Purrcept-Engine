"""以真实内联媒体核验编码大小与上下文 token 估算相互独立。

覆盖图片压缩与元信息、音频采样率与编码、离线执行、并发只读和真正超额。
所有文件在内存合成，传输与设备均不参与。
"""

from __future__ import annotations

import base64
import hashlib
import io
import socket
import wave
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Protocol, cast

import av
import pytest
from av.audio.stream import AudioStream
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from purrcept_engine.domain.context.capacity import (
    CapacityError,
    DecisionCapacity,
    GenerationCapacity,
    assess_capacity,
    check_capacity,
)
from purrcept_engine.serialization import encode


class _AudioWriter(Protocol):
    """限定测试借用的编码入口；容器资源仍归 av.open 上下文所有。"""

    def add_stream(self, codec_name: str, *, rate: int) -> AudioStream:
        """创建本测试选择的 MP3 编码流。"""
        ...


def _request(part: Mapping[str, object]) -> dict[str, object]:
    """模型别名不对应任何 tokenizer 或 Provider 专用配置。"""
    return {
        "model": "openai/arbitrary-model-alias",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": [part]}],
    }


def _image(
    width: int, height: int, *, compressed: bool = True, format: str = "PNG"
) -> dict[str, object]:
    """改变压缩参数和元信息，保留相同像素内容与分辨率。"""
    buffer = io.BytesIO()
    info = PngInfo()
    info.add_text("comment", "metadata" * (1 if compressed else 10_000))
    with Image.new("RGB", (width, height), (20, 30, 40)) as image:
        image.save(buffer, format=format, compress_level=9 if compressed else 0, pnginfo=info)
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:image/{format.lower()};base64,"
            + base64.b64encode(buffer.getvalue()).decode()
        },
    }


def _audio(seconds: int, *, sample_rate: int = 16000) -> dict[str, object]:
    """按真实样本生成 WAV，时长相同但采样率可改变文件大小。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(bytes(2 * sample_rate * seconds))
    return {
        "type": "input_audio",
        "input_audio": {"format": "wav", "data": base64.b64encode(buffer.getvalue()).decode()},
    }


def _measure(part: Mapping[str, object]) -> int:
    return check_capacity(_request(part), GenerationCapacity(100_000, 100)).estimate.input_units


def test_same_image_size_is_independent_of_compression_metadata_and_format() -> None:
    """大 PNG 与同尺寸的小 PNG/JPEG 占用接近，分辨率增加才提高估算。"""
    small = _image(1024, 1024)
    large = _image(1024, 1024, compressed=False)
    assert len(encode(large)) > len(encode(small)) * 100
    assert _measure(small) == _measure(large) == _measure(_image(1024, 1024, format="JPEG"))
    assert _measure(_image(1536, 1024)) > _measure(small)


@pytest.mark.parametrize("format", ["GIF", "PNG", "WEBP"])
def test_animated_images_preserve_all_frames_and_pass_capacity_checks(format: str) -> None:
    """真实 GIF、APNG 和 WebP 多帧内容应按帧计量，不能退回旧单帧规则拒绝。"""
    buffer = io.BytesIO()
    with Image.new("RGB", (32, 32), "red") as first, Image.new("RGB", (32, 32), "blue") as second:
        first.save(
            buffer, format=format, save_all=True, append_images=[second], duration=100, loop=0
        )
    data = buffer.getvalue()
    with Image.open(io.BytesIO(data)) as image:
        image.seek(1)
        assert image.tell() == 1
        with pytest.raises(EOFError):
            image.seek(2)
    part = {
        "type": "image_url",
        "image_url": {
            "url": f"data:image/{format.lower()};base64," + base64.b64encode(data).decode()
        },
    }
    request = _request(part)
    original = deepcopy(request)
    result = check_capacity(request, GenerationCapacity(10_000, 100))
    # 两帧均为一个分块，文字布局相同；增加一帧只增加该帧的视觉额度。
    assert result.estimate.input_units == _measure(_image(32, 32, format=format)) + 512
    assert result.within_budget
    assert result.estimate.method == "multimodal-heuristic-v1"
    assert request == original


def test_audio_uses_duration_instead_of_sample_rate_or_encoded_size() -> None:
    """一分钟原声不会因约 2 MiB WAV 变成数百万 token；增一秒仅补时长额度。"""
    short = _audio(60)
    assert _measure(short) == _measure(_audio(60, sample_rate=48000))
    assert _measure(_audio(61)) - _measure(short) == 32
    assert _measure(short) < 2500


def test_mp3_and_wav_use_comparable_duration_estimates() -> None:
    """压缩音频自动识别，不能因压缩体积小就比同长度 PCM 低几个数量级。"""
    buffer = io.BytesIO()
    with av.open(buffer, mode="w", format="mp3") as container:
        stream = cast(_AudioWriter, container).add_stream("libmp3lame", rate=16000)
        frame = av.AudioFrame(format="s16", layout="mono", samples=16000)
        frame.sample_rate = 16000
        frame.planes[0].update(bytes(32000))
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    mp3 = {
        "type": "input_audio",
        "input_audio": {"format": "mp3", "data": base64.b64encode(buffer.getvalue()).decode()},
    }
    assert abs(_measure(mp3) - _measure(_audio(1))) <= 4


def test_media_estimation_is_offline_readonly_and_hashes_original_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """媒体分离只用于估算，指纹、原声、原图与原请求始终对应完整原件。"""

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("容量估算不得访问网络")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    for part in (
        _image(32, 32),
        _audio(1),
        {"type": "image_url", "image_url": {"url": "https://never-fetch.invalid/image"}},
    ):
        request = _request(part)
        original = deepcopy(request)
        result = check_capacity(request, GenerationCapacity(10_000, 100))
        assert result.estimate.method == "multimodal-heuristic-v1"
        assert result.payload_sha256 == hashlib.sha256(encode(original).encode("utf-8")).hexdigest()
        assert request == original


def test_generation_and_decision_images_share_the_builtin_estimator() -> None:
    """专用判断的 input_image 与生成的 image_url 使用相同的尺寸依据。"""
    image = _image(512, 512)
    mapping = image["image_url"]
    assert isinstance(mapping, dict)
    decision: dict[str, object] = {
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_image", "image_url": cast(str, mapping["url"])}],
            }
        ]
    }
    measured = check_capacity(decision, DecisionCapacity(10_000))
    assert 512 <= measured.estimate.input_units < 600
    assert (
        measured.estimate.method
        == assess_capacity(_request(image), GenerationCapacity(10_000, 100)).estimate.method
    )


def test_long_audio_exceeds_budget_without_altering_originals() -> None:
    """超过输入预算的真实媒体时长仍须阻止派发，而不是按附件常数放行。"""
    request = _request(_audio(60))
    original = deepcopy(request)
    declaration = GenerationCapacity(1000, 100)
    with pytest.raises(CapacityError) as caught:
        check_capacity(request, declaration)
    assert caught.value.code == "over_capacity"
    assert caught.value.assessment is not None
    assert caught.value.assessment.estimate.input_units >= 1920
    assert request == original


@pytest.mark.parametrize(
    "part",
    [
        {"type": "input_audio", "input_audio": {"format": "wav", "data": "!bad"}},
        {"type": "input_audio", "input_audio": {"format": "wav", "data": "aGVsbG8="}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
        {"type": "future-media", "data": "unrecognized"},
    ],
)
def test_invalid_media_is_not_silently_counted_as_text_or_zero(part: dict[str, object]) -> None:
    """坏编码和坏文件显式失败，不用文件大小、零量或默认时长掩盖。"""
    with pytest.raises(CapacityError) as caught:
        check_capacity(_request(part), GenerationCapacity(100_000, 100))
    assert caught.value.code == "invalid_material"


def test_audio_playlist_cannot_open_external_files(tmp_path: Path) -> None:
    """即使容器声称有外部片段，内存媒体探测也不得读取引用的本地文件。"""
    part = _audio(1)
    audio = part["input_audio"]
    assert isinstance(audio, dict)
    segment = tmp_path / "segment.wav"
    segment.write_bytes(base64.b64decode(cast(str, audio["data"])))
    playlist = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n"
        "#EXTINF:1.0,\n" + segment.as_uri() + "\n#EXT-X-ENDLIST\n"
    ).encode()
    request = _request(
        {
            "type": "input_audio",
            "input_audio": {"format": "hls", "data": base64.b64encode(playlist).decode()},
        }
    )
    with pytest.raises(CapacityError) as caught:
        check_capacity(request, GenerationCapacity(10_000, 100))
    assert caught.value.code == "invalid_material"


def test_parallel_assessments_do_not_share_mutable_measurement_state() -> None:
    """独立会话可并发预检同一原声请求，估算与指纹不依赖调用顺序。"""
    request = _request(_audio(1))
    original = deepcopy(request)
    declaration = GenerationCapacity(10_000, 100)
    expected = assess_capacity(request, declaration)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(assess_capacity, request, declaration) for _ in range(16)]
        results = [future.result() for future in futures]
    assert results == [expected] * 16
    assert request == original
