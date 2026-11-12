"""Fish Audio v3 的同步文件与 aligned SSE 传输，不拥有现场队列或播放器。

一次开始的合成只提交一次付费请求，先查能力目录和音色，再读取有界
二进制。流式必须取得 completed，网络 EOF 不构成完成。httpx 客户端
由本插件持有，所有查询和合成共用显式代理配置；省略时直连。
每阶段保留安全的传输进度与循环调度延迟，业务失败的 ERROR 仍归派发服务。
关闭不修改进程环境，也不借用身体的网络或原生资源。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote
from uuid import uuid4

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
_LOG = logging.getLogger(__name__)


def _transfer_error(
    stage: str,
    error: httpx.HTTPError | TimeoutError | ActionProtocolError,
    transfer: _TransferTrace,
) -> AudioError:
    """将阶段和安全异常分类写入回执，不复制远端正文、URL 或认证信息。

    业务所有者会将 AudioError 转为持久回执，因此关键诊断必须进入此短消息；
    原始异常链仍由调用点保留，不能依赖最终回执继续保存 Python 异常对象。
    """
    detail = type(error).__name__
    if isinstance(error, httpx.HTTPStatusError):
        detail += f"；HTTP {error.response.status_code}"
    header_state = "已收到响应头" if transfer.status_code is not None else "未收到响应头"
    return AudioError(
        f"Fish Audio {stage}失败（{detail}）。"
        f"阶段耗时 {transfer.elapsed():.3f} 秒；{header_state}；"
        f"响应体已接收 {transfer.body_bytes} 字节；最后网络步骤 {transfer.transport_step}；"
        f"事件循环最大延迟 {transfer.loop_lag_ms():.1f} 毫秒。"
    )


@dataclass(slots=True)
class _TransferTrace:
    """单次 HTTP 阶段独占的有界观测值，不跨并发 record 共享进度。

    只保留网络步骤名、计时和字节数，不读取 trace info 中的认证头或正文。
    一秒探针测量宿主事件循环的调度延迟，阶段结束必须撤销探针。
    """

    identity: str
    stage: str
    proxy_enabled: bool
    client_id: str
    deadline: float
    loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.get_running_loop)
    started: float = field(init=False)
    next_probe: float = field(init=False)
    probe: asyncio.TimerHandle | None = None
    max_loop_lag: float = 0.0
    status_code: int | None = None
    headers_ms: float | None = None
    first_byte_ms: float | None = None
    body_bytes: int = 0
    transport_step: str = "request_start"
    connections_started: int = 0

    def __post_init__(self) -> None:
        """阶段创建即接管探针；调用者必须在所有退出路径执行 finish。"""
        self.started = self.loop.time()
        self.next_probe = self.started + 1
        self.probe = self.loop.call_at(self.next_probe, self._sample_loop)

    def _sample_loop(self) -> None:
        """迟到只计一次，不为补齐错过的探针连续调度大量回调。"""
        now = self.loop.time()
        self.max_loop_lag = max(self.max_loop_lag, now - self.next_probe)
        self.next_probe = now + 1
        self.probe = self.loop.call_at(self.next_probe, self._sample_loop)

    def elapsed(self) -> float:
        """返回本阶段单调耗时，不把前置查询时间算作纯合成时间。"""
        return self.loop.time() - self.started

    def loop_lag_ms(self) -> float:
        """即使超时先于探针回调执行，也保留已经错过的调度时间。"""
        return max(self.max_loop_lag, self.loop.time() - self.next_probe, 0) * 1000

    async def trace(self, name: str, info: Mapping[str, object]) -> None:
        """HTTP Core 只交接阶段名，info 不展开、不复制到普通日志。"""
        del info
        # 关闭响应也会产生 trace；不能让收尾覆盖真正失败时等待的网络步骤。
        if ".response_closed." in name or self.transport_step.endswith(".failed"):
            return
        if name == "connection.connect_tcp.started":
            self.connections_started += 1
        self.transport_step = name

    def record_bytes(self, size: int) -> None:
        """只以非空响应块建立首字节时间，累计解压前的网络正文长度。"""
        if size and self.first_byte_ms is None:
            self.first_byte_ms = self.elapsed() * 1000
        self.body_bytes += size

    def log(self, outcome: str) -> None:
        """业务失败仍由派发服务唯一记录 ERROR，这里只写受控 INFO 进度。"""
        _LOG.info(
            "语音传输阶段状态已更新。",
            extra={
                "call_id": self.identity,
                "stage": self.stage,
                "outcome": outcome,
                "category": "proxy" if self.proxy_enabled else "direct",
                "transport_id": self.client_id,
                "connection_attempts": self.connections_started,
                "remaining_seconds": round(max(0, self.deadline - self.loop.time()), 3),
                "duration_ms": round(self.elapsed() * 1000, 1),
                "status_code": self.status_code,
                "transport_step": self.transport_step,
                "headers_ms": self.headers_ms,
                "first_byte_ms": self.first_byte_ms,
                "body_bytes": self.body_bytes,
                "event_loop_lag_ms": round(self.loop_lag_ms(), 1),
            },
        )

    def finish(self, outcome: str) -> None:
        """退出阶段时撤销唯一探针，防止长期客户端遗留循环引用与回调。"""
        if self.probe is not None:
            self.probe.cancel()
            self.probe = None
        self.log(outcome)


class _ObservedBody(httpx.AsyncByteStream):
    """借用原响应流，记录解压前的响应体到达，不读取或保存音频正文。"""

    def __init__(self, stream: httpx.AsyncByteStream, transfer: _TransferTrace) -> None:
        """借用当前响应流和当前阶段计量，不接管源客户端。"""
        self.stream, self.transfer = stream, transfer

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """逐块原样交付，同时保留响应体到达时刻和已接收长度。"""
        async for chunk in self.stream:
            self.transfer.record_bytes(len(chunk))
            yield chunk

    async def aclose(self) -> None:
        """响应仍通过 HTTPX 原有关闭路径归还连接，不另开或重置连接池。"""
        await self.stream.aclose()


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
    """在付费前核验所选模型、音色、操作和控制参数的实际支持范围。

    Args:
        payload: 已归一化的文本、模型、音色、格式和通用控制参数。
        catalog: Fish Audio 当前能力目录返回的模型及其支持范围。
        voice: 当前音色查询结果，包含兼容的模型身份。
        streaming: 是否采用流式音频传输与播放。

    Raises:
        AudioError: 声音配置、端点请求、返回音频格式或时长不满足当前合成与播放要求。

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
        self._client_id = uuid4().hex
        # 网络路由归此实例所有；环境代理和 NO_PROXY 不得改变已声明的路由。
        self.http = httpx.AsyncClient(
            timeout=settings.timeout_seconds,
            proxy=settings.proxy,
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
        self, payload: JsonObject, headers: dict[str, str], *, streaming: bool, deadline: float
    ) -> None:
        """两项元数据共享上层期限，验证失败不能继续触发付费生成。"""
        root = self.settings.api_base.rstrip("/")
        identity = headers["X-Request-Id"]
        catalog = await self._json(
            root + "/speech/tts/capabilities",
            {},
            stage="能力目录查询",
            deadline=deadline,
            identity=identity,
        )
        voice_id = payload["voiceId"]
        if not isinstance(voice_id, str):
            raise RuntimeError("归一化音色身份必须为字符串。")
        voice = await self._json(
            root + "/voices/" + quote(voice_id, safe=""),
            {"Authorization": headers["Authorization"]},
            stage="音色查询",
            deadline=deadline,
            identity=identity,
        )
        validate_request(payload, catalog, voice, streaming=streaming)

    async def _json(
        self,
        url: str,
        headers: dict[str, str],
        *,
        stage: str,
        deadline: float,
        identity: str,
    ) -> JsonObject:
        """在共享绝对期限内读取一项元数据，超时归到实际正在等待的阶段。"""
        async with self._request(
            "GET", url, headers, stage=stage, deadline=deadline, identity=identity
        ) as response:
            raw = bytearray()
            async for piece in response.aiter_bytes():
                raw.extend(piece)
                if len(raw) > 1_048_576:
                    raise AudioError(f"Fish Audio {stage}元数据超容量。")
            return decode_object(bytes(raw))

    @asynccontextmanager
    async def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        *,
        stage: str,
        deadline: float,
        identity: str,
        payload: JsonObject | None = None,
    ) -> AsyncGenerator[httpx.Response, None]:
        """在原共享期限内记录一个阶段，保留连接复用和一次派发的原有语义。

        响应和循环探针均归此上下文关闭；外层取消仍是取消，只有已分类的
        传输/协议异常转为携带安全进度的业务回执。不增加重试或修改路由。
        """
        transfer = _TransferTrace(
            identity, stage, self.settings.proxy is not None, self._client_id, deadline
        )
        outcome = "failed"
        try:
            transfer.log("started")
            async with asyncio.timeout_at(deadline):
                async with self.http.stream(
                    method,
                    url,
                    headers=headers,
                    json=payload,
                    extensions={"trace": transfer.trace},
                ) as response:
                    transfer.status_code = response.status_code
                    transfer.headers_ms = round(transfer.elapsed() * 1000, 1)
                    response.raise_for_status()
                    stream = response.stream
                    if not isinstance(stream, httpx.AsyncByteStream):
                        raise RuntimeError("异步语音客户端返回了非异步响应流。")
                    response.stream = _ObservedBody(stream, transfer)
                    yield response
            outcome = "completed"
        except (asyncio.CancelledError, GeneratorExit):
            outcome = "cancelled"
            raise
        except (httpx.HTTPError, TimeoutError, ActionProtocolError) as error:
            raise _transfer_error(stage, error, transfer) from error
        finally:
            transfer.finish(outcome)

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
        deadline = asyncio.get_running_loop().time() + self.settings.timeout_seconds
        # 顺序与总预算不变；每阶段继承同一绝对期限，后续阶段不能重新计时。
        # 分阶段建立超时上下文，使总期限耗尽也能留下准确的等待位置。
        await self._metadata(payload, headers, streaming=False, deadline=deadline)
        async with self._request(
            "POST",
            self.settings.api_base.rstrip("/") + "/speech/tts",
            headers,
            stage="文件合成",
            deadline=deadline,
            identity=identity,
            payload=payload,
        ) as response:
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
        deadline = (
            asyncio.get_running_loop().time()
            + self.settings.timeout_seconds
            + self.settings.playback_timeout_seconds
        )
        await self._metadata(payload, headers, streaming=True, deadline=deadline)
        async with self._request(
            "POST",
            self.settings.api_base.rstrip("/") + "/speech/tts/aligned",
            headers,
            stage="流式合成",
            deadline=deadline,
            identity=identity,
            payload=payload,
        ) as response:
            if response.headers.get("content-type", "").split(";")[0] != "text/event-stream":
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

    async def close(self) -> None:
        """所有现场与导出借用结束后释放同一连接池。"""
        await self.http.aclose()
