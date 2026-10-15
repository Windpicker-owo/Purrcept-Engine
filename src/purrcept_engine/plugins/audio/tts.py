"""Fish Audio v3 的同步文件与 aligned SSE 传输，不拥有现场队列或播放器。

一次开始的合成只提交一次付费请求，先查能力目录和音色，再读取有界
二进制。流式必须取得 completed，网络 EOF 不构成完成。httpx 客户端
由本插件持有，关闭不修改进程环境，也不借用身体的网络或原生资源。
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import AsyncGenerator
from typing import Protocol
from urllib.parse import quote

import httpx

from purrcept_engine.plugin_system import AudioError
from purrcept_engine.plugins.audio.models import (
    AudioSettings,
    RecordParameters,
    SpeechAudio,
    SpeechParameters,
)
from purrcept_engine.values import ActionProtocolError, JsonObject, decode_object

MAX_AUDIO_BYTES = 8 * 1024 * 1024


class SpeechSource(Protocol):
    """输出所有者构造的合成接缝，生产实现 FishSource，替身用于离线验证。

    现场所有者串行调用；record 使用独立显式执行名额。实现不重试、不播放，
    async 等待取消未必能终止底层同步工作，所有者仍保存任务到真实结束。
    stream 按序交 MP3 字节；不得把服务端接受请求当作实际合成完成。
    """

    async def synthesize(
        self, parameters: SpeechParameters | RecordParameters, identity: str
    ) -> SpeechAudio:
        """取得完整已校验音频，未知时长保持为空。

        Args:
            parameters: 已通过能力参数模型校验的调用参数。
            identity: 本次合成的稳定关联身份，不等同于平台提供业务幂等。

        Returns:
            实际合成或处理完成的完整音频与采样信息。
        """
        ...

    def stream(self, parameters: SpeechParameters, identity: str) -> AsyncGenerator[bytes, None]:
        """读取一次 aligned MP3 流，异常与截断不可自动续传。

        Args:
            parameters: 已通过能力参数模型校验的调用参数。
            identity: 本次合成的稳定关联身份，不等同于平台提供业务幂等。

        Returns:
            按原始采集、解码或播放顺序逐块交付的完整字节流。
        """
        ...

    async def close(self) -> None:
        """所有借用结束后收回本实例网络资源。"""
        ...


def validate_request(
    payload: JsonObject, catalog: JsonObject, voice: JsonObject, *, streaming: bool
) -> None:
    """校验活动控制的命令种类与字段组合，构造明确请求。

    Args:
        payload: 当前边界采用的业务 JSON 或完整模型请求材料。
        catalog: 全部已启用插件共同生成的冻结目录。
        voice: 实际发给合成端点的显式声线参数。
        streaming: 是否采用流式音频传输与播放。

    Raises:
        AudioError: 声音配置、端点请求、返回音频格式或时长不满足当前合成与播放要求。
        ActivityError: 命令不属于 start/pause/resume/end/adjust/extend，字段组合不匹配，
            或所需身份、目标与预算无效。

    Note:
        付费前核验实时能力；不丢弃未知或不支持的语速/增益参数。
    """
    models = catalog.get("models")
    if not isinstance(models, list):
        raise AudioError("Fish Audio 能力目录缺少 models。")
    model = next(
        (
            item
            for item in models
            if isinstance(item, dict) and item.get("id") == payload["modelId"]
        ),
        None,
    )
    if model is None or model.get("available") is not True:
        raise AudioError("Fish Audio 模型不可用。")
    operations = model.get("supportedOperations")
    operation = "alignedStreaming" if streaming else "synchronous"
    if not isinstance(operations, dict) or operations.get(operation) is not True:
        raise AudioError("Fish Audio 模型不支持所需合成方式。")
    formats = model.get("outputFormats")
    if not isinstance(formats, list) or payload["format"] not in formats:
        raise AudioError("Fish Audio 模型不支持所需格式。")
    info = voice.get("voice")
    compatible = info.get("modelIds") if isinstance(info, dict) else None
    if not isinstance(compatible, list) or payload["modelId"] not in compatible:
        raise AudioError("Fish Audio 音色与模型不兼容。")
    maximum, text = model.get("maxTextLength"), payload["text"]
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not isinstance(text, str):
        raise AudioError("Fish Audio 文本能力字段无效。")
    if len(text) > maximum:
        raise AudioError("Fish Audio 文本超过模型上限。")
    controls = model.get("controls")
    for name in ("speed", "volume"):
        bounds = controls.get(name) if isinstance(controls, dict) else None
        low = bounds.get("min") if isinstance(bounds, dict) else None
        high = bounds.get("max") if isinstance(bounds, dict) else None
        value = payload[name]
        if (
            not isinstance(low, (int, float))
            or isinstance(low, bool)
            or not isinstance(high, (int, float))
            or isinstance(high, bool)
            or not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not low <= value <= high
        ):
            raise AudioError("Fish Audio 控制范围无效或参数不受支持。")


class FishSource:
    """一个明确端点/密钥的 httpx 所有者，不缓存能力或跨失败重发。"""

    def __init__(
        self, settings: AudioSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """构造连接池但不请求。

        Args:
            settings: 当前组件已校验的配置或纯声明。
            transport: 显式注入的 HTTP 传输；None 使用标准传输实现。

        Note:
            无显式密钥直到合成边界才报错。
        """
        self.settings = settings
        self.http = httpx.AsyncClient(
            timeout=settings.timeout_seconds,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    def _headers(self, identity: str) -> dict[str, str]:
        key = self.settings.api_key.get_secret_value()
        if not key.strip():
            raise AudioError("Fish Audio api_key 为空。")
        return {
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "X-Request-Id": identity,
            "Idempotency-Key": identity,
        }

    def _payload(
        self, parameters: SpeechParameters | RecordParameters, *, streaming: bool
    ) -> JsonObject:
        requested = parameters.format
        if streaming and requested not in ("", "pcm"):
            raise AudioError("流式 speak 的本地输出只接受省略格式或 pcm。")
        if not streaming and requested == "pcm":
            raise AudioError("文件合成不能请求无容器 PCM。")
        return {
            "text": parameters.text.strip(),
            "voiceId": parameters.voice_id.strip() or self.settings.voice_id,
            "modelId": self.settings.model,
            "format": "mp3" if streaming else requested or self.settings.audio_format,
            "speed": self.settings.speed if parameters.speed is None else parameters.speed,
            "volume": parameters.volume,
        }

    async def _metadata(
        self, payload: JsonObject, headers: dict[str, str], *, streaming: bool
    ) -> None:
        """两项元数据共享上层期限，验证失败不能继续触发付费生成。"""
        root = self.settings.api_base.rstrip("/")
        catalog = await self._json(root + "/speech/tts/capabilities", {})
        voice_id = payload["voiceId"]
        if not isinstance(voice_id, str):
            raise RuntimeError("归一化音色身份必须为字符串。")
        voice = await self._json(
            root + "/voices/" + quote(voice_id, safe=""),
            {"Authorization": headers["Authorization"]},
        )
        validate_request(payload, catalog, voice, streaming=streaming)

    async def _json(self, url: str, headers: dict[str, str]) -> JsonObject:
        async with self.http.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            raw = bytearray()
            async for piece in response.aiter_bytes():
                raw.extend(piece)
                if len(raw) > 1_048_576:
                    raise AudioError("Fish Audio 元数据超容量。")
            try:
                return decode_object(bytes(raw))
            except ActionProtocolError as error:
                raise AudioError("Fish Audio元数据协议无效。") from error

    async def synthesize(
        self, parameters: SpeechParameters | RecordParameters, identity: str
    ) -> SpeechAudio:
        """一次文件合成，全程同一时间预算，真实 MIME 与请求必须一致。

        Args:
            parameters: 已通过能力参数模型校验的调用参数。
            identity: 本次合成的稳定关联身份，不等同于平台提供业务幂等。

        Returns:
            实际合成或处理完成的完整音频与采样信息。

        Raises:
            AudioError: 声音配置、端点请求、返回音频格式或时长不满足当前合成与播放要求。
        """
        headers, payload = self._headers(identity), self._payload(parameters, streaming=False)
        try:
            import asyncio

            async with asyncio.timeout(self.settings.timeout_seconds):
                await self._metadata(payload, headers, streaming=False)
                async with self.http.stream(
                    "POST",
                    self.settings.api_base.rstrip("/") + "/speech/tts",
                    json=payload,
                    headers=headers,
                ) as response:
                    response.raise_for_status()
                    mime = response.headers.get("content-type", "").split(";")[0]
                    actual = {
                        "audio/mpeg": "mp3",
                        "audio/mp3": "mp3",
                        "audio/wav": "wav",
                        "audio/x-wav": "wav",
                        "audio/wave": "wav",
                    }.get(mime)
                    if actual != payload["format"] or actual not in ("mp3", "wav"):
                        raise AudioError("Fish Audio 音频 Content-Type 与请求不符。")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_AUDIO_BYTES:
                            raise AudioError("合成音频超过8 MiB。")
                    if not raw:
                        raise AudioError("Fish Audio 返回空音频。")
                    return SpeechAudio(bytes(raw), actual)
        except (httpx.HTTPError, TimeoutError) as error:
            # 外围传输分类一次；错误不回显密钥、完整端点或服务端正文。
            raise AudioError("Fish Audio 文件合成传输失败。") from error

    async def stream(
        self, parameters: SpeechParameters, identity: str
    ) -> AsyncGenerator[bytes, None]:
        """逐事件读 MP3，单事件/总字节均有限，completed 才确认生产终止。

        Args:
            parameters: 已通过能力参数模型校验的调用参数。
            identity: 本次合成的稳定关联身份，不等同于平台提供业务幂等。

        Yields:
            按原始采集、解码或播放顺序逐块交付的完整字节流。

        Raises:
            AudioError: 声音配置、端点请求、返回音频格式或时长不满足当前合成与播放要求。
        """
        headers, payload = self._headers(identity), self._payload(parameters, streaming=True)
        try:
            import asyncio

            async with asyncio.timeout(
                self.settings.timeout_seconds + self.settings.playback_timeout_seconds
            ):
                await self._metadata(payload, headers, streaming=True)
                async with self.http.stream(
                    "POST",
                    self.settings.api_base.rstrip("/") + "/speech/tts/aligned",
                    json=payload,
                    headers=headers,
                ) as response:
                    response.raise_for_status()
                    if (
                        response.headers.get("content-type", "").split(";")[0]
                        != "text/event-stream"
                    ):
                        raise AudioError("Fish Audio 流式响应格式无效。")
                    event, size, total = "", 0, 0
                    lines: list[str] = []
                    async for line in response.aiter_lines():
                        size += len(line.encode())
                        if size > 8_000_000:
                            raise AudioError("Fish Audio 流式事件超容量。")
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:"):
                            lines.append(line[5:].lstrip())
                        elif not line:
                            if event == "error":
                                raise AudioError("Fish Audio 流式合成明确失败。")
                            if event == "completed":
                                if total == 0:
                                    raise AudioError("Fish Audio completed前没有实际音频。")
                                return
                            if event == "audio":
                                data = decode_object("\n".join(lines).encode())
                                encoded = data.get("audioBase64")
                                if not isinstance(encoded, str) or not encoded:
                                    raise AudioError("Fish Audio audio 事件缺少音频。")
                                try:
                                    chunk = base64.b64decode(encoded, validate=True)
                                except binascii.Error as error:
                                    raise AudioError("Fish Audio 音频编码无效。") from error
                                total += len(chunk)
                                if total > MAX_AUDIO_BYTES:
                                    raise AudioError("流式音频超过8 MiB。")
                                if chunk:
                                    yield chunk
                            event, lines, size = "", [], 0
                    raise AudioError("Fish Audio 流在 completed 前结束。")
        except (httpx.HTTPError, TimeoutError, ActionProtocolError) as error:
            raise AudioError("Fish Audio 流式合成传输失败。") from error

    async def close(self) -> None:
        """所有现场与导出借用结束后释放同一连接池。"""
        await self.http.aclose()
