"""显式读取 WAV/MP3 文件，把完整音频接管为后续模型请求的原生材料。

audio_files_blueprint 声明默认 me.listen_audio；构造声明不读取文件、加载解码器
或建立连接。AudioFileRuntime 拥有 HTTP 客户端和读取任务，先在工作线程核验
实际容器与解码时长，再通过 EvidencePort 保存原字节、来源及调用因果关系。
不播放声音、不采集麦克风或进行 ASR。取消观察不遗弃已经开始的读取或提交；
关闭等待它们结束后才释放客户端，原生音频仍须经过后续请求容量与呈现核验。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import math
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from purrcept_engine.errors import ConfigError, HostStateError
from purrcept_engine.infrastructure.files import DownloadError, download_file
from purrcept_engine.plugin_system import (
    CallContext,
    CallOutcome,
    CapabilityDeclaration,
    CapabilityInputError,
    EvidencePort,
    EvidenceSnapshot,
    FunctionCapability,
    MediaAsset,
    MediaReference,
    PluginBlueprint,
    PluginPorts,
    PluginRuntime,
)
from purrcept_engine.values import JsonObject, encode_json

MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_AUDIO_SECONDS = 600.0


class _ListenAudio(BaseModel):
    """只声明明确来源；media_id 用于归因，不替代实际文件读取。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    source: str = Field(
        min_length=1, pattern=r"\S", description="WAV/MP3 的本地文件路径或 HTTP(S) URL。"
    )
    media_id: str = Field(default="", description="可选原始刺激媒体 ID，不填写消息 ID。")


class AudioFileInputError(ValueError):
    """文件格式、字节量或解码时长不满足读取契约，可由模型纠正来源。"""


def _local_bytes(source: str, base_dir: Path) -> bytes:
    """相对路径沿主配置目录解释，实际读取上限之外一个字节以识别超额。"""
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    with path.open("rb") as stream:
        return stream.read(MAX_AUDIO_BYTES + 1)


def _audio_properties(data: bytes) -> tuple[str, float, int, int]:
    """按真实容器解码首条音轨，只接受能原样交给 Core 的 WAV/MP3。

    解码不缓存完整 PCM，不靠扩展名、HTTP MIME 或容器声明时长判断。每帧累加
    后立即拒绝超过十分钟的文件，不裁剪后伪装全文。禁用底层协议，内存文件
    也不能通过播放列表引用发起网络访问或读取其他本地文件。
    """
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise AudioFileInputError("音频必须非空且不超过 20 MiB。")
    import av
    from av.error import FFmpegError

    try:
        with av.open(io.BytesIO(data), mode="r", options={"protocol_whitelist": ""}) as container:
            formats = set(container.format.name.split(","))
            media_type = (
                "audio/wav" if "wav" in formats else "audio/mpeg" if "mp3" in formats else None
            )
            if media_type is None:
                raise AudioFileInputError("当前原生音频读取只支持 WAV 和 MP3。")
            if not container.streams.audio:
                raise AudioFileInputError("文件没有可解码的音轨。")
            duration, sample_rate, channels = 0.0, 0, 0
            for frame in container.decode(audio=0):
                if frame.sample_rate <= 0:
                    raise AudioFileInputError("音频采样率无效。")
                duration += frame.samples / frame.sample_rate
                sample_rate, channels = frame.sample_rate, len(frame.layout.channels)
                if duration > MAX_AUDIO_SECONDS:
                    raise AudioFileInputError("音频超过 10 分钟，请提供较短的完整文件。")
    except (FFmpegError, OSError, ZeroDivisionError) as error:
        raise AudioFileInputError("未取得完整、可解码的 WAV/MP3 音频。") from error
    if not math.isfinite(duration) or duration <= 0 or channels <= 0:
        raise AudioFileInputError("音频必须含有有效的非空音轨。")
    return media_type, duration, sample_rate, channels


