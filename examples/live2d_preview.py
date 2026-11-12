"""用正式身体运行时在本机 VTS 演示动作，供人工校准和反馈。

显式指定已有 profile/token；只连接身体，不启动主体、TTS 或音频播放。
每轮对比放松与强调动作，侧向停留展示独立身体跟随；控制台标记当前阶段。
--stop-file 指定的文件出现时平滑回正并关闭连接，--seconds 限制总时长。
不修改校准文件或 VTS 绑定；首次令牌授权仍由 VTS 原生界面完成。
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from purrcept_engine.plugin_system import BodyIntent, CallContext
from purrcept_engine.plugins.live2d.models import BodyStyle, ModelProfile
from purrcept_engine.plugins.live2d.runtime import Live2DRuntime
from purrcept_engine.plugins.live2d.vts import VTSDriver

_SCENES = (
    ("安静待机与眨眼", 4.0, BodyIntent(valence=0.0, gaze_x=0.0, gaze_y=0.0)),
    ("放松看向一侧：观察肩部和躯干延后跟随", 5.0, BodyIntent(gaze_x=0.8, effort=0.0)),
    ("放松看向另一侧：观察身体换向", 5.0, BodyIntent(gaze_x=-0.8, effort=0.0)),
    ("放松回望观众", 3.0, BodyIntent(gaze_x=0.0, gaze_y=0.0, effort=0.0)),
    ("轻点头：无反向起势", 3.0, BodyIntent(gesture="nod", effort=0.0)),
    ("轻摇头：平滑往返", 3.0, BodyIntent(gesture="shake", effort=0.0)),
    ("强调转向：同样幅度，更快到位", 5.0, BodyIntent(gaze_x=0.8, effort=1.0)),
    ("回正恢复放松，不继承上次力度", 3.0, BodyIntent(gaze_x=0.0)),
    ("中等力度点头", 3.0, BodyIntent(gesture="nod", effort=0.65)),
    ("强调点头：起势、主动作、停留", 3.0, BodyIntent(gesture="nod", effort=1.0)),
    ("强调摇头", 3.0, BodyIntent(gesture="shake", effort=1.0)),
    ("放松偏头开始", 0.4, BodyIntent(gesture="tilt", effort=0.0)),
    ("只更新微笑：偏头继续完成", 3.6, BodyIntent(valence=0.7)),
    ("表情与姿态自然恢复", 5.0, BodyIntent(valence=0.0, tension=0.0)),
)


async def preview(profile_path: Path, token_path: Path, stop_file: Path, seconds: float) -> None:
    """运行有界真实预览，失败或停止时归还正式运行时的所有资源。

    Args:
        profile_path: 现有模型校准 JSON 路径。
        token_path: 本机 VTS 认证令牌路径，内容不输出。
        stop_file: 文件存在即停止，调用方须为每次运行选择新的路径。
        seconds: 最多演示秒数，必须为正数。

    Raises:
        RuntimeError: 连接或实际帧确认失败。
        ValueError: 时长无效或模型校准格式无效。
    """
    if not 0 < seconds <= 3600:
        raise ValueError("预览时长须在 0 到 3600 秒之间。")
    profile = ModelProfile.model_validate_json(await asyncio.to_thread(profile_path.read_bytes))
    body = Live2DRuntime(
        "live2d-preview",
        lambda: VTSDriver("ws://127.0.0.1:8001", token_path, profile),
        duration_seconds=12.0,
        style=BodyStyle(),
        gaze_calibration=profile.gaze,
        motion_seed=20261106,
    )
    try:
        result = await body.connect()
        if result.state != "confirmed":
            raise RuntimeError(str(result.value))
        print("VTS 已认证，模型身份与首帧注入已确认。", flush=True)
        deadline, sequence = time.monotonic() + seconds, 0
        while time.monotonic() < deadline and not await asyncio.to_thread(stop_file.exists):
            for label, duration, intent in _SCENES:
                if time.monotonic() >= deadline or await asyncio.to_thread(stop_file.exists):
                    break
                sequence += 1
                print(f"阶段 {sequence}: {label}", flush=True)
                outcome = await body.apply(
                    intent, CallContext(f"preview-{sequence}", "preview", deadline)
                )
                if outcome.state != "confirmed":
                    raise RuntimeError(str(outcome.value))
                until = min(deadline, time.monotonic() + duration)
                for _ in range(int(duration * 10) + 1):
                    if time.monotonic() >= until or await asyncio.to_thread(stop_file.exists):
                        break
                    await asyncio.sleep(0.1)
        # 正常停止先给现有连接发送中性目标，再让动态系统回落；异常不假装已回正。
        await body.apply(
            BodyIntent(
                valence=0.0, arousal=0.0, tension=0.0, gaze_x=0.0, gaze_y=0.0, gesture="none"
            ),
            CallContext("preview-finish", "preview", time.monotonic() + 3),
        )
        await asyncio.sleep(1.5)
    finally:
        await body.close()
        print("预览连接已关闭。", flush=True)


def main() -> None:
    """解析显式路径并启动预览；Ctrl+C 同样通过 finally 关闭设备连接。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--token", required=True, type=Path)
    parser.add_argument("--stop-file", required=True, type=Path)
    parser.add_argument("--seconds", type=float, default=180.0)
    args = parser.parse_args()
    asyncio.run(preview(args.profile, args.token, args.stop_file, args.seconds))


if __name__ == "__main__":
    main()
