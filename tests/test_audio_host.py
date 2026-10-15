"""真实Host装配/Worker/Store验证声音与独立身体的调用方接线。

两种构造顺序都要在start前完成有限端口绑定。所有网络/设备是替身，
动作由真实Worker和冻结能力目录执行，回执来自同一个正式Store。
不连接真实模型、TTS、消息、麦克风、扬声器或VTS。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from purrcept_engine import EngineConfig, EngineHost
from purrcept_engine.plugins.audio.models import (
    AudioSettings,
    PlaybackResult,
    RecordParameters,
    SpeechAudio,
    SpeechParameters,
)
from purrcept_engine.plugins.audio.plugin import make_factory as audio_factory
from purrcept_engine.plugins.live2d.models import ModelProfile
from purrcept_engine.plugins.live2d.plugin import Live2DSettings
from purrcept_engine.plugins.live2d.plugin import make_factory as body_factory
from purrcept_engine.values import decode_object


class Source:
    """真实能力绑定内的无网络合成器，只提供本地不可变字节。"""

    async def synthesize(
        self, parameters: SpeechParameters | RecordParameters, identity: str
    ) -> SpeechAudio:
        del parameters, identity
        return SpeechAudio(b"local-pcm", "mp3")

    async def stream(
        self, parameters: SpeechParameters, identity: str
    ) -> AsyncGenerator[bytes, None]:
        del parameters, identity
        yield b"local-pcm"

    async def close(self) -> None:
        return


class Player:
    """无声播放时钟替身，等身体真实send采用后再结束同身份声音。"""

    def __init__(self) -> None:
        self.played = 0
        self.visual = asyncio.Event()

    def check_available(self) -> None:
        return

    async def play(
        self,
        pcm: AsyncIterator[bytes],
        *,
        playback_id: str,
        deadline: float,
        started: Callable[[], Awaitable[None]],
        progress: Callable[[float, float], None],
    ) -> PlaybackResult:
        del playback_id, deadline
        _ = [chunk async for chunk in pcm]
        self.played += 1
        await started()
        progress(0.1, 0.4)
        await asyncio.wait_for(self.visual.wait(), 1)
        return PlaybackResult("completed", True, 0.1)

    async def interrupt(self, playback_id: str | None = None) -> None:
        del playback_id
        self.visual.set()

    async def close(self) -> None:
        return


class BodyDriver:
    """模拟视觉协议响应，但资源与播放器实例保持独立。"""

    def __init__(self, player: Player) -> None:
        self.player = player
        self.closed = False

    async def connect(self) -> None:
        return

    async def send(self, channels: dict[str, float]) -> None:
        if channels["valence"] > 0:
            self.player.visual.set()

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("body_first", [True, False])
async def test_host_assembles_once_then_real_worker_speak_links_independent_body(
    tmp_path: Path, body_first: bool
) -> None:
    source, player = Source(), Player()
    body = BodyDriver(player)
    profile = tmp_path / "profile.json"
    profile.write_text(
        '{"model_id":"offline","bindings":[{"parameter":"PurrceptSmile","sources":{"valence":1.0}}]}',
        encoding="utf-8",
    )

    def synthesis(settings: AudioSettings) -> Source:
        del settings
        return source

    def playback(settings: AudioSettings) -> Player:
        del settings
        return player

    def visual(settings: Live2DSettings, token: Path, calibration: ModelProfile) -> BodyDriver:
        del settings, token, calibration
        return body

    plugins: list[dict[str, object]] = [
        {"instance_id": "audio", "entry": "audio-test", "config": {"playback_backend": "ffplay"}},
        {"instance_id": "body", "entry": "body-test", "config": {"profile_path": str(profile)}},
    ]
    if body_first:
        plugins.reverse()
    settings: dict[str, object] = {
        "host": {"data_dir": tmp_path / "data"},
        "console": False,
        "models": {
            "offline": {
                "kind": "generation",
                "model": "openai/offline",
                "api_base": "http://127.0.0.1:9999/v1",
                "api_key": "synthetic",
                "context_window": 100000,
                "max_output_tokens": 1000,
                "capacity_rule": "reference-text-only-v1",
            }
        },
        "subject": {"foreground": "offline", "reflection": "offline", "summary": "offline"},
        "plugins": plugins,
    }
    host = EngineHost(
        EngineConfig.validate(settings, base_dir=tmp_path),
        plugin_factories={
            "audio-test": audio_factory(synthesis, playback, decode=False),
            "body-test": body_factory(visual),
        },
    )
    await host.start()
    try:
        action = await host.execute(
            'from world import me\nreturn await me.microphone.speak(text="现场", '
            'body={"valence":0.5,"gesture":"nod"})'
        )
        assert action.state == "completed"
        call = action.calls[0]
        assert call.state == "confirmed" and call.result_json is not None
        result = decode_object(call.result_json.encode())
        assert result["started"] is True and result["completed"] is True
        assert result["playback_id"] != call.call_id
        body_result = result["body"]
        assert isinstance(body_result, dict) and body_result["adopted"] is True
        assert player.played == 1
        assert "me.microphone.interrupt" in host.catalog.bindings
        assert "me.live2d.apply" in host.catalog.bindings
        assert "me.voice_input.interrupt" not in host.catalog.bindings
        assert host.catalog.bindings["me.microphone.speak"].declaration.concurrency == "parallel"
    finally:
        await host.close()
    assert body.closed