class AudioFileRuntime:
    """应用默认音频读取资源；每次调用的真实工作由本实例持有直到结束。

    Note:
        由 Blueprint 构造，宿主在同一事件循环调用能力、观察状态及关闭。
        并发读取共用客户端，工作线程只读取文件与解码，不修改实例状态。
        证据端口借自宿主；本实例只归还自己的任务和客户端，不关闭存储。
    """

    def __init__(self, base_dir: Path, evidence: EvidencePort, client: httpx.AsyncClient) -> None:
        """借入明确的路径基准与证据端口，取得客户端的唯一关闭责任。

        Args:
            base_dir: 本地相对路径使用的主配置目录。
            evidence: 宿主借出的同库原文和媒体接管入口。
            client: 当前实例独占、由 close 释放的 HTTP 客户端。
        """
        self.base_dir, self.evidence, self.client = base_dir, evidence, client
        self._tasks: set[asyncio.Task[CallOutcome]] = set()
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def listen_audio(self, parameters: _ListenAudio, context: CallContext) -> CallOutcome:
        """保存明确文件的实际音频，不把路径、回执或转写当作已听到内容。

        Args:
            parameters: 已按能力 schema 校验的来源及可选归因。
            context: 执行器冻结的真实调用和行动身份。

        Returns:
            已接管的完整音频引用和格式、大小、时长等元信息。

        Raises:
            HostStateError: 本实例正在关闭，拒绝新的读取。
        """
        if self._closing:
            raise HostStateError("音频文件读取资源正在关闭。")
        task = asyncio.create_task(
            self._read(parameters, context), name="audio-file:" + context.call_id
        )
        self._tasks.add(task)
        task.add_done_callback(self._read_done)
        return await asyncio.shield(task)

    def _read_done(self, task: asyncio.Task[CallOutcome]) -> None:
        """收回已结束的工作，并取回无人继续等待的异常。"""
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _read(self, parameters: _ListenAudio, context: CallContext) -> CallOutcome:
        """有界读取与格式核验结束后才接管；存储故障保持原类型。"""
        source = parameters.source.strip()
        try:
            if "://" in source:
                data = await download_file(
                    self.client, source, max_bytes=MAX_AUDIO_BYTES, timeout_seconds=15.0
                )
            else:
                data = await asyncio.to_thread(_local_bytes, source, self.base_dir)
            media_type, duration, rate, channels = await asyncio.to_thread(_audio_properties, data)
        except (AudioFileInputError, DownloadError, OSError) as error:
            raise CapabilityInputError(str(error)) from error

        reference = MediaReference(hashlib.sha256(data).hexdigest(), media_type, len(data))
        metadata: JsonObject = {
            "source": source,
            "call_id": context.call_id,
            "action_id": context.action_id,
            "input_id": context.input_id,
            "duration_seconds": duration,
            "sample_rate": rate,
            "channels": channels,
        }
        if parameters.media_id:
            metadata["origin_media_id"] = parameters.media_id
        # 只有正式接管后的原字节能成为后续请求的 AudioBlock。读取不会新增外界输入，
        # 可选 media_id 也不能代替宿主签发的证据身份或呈现资格。
        saved = await self.evidence.capture(
            EvidenceSnapshot(
                evidence_id="audio-file-" + uuid4().hex,
                version=1,
                kind="audio.listen",
                text="音频读取能力实际取得的原始音频文件，未自动生成转写。",
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
                "duration_seconds": duration,
                "sample_rate": rate,
                "channels": channels,
                "evidence_ref": saved.evidence_id,
            },
            media=saved.media,
        )

    def snapshot(self) -> JsonObject:
        """只读取资源阶段和实际在途数，不读取文件或触发音频播放。

        Returns:
            即时资源事实；ready 仅表示可接受读取，不表示模型已接收任何音频。
        """
        return {
            "state": "closed" if self._closed else "closing" if self._closing else "ready",
            "pending_reads": len(self._tasks),
        }

    async def prepare_request(self) -> JsonObject:
        """提供读取资源的当前可用阶段，不向提示词写入在途文件路径。

        Returns:
            供宿主冻结进请求上下文的轻量状态，不包含音频或原始证据正文。
        """
        return {"state": self.snapshot()["state"]}

    @property
    def resources_released(self) -> bool:
        """报告客户端已实际关闭，不以在途数量推断资源已经归还。

        Returns:
            True 表示真实任务已经结束且客户端关闭成功，可由宿主归还借出的端口。
        """
        return self._closed

    async def close(self) -> None:
        """等待同一次收尾；取消观察不关闭仍被读取任务借用的资源。

        Note:
            先拒绝新读取，再等待真实读取和证据提交。重复调用观察同一收尾任务；
            内部或存储故障以异常组报告，不能用成功关闭客户端掩盖原始失败。
        """
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="audio-file-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        """等待读取及提交归还后关闭客户端，内部或权威故障仍向宿主报告。"""
        results = await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        failures = [
            r
            for r in results
            if isinstance(r, BaseException) and not isinstance(r, CapabilityInputError)
        ]
        try:
            await self.client.aclose()
        except BaseException as error:
            failures.append(error)
        else:
            self._closed = True
        if failures:
            raise BaseExceptionGroup("音频文件读取或正式接管收尾失败。", failures)


def audio_files_blueprint(
    base_dir: Path,
    *,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> PluginBlueprint:
    """声明主体默认音频文件读取能力，检查目录时不取得实际资源。

    Args:
        base_dir: 必须为绝对路径的主配置目录。
        client_factory: 显式测试传输接缝；缺省创建无代理、无自动重试的客户端。

    Returns:
        由应用与 subject 对象一起装配的纯能力声明。

    Raises:
        ConfigError: 主配置目录不是绝对路径。
    """
    if not base_dir.is_absolute():
        raise ConfigError("音频文件读取必须提供绝对的主配置基准目录。")
    declaration = CapabilityDeclaration(
        "engine:listen_audio",
        "subject",
        "listen_audio",
        "读取音频 URL 或本地文件，将完整原生音频交给后续模型请求。",
        _ListenAudio,
        30.0,
        'await me.listen_audio(source="recording.wav")',
        "支持 WAV/MP3，最大20 MiB和10分钟；相对路径以配置目录为基准。"
        "不自动播放或转写。取得回执后，须在后续支持 audio 的模型请求实际收到音频，"
        "再判断声音内容；容量不足会明确拒绝，不截断文件或用路径冒充已经听过。",
    )

    async def build(ports: PluginPorts) -> PluginRuntime:
        """只在宿主构造资源时创建客户端；真实读取才加载音频解码器。"""
        if ports.evidence is None:
            raise ConfigError("默认音频文件读取需要宿主原文/媒体端口。")
        client = (
            httpx.AsyncClient(timeout=15.0, trust_env=False)
            if client_factory is None
            else client_factory()
        )
        runtime = AudioFileRuntime(base_dir, ports.evidence, client)
        return PluginRuntime(
            (FunctionCapability(declaration, runtime.listen_audio),),
            close=runtime.close,
            snapshot=runtime.snapshot,
            prepare_request=runtime.prepare_request,
            resources_released=lambda: runtime.resources_released,
        )

    return PluginBlueprint((), (declaration,), build)
