"""离线验证韵律候选、实际播放时间门槛与显式动作优先级。

确定性 PCM 只提供包络和停顿，不冒充真实语言重音。集成测试通过正式
Live2DRuntime 的 BodyPort 和记录驱动，验证无 OVR 时也能工作、暂停保持、
中断撤销及恢复不补演，不调用音频设备或付费合成。
"""

import struct
from itertools import pairwise

import pytest
from pydantic import ValidationError

from purrcept_engine.plugin_system import BodyIntent, CallContext
from purrcept_engine.plugins.live2d.models import BodyStyle, GazeCalibration
from purrcept_engine.plugins.live2d.motion import MotionController
from purrcept_engine.plugins.live2d.prosody import SpeechCue, SpeechPerformance
from purrcept_engine.plugins.live2d.runtime import Live2DRuntime


def pcm(level: float) -> bytes:
    """生成固定能量的20ms小端采样，交替正负避免直流偏置。"""
    sample = round(level * 32767)
    return struct.pack("<hh", sample, -sample) * 320


def phrase() -> list[float]:
    """先平稳发声、一次能量突出，随后停顿；共1.4秒。"""
    return [0.02] * 25 + [0.18] * 6 + [0.03] * 19 + [0.0] * 20


def prepared(intent: BodyIntent, levels: list[float] | None = None) -> SpeechPerformance:
    """模拟 PCM 先于真实媒体时钟到达的准备顺序。"""
    result = SpeechPerformance(BodyStyle())
    for index, level in enumerate(phrase() if levels is None else levels):
        assert result.feed("voice", 32000, index * 640, pcm(level))
    assert result.sample(1.4, 10.0) is None
    result.begin("voice", intent, 30.0)
    return result


@pytest.mark.parametrize(
    ("delivery", "gesture"),
    [("explain", "nod"), ("emphasize", "nod"), ("deny", "shake"), ("neutral", None)],
)
def test_semantics_select_action_at_energy_event(delivery: str, gesture: str | None) -> None:
    speech = prepared(BodyIntent.model_validate({"delivery": delivery, "effort": 0.0}))
    assert speech.sample(0.4, 10.4) is None
    cue = speech.sample(0.60, 10.6)
    assert cue is not None and cue.gesture == gesture
    assert cue.effort == 0.0
    assert speech.sample(0.60, 11.0) is None


def test_question_uses_pause_and_explicit_gesture_suppresses_auto_gestures() -> None:
    speech = prepared(BodyIntent(delivery="question"))
    accent = speech.sample(0.60, 10.6)
    assert accent is not None and accent.gesture is None
    pause = speech.sample(1.35, 11.35)
    assert pause is not None and pause.pause and pause.gesture == "tilt"
    for gesture in ("shake", "none"):
        explicit = prepared(
            BodyIntent.model_validate({"delivery": "emphasize", "gesture": gesture})
        )
        cue = explicit.sample(0.60, 10.6)
        assert cue is not None and cue.gesture is None


def test_steady_loud_audio_silence_and_candidate_density_are_bounded() -> None:
    for levels in ([0.0] * 1500, [0.5] * 1500):
        speech = prepared(BodyIntent(delivery="emphasize"), levels)
        cues = [speech.sample(index / 50, 10 + index / 50) for index in range(900)]
        assert sum(cue is not None and cue.gesture is not None for cue in cues) <= 1

    speech = prepared(BodyIntent(delivery="emphasize"), phrase() * 30)
    selected: list[float] = []
    for index in range(900):
        cue = speech.sample(index / 50, 10 + index / 50)
        if cue is not None and cue.gesture is not None:
            selected.append(index / 50)
    assert 2 <= len(selected) <= 6
    assert all(right - left >= 3.0 for left, right in pairwise(selected))


