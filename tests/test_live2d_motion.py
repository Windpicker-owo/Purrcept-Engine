"""离线回放动作契约：协调、独立期限、时间一致性以及中断恢复。

只观察最终通道，不用具体积分步骤反生成预期。真实模型的方向与绑定仍需
连机观察；这里验证调度器不会因帧率、字段更新或旧播放取消破坏行为。
"""

import math
from itertools import pairwise

import pytest
from pydantic import ValidationError

from purrcept_engine.plugin_system.body import BodyIntent
from purrcept_engine.plugins.live2d.models import BodyStyle, GazeCalibration
from purrcept_engine.plugins.live2d.motion import MotionController


def controller() -> MotionController:
    """提供禁用待机微动的可重放角色，便于隔离语义动作。"""
    result = MotionController(BodyStyle(idle_motion_scale=0.0), GazeCalibration(), seed=19)
    result.reset(0.0)
    return result


def advance(body: MotionController, start: float, end: float) -> dict[str, float]:
    """以播放式时间推进，避免把长时间直接跳转误当连续运行。"""
    for index in range(1, math.ceil((end - start) * 120)):
        body.sample(min(end, start + index / 120))
    return body.sample(end)


def test_eye_leads_head_body_and_compensates_final_head() -> None:
    body = controller()
    body.submit(BodyIntent(gaze_x=0.8), now=0, expires=10, playback=None)
    early = body.sample(0.1)
    assert early["gaze_x"] > 0.5
    assert early["head_x"] == early["body_x"] == 0
    following = body.sample(0.4)
    assert following["head_x"] > 0.05 and following["body_x"] == 0
    final = advance(body, 0.4, 3)
    assert final["body_x"] > 0
    effective = final["gaze_x"] + 0.8 * final["head_x"] + 0.3 * final["body_x"]
    assert effective == pytest.approx(0.8, abs=0.005)
    body.confirm(BodyIntent(gesture="shake"), now=3, expires=8, playback=None)
    frames = [body.sample(3 + index / 120) for index in range(1, 110)]
    assert max(abs(frame["head_x"] - final["head_x"]) for frame in frames) > 0.025
    assert (
        max(
            abs(frame["gaze_x"] + 0.8 * frame["head_x"] + 0.3 * frame["body_x"] - 0.8)
            for frame in frames
        )
        < 0.04
    )


def test_emotion_update_preserves_gesture_gaze_and_their_expiry() -> None:
    actual, reference = controller(), controller()
    for body in (actual, reference):
        intent = BodyIntent(gaze_x=0.6, gesture="nod")
        body.submit(intent, now=0, expires=1.0, playback=None)
        body.confirm(intent, now=0, expires=1.0, playback=None)
        body.sample(0.25)
    actual.submit(BodyIntent(valence=0.8), now=0.25, expires=4, playback=None)
    actual_frames: list[dict[str, float]] = []
    reference_frames: list[dict[str, float]] = []
    for index in range(31, 301):
        actual_frames.append(actual.sample(index / 120))
        reference_frames.append(reference.sample(index / 120))
    for frame, expected in zip(actual_frames, reference_frames, strict=True):
        for name in ("head_x", "head_y", "gaze_x", "body_x"):
            assert frame[name] == pytest.approx(expected[name])
    assert actual_frames[-1]["valence"] > 0.7
    assert abs(actual_frames[-1]["gaze_x"]) < 0.02


def test_revoke_only_owned_fields_and_cancel_without_pose_jump() -> None:
    body = controller()
    intent = BodyIntent(valence=0.8, gaze_x=0.6, gesture="tilt")
    body.submit(intent, now=0, expires=8, playback="old")
    body.confirm(intent, now=0, expires=8, playback="old")
    body.sample(0.3)
    body.submit(BodyIntent(valence=-0.5), now=0.3, expires=5, playback=None)
    before = body.sample(0.5)
    body.revoke("old", 0.5)
    assert body.sample(0.5) == before
    after = body.sample(0.5 + 1 / 120)
    assert abs(after["head_z"] - before["head_z"]) < 0.015
    final = advance(body, 0.5 + 1 / 120, 3)
    assert final["valence"] == pytest.approx(-0.5, abs=0.002)
    assert abs(final["gaze_x"]) < 0.005 and abs(final["head_z"]) < 0.005


