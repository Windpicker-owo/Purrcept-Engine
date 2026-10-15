"""每段音频独占的 FFmpeg 解码与调音，不拥有扬声器或身体连接。

完整文件与 aligned MP3 流共用 mono S16 PCM 时间线。滤镜配置在付费前
读取一次；跨网络分片保持一个 FFmpeg 状态，取消不刷新尾音。生产 PCM
与能量都在调音后取得，避免口型使用与现场声音不同的音轨。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable
from contextlib import aclosing
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from purrcept_engine.infrastructure.audio.wav import encode_wav
from purrcept_engine.plugin_system import AudioError
from purrcept_engine.plugins.audio.models import SpeechAudio


class VoiceProfile(BaseModel):
    """调音室与 bot 共用的冻结预设，enabled=false 时不采用其余滤镜。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)
    version: int = Field(default=1, ge=1, le=1)
    enabled: bool = False
    pitch_semitones: float = Field(default=0.0, ge=-3, le=3)
    highpass_hz: float = Field(default=0.0, ge=0, le=200)
    low_db: float = Field(default=0.0, ge=-9, le=9)
    mid_db: float = Field(default=0.0, ge=-9, le=9)
    mid_hz: float = Field(default=1800.0, ge=300, le=5000)
    mid_q: float = Field(default=0.8, ge=0.3, le=3)
    high_db: float = Field(default=0.0, ge=-9, le=9)
    gain_db: float = Field(default=0.0, ge=-18, le=6)
    limiter: bool = True

    def filters(self, sample_rate: int) -> str:
        """连续滤镜保持音高调整后的时长，并补偿 limiter lookahead。

        Args:
            sample_rate: PCM 的明确采样率（Hz）。

        Raises:
            AudioError: 调音中频必须低于 Nyquist 频率。
        """
        nodes: list[str] = []
        if self.enabled:
            # 调音语义以目标mono采样率为基准，不能在远端原采样率上
            # 使用asetrate，否则不同源文件会产生不同语速与音高。
            nodes.extend(
                (f"aresample={sample_rate}", "aformat=sample_fmts=s16:channel_layouts=mono")
            )
            if self.pitch_semitones:
                shifted = round(sample_rate * 2 ** (self.pitch_semitones / 12))
                nodes.extend(
                    (
                        f"asetrate={shifted}",
                        f"aresample={sample_rate}",
                        f"atempo={sample_rate / shifted}",
                    )
                )
            if self.highpass_hz:
                nodes.append(f"highpass=f={self.highpass_hz}")
            if self.low_db:
                nodes.append(f"bass=g={self.low_db}:f=200")
            if self.mid_db:
                if self.mid_hz >= sample_rate / 2:
                    raise AudioError("调音中频必须低于 Nyquist 频率。")
                nodes.append(f"equalizer=f={self.mid_hz}:t=q:w={self.mid_q}:g={self.mid_db}")
            if self.high_db:
                nodes.append(f"treble=g={self.high_db}:f={min(6000, sample_rate * 0.4)}")
            if self.gain_db:
                nodes.append(f"volume={self.gain_db}dB")
            if self.limiter:
                nodes.append(
                    "alimiter=limit=0.891250938:attack=5:release=60:level=false:latency=true"
                )
        return ",".join(nodes)


def read_profile(path: Path | None) -> VoiceProfile:
    """缺失文件是明确旁通。

    Args:
        path: 此操作定位的明确路径。

    Returns:
        已校验的声线预设。

    Raises:
        AudioError: 声线预设超过16 KiB。

    Note:
        已存在的坏文件、超限或 I/O 错误不得静默回退。
    """
    if path is None:
        return VoiceProfile()
    try:
        with path.open("rb") as stream:
            data = stream.read(16_385)
    except FileNotFoundError:
        return VoiceProfile()
    if len(data) > 16_384:
        raise AudioError("声线预设超过16 KiB。")
    return VoiceProfile.model_validate_json(data)


def check_program(executable: str) -> None:
    """显式输出启用后检查程序，不自动安装或猜另一播放器。

    Args:
        executable: 显式配置的外部程序路径，不自动寻找或安装替代程序。

    Raises:
        AudioError: 参数、材料或当前状态不满足此入口的约束。
    """
    if shutil.which(executable) is None:
        raise AudioError(f"音频程序不可用：{executable}。")


async def _diagnostics(stream: asyncio.StreamReader) -> bytes:
    """诊断超限仍排空管道，避免停止读取造成FFmpeg写阻塞。"""
    collected = bytearray()
    overflow = False
    while piece := await stream.read(2048):
        if len(collected) + len(piece) > 8192:
            overflow = True
        if len(collected) < 8192:
            collected.extend(piece[: 8192 - len(collected)])
    if overflow:
        raise AudioError("FFmpeg诊断超过8 KiB。")
    return bytes(collected)


