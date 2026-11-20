"""使用现有 FFmpeg 的 Whisper 滤镜转写一个分 P 内的选定区间。

音轨由网页适配器取得，临时文件仅存活到本次识别结束。输出时间从裁切后
的音频相对秒数映射回分 P 时间轴。引擎模型只收到已完成的转写文本。
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO

from purrcept_engine.values import decode_object

from .source import SourceError, TranscriptLine


async def transcribe(
    audio: BinaryIO,
    model: Path,
    start: float,
    end: float,
) -> tuple[TranscriptLine, ...]:
    """转写窗口内的本地音轨，保留 Whisper 给出的每句起止时间。"""
    with audio, TemporaryDirectory(prefix="purrcept-bili-asr-") as folder:
        input_file = Path(folder) / "audio.m4s"
        await asyncio.to_thread(input_file.write_bytes, audio.read())
        # FFmpeg 先解析滤镜图，再解析滤镜参数；文件名要分别转义两层。
        option = re.sub(r"([\\':\s])", r"\\\1", model.name)
        option = re.sub(r"([\\'\[\],;\s])", r"\\\1", option)
        filter_graph = (
            f"asetpts=PTS-STARTPTS,whisper=model={option}:language=auto:queue=20"
            ":use_gpu=false:destination=-:format=json"
        )
        try:
            worker = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-protocol_whitelist",
                "file,pipe",
                "-f",
                "mov",
                "-ss",
                f"{start:.6f}",
                "-t",
                f"{end - start:.6f}",
                "-i",
                str(input_file),
                "-map",
                "0:a:0",
                "-af",
                filter_graph,
                "-f",
                "null",
                "-",
                cwd=model.parent,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError as error:
            raise SourceError("asr_unavailable", f"无法启动本地 FFmpeg：{error}") from error
        try:
            async with asyncio.timeout(35):
                stdout, stderr = await worker.communicate()
        except TimeoutError as error:
            raise SourceError("asr_timeout", "所选片段的本地转写超时。") from error
        finally:
            if worker.returncode is None:
                with suppress(ProcessLookupError):
                    worker.kill()
                await worker.communicate()
        if worker.returncode != 0:
            details = stderr.decode("utf-8", errors="replace").strip().splitlines()
            message = details[-1] if details else f"退出码 {worker.returncode}"
            raise SourceError("asr_failed", f"本地转写失败：{message}")

    lines: list[TranscriptLine] = []
    for raw in stdout.splitlines():
        if not raw.strip():
            continue
        try:
            segment = decode_object(raw)
            begin, finish, text = segment.get("start"), segment.get("end"), segment.get("text")
            if type(begin) is not int or type(finish) is not int or not isinstance(text, str):
                raise ValueError("转写结果缺少时间或文本。")
        except ValueError as error:
            raise SourceError("asr_failed", "FFmpeg 返回了无效的转写片段。") from error
        first = max(start, start + begin / 1000)
        last = min(end, start + finish / 1000)
        if first < last and text.strip():
            lines.append(TranscriptLine(start=first, end=last, text=text.strip()[:4000]))
    return tuple(lines)
