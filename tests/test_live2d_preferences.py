"""验证持续空闲行为、偏好热更新、口型范围与独立调节页的保存边界。

只使用离线驱动和临时文件；真实设备与听感仍由人工连机验收。
"""

import asyncio
from itertools import pairwise
from pathlib import Path

import httpx
import pytest
from test_live2d import Driver
from test_web import local_asgi_only as local_asgi_only

from purrcept_engine.app.live2d_studio.server import create_studio
from purrcept_engine.plugin_system import BodyIntent, ExpressionAssessment
from purrcept_engine.plugins.live2d.models import VISEMES, BodyStyle, GazeCalibration
from purrcept_engine.plugins.live2d.motion import MotionController
from purrcept_engine.plugins.live2d.mouth import MouthController, viseme_mouth_form
from purrcept_engine.plugins.live2d.preferences import (
    PreferenceConflict,
    Preferences,
    load_preferences,
    save_preferences,
)
from purrcept_engine.plugins.live2d.runtime import Live2DRuntime


def test_idle_moves_head_body_and_gaze_at_different_frame_rates() -> None:
    def replay(rate: int) -> list[dict[str, float]]:
        body = MotionController(BodyStyle(), GazeCalibration(), seed=5)
        body.reset(0)
        return [body.sample(index / rate) for index in range(1, 30 * rate + 1)]

    slow, fast = replay(30), replay(60)
    for name in ("head_x", "head_y", "head_z", "body_x", "gaze_x"):
        values = [frame[name] for frame in slow]
        assert max(values) - min(values) > 0.08
        assert max(abs(a[name] - b[name]) for a, b in zip(slow, fast[1::2], strict=True)) < 1e-8
        # 扫视允许比头身更快；头身在一帧内不能出现可见姿态跳变。
        assert max(abs(a - b) for a, b in pairwise(values)) < (0.5 if name == "gaze_x" else 0.08)


def test_idle_yields_to_explicit_and_context_focus_including_zero() -> None:
    for mode in ("explicit", "context"):
        body = MotionController(BodyStyle(), GazeCalibration(), seed=5)
        body.reset(0)
        if mode == "explicit":
            body.submit(BodyIntent(gaze_x=0.0, gaze_y=0.0), now=0, expires=12, playback=None)
        else:
            body.observe(
                ExpressionAssessment(
                    emotion="unchanged",
                    emotion_confidence=0.0,
                    focus="audience",
                    focus_confidence=1.0,
                ),
                now=0,
                deadline=12,
                urgent=False,
            )
        frames = [body.sample(index / 60) for index in range(1, 301)]
        assert max(abs(frame["head_x"]) for frame in frames) < 1e-6


def test_pose_update_preserves_motion_and_blink_can_still_close() -> None:
    body = MotionController(BodyStyle(idle_motion_scale=0.0), GazeCalibration(), seed=4)
    body.reset(0)
    old = body.sample(0.5)
    body.configure(Preferences(model_id="test", head_y=0.3, body_z=-0.2, eye_open=0.6))
    assert body.sample(0.5) == old
    early = body.sample(0.5 + 1 / 120)
    assert abs(early["head_y"] - old["head_y"]) < 0.01
    frames = [body.sample(index / 60) for index in range(31, 601)]
    assert frames[-1]["head_y"] == pytest.approx(0.3)
    assert frames[-1]["body_z"] == pytest.approx(-0.2, abs=0.001)
    assert min(frame["eye_open"] for frame in frames) == 0


def test_gain_uses_full_mouth_range_without_chatter_or_stuck_open() -> None:
    original, tuned = MouthController(BodyStyle()), MouthController(BodyStyle())
    tuned.configure(Preferences(model_id="test", mouth_gain=1.8, mouth_gamma=0.75))
    silent = (0.0,) * len(VISEMES)
    before = after = 0.0
    for index in range(1, 61):
        before = original.sample(index / 60, 0.7, silent, active=True)[0]
        after = tuned.sample(index / 60, 0.7, silent, active=True)[0]
    assert after > 0.95 and before < 0.6
    assert tuned.sample(1.05, 0, silent, active=True)[0] > 0.9
    for index in range(64, 181):
        after = tuned.sample(index / 60, 0, silent, active=False)[0]
    assert after == 0