async def _decode_raw(
    encoded: AsyncIterator[bytes],
    *,
    executable: str,
    sample_rate: int,
    input_format: Literal["mp3", "wav", "flac"] | None = None,
    max_seconds: int = 180,
) -> AsyncGenerator[bytes, None]:
    """单次 FFmpeg 连续解码，网络写入与 PCM 读取并行但都由本迭代器收回。

    管道自然背压限制缓存；PCM 总长最多180秒。解码器失败保留 stderr
    错误，不继续刷新旧数据。取消或关闭迭代器先终止进程再归还资源。
    """
    if not 1 <= max_seconds <= 300 or not 8000 <= sample_rate <= 48000:
        raise ValueError("解码时长必须在1..300秒，采样率必须在8000..48000Hz。")
    check_program(executable)
    arguments = [executable, "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "pipe"]
    if input_format is not None:
        arguments.extend(("-f", input_format))
    arguments.extend(("-i", "pipe:0", "-ac", "1", "-ar", str(sample_rate)))
    arguments.extend(("-f", "s16le", "pipe:1"))
    launch = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    )
    try:
        process = await asyncio.shield(launch)
    except asyncio.CancelledError:
        # 取得资源不能随启动等待消失；迟到进程只清理，不能继续解码。
        process = await launch
        if process.returncode is None:
            process.kill()
        await process.wait()

        raise
    stdin, stdout, stderr = process.stdin, process.stdout, process.stderr
    if stdin is None or stdout is None or stderr is None:
        raise RuntimeError("音频解码子进程缺少已请求管道。")

    async def feed() -> None:
        """按原始顺序向解码进程写入素材，并在结束时关闭标准输入。"""
        try:
            async for chunk in encoded:
                stdin.write(chunk)
                await stdin.drain()
        finally:
            stdin.close()

    feeder = asyncio.create_task(feed(), name="audio-decode-feed")

    errors = asyncio.create_task(_diagnostics(stderr), name="audio-decode-errors")
    count = 0
    try:
        while chunk := await stdout.read(4096):
            count += len(chunk)
            if count > sample_rate * 2 * max_seconds:
                raise AudioError(f"解码音频超过{max_seconds}秒。")
            yield chunk
        pipe_closed = False
        try:
            await feeder
        except (BrokenPipeError, ConnectionResetError):
            # 非法媒体可能先让解码器关闭输入；最终进程/诊断拥有外部错误分类。
            pipe_closed = True
        code = await process.wait()
        error = await errors
        if code != 0 or error or pipe_closed:
            raise AudioError("FFmpeg 音频解码或调音失败。")
        if count == 0 or count % 2:
            raise AudioError("FFmpeg 未产生完整 PCM。")
    finally:
        if process.returncode is None:
            process.kill()
        feeder.cancel()
        await asyncio.gather(feeder, errors, return_exceptions=True)
        await process.wait()


async def decode_pcm(
    encoded: AsyncIterator[bytes],
    *,
    executable: str,
    sample_rate: int,
    profile: VoiceProfile,
    input_format: Literal["mp3", "wav", "flac"] | None = None,
    max_seconds: int = 180,
) -> AsyncGenerator[bytes, None]:
    """先解码目标mono PCM，再以固定原样本数应用一段独立调音状态。

    Args:
        encoded: 合成源实际交付的有界编码音频流，不在解码器内重新请求合成。
        executable: 显式配置的外部程序路径，不自动寻找或安装替代程序。
        sample_rate: PCM 的明确采样率（Hz）。
        profile: 当前操作采用的已校验模型或声线预设。
        input_format: 明确的输入编码格式；None 由解码器识别。
        max_seconds: 本次音频处理允许的最大时长（秒）。 默认 180。

    Yields:
        按原始采集、解码或播放顺序逐块交付的完整字节流。

    Note:
        禁用预设只有解码进程。启用时第二进程接收PCM并补偿滤镜尾帧，
        变调/limiter不得增加或减少原采样数；取消丢弃尾帧，不补发旧音频。
    """
    profile.filters(sample_rate)
    async with aclosing(
        _decode_raw(
            encoded,
            executable=executable,
            sample_rate=sample_rate,
            input_format=input_format,
            max_seconds=max_seconds,
        )
    ) as raw:
        if profile.enabled:
            async with aclosing(
                _filter_pcm(raw, executable=executable, sample_rate=sample_rate, profile=profile)
            ) as processed:
                async for chunk in processed:
                    yield chunk
        else:
            async for chunk in raw:
                yield chunk


