"""经真实 Host、Windows Worker 和临时 Store 验证语音表演的完整接入。

仅合成源、播放器时钟和 VTS 传输使用替身。PCM 沿 AudioRuntime 的正式
交接路径准备，Worker 通过已发布的 speak schema 选择交流意图与力度，
测试同时观察开始前静止、播放期间表情/动作/口型，以及结束后的回落。
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from pathlib import Path

from examples.live2d_dialogue_preview import LocalSpeech, build_host, run_round
from purrcept_engine import EngineConfig, EngineHost
from purrcept_engine.plugins.audio.models import (
    PlaybackResult,
    RecordParameters,
    SpeechAudio,
    SpeechParameters,
)
from purrcept_engine.plugins.audio.plugin import make_factory as audio_factory
from purrcept_engine.plugins.live2d.plugin import make_factory as body_factory
from purrcept_engine.values import decode_object


class Source:
    """提供确定性流式 PCM，记录 Worker 实际交给合成源的已校验参数。"""

    def __init__(self) -> None:
        self.parameters: SpeechParameters | None = None

    async def synthesize(
        self, parameters: SpeechParameters | RecordParameters, identity: str
    ) -> SpeechAudio:
        """本测试只允许流式路径，错误分流必须使测试失败。"""
        raise AssertionError("语音表演集成测试不应走文件合成路径。")

    async def stream(
        self, parameters: SpeechParameters, identity: str
    ) -> AsyncGenerator[bytes, None]:
        """输入包含一次能量突出和一个停顿，不连接外部服务。"""
        del identity
        self.parameters = parameters
        levels = [0.02] * 25 + [0.18] * 6 + [0.03] * 19 + [0.0] * 20
        for level in levels:
            value = round(level * 32767)
            yield struct.pack("<hh", value, -value) * 320

    async def close(self) -> None:
        """替身不拥有网络资源。"""


class Driver:
    """记录正式帧循环的输出，以帧事件协调播放替身，不直接调用内部控制器。"""

    def __init__(self) -> None:
        self.frames: list[dict[str, float]] = []
        self.frame = asyncio.Event()
        self.closed = False

    async def connect(self) -> None:
        """模拟本地协议连接成功。"""

    async def send(self, channels: dict[str, float]) -> None:
        """保留完成合成的帧，并通知播放器可以提交下一项媒体进度。"""
        self.frames.append(dict(channels))
        self.frame.set()

    async def close(self) -> None:
        """记录独立身体资源已归还。"""
        self.closed = True

    async def next_frame(self) -> None:
        """等待真实帧循环而不是手工调用 step；一秒无输出即失败。"""
        self.frame.clear()
        await asyncio.wait_for(self.frame.wait(), 1.0)


class Player:
    """无声媒体时钟替身：先消费准备材料，再显式开始并逐步报告进度。"""

    def __init__(self, driver: Driver) -> None:
        self.driver = driver
        self.before_start: dict[str, float] = {}
        self.playing: list[dict[str, float]] = []
        self.closed = False

    def check_available(self) -> None:
        """替身没有外部设备依赖。"""

    async def play(
        self,
        pcm: AsyncIterator[bytes],
        *,
        playback_id: str,
        deadline: float,
        started: Callable[[], Awaitable[None]],
        progress: Callable[[float, float], None],
    ) -> PlaybackResult:
        """只有 started 之后才推进媒体时间，提前到达的 PCM 不得提前表演。"""
        del playback_id, deadline
        blocks = [block async for block in pcm]
        assert len(blocks) == 70 and all(len(block) == 1280 for block in blocks)
        await self.driver.next_frame()
        self.before_start = self.driver.frames[-1]
        await started()
        for index in range(70):
            progress((index + 1) / 50, 0.7 if index < 50 else 0.0)
            await self.driver.next_frame()
            self.playing.append(self.driver.frames[-1])
        return PlaybackResult("completed", True, 1.4)

    async def interrupt(self, playback_id: str | None = None) -> None:
        """本测试在所有媒体回调完成后关闭，不持有待中断的设备进程。"""

    async def close(self) -> None:
        """记录声音资源已归还。"""
        self.closed = True


async def test_context_reaches_body_through_continuous_host_dialogue(tmp_path: Path) -> None:
    """连续输入实际走注意、前台、Worker、声音与身体，自动判断不依赖 apply。"""
    driver = Driver()
    player = Player(driver)
    source = LocalSpeech(struct.pack("<hh", 1000, -1000) * 320 * 70)
    profile = tmp_path / "profile.json"
    profile.write_text(
        '{"model_id":"offline","bindings":[{"parameter":"PurrceptHead","sources":{"head_x":1.0}}]}',
        encoding="utf-8",
    )
    host, attention, brain = build_host(
        profile,
        tmp_path / "token",
        tmp_path / "dialogue",
        source,
        player,
        body_factory(lambda _settings, _token, _profile: driver),
    )
    await host.start()
    try:
        await driver.next_frame()
        for index in range(3):
            player.playing.clear()
            await run_round(host, index)
            assert source.plays == index + 1
            assert attention.rounds == index + 1 and brain.calls == 2 * (index + 1)
            assert len(player.playing) == 70
            if index == 0:
                assert min(frame["body_x"] for frame in player.playing) < -0.1
                assert max(frame["smile"] for frame in player.playing) > 0.25
            elif index == 1:
                assert max(frame["brow_l"] - frame["brow_r"] for frame in player.playing) > 0.25
            else:
                assert player.playing[-1]["mouth_form"] < player.playing[0]["mouth_form"]
        assert host.query_health()["can_process"] is True
    finally:
        await host.close()
    assert player.closed and driver.closed and host.query().phase == "closed"


async def test_real_worker_delivery_reaches_body_and_releases_after_speech(tmp_path: Path) -> None:
    """公开 schema、宿主接线、PCM 候选和最终通道必须在同一次调用中贯通。"""
    source, driver = Source(), Driver()
    player = Player(driver)
    profile = tmp_path / "profile.json"
    profile.write_text(
        '{"model_id":"offline","bindings":[{"parameter":"PurrceptHeadY","sources":{"head_y":1.0}}]}',
        encoding="utf-8",
    )
    config = EngineConfig.validate(
        {
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
                }
            },
            "subject": {"foreground": "offline", "reflection": "offline"},
            "plugins": [
                {
                    "instance_id": "audio",
                    "entry": "audio-test",
                    "config": {"playback_backend": "streaming"},
                },
                {
                    "instance_id": "body",
                    "entry": "body-test",
                    "config": {"profile_path": str(profile), "style": {"idle_motion_scale": 0.0}},
                },
            ],
        },
        base_dir=tmp_path,
    )
    host = EngineHost(
        config,
        plugin_factories={
            "audio-test": audio_factory(lambda _: source, lambda _: player, decode=False),
            "body-test": body_factory(lambda _settings, _token, _profile: driver),
        },
    )
    await host.start()
    try:
        action = await host.execute(
            'from world import me\nreturn await me.microphone.speak(text="解释一下", '
            'body={"delivery":"explain","effort":0.0,"valence":0.6})'
        )
        assert action.state == "completed" and len(action.calls) == 1
        call = action.calls[0]
        assert call.state == "confirmed" and call.result_json is not None
        result = decode_object(call.result_json.encode())
        assert result["started"] is True and result["completed"] is True
        body_result = result["body"]
        assert isinstance(body_result, dict) and body_result["adopted"] is True
        assert source.parameters is not None and source.parameters.body is not None
        assert source.parameters.body.delivery == "explain" and source.parameters.body.effort == 0

        assert player.before_start["mouth_open"] == player.before_start["head_y"] == 0
        assert player.before_start["smile"] == 0
        assert max(frame["head_y"] for frame in player.playing) > 0.05
        assert max(frame["brow_l"] for frame in player.playing) > 0.10
        assert max(frame["smile"] for frame in player.playing) > 0.35
        assert 0.2 < max(frame["mouth_open"] for frame in player.playing) <= 0.75

        # 结束后的恢复仍由正式帧循环完成，声音返回不能留下常开嘴或永久交流表情。
        for _ in range(60):
            await driver.next_frame()
        settled = driver.frames[-1]
        assert settled["mouth_open"] == 0 and abs(settled["head_y"]) < 0.001
        assert settled["smile"] < 0.01 and abs(settled["brow_l"]) < 0.01
    finally:
        await host.close()
    assert driver.closed and player.closed