def test_preference_backup_conflict_and_model_identity(tmp_path: Path) -> None:
    path = tmp_path / "q.preferences.json"
    value, revision = load_preferences(path, "q")
    revision = save_preferences(path, value, revision)
    old = path.read_bytes()
    save_preferences(path, value.model_copy(update={"head_y": 0.2}), revision)
    assert next((tmp_path / "backups").iterdir()).read_bytes() == old
    with pytest.raises(PreferenceConflict):
        save_preferences(path, value, revision)
    with pytest.raises(ValueError, match="不符"):
        load_preferences(path, "different")


def test_two_channel_mouth_uses_actual_visemes_without_inventing_silent_motion() -> None:
    def vowel(name: str) -> tuple[float, ...]:
        return tuple(0.8 if key == name else 0.0 for key in VISEMES)

    assert viseme_mouth_form(vowel("E")) > 0.7
    assert viseme_mouth_form(vowel("ou")) < -0.7
    assert viseme_mouth_form(vowel("PP")) == 0
    assert viseme_mouth_form((0.0,) * len(VISEMES)) == 0


async def test_runtime_adopts_file_then_keeps_last_valid_on_bad_file(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    save_preferences(path, Preferences(model_id="q", head_y=0.3, idle_enabled=False), "missing")
    driver = Driver()
    runtime = Live2DRuntime("test", lambda: driver, model_id="q", preferences_path=path)
    try:
        await runtime.connect()
        assert runtime.snapshot()["preferences_revision"] != ""
        await asyncio.sleep(0.8)
        assert driver.frames[-1]["head_y"] > 0.25
        path.write_text('{"model_id":"wrong"}', encoding="utf-8")
        await asyncio.sleep(0.5)
        assert runtime.snapshot()["preferences_error"] is not None
        assert driver.frames[-1]["head_y"] > 0.25
    finally:
        await runtime.close()
    assert runtime.resources_released


async def test_studio_preview_save_and_source_protection(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    driver = Driver()
    runtime = Live2DRuntime("test", lambda: driver, model_id="q")
    app = create_studio(path, runtime, "q", port=8767, token="test")
    headers = {"X-Purrcept-Token": "test", "Origin": "http://127.0.0.1:8767"}
    transport = httpx.ASGITransport(app, client=("127.0.0.1", 1234))
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8767"
        ) as client:
            assert (await client.get("/")).status_code == 200
            assert (await client.get("/assets/studio.js")).status_code == 200
            assert (await client.get("/assets/secret")).status_code == 404
            state = (await client.get("/api/session")).json()
            assert not driver.frames
            value = state["preferences"] | {"head_y": 0.2}
            payload = {"preferences": value, "revision": state["revision"]}
            assert (await client.post("/api/preview", json=payload)).status_code == 403
            assert (
                await client.post("/api/preview", headers=headers, json=payload)
            ).status_code == 200
            assert not path.exists()
            saved = await client.post("/api/save", headers=headers, json=payload)
            assert saved.status_code == 200
            assert load_preferences(path, "q")[0].head_y == 0.2
            assert (
                await client.post("/api/save", headers=headers, json=payload)
            ).status_code == 409
            payload["preferences"] = value | {"model_id": "other"}
            assert (
                await client.post("/api/preview", headers=headers, json=payload)
            ).status_code == 400
            assert (await client.post("/api/device/connect", headers=headers)).json()["connected"]
            assert (await client.post("/api/device/disconnect", headers=headers)).json()[
                "connected"
            ] is False
    finally:
        await runtime.close()