def test_blinks_have_curves_irregular_intervals_and_expression_baseline() -> None:
    body = controller()
    body.submit(BodyIntent(valence=1.0), now=0, expires=30, playback=None)
    frames = [body.sample(index / 120)["eye_open"] for index in range(1, 2401)]
    onsets = [index for index in range(1, len(frames)) if frames[index - 1] >= 0.8 > frames[index]]
    intervals = [right - left for left, right in pairwise(onsets)]
    assert len(onsets) >= 3 and len(set(intervals)) > 1
    assert min(frames) == 0.0
    assert any(0.1 < value < 0.7 for value in frames)
    assert all(0 <= value <= 1 for value in frames)
    assert max(frames[600:]) == pytest.approx(0.88, abs=0.001)


def test_output_rate_does_not_change_motion_or_random_sequence() -> None:
    def replay(hz: int) -> list[dict[str, float]]:
        body = MotionController(BodyStyle(), GazeCalibration(), seed=97)
        body.reset(0)
        body.submit(BodyIntent(gaze_x=0.7, tension=0.8), now=0, expires=8, playback=None)
        samples: list[dict[str, float]] = []
        for index in range(1, hz * 16 + 1):
            frame = body.sample(index / hz)
            if index % hz == 0:
                samples.append(frame)
        return samples

    reference = replay(60)
    for hz in (15, 30):
        for actual, expected in zip(replay(hz), reference, strict=True):
            assert actual == pytest.approx(expected, abs=1e-8)


def test_explicit_cancel_and_zero_are_distinct_from_omission() -> None:
    body = controller()
    intent = BodyIntent(gaze_x=0.5, gesture="tilt")
    body.submit(intent, now=0, expires=10, playback=None)
    body.confirm(intent, now=0, expires=10, playback=None)
    body.sample(0.4)
    body.submit(BodyIntent(gaze_x=0.0, gesture="none"), now=0.4, expires=10, playback=None)
    final = advance(body, 0.4, 3)
    assert abs(final["head_z"]) < 0.001 and abs(final["gaze_x"]) < 0.001


def test_stall_discards_old_actions_and_reset_clears_momentum() -> None:
    body = controller()
    intent = BodyIntent(valence=1.0, gaze_x=1.0, gesture="tilt")
    body.submit(intent, now=0, expires=1, playback="voice")
    body.confirm(intent, now=0, expires=1, playback="voice")
    body.sample(0.4)
    body.sample(20)
    final = advance(body, 20, 23)
    assert abs(final["head_z"]) < 0.001 and abs(final["gaze_x"]) < 0.001
    body.reset(23)
    fresh = body.sample(23)
    assert fresh["valence"] == fresh["head_x"] == fresh["head_z"] == 0
    assert fresh["eye_open"] == 1
    with pytest.raises(ValueError):
        body.sample(22)
    with pytest.raises(ValueError):
        body.sample(float("nan"))


def test_active_turn_and_gesture_reach_a_real_hold_without_spring_tail() -> None:
    """主动动作须有明确到位与停留，防止重新接入弹簧造成持续缓动。"""
    body = controller()
    body.submit(BodyIntent(gaze_x=0.8, effort=1.0), now=0, expires=8, playback=None)
    arrival = body.sample(0.4)
    held = body.sample(0.6)
    assert arrival["head_x"] > 0.2
    assert held["head_x"] == pytest.approx(arrival["head_x"], abs=0.001)
    body.confirm(BodyIntent(gesture="tilt", effort=1.0), now=0.6, expires=8, playback=None)
    peak = body.sample(0.85)
    holding = body.sample(1.2)
    assert peak["head_z"] > 0.15
    assert holding["head_z"] == pytest.approx(peak["head_z"], abs=0.001)
    restored = advance(body, 1.2, 1.8)
    assert abs(restored["head_z"]) < 0.001


