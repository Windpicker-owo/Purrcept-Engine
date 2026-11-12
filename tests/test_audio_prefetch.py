"""用实际子进程验证媒体时钟背压，管道可写也不能提前吞完身体采样。"""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncGenerator

import pytest

from purrcept_engine.plugins.audio import player as player_module
from purrcept_engine.plugins.audio.player import FFplayPlayer


async def test_stalled_media_clock_bounds_prefetch_and_remains_interruptible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """播放器仍消费管道但媒体时钟暂停；上游保持不足一秒，停止立即归还。"""

    async def launch(*arguments: str) -> asyncio.subprocess.Process:
        del arguments
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys\nsys.stderr.write('0.0 M-A: 0.000 fd=0\\r')\n"
            "sys.stderr.flush()\nsys.stdin.buffer.read()\n",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    monkeypatch.setattr(player_module, "launch", launch)
    player = FFplayPlayer(sys.executable)
    ready = asyncio.Event()
    produced = 0

    async def pcm() -> AsyncGenerator[bytes]:
        nonlocal produced
        for _ in range(500):
            produced += 1
            if produced == 38:
                ready.set()
            yield bytes(1280)

    async def started() -> None:
        pass

    task = asyncio.create_task(
        player.play(
            pcm(),
            playback_id="bounded",
            deadline=asyncio.get_running_loop().time() + 5,
            started=started,
            progress=lambda elapsed, energy: None,
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), 2)
        await asyncio.sleep(0.1)
        assert 38 <= produced <= 40
        await player.interrupt()
        result = await asyncio.wait_for(task, 1)
        assert result.status == "interrupted" and result.started
    finally:
        await player.close()
        await task
