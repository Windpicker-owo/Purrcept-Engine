"""用本地语音和真实 FFplay 时钟验证 Live2D 韵律动作，不调用 TTS。

必须明确提供已校准模型、令牌和本地音频；本脚本会实际播放声音。先将短音频
解码为 mono s16le，再按正式 BodyPort 顺序交付 PCM、实际开始、媒体进度和结束。
同一段音频依次使用中性、解释、疑问、否定和强调语义，便于比较动作选择；
这些语义是演示指定，不表示分析器理解了文本。stop-file 或总期限会停止播放。
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from pathlib import Path

from purrcept_engine.plugin_system import BodyIntent, CallContext
from purrcept_engine.plugins.audio.dsp import VoiceProfile, decode_pcm
from purrcept_engine.plugins.audio.player import FFplayPlayer
from purrcept_engine.plugins.live2d.models import ModelProfile
from purrcept_engine.plugins.live2d.runtime import Live2DRuntime
from purrcept_engine.plugins.live2d.vts import VTSDriver

_RATE = 32000
_SCENES = (
    ("中性：观察口型开合惯性", BodyIntent(delivery="neutral", valence=0.0, tension=0.0)),
    (
        "友好解释：微笑、放松眼睑与点头",
        BodyIntent(delivery="explain", valence=0.65, tension=0.0, effort=0.0),
    ),
    (
        "疑问：不对称抬眉与偏头",
        BodyIntent(delivery="question", valence=0.0, tension=0.1, effort=0.0),
    ),
    (
        "认真否定：收眉、嘴角回落与摇头",
        BodyIntent(delivery="deny", valence=-0.35, tension=0.35, effort=0.15),
    ),
    (
        "强调：抬眉与较明显点头",
        BodyIntent(delivery="emphasize", valence=0.25, tension=0.0, effort=0.65),
    ),
)


async def load_pcm(path: Path, ffmpeg: str) -> bytes:
    """解码最长30秒的明确本地音频，演示不引入网络或合成费用。

    Args:
        path: 操作者选择的本地音频文件，最多8 MiB。
        ffmpeg: 明确的本机解码程序。

    Returns:
        32 kHz mono s16le，内容不写回原文件。

    Raises:
        ValueError: 文件过大或解码结果不足一个20ms采样块。
    """
    if (await asyncio.to_thread(path.stat)).st_size > 8 * 1024 * 1024:
        raise ValueError("演示音频不能超过8 MiB。")
    data = await asyncio.to_thread(path.read_bytes)

    async def encoded() -> AsyncIterator[bytes]:
        yield data

    blocks: list[bytes] = []
    async with aclosing(
        decode_pcm(
            encoded(), executable=ffmpeg, sample_rate=_RATE, profile=VoiceProfile(), max_seconds=30
        )
    ) as decoded:
        async for block in decoded:
            blocks.append(block)
    result = b"".join(blocks)
    if len(result) < _RATE // 50 * 2:
        raise ValueError("演示音频不足20ms。")
    return result


async def preview(
    profile_path: Path,
    token_path: Path,
    audio_path: Path,
    stop_file: Path,
    seconds: float,
    ffmpeg: str,
    ffplay: str,
) -> None:
    """有界播放本地语音并联动真实 VTS，关闭时归还两个设备所有者。

    Args:
        profile_path: 与当前 VTS 模型一致的人工校准。
        token_path: 本机认证令牌；不输出内容。
        audio_path: 已授权播放的本地短语音。
        stop_file: 文件出现即停止；每次启动使用不同路径。
        seconds: 整体播放期限，范围(0,900]秒。
        ffmpeg: 本机解码器路径或程序名。
        ffplay: 本机播放器路径或程序名。

    Raises:
        ValueError: 时长或输入文件无效。
        RuntimeError: VTS 未确认连接或播放器没有正常完成。
    """
    if not 0 < seconds <= 900:
        raise ValueError("演示期限须在0到900秒之间。")
    pcm = await load_pcm(audio_path, ffmpeg)
    profile = ModelProfile.model_validate_json(await asyncio.to_thread(profile_path.read_bytes))
    player = FFplayPlayer(ffplay, sample_rate=_RATE)
    player.check_available()
    body = Live2DRuntime(
        "speech-preview",
        lambda: VTSDriver("ws://127.0.0.1:8001", token_path, profile),
        gaze_calibration=profile.gaze,
        motion_seed=20261106,
    )
    stop = asyncio.Event()
    watcher: asyncio.Task[None] | None = None
    try:
        if (await body.connect()).state != "confirmed":
            raise RuntimeError("VTS连接未确认。")
        deadline = time.monotonic() + seconds
        print(f"真实播放已就绪，本地音频长{len(pcm) / (_RATE * 2):.2f}秒。", flush=True)

        async def watch_stop() -> None:
            """停止文件独立于媒体回调，播放器卡住时也能撤销播放。"""
            for _ in range(int(seconds * 10) + 2):
                if time.monotonic() >= deadline or await asyncio.to_thread(stop_file.exists):
                    break
                await asyncio.sleep(0.1)
            stop.set()
            await player.interrupt()

        watcher = asyncio.create_task(watch_stop(), name="speech-preview-stop")
        sequence = 0
        while not stop.is_set():
            for label, intent in _SCENES:
                if watcher.done():
                    watcher.result()
                if stop.is_set():
                    break
                sequence += 1
                identity = f"speech-preview-{sequence}"
                print(f"阶段{sequence}: {label}", flush=True)

                async def chunks(playback: str = identity) -> AsyncGenerator[bytes, None]:
                    """按同一PCM时间线交给身体和播放器，不用写入时刻伪造播放开始。"""
                    size = _RATE // 50 * 2
                    for offset in range(0, len(pcm), size):
                        block = pcm[offset : offset + size]
                        if len(block) == size:
                            body.playback_pcm(playback, _RATE, offset // 2, block)
                        yield block

                async def started(playback: str = identity, semantic: BodyIntent = intent) -> None:
                    result = body.playback_started(playback, semantic, deadline)
                    print(f"媒体时钟确认开始；身体受理={result['accepted']}", flush=True)

                def progress(elapsed: float, energy: float, playback: str = identity) -> None:
                    body.playback_progress(playback, elapsed, energy)

                try:
                    async with aclosing(chunks()) as source:
                        result = await player.play(
                            source,
                            playback_id=identity,
                            deadline=deadline,
                            started=started,
                            progress=progress,
                        )
                    print(
                        f"播放结果：{result.status}，媒体进度{result.elapsed_seconds:.2f}秒。",
                        flush=True,
                    )
                    if (
                        result.status != "completed"
                        and not stop.is_set()
                        and time.monotonic() < deadline
                    ):
                        raise RuntimeError(f"播放器未正常完成：{result.status}")
                finally:
                    body.playback_finished(identity)
                try:
                    await asyncio.wait_for(stop.wait(), 2.0)
                except TimeoutError:
                    pass
        await body.apply(
            BodyIntent(valence=0.0, gaze_x=0.0, gaze_y=0.0, gesture="none"),
            CallContext("speech-preview-finish", "preview", time.monotonic() + 3),
        )
        await asyncio.sleep(1.0)
    finally:
        try:
            if watcher is not None:
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass
        finally:
            # 停止观察器的真实故障仍向外报告，但不能跳过任一设备的收尾。
            try:
                await player.close()
            finally:
                await body.close()
        print("语音演示与VTS连接已关闭。", flush=True)


def main() -> None:
    """要求明确输入文件，不从账号配置读取声音素材或调用在线模型。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--token", required=True, type=Path)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--stop-file", required=True, type=Path)
    parser.add_argument("--seconds", type=float, default=180)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffplay", default="ffplay")
    args = parser.parse_args()
    asyncio.run(
        preview(
            args.profile,
            args.token,
            args.audio,
            args.stop_file,
            args.seconds,
            args.ffmpeg,
            args.ffplay,
        )
    )


if __name__ == "__main__":
    main()
