"""本地无声子进程和FFmpeg内存音频验证设备生命周期与调音。

生命周期测试以Python进程报告模拟媒体时钟；真实FFplay接缝强制SDL dummy
输出，FFmpeg只读本次合成WAV。测试不打开麦克风、扬声器、网络或VTS。
真实DLL和设备效果另需验收。
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import struct
import subprocess
import sys
import wave
from collections.abc import AsyncGenerator
from contextlib import aclosing

import pytest

from purrcept_engine.domain.audio.errors import AudioError
from purrcept_engine.infrastructure.audio.wav import encode_wav
from purrcept_engine.plugins.audio import dsp as dsp_module
from purrcept_engine.plugins.audio import player as player_module
from purrcept_engine.plugins.audio.dsp import VoiceProfile, decode_pcm, process_audio
from purrcept_engine.plugins.audio.models import SpeechAudio
from purrcept_engine.plugins.audio.player import FFplayPlayer


@pytest.mark.parametrize("filtered", [False, True])
async def test_real_decoder_close_reclaims_a_full_output_pipe(
    monkeypatch: pytest.MonkeyPatch, filtered: bool
) -> None:
    """消费端提前退出时丢弃管道积压，停止不能等已经暂停读取的 stdout 关闭。"""
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("本地 FFmpeg 未安装。")
    create = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []

    async def launch(*arguments: str, **options: object) -> asyncio.subprocess.Process:
        del options
        process = await create(
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(dsp_module.asyncio, "create_subprocess_exec", launch)

    async def encoded() -> AsyncGenerator[bytes, None]:
        yield encode_wav(bytes(32000 * 2 * 20), 32000)

    decoded = decode_pcm(
        encoded(),
        executable=executable,
        sample_rate=32000,
        profile=VoiceProfile(enabled=filtered),
    )
    assert await anext(decoded)
    # 故意停止消费，给真实子进程填满有限管道的机会；这不涉及声音设备。
    await asyncio.sleep(0.2)
    closing = asyncio.create_task(decoded.aclose())
    try:
        done, _ = await asyncio.wait((closing,), timeout=1)
        assert done, "解码关闭被未消费的 PCM 管道阻塞。"
        await closing
        assert all(process.returncode is not None for process in processes)
    finally:
        # 回归失败时也排空本次真实管道，避免测试留下进程或取消正在清理的所有者。
        for process in reversed(processes):
            if process.returncode is None:
                process.kill()
            if process.stdout is not None:
                while await process.stdout.read(65536):
                    pass
        await closing


async def test_ffplay_uses_reported_clock_and_reclaims_actual_local_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched, permit = asyncio.Event(), asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []
    script = (
        "import sys\nsys.stdin.buffer.read()\n"
        "sys.stderr.write('\\x1b[2K0.10 M-A: 0.000 fd=0\\r\\x1b[2K')\nsys.stderr.flush()\n"
    )

    async def launch(*arguments: str) -> asyncio.subprocess.Process:
        del arguments
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        processes.append(process)
        launched.set()
        return process

    monkeypatch.setattr(player_module, "launch", launch)
    owner = FFplayPlayer(sys.executable)
    actual_started = asyncio.Event()
    positions: list[float] = []

    async def pcm() -> AsyncGenerator[bytes, None]:
        await permit.wait()
        yield bytes(3200)

    async def started() -> None:
        actual_started.set()

    def progress(position: float, energy: float) -> None:
        del energy
        positions.append(position)

    task = asyncio.create_task(
        owner.play(
            pcm(),
            playback_id="play",
            deadline=asyncio.get_running_loop().time() + 5,
            started=started,
            progress=progress,
        )
    )
    await launched.wait()
    assert not actual_started.is_set()
    permit.set()
    result = await task
    assert result.status == "completed" and result.started
    assert positions == [0.1]
    assert all(process.returncode is not None for process in processes)
    await owner.close()


async def test_real_ffplay_pcm_command_and_clock_with_dummy_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实二进制核验命令行与时钟解析，SDL dummy 保证不取得扬声器。"""
    executable = shutil.which("ffplay")
    if executable is None:
        pytest.skip("本地 FFplay 未安装。")
    monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
    owner = FFplayPlayer(executable)
    actual_started = asyncio.Event()
    positions: list[float] = []

    async def pcm() -> AsyncGenerator[bytes, None]:
        yield bytes(64000)

    async def started() -> None:
        actual_started.set()

    def progress(position: float, energy: float) -> None:
        del energy
        positions.append(position)

    try:
        result = await owner.play(
            pcm(),
            playback_id="dummy-output",
            deadline=asyncio.get_running_loop().time() + 10,
            started=started,
            progress=progress,
        )
        assert result.status == "completed", result.error
        assert result.started and actual_started.is_set()
        assert positions and positions[-1] > 0
    finally:
        await owner.close()