def test_late_expired_and_retired_events_never_replay() -> None:
    speech = prepared(BodyIntent(delivery="emphasize"))
    assert speech.sample(3.0, 13.0) is None
    assert speech.pending_events == 0
    speech = prepared(BodyIntent(delivery="emphasize"))
    assert speech.sample(0.60, 31.0) is None
    speech.finish("voice")
    assert speech.pending_events == 0
    assert not speech.feed("voice", 32000, 0, pcm(0.3))
    assert speech.feed("next", 32000, 0, pcm(0.0))
    speech.begin("next", BodyIntent(delivery="question"), 50)
    speech.finish("voice")
    assert speech.feed("next", 32000, 640, pcm(0.0))


def test_gaps_duplicates_and_foreign_pcm_do_not_create_false_emphasis() -> None:
    speech = SpeechPerformance(BodyStyle())
    speech.begin("voice", BodyIntent(delivery="emphasize"), 20)
    assert speech.feed("voice", 32000, 0, pcm(0.0))
    assert not speech.feed("voice", 32000, 0, pcm(0.5))
    assert not speech.feed("old", 32000, 0, pcm(0.5))
    for index in range(50, 70):
        assert speech.feed("voice", 32000, index * 640, pcm(0.5))
    assert speech.pending_events == 0


def test_candidate_storage_keeps_early_events_when_preparation_runs_ahead() -> None:
    speech = prepared(BodyIntent(delivery="emphasize"), phrase() * 300)
    assert speech.pending_events == 512
    assert speech.dropped_events > 0
    cue = speech.sample(0.60, 10.6)
    assert cue is not None and cue.gesture == "nod"


def test_speech_motion_freezes_at_media_pause_and_explicit_action_preempts() -> None:
    body = MotionController(BodyStyle(idle_motion_scale=0.0), GazeCalibration(), seed=9)
    body.reset(0)
    frame = body.sample(0)
    body.speech_cue(
        SpeechCue("nod", 0.0, 0.55, False), playback="voice", elapsed=0, now=0, expires=10
    )
    for index in range(1, 37):
        body.advance_speech("voice", index / 120)
        frame = body.sample(index / 120)
    assert frame["head_y"] > 0.03
    assert body.sample(0.7)["head_y"] == pytest.approx(frame["head_y"])
    # 显式动作打断自动动作时不能被自动动作刚启动的冷却期拒绝。
    body.confirm(BodyIntent(gesture="shake", effort=1.0), now=0.7, expires=10, playback=None)
    moved = body.sample(0.95)
    assert abs(moved["head_x"]) > 0.1
    body.revoke("voice", 0.95)
    assert abs(body.sample(1.1)["head_x"]) > 0.01


def test_automatic_gesture_does_not_override_explicit_action() -> None:
    body = MotionController(BodyStyle(idle_motion_scale=0.0), GazeCalibration(), seed=9)
    body.reset(0)
    body.confirm(BodyIntent(gesture="tilt", effort=0.0), now=0, expires=10, playback=None)
    body.speech_cue(
        SpeechCue("shake", 1.0, 1.0, False), playback="voice", elapsed=0, now=0.2, expires=10
    )
    result = body.sample(0.5)
    assert result["head_x"] == 0 and result["head_z"] > 0.1


