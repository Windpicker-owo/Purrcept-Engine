"""独立OVR分析进程的有限PCM请求，不拥有扬声器或VTS连接。

每次请求只有一个20ms调音后采样块，主身体任务串行等真实结果。原生
初始化与崩溃隔离在该进程；宿主不加载DLL。取消等待不能遗留进程，
关闭持有同一取得/退出任务，样本身份由身体核验后才可驱动当前口型。
"""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from purrcept_engine.plugins.live2d.models import VISEMES, BodyError
from purrcept_engine.values import decode_object, encode_json


@dataclass(frozen=True, slots=True)
class VisemeFrame:
    """分析位置已减去OVR延迟，权重严格沿固定十五视素顺序。

    Attributes:
        sample: 当前输入的原始采样值。
        weights: 当前实际分析得到的各音素通道权重。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    sample: int
    weights: tuple[float, ...]


class VisemeAnalyzer(Protocol):
    """身体独占的独立原生分析资源，内置AnalyzerProcess。

    start 取得资源，reset 在新播放真实媒体身份建立时清旧识别状态。
    analyze 串行调用，不代表样本已经DAC播放；运行时另以实际播放时钟
    选择对应采样帧。close 回收全部进程/请求后返回，不影响音频播放器。
    """

    async def start(self) -> None:
        """取得独立分析进程并确认OVR初始化。"""
        ...

    async def reset(self) -> None:
        """串行清除上一媒体识别状态。"""
        ...

    async def analyze(self, pcm: bytes, sample: int) -> VisemeFrame:
        """分析一个20msS16块，不能吞DLL错误降级冒充视素成功。

        Args:
            pcm: 已解码的 PCM 字节或按播放顺序产生的字节流。
            sample: 当前输入的原始采样值。

        Returns:
            当前 PCM 片段实际分析得到的口型权重。
        """
        ...

    async def close(self) -> None:
        """实际退出后返回，失败时仍保留资源引用。"""
        ...


class AnalyzerProcess:
    """每个身体实例只拥有一个OVR子进程，不与音频共享崩溃边界。"""

    def __init__(self, library: Path, sample_rate: int) -> None:
        """冻结显式DLL与采样率，构造不加载任何原生资源。

        Args:
            library: 固定版本的本地分析动态库路径，仅在独立进程加载。
            sample_rate: PCM 的明确采样率（Hz）。
        """
        self._library, self._rate = library, sample_rate
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> None:
        """有界等待ready，启动期间取消也收回已经创建的进程。

        Raises:
            BodyError: 身体或分析器未就绪，原生协议材料无效，或资源无法完成此次操作。
            RuntimeError: OVR进程缺少已请求stdout。
        """
        if self._process is not None or self._closed:
            raise BodyError("OVR分析资源不能重复创建。")
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "purrcept_engine.plugins.live2d.analyzer_worker",
                "--library",
                str(self._library),
                "--rate",
                str(self._rate),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=16384,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        )
        try:
            self._process = await asyncio.shield(launch)
        except asyncio.CancelledError:
            self._process = await launch
            await self.close()
            raise
        process = self._process
        if process is None or process.stdout is None:
            raise RuntimeError("OVR进程缺少已请求stdout。")
        try:
            async with asyncio.timeout(10):
                ready = decode_object(await process.stdout.readline())
        except (TimeoutError, ValueError) as error:
            raise BodyError("OVR分析进程未确认初始化。") from error
        if ready.get("ready") is not True:
            raise BodyError("OVR分析进程初始化失败。")

    async def _request(self, request: bytes) -> bytes:
        """单帧JSON仅含有界PCM/样本位置，通信故障不静默返回零视素。"""
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise BodyError("OVR分析进程未在线。")
        async with self._lock, asyncio.timeout(2):
            try:
                process.stdin.write(request + b"\n")
                await process.stdin.drain()
                response = await process.stdout.readline()
            except (BrokenPipeError, ConnectionResetError) as error:
                raise BodyError("OVR分析进程已断开。") from error
            if not response:
                raise BodyError("OVR分析进程在回执前退出。")
            return response

    async def reset(self) -> None:
        """清旧上下文的真实确认先于新媒体分析。

        Raises:
            BodyError: OVR重置未确认。
        """
        response = decode_object(await self._request(encode_json({"operation": "reset"})))
        if response.get("reset") is not True:
            raise BodyError("OVR重置未确认。")

    async def analyze(self, pcm: bytes, sample: int) -> VisemeFrame:
        """核验原生结果的采样位置和十五项有限概率，不猜遗漏字段。

        Args:
            pcm: 已解码的 PCM 字节或按播放顺序产生的字节流。
            sample: 当前输入的原始采样值。

        Returns:
            当前 PCM 片段实际分析得到的口型权重。

        Raises:
            ValueError: OVR需要一个20ms完整S16采样块。
            BodyError: 身体或分析器未就绪，原生协议材料无效，或资源无法完成此次操作。
        """
        if len(pcm) != self._rate // 50 * 2 or sample < 0:
            raise ValueError("OVR需要一个20ms完整S16采样块。")
        try:
            response = decode_object(
                await self._request(
                    encode_json(
                        {
                            "operation": "analyze",
                            "sample": sample,
                            "pcm": base64.b64encode(pcm).decode(),
                        }
                    )
                )
            )
        except TimeoutError as error:
            raise BodyError("OVR分析回执超时。") from error
        position, weights = response.get("sample"), response.get("weights")
        if (
            not isinstance(position, int)
            or isinstance(position, bool)
            or position < 0
            or not isinstance(weights, list)
            or len(weights) != len(VISEMES)
        ):
            raise BodyError("OVR视素回执字段无效。")
        values: list[float] = []
        for weight in weights:
            if (
                not isinstance(weight, (int, float))
                or isinstance(weight, bool)
                or not 0 <= weight <= 1
            ):
                raise BodyError("OVR视素权重无效。")
            values.append(float(weight))
        return VisemeFrame(position, tuple(values))

    async def close(self) -> None:
        """给正常EOF收尾机会，有限等待后终止，进程退出才清引用。"""
        self._closed = True
        process = self._process
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            async with asyncio.timeout(2):
                await process.wait()
        except TimeoutError:
            if process.returncode is None:
                process.kill()
            await process.wait()
        if self._process is process:
            self._process = None