@pytest.mark.parametrize("effort", [-0.1, 1.1, float("nan"), float("inf")])
def test_effort_rejects_invalid_values(effort: float) -> None:
    with pytest.raises(ValidationError):
        BodyIntent(effort=effort)


def test_relaxed_nod_has_no_anticipation_and_emphasis_increases_peak_speed() -> None:
    """轻重差异需要体现在运动曲线上，不能只给同一个快速动作改名称。"""
    traces: list[list[float]] = []
    for effort in (0.0, 0.65, 1.0):
        body = controller()
        body.confirm(BodyIntent(gesture="nod", effort=effort), now=0, expires=4, playback=None)
        traces.append([body.sample(index / 120)["head_y"] for index in range(181)])
    relaxed, medium, strong = traces
    assert min(relaxed) >= -1e-8
    assert min(strong[:12]) < -0.02
    speeds = [max(abs(right - left) * 120 for left, right in pairwise(trace)) for trace in traces]
    assert speeds[0] < speeds[1] < speeds[2]
    assert all(abs(trace[-1]) < 0.001 for trace in traces)
    assert max(medium) > max(relaxed)


def test_gaze_effort_changes_timing_but_not_destination_or_later_requests() -> None:
    relaxed, strong = controller(), controller()
    for body, effort in ((relaxed, 0.0), (strong, 1.0)):
        body.submit(BodyIntent(gaze_x=0.8, effort=effort), now=0, expires=10, playback=None)
    soft_frame, strong_frame = relaxed.sample(0.4), strong.sample(0.4)
    assert strong_frame["head_x"] > soft_frame["head_x"] * 2
    assert advance(relaxed, 0.4, 2)["head_x"] == pytest.approx(advance(strong, 0.4, 2)["head_x"])
    for body in (relaxed, strong):
        body.submit(BodyIntent(gaze_x=-0.8), now=2, expires=10, playback=None)
    for index in range(241, 361):
        assert relaxed.sample(index / 120)["head_x"] == pytest.approx(
            strong.sample(index / 120)["head_x"]
        )


def test_effort_and_arousal_alone_do_not_retime_active_motion() -> None:
    actual, reference = controller(), controller()
    intent = BodyIntent(gaze_x=0.8, gesture="nod", effort=0.0)
    for body in (actual, reference):
        body.submit(intent, now=0, expires=5, playback=None)
        body.confirm(intent, now=0, expires=5, playback=None)
        body.sample(0.2)
    actual.submit(BodyIntent(effort=1.0, arousal=1.0), now=0.2, expires=5, playback=None)
    for index in range(25, 181):
        frame, expected = actual.sample(index / 120), reference.sample(index / 120)
        for channel in ("head_x", "head_y", "body_x"):
            assert frame[channel] == pytest.approx(expected[channel])


def test_body_follows_sustained_gaze_with_visible_range_but_ignores_short_glances() -> None:
    body = controller()
    body.submit(BodyIntent(gaze_x=0.8), now=0, expires=10, playback=None)
    assert body.sample(0.3)["body_x"] == 0
    body.submit(BodyIntent(gaze_x=0.0), now=0.3, expires=10, playback=None)
    assert advance(body, 0.3, 2)["body_x"] == 0

    body.submit(BodyIntent(gaze_x=0.8), now=2, expires=10, playback=None)
    settled = advance(body, 2, 5)
    assert 0.35 < settled["body_x"] < 0.5
    assert 0.08 < settled["body_z"] < 0.15
    body.submit(BodyIntent(gaze_x=-0.8), now=5, expires=10, playback=None)
    first = body.sample(5 + 1 / 120)
    assert abs(first["body_x"] - settled["body_x"]) < 0.01
    assert advance(body, 5 + 1 / 120, 8)["body_x"] < -0.35