@pytest.mark.parametrize("effort", [0.0, 0.65, 1.0])
def test_speech_trajectories_finish_consistently_at_different_output_rates(effort: float) -> None:
    for gesture in ("nod", "shake", "tilt"):
        traces: list[list[tuple[float, ...]]] = []
        for hz in (15, 30, 60):
            body = MotionController(BodyStyle(idle_motion_scale=0.0), GazeCalibration(), seed=9)
            body.reset(0)
            intent = BodyIntent.model_validate({"gesture": gesture})
            assert intent.gesture in ("nod", "shake", "tilt")
            cue = SpeechCue(intent.gesture, effort, 0.55, False)
            body.speech_cue(cue, playback="voice", elapsed=0, now=0, expires=10)
            frames: list[tuple[float, ...]] = []
            for index in range(1, hz * 3 + 1):
                body.advance_speech("voice", index / hz)
                frame = body.sample(index / hz)
                if index % (hz // 15) == 0:
                    frames.append(tuple(frame[name] for name in ("head_x", "head_y", "head_z")))
            assert frames[-1] == pytest.approx((0, 0, 0), abs=0.001)
            traces.append(frames)
        for actual in traces[1:]:
            for frame, expected in zip(actual, traces[0], strict=True):
                assert frame == pytest.approx(expected, abs=0.001)


def test_media_clock_jump_discards_old_motion_and_recovers_smoothly() -> None:
    body = MotionController(BodyStyle(idle_motion_scale=0.0), GazeCalibration(), seed=9)
    body.reset(0)
    body.speech_cue(
        SpeechCue("nod", 0.0, 0.55, False), playback="voice", elapsed=0, now=0, expires=10
    )
    for index in range(1, 37):
        body.advance_speech("voice", index / 120)
        body.sample(index / 120)
    before = body.sample(0.3)
    body.advance_speech("voice", 8.0)
    after = body.sample(0.3 + 1 / 120)
    assert abs(after["head_y"] - before["head_y"]) < 0.01
    for index in range(38, 121):
        body.sample(index / 120)
    assert abs(body.sample(1.0)["head_y"]) < 0.001


class Clock:
    """提供媒体时钟之外的单调时间，允许单独暂停播放器。"""

    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        return self.now


class Driver:
    """记录正式帧发送结果，不创建真实设备或网络。"""

    def __init__(self) -> None:
        self.frames: list[dict[str, float]] = []

    async def connect(self) -> None:
        """立即确认本地替身就绪。"""

    async def send(self, channels: dict[str, float]) -> None:
        """保留发送帧供时序断言。"""
        self.frames.append(dict(channels))

    async def close(self) -> None:
        """替身没有需等待释放的外部资源。"""


async def test_runtime_pcm_without_ovr_waits_for_player_and_cancels_on_finish() -> None:
    clock, driver = Clock(), Driver()
    body = Live2DRuntime(
        "speech", lambda: driver, monotonic=clock, style=BodyStyle(idle_motion_scale=0.0)
    )
    await body.connect()
    try:
        for index, level in enumerate(phrase()):
            body.playback_pcm("voice", 32000, index * 640, pcm(level))
        clock.now = 10.4
        await body.step()
        assert driver.frames[-1]["head_y"] == 0
        pending = body.snapshot()["speech_pending_events"]
        assert isinstance(pending, int) and pending > 0
        body.playback_started("voice", BodyIntent(delivery="emphasize", effort=0.0), 20)
        await body.step()
        body.playback_progress("voice", 0.6, 0.3)
        await body.step()
        for index in range(1, 37):
            clock.now += 1 / 120
            body.playback_progress("voice", 0.6 + index / 120, 0.2)
            await body.step()
        active = driver.frames[-1]["head_y"]
        assert active > 0.03
        clock.now += 0.3
        await body.step()
        assert driver.frames[-1]["head_y"] == pytest.approx(active)
        body.playback_progress("voice", 0.5, 0.9)
        assert body.snapshot()["elapsed_seconds"] == pytest.approx(0.9)
        body.playback_finished("voice")
        assert body.snapshot()["speech_pending_events"] == 0
        body.playback_progress("voice", 1.3, 0.9)
        for _ in range(60):
            clock.now += 1 / 60
            await body.step()
        assert abs(driver.frames[-1]["head_y"]) < 0.001
        assert driver.frames[-1]["mouth_open"] == 0
        result = await body.apply(BodyIntent(delivery="question"), CallContext("bad", "test", 20))
        assert result.state == "failed"
        await body.disconnect()
        await body.connect()
        assert body.snapshot()["speech_pending_events"] == 0
    finally:
        await body.close()


def test_delivery_schema_rejects_unknown_semantics() -> None:
    with pytest.raises(ValidationError):
        BodyIntent.model_validate({"delivery": "guess_from_loudness"})
