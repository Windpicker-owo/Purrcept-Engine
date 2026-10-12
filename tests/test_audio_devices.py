"""本地无声子进程和FFmpeg内存音频验证设备生命周期与调音。

FFplay被明确替换为只报告模拟媒体时钟的Python进程；FFmpeg只读本次
合成WAV，不打开麦克风、扬声器、网络或VTS。真实DLL和设备效果另需验收。
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

from purrcept_engine.plugins.audio import player as player_module
from purrcept_engine.plugins.audio.dsp import VoiceProfile, decode_pcm, encode_wav, process_audio
from purrcept_engine.plugins.audio.models import AudioError, SpeechAudio
from purrcept_engine.plugins.audio.player import FFplayPlayer


async def test_ffplay_uses_reported_clock_and_reclaims_actual_local_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched, permit = asyncio.Event(), asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []
    script = (
        "import sys\nsys.stdin.buffer.read()\n"
        "sys.stderr.write('0.10 M-A: 0.000 fd=0\\r')\nsys.stderr.flush()\n"
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
