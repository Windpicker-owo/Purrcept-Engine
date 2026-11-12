"""验证口型惯性、短间隙合并与语音表情的独立生命周期。

数值测试只判断连续性、运动量与所有权，不替代模型上的视觉验收。
"""

from itertools import pairwise

import pytest

from purrcept_engine.plugin_system import BodyIntent
from purrcept_engine.plugins.live2d.models import VISEMES, BodyStyle, GazeCalibration
from purrcept_engine.plugins.live2d.motion import MotionController
from purrcept_engine.plugins.live2d.mouth import MouthController

_SILENT = (0.0,) * len(VISEMES)


def test_mouth_reduces_fast_open_close_fluctuations_and_limits_opening() -> None:
    mouth = MouthController(BodyStyle())
    raw = [0.8 if index % 2 == 0 else 0.0 for index in range(120)]
    frames = [
        mouth.sample((index + 1) / 60, value, _SILENT, active=True)[0]
        for index, value in enumerate(raw)
    ]
    total = sum(abs(right - left) for left, right in pairwise(frames))
    original = sum(abs(right - left) for left, right in pairwise(raw))
    assert total < original * 0.1
    assert 0.5 < max(frames) <= 0.75
    assert max(abs(right - left) for left, right in pairwise(frames)) <= 4.5 / 60 + 1e-9


def test_short_gap_stays_open_but_real_pause_and_finish_close() -> None:
    mouth = MouthController(BodyStyle())
    for index in range(1, 31):
        mouth.sample(index / 60, 0.8, _SILENT, active=True)
    held = mouth.sample(0.55, 0, _SILENT, active=True)[0]
    assert held > 0.5
    closing = mouth.sample(0.7, 0, _SILENT, active=True)[0]
    assert 0 < closing < held
    for index in range(43, 121):
        mouth.sample(index / 60, 0, _SILENT, active=False)
    assert mouth.sample(2.0, 0, _SILENT, active=False)[0] == 0


def test_visemes_blend_without_bypassing_the_controller_or_surviving_reset() -> None:
    mouth = MouthController(BodyStyle())
    vowel = tuple(1.0 if name == "aa" else 0.0 for name in VISEMES)
    other = tuple(1.0 if name == "E" else 0.0 for name in VISEMES)
    opening, weights = mouth.sample(0.1, 1.0, vowel, active=True)
    assert 0 < opening < 0.75 and 0 < weights[10] < 1
    _, shifted = mouth.sample(0.12, 1.0, other, active=True)
    assert shifted[10] > 0 and shifted[11] > 0 and sum(shifted) <= 1.0
    mouth.reset(0.2)
    assert mouth.sample(0.2, 0, _SILENT, active=False) == (0.0, _SILENT)


def test_speech_expression_is_readable_and_releases_without_erasing_new_emotion() -> None:
    motion = MotionController(BodyStyle(idle_motion_scale=0.0), GazeCalibration(), seed=4)
    motion.reset(0)
    motion.submit(BodyIntent(delivery="question"), now=0, expires=5, playback="voice")
    for index in range(1, 61):
        motion.sample(index / 60)
    expression = motion.sample(1.0)
    assert expression["brow_l"] > 0.35
    assert expression["brow_l"] - expression["brow_r"] > 0.25
    motion.submit(BodyIntent(valence=0.8), now=1.0, expires=8, playback=None)
    motion.revoke("voice", 1.0)
    assert motion.sample(1.0)["brow_l"] == expression["brow_l"]
    for index in range(61, 241):
        motion.sample(index / 60)
    settled = motion.sample(4.0)
    assert abs(settled["brow_l"]) < 0.001
    assert settled["smile"] == pytest.approx(0.32, abs=0.002)


@pytest.mark.parametrize("delivery", ["explain", "question", "deny", "emphasize"])
def test_expression_expires_and_preserves_blink_closure(delivery: str) -> None:
    motion = MotionController(BodyStyle(blink_interval_seconds=1.0), GazeCalibration(), seed=4)
    motion.reset(0)
    intent = BodyIntent.model_validate({"delivery": delivery})
    motion.submit(intent, now=0, expires=2.0, playback="voice")
    frames = [motion.sample(index / 120) for index in range(1, 601)]
    assert min(frame["eye_open"] for frame in frames[:240]) == 0
    assert any(abs(frame["brow_l"]) > 0.1 for frame in frames[:240])
    assert abs(frames[-1]["brow_l"]) < 0.001
