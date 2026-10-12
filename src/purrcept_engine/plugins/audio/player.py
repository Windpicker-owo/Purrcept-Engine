"""独立 FFplay PCM 播放器与真实播放时钟，不合成或拥有 Live2D。

设备进度来自 FFplay 报出的媒体时钟；管道写入仅表示排队，不能触发
BodyIntent。停止先设置本次撤销标记，再杀掉当前进程；启动窗口也要
收回已创建进程。临时网络/解码迭代器由调用方收回，不跨播放复用。
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import struct
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from .dsp import check_program
from .models import AudioError, PlaybackResult

_CLOCK = re.compile(rb"^\s*(-?\d+(?:\.\d+)?)\s+[AM]-[AV]:")


class AudioPlayer(Protocol):
    """音频所有者创建、串行借用的设备，内置 FFplayPlayer。

    play 接受调音后的 mono S16 PCM，必须以实际媒体时钟回调 started，
    不能以请求、进程创建或写入管道宣称播放开始。interrupt 不能永久关闭
    输出；close 才拒绝新播放。失败可能已经播放，结果不得触发自动重试。
    """

    def check_available(self) -> None:
        """付费合成前检查本地依赖，不能打开设备或播放。"""
        ...

    async def play(
        self,
        pcm: AsyncIterator[bytes],
        *,
        playback_id: str,
        deadline: float,
        started: Callable[[], Awaitable[None]],
        progress: Callable[[float, float], None],
    ) -> PlaybackResult:
        """等待资源实际终止，期限覆盖设备等待；取消保留真实终态。"""
        ...

    async def interrupt(self, playback_id: str | None = None) -> None:
        """撤当前启动/播放资格并等待当前进程真正退出。"""
        ...

    async def close(self) -> None:
        """禁止新播放并收回当前资源，重复关闭等待同一事实。"""
        ...


async def launch(*arguments: str) -> asyncio.subprocess.Process:
    """拥有启动任务直到实际返回；取消窗口也收回已创建子进程。

    shield 保护的是资源取得，不赋予迟到播放资格。取消后先取回进程并
    终止，再重新抛出取消，避免 Windows 启动期间遗留无主资源。
    """
    task = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        process = await task
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise


class FFplayPlayer:
    """仅占有音频子进程；FFplay 时钟确认不证明物理听感或精确 DAC 延迟。"""

    def __init__(
        self,
        executable: str = "ffplay",
        *,
        sample_rate: int = 32000,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """保存冻结程序/采样率，构造不创建播放器。"""
        self._executable, self._rate, self._clock = executable, sample_rate, monotonic
        self._process: asyncio.subprocess.Process | None = None
        self._closed, self._revoked = False, False
        self._playing = False
        self._identity: str | None = None

    def check_available(self) -> None:
        """关闭或缺失程序在任何付费合成前明确失败。"""
        if self._closed:
            raise AudioError("音频播放器已关闭。")
        check_program(self._executable)

    async def play(
        self,
        pcm: AsyncIterator[bytes],
        *,
        playback_id: str,
        deadline: float,
        started: Callable[[], Awaitable[None]],
        progress: Callable[[float, float], None],
    ) -> PlaybackResult:
        """一个媒体时钟对应一个身份，真实 started 之后才联动视觉。"""
        self.check_available()
        if self._playing:
            raise RuntimeError("音频队列违反单播放器不变量。")
        self._playing, self._revoked = True, False
        self._identity = playback_id
        adopted, elapsed = False, 0.0
        energies: list[float] = []
        pending = bytearray()
        frame_size = max(2, self._rate // 50 * 2)
        failures: list[bytes] = []
        status: str = "completed"
        process: asyncio.subprocess.Process | None = None
        tasks: list[asyncio.Task[None]] = []
        try:
            process = await launch(
                self._executable,
                "-nodisp",
                "-hide_banner",
                "-autoexit",
                "-loglevel",
                "error",
                "-stats",
                "-f",
                "s16le",
                "-ar",
                str(self._rate),
                "-ac",
                "1",
                "-i",
                "pipe:0",
            )
            self._process = process
            stdin, stderr = process.stdin, process.stderr
            if stdin is None or stderr is None:
                raise RuntimeError("播放器缺少已请求管道。")
            if self._revoked or self._closed:
                if process.returncode is None:
                    process.kill()
                return PlaybackResult("not_started", False)

            async def feed() -> None:
                try:
                    async for chunk in pcm:
                        if self._revoked or self._closed:
                            return
                        pending.extend(chunk)
                        while len(pending) >= frame_size:
                            frame = bytes(pending[:frame_size])
                            del pending[:frame_size]
                            samples = struct.unpack("<" + "h" * (len(frame) // 2), frame)
                            energy = (
                                math.sqrt(sum(sample * sample for sample in samples) / len(samples))
                                / 32768
                            )
                            energies.append(min(1.0, energy * 4))
                        stdin.write(chunk)
                        await stdin.drain()
                finally:
                    stdin.close()

            async def observe() -> None:
                nonlocal adopted, elapsed
                buffer = bytearray()
                while piece := await stderr.read(2048):
                    buffer.extend(piece)
                    if len(buffer) > 8192:
                        raise AudioError("播放器诊断帧超过8 KiB。")
                    while (boundary := re.search(rb"[\r\n]", buffer)) is not None:
                        line = bytes(buffer[: boundary.start()])
                        del buffer[: boundary.end()]
                        if match := _CLOCK.match(line):
                            position = float(match.group(1))
                            if not math.isfinite(position) or position > 300:
                                raise AudioError("播放器媒体时钟无效。")
                            if position >= 0 and not self._revoked:
                                if not adopted:
                                    adopted = True
                                    await started()
                                elapsed = max(elapsed, position)
                                index = min(len(energies) - 1, max(0, int(position * 50)))
                                progress(position, 0.0 if index < 0 else energies[index])
                        elif line.strip() and b"nan" not in line:
                            failures.append(line[:1024])
                            if len(failures) > 8:
                                raise AudioError("播放器报告过多错误。")
                if buffer.strip() and _CLOCK.match(buffer) is None and b"nan" not in buffer:
                    failures.append(bytes(buffer[:1024]))

            tasks = [
                asyncio.create_task(feed(), name="audio-player-feed"),
                asyncio.create_task(observe(), name="audio-player-clock"),
            ]
            try:
                async with asyncio.timeout(max(0.0, deadline - self._clock())):
                    await asyncio.gather(*tasks)
                    code = await process.wait()
                if self._revoked:
                    status = "interrupted" if adopted else "not_started"
                elif code != 0 or failures or not adopted:
                    status = "failed"
            except TimeoutError:
                status = "timed_out" if adopted else "not_started"
            except asyncio.CancelledError:
                status = "interrupted" if adopted else "not_started"
            except (BrokenPipeError, ConnectionResetError):
                if not self._revoked:
                    status = "failed"
                else:
                    status = "interrupted" if adopted else "not_started"
            if status == "completed":
                return PlaybackResult("completed", adopted, elapsed)
            if status == "interrupted":
                return PlaybackResult("interrupted", adopted, elapsed)
            if status == "timed_out":
                return PlaybackResult("timed_out", adopted, elapsed, "现场播放期限已到。")
            if status == "not_started":
                return PlaybackResult("not_started", False, elapsed, "播放资格已撤销或期限已到。")
            return PlaybackResult("failed", adopted, elapsed, "播放器未确认正常结束。")
        finally:
            self._revoked = True
            if process is not None and process.returncode is None:
                process.kill()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if process is not None:
                await process.wait()
            if self._process is process:
                self._process = None
            self._playing = False
            if self._identity == playback_id:
                self._identity = None

    async def interrupt(self, playback_id: str | None = None) -> None:
        """启动与进程共享撤销标记，不能让迟到启动重新发声。"""
        if playback_id is not None and playback_id != self._identity:
            return
        self._revoked = True
        process = self._process
        if process is not None:
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def close(self) -> None:
        """释放音频资源，完全不触碰身体连接。"""
        self._closed = True
        await self.interrupt()
