"""验证语境的有限所有权、置信门槛和连续响应，不模拟模型或真实肌肉。"""

import pytest
from pydantic import ValidationError

from purrcept_engine.plugin_system import BodyIntent, ExpressionAssessment
from purrcept_engine.plugins.live2d.context import ContextController
from purrcept_engine.plugins.live2d.models import BodyStyle, FocusLayout, GazeCalibration
from purrcept_engine.plugins.live2d.motion import MotionController


def observation(**changes: object) -> ExpressionAssessment:
    """构造严格验证的明确判断，允许单项覆盖以观察独立接纳。"""
    return ExpressionAssessment.model_validate(
        {
            "emotion": "warm",
            "emotion_confidence": 0.9,
            "focus": "chat",
            "focus_confidence": 0.9,
            **changes,
        }
    )


def test_confidence_is_independent_and_does_not_extend_deadlines() -> None:
    body = ContextController(BodyStyle(), FocusLayout())
    body.observe(observation(), now=0, deadline=4, urgent=False)
    assert body.sample(1)["gaze_x"] == -0.65
    old = body.sample(3)["valence"]
    body.observe(
        observation(
            emotion="displeased", emotion_confidence=0.1, focus="game", focus_confidence=0.1
        ),
        now=3,
        deadline=15,
        urgent=True,
    )
    assert body.sample(5)["gaze_x"] == 0
    assert 0 < body.sample(10)["valence"] < old
    body.observe(
        observation(emotion_confidence=0.1, focus="game"), now=10, deadline=14, urgent=False
    )
    assert body.sample(10)["gaze_x"] == 0.5
    assert body.sample(10)["valence"] < old


def test_focus_holds_replaces_candidate_and_urgent_interrupts() -> None:
    body = ContextController(BodyStyle(), FocusLayout())
    body.observe(observation(), now=0, deadline=15, urgent=False)
    body.observe(observation(focus="game"), now=0.2, deadline=10, urgent=False)
    assert body.sample(0.8)["gaze_x"] == -0.65
    body.observe(observation(focus="work"), now=0.9, deadline=10, urgent=False)
    assert body.sample(1.21)["gaze_x"] == 0.25
    body.observe(observation(focus="speaker"), now=1.3, deadline=10, urgent=True)
    assert body.sample(1.3)["gaze_x"] == 0
    assert body.sample(1.3)["gaze_y"] == 0.08


def test_expired_candidate_is_never_replayed_and_same_focus_does_not_restart_hold() -> None:
    body = ContextController(BodyStyle(), FocusLayout())
    body.observe(observation(), now=0, deadline=10, urgent=False)
    body.observe(observation(focus="game"), now=0.2, deadline=0.7, urgent=False)
    assert body.sample(1.3)["gaze_x"] == -0.65
    body.observe(observation(), now=1.4, deadline=10, urgent=False)
    body.observe(observation(focus="game"), now=1.5, deadline=10, urgent=False)
    assert body.sample(1.5)["gaze_x"] == 0.5
    body.observe(observation(focus="work"), now=2, deadline=1, urgent=True)
    assert body.sample(2)["gaze_x"] == 0.5
    assert body.sample(20)["gaze_x"] == 0


def test_emotion_is_continuous_on_retarget_and_expiry_then_resets() -> None:
    body = ContextController(BodyStyle(), FocusLayout())
    body.observe(observation(), now=0, deadline=4, urgent=False)
    assert body.sample(0)["valence"] == 0
    before = body.sample(2)["valence"]
    body.observe(observation(emotion="displeased"), now=2, deadline=4, urgent=False)
    assert body.sample(2)["valence"] == before
    assert body.sample(3)["valence"] < before
    at_expiry = body.sample(4)["valence"]
    assert body.sample(4.00001)["valence"] == pytest.approx(at_expiry, abs=1e-5)
    assert abs(body.sample(50)["valence"]) < 0.001
    body.reset(50)
    assert body.sample(50)["valence"] == body.sample(50)["gaze_x"] == 0


def test_explicit_fields_mask_context_and_cancellation_preserves_context() -> None:
    body = MotionController(BodyStyle(idle_motion_scale=0.0), GazeCalibration(), seed=1)
    body.reset(0)
    body.observe(observation(), now=0, deadline=12, urgent=True)
    body.submit(BodyIntent(gaze_x=0.8, valence=-0.6), now=0, expires=4, playback="speech")
    frame = body.sample(0)
    for i in range(1, 241):
        frame = body.sample(i / 120)
    assert frame["valence"] < -0.59
    assert frame["gaze_x"] + 0.8 * frame["head_x"] + 0.3 * frame["body_x"] == pytest.approx(
        0.8, abs=0.01
    )
    body.revoke("speech", 2)
    for i in range(241, 601):
        frame = body.sample(i / 120)
    assert frame["valence"] > 0.6
    assert frame["body_x"] < -0.2
    body.reset(5)
    assert body.sample(5)["valence"] == 0


def test_layout_and_assessment_reject_invalid_model_values() -> None:
    with pytest.raises(ValidationError):
        FocusLayout(chat=(1.1, 0.0))
    with pytest.raises(ValidationError):
        observation(emotion_confidence=float("nan"))
    with pytest.raises(ValidationError):
        observation(focus="invented")
    with pytest.raises(ValidationError):
        observation(emotion_confidence="0.9")