@pytest.mark.parametrize("broken_pipe", [False, True])
async def test_ffplay_failure_preserves_exit_code_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
    broken_pipe: bool,
) -> None:
    """启动失败的错误回执须保留诊断，包含 stderr 晚于管道关闭的情况。"""
    exited = asyncio.Event()

    async def launch(*arguments: str) -> asyncio.subprocess.Process:
        del arguments
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('Option not found\\n'); sys.exit(1)",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if broken_pipe:
            await process.wait()
        exited.set()
        return process

    monkeypatch.setattr(player_module, "launch", launch)
    owner = FFplayPlayer(sys.executable)

    async def pcm() -> AsyncGenerator[bytes, None]:
        await exited.wait()
        if broken_pipe:
            yield bytes(64000)

    async def started() -> None:
        pytest.fail("启动失败不得报告开始播放")

    def progress(position: float, energy: float) -> None:
        pytest.fail("启动失败不得报告播放进度")

    try:
        result = await owner.play(
            pcm(),
            playback_id="invalid-command",
            deadline=asyncio.get_running_loop().time() + 5,
            started=started,
            progress=progress,
        )
        assert result.status == "failed" and not result.started
        assert result.error is not None
        assert "退出码 1" in result.error and "Option not found" in result.error
    finally:
        await owner.close()


async def test_old_stop_identity_does_not_kill_a_new_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []
    script = (
        "import sys,time\nsys.stderr.write('0.10 M-A: 0.000 fd=0\\r')\n"
        "sys.stderr.flush()\ntime.sleep(30)\n"
    )

    async def launch(*arguments: str) -> asyncio.subprocess.Process:
        del arguments
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(player_module, "launch", launch)
    owner = FFplayPlayer(sys.executable)

    async def pcm() -> AsyncGenerator[bytes, None]:
        yield bytes(3200)

    async def started() -> None:
        entered.set()

    def progress(position: float, energy: float) -> None:
        del position, energy

    task = asyncio.create_task(
        owner.play(
            pcm(),
            playback_id="new",
            deadline=asyncio.get_running_loop().time() + 5,
            started=started,
            progress=progress,
        )
    )
    await entered.wait()
    await owner.interrupt("old")
    assert processes[0].returncode is None
    await owner.interrupt("new")
    result = await task
    assert result.status == "interrupted" and result.started
    assert processes[0].returncode is not None
    await owner.close()


async def test_real_ffmpeg_wav_pipeline_preserves_bypass_and_applies_shared_gain() -> None:
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("本地FFmpeg未安装；不下载或运行设备。")
    pcm = struct.pack("<" + "h" * 3200, *([2000] * 3200))
    audio = SpeechAudio(encode_wav(pcm, 32000), "wav")
    bypass = await process_audio(
        audio, executable=executable, sample_rate=32000, profile=VoiceProfile()
    )
    assert bypass is audio
    result = await process_audio(
        audio,
        executable=executable,
        sample_rate=32000,
        profile=VoiceProfile(enabled=True, gain_db=-6.0, limiter=False),
    )
    with wave.open(io.BytesIO(result.data), "rb") as source:
        assert source.getframerate() == 32000
        assert source.getnframes() == 3200
        samples = struct.unpack("<" + "h" * 3200, source.readframes(3200))
    assert 990 <= samples[1000] <= 1010
    assert result.duration_seconds == 0.1


async def test_real_ffmpeg_invalid_audio_is_external_audio_error() -> None:
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("本地FFmpeg未安装。")

    async def encoded() -> AsyncGenerator[bytes, None]:
        yield b"RIFF" + bytes(16)

    with pytest.raises(AudioError):
        async with aclosing(
            decode_pcm(
                encoded(),
                executable=executable,
                sample_rate=32000,
                profile=VoiceProfile(),
                input_format="wav",
            )
        ) as decoded:
            _ = [part async for part in decoded]


@pytest.mark.parametrize("pitch", [-3.0, 3.0])
async def test_real_ffmpeg_pitch_filter_keeps_original_sample_count(pitch: float) -> None:
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("本地FFmpeg未安装。")
    audio = SpeechAudio(encode_wav(struct.pack("<" + "h" * 4800, *([1000] * 4800)), 24000), "wav")
    result = await process_audio(
        audio,
        executable=executable,
        sample_rate=32000,
        profile=VoiceProfile(enabled=True, pitch_semitones=pitch, limiter=False),
    )
    with wave.open(io.BytesIO(result.data), "rb") as source:
        assert source.getframerate() == 32000
        assert source.getnframes() == 6400
    assert result.duration_seconds == 0.2
