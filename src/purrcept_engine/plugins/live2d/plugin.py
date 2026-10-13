"""Live2D 纯声明工厂，只保留已批准 me.live2d.apply(intent) 模型能力。

明确启用插件后 start 登记独立有限恢复任务，外围离线保留可查询事实；音频
端口由 app 在全部构造后接线。设备状态进入动态快照，不随连接改变目录。
校准在 build 读取，令牌由连接所有者读取；纯 prepare 不请求 VTS 或加载设备 SDK。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from purrcept_engine.execution.catalog import CapabilityDeclaration, ObjectDeclaration
from purrcept_engine.execution.providers import CallContext, CallOutcome, FunctionCapability
from purrcept_engine.plugins.base import (
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
    typed_factory,
)
from purrcept_engine.plugins.panels import DevicePanel

from .analyzer import AnalyzerProcess
from .models import BodyIntent, BodyStyle, ModelProfile
from .runtime import Live2DRuntime
from .vts import BodyDriver, VTSDriver


class Live2DSettings(BaseModel):
    """独立视觉连接和人工模型校准，期限初值4秒，不包含声音开关。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    live2d_id: str = Field(default="live2d", pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    vts_url: str = "ws://127.0.0.1:8001"
    token_path: str = ".purrcept/live2d/token"
    profile_path: str = Field(min_length=1)
    reaction_seconds: float = Field(default=4.0, ge=0.1, le=30, allow_inf_nan=False)
    frame_hz: float = Field(default=30.0, ge=1, le=60, allow_inf_nan=False)
    sample_rate: Literal[16000, 24000, 32000, 44100] = 32000
    ovr_library: str = ""
    style: BodyStyle = Field(default_factory=BodyStyle)

    @model_validator(mode="after")
    def local_endpoint(self) -> Live2DSettings:
        """VTS只使用本机显式连接，不把配置凭据或远程地址混入输出。"""
        url = urlsplit(self.vts_url)
        if (
            url.scheme != "ws"
            or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("VTS必须使用不带凭据/查询串的本机ws端点。")
        return self


class _Apply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    intent: BodyIntent


def make_factory(
    driver_factory: Callable[[Live2DSettings, Path, ModelProfile], BodyDriver],
    *,
    required_modules: tuple[str, ...] = (),
) -> PluginFactory:
    """将生产或离线驱动接入同一纯声明与 BodyPort 生命周期。"""

    def declare(instance_id: str, settings: Live2DSettings, base_dir: Path) -> PluginBlueprint:
        object_id = instance_id + ":live2d"
        apply = CapabilityDeclaration(
            instance_id + ":live2d.apply",
            object_id,
            "apply",
            "独立采用一项有期限的身体语义姿态。",
            _Apply,
            10.0,
            'result = await {path}(intent={"valence": 0.4, "gesture": "nod"})',
            "数值valence/gaze为[-1,1]，arousal/tension为[0,1]；gesture为none/nod/shake/tilt。缺省数值为0、手势为none。宿主分配身份，独立意图初值4秒；accepted不等于VTS已采用，恢复不补演。说话姿态可直接给speak(body=...)，不需要先创建表达句柄。",
            concurrency="parallel",
        )
        profile_path, token_path = (
            (base_dir / settings.profile_path).resolve(),
            (base_dir / settings.token_path).resolve(),
        )

        async def build(ports: PluginPorts) -> PluginRuntime:
            del ports
            import asyncio

            raw = await asyncio.to_thread(profile_path.read_bytes)
            if len(raw) > 262144:
                raise ValueError("身体校准文件超过256 KiB。")
            profile = ModelProfile.model_validate_json(raw)
            uses_visemes = any(
                name.startswith("viseme_")
                for binding in profile.bindings
                for name in binding.sources
            )
            if uses_visemes and not settings.ovr_library:
                raise ValueError("身体校准声明视素通道时必须配置明确OVRLipSync库路径。")
            library = (base_dir / settings.ovr_library).resolve() if settings.ovr_library else None
            if library is not None and not library.is_file():
                raise ValueError("配置的OVRLipSync库路径不存在。")

            def analyzer() -> AnalyzerProcess:
                if library is None:
                    raise RuntimeError("未配置OVR时不能创建分析器。")
                return AnalyzerProcess(library, settings.sample_rate)

            runtime = Live2DRuntime(
                instance_id,
                lambda: driver_factory(settings, token_path, profile),
                duration_seconds=settings.reaction_seconds,
                frame_hz=settings.frame_hz,
                analyzer_factory=analyzer if library is not None else None,
                sample_rate=settings.sample_rate,
                lip_sync_offset_ms=profile.lip_sync_offset_ms,
                style=settings.style,
            )

            async def run_apply(parameters: _Apply, context: CallContext) -> CallOutcome:
                return await runtime.apply(parameters.intent, context)

            def revision() -> str:
                return str(runtime.snapshot()["generation"])

            return PluginRuntime(
                (FunctionCapability(apply, run_apply),),
                start=runtime.start,
                stop_inputs=runtime.stop_inputs,
                close=runtime.close,
                snapshot=runtime.snapshot,
                panel=DevicePanel("live2d", (), revision),
                body=runtime,
                resources_released=lambda: runtime.resources_released,
            )

        return PluginBlueprint(
            (ObjectDeclaration(object_id, settings.live2d_id, "身体", "subject"),),
            (apply,),
            build,
            required_modules=required_modules,
        )

    return typed_factory(Live2DSettings, declare)


factory = make_factory(
    lambda settings, path, profile: VTSDriver(settings.vts_url, path, profile),
    required_modules=("websockets",),
)