async def _filter_pcm(
    pcm: AsyncIterator[bytes], *, executable: str, sample_rate: int, profile: VoiceProfile
) -> AsyncGenerator[bytes, None]:
    """连续滤镜只返回原采样总数，两秒补零推进尾音而不扩大输出时长。"""
    launch = asyncio.create_task(
        asyncio.create_subprocess_exec(
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-af",
            profile.filters(sample_rate),
            "-f",
            "s16le",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    )
    process: asyncio.subprocess.Process | None = None
    feeder: asyncio.Task[None] | None = None
    diagnostics: asyncio.Task[bytes] | None = None
    try:
        created = await asyncio.shield(launch)
        process = created
        stdin, stdout, stderr = created.stdin, created.stdout, created.stderr
        if stdin is None or stdout is None or stderr is None:
            raise RuntimeError("音频调音进程缺少已请求管道。")
        received, emitted = 0, 0

        async def feed() -> None:
            nonlocal received
            try:
                async for chunk in pcm:
                    received += len(chunk)
                    stdin.write(chunk)
                    await stdin.drain()
                # atempo需要相邻窗口；补零只推进滤镜，输出另按received裁切。
                stdin.write(bytes(sample_rate * 2 * 2))
                await stdin.drain()
            finally:
                stdin.close()

        feeder = asyncio.create_task(feed(), name="audio-filter-feed")
        diagnostics = asyncio.create_task(_diagnostics(stderr), name="audio-filter-errors")
        while chunk := await stdout.read(4096):
            available = max(0, received - emitted)
            part = chunk[:available]
            emitted += len(part)
            if part:
                yield part
        await feeder
        error = await diagnostics
        code = await created.wait()
        if code != 0 or error or emitted != received:
            raise AudioError("FFmpeg调音未完整刷新原采样数。")
    finally:
        if process is None:
            process = await launch
        if process.returncode is None:
            process.kill()
        # 局部任务只在资源取得后创建；关闭迭代器不能留下输入/诊断借用。
        operations: list[Awaitable[object]] = []
        if feeder is not None:
            feeder.cancel()
            operations.append(feeder)
        if diagnostics is not None:
            operations.append(diagnostics)
        if operations:
            await asyncio.gather(*operations, return_exceptions=True)
        await process.wait()


async def process_audio(
    audio: SpeechAudio, *, executable: str, sample_rate: int, profile: VoiceProfile
) -> SpeechAudio:
    """完整文件沿同一PCM/DSP图导出原格式。

    Args:
        audio: 包含实际音频字节、编码与采样信息的素材。
        executable: 显式配置的外部程序路径，不自动寻找或安装替代程序。
        sample_rate: PCM 的明确采样率（Hz）。
        profile: 当前操作采用的已校验模型或声线预设。

    Returns:
        实际合成或处理完成的完整音频与采样信息。

    Raises:
        AudioError: 调音后MP3导出失败；调音导出为空或超过8 MiB。

    Note:
        旁通保留原编码字节。

        PCM展开最多300秒，MP3编码只执行一次，不把重新编码延迟解释为
        播放进度。输出最多8 MiB；本函数不播放或保存文件。
    """
    if not profile.enabled:
        return audio

    async def chunks() -> AsyncGenerator[bytes, None]:
        """按原始顺序交付完整字节块，不改变素材内容。"""
        yield audio.data

    async with aclosing(
        decode_pcm(
            chunks(),
            executable=executable,
            sample_rate=sample_rate,
            profile=profile,
            input_format=audio.format,
            max_seconds=300,
        )
    ) as decoded:
        pcm = b"".join([part async for part in decoded])
    if audio.format == "wav":
        encoded = encode_wav(pcm, sample_rate)
    else:
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "s16le",
                "-ar",
                str(sample_rate),
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "128k",
                "-f",
                "mp3",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        )
        process: asyncio.subprocess.Process | None = None
        try:
            created = await asyncio.shield(launch)
            process = created
            encoded, error = await created.communicate(pcm)
            if created.returncode != 0 or error:
                raise AudioError("调音后MP3导出失败。")
        finally:
            if process is None:
                process = await launch
            if process.returncode is None:
                process.kill()
            await process.wait()
    if not encoded or len(encoded) > 8 * 1024 * 1024:
        raise AudioError("调音导出为空或超过8 MiB。")
    return SpeechAudio(encoded, audio.format, len(pcm) / 2 / sample_rate)
