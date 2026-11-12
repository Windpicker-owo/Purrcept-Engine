"""从明确配置装配独立表演调节页，不启动主体、消息或在线合成。"""

import argparse
from pathlib import Path

import uvicorn

from purrcept_engine.app.config import load_config
from purrcept_engine.app.live2d_studio.server import create_studio
from purrcept_engine.plugins.audio.dsp import read_profile
from purrcept_engine.plugins.audio.models import AudioSettings
from purrcept_engine.plugins.live2d.analyzer import AnalyzerProcess
from purrcept_engine.plugins.live2d.models import ModelProfile
from purrcept_engine.plugins.live2d.plugin import Live2DSettings
from purrcept_engine.plugins.live2d.preferences import load_preferences
from purrcept_engine.plugins.live2d.runtime import Live2DRuntime
from purrcept_engine.plugins.live2d.vts import VTSDriver


def main() -> None:
    """读取配置中的唯一 Live2D，页面明确连接后才启动帧循环。

    Note:
        --audio 只允许本机短音频试听；不会使用配置中的 TTS 密钥。
    """
    parser = argparse.ArgumentParser(description="Purrcept Live2D 表演调节室")
    parser.add_argument("--config", type=Path, default=Path("purrcept.toml"))
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--audio", type=Path)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口须在 1 到 65535 之间。")
    config = load_config(args.config)
    base = args.config.resolve().parent
    selected = [p for p in config.plugins if p.enabled and p.entry == "live2d"]
    if len(selected) != 1:
        parser.error("配置须启用且只启用一个 Live2D 插件。")
    settings = Live2DSettings.model_validate(selected[0].configuration())
    profile_path = base / settings.profile_path
    with profile_path.open("rb") as stream:
        raw = stream.read(262145)
    if len(raw) > 262144:
        parser.error("模型校准文件超过 256 KiB。")
    profile = ModelProfile.model_validate_json(raw)
    path = (
        base / settings.preferences_path
        if settings.preferences_path
        else profile_path.with_suffix(".preferences.json")
    )
    preferences, _ = load_preferences(path, profile.model_id)
    library = base / settings.ovr_library if settings.ovr_library else None

    def analyzer() -> AnalyzerProcess:
        """与正式插件一样按明确本地库创建独立视素分析器。"""
        assert library is not None
        return AnalyzerProcess(library, settings.sample_rate)

    runtime = Live2DRuntime(
        "live2d-studio",
        lambda: VTSDriver(settings.vts_url, base / settings.token_path, profile),
        duration_seconds=settings.reaction_seconds,
        frame_hz=settings.frame_hz,
        sample_rate=settings.sample_rate,
        style=settings.style,
        gaze_calibration=profile.gaze,
        focus_layout=profile.focus,
        lip_sync_offset_ms=profile.lip_sync_offset_ms,
        model_id=profile.model_id,
        analyzer_factory=analyzer if library else None,
    )
    runtime.configure(preferences)
    voices = [p for p in config.plugins if p.enabled and p.entry == "microphone"]
    audio = AudioSettings.model_validate(voices[0].configuration()) if voices else AudioSettings()
    app = create_studio(
        path,
        runtime,
        profile.model_id,
        port=args.port,
        sample=args.audio.resolve() if args.audio else None,
        sample_rate=settings.sample_rate,
        ffmpeg=audio.processing_executable,
        ffplay=audio.playback_executable,
        voice_profile=read_profile(
            base / audio.postprocess_profile if audio.postprocess_profile else None
        ),
    )
    print(f"Live2D 调节室：http://127.0.0.1:{args.port}/；在页面连接 VTS 后预览。", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
