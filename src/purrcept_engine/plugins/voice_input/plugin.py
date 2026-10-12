"""声明本机收音的有限控制，生产与替身经过同一 factory/build 边界。

默认不收音，开始才加载声卡/VAD；状态查询不采集。停止只释放输入，显式
停止输出属于 me.microphone.interrupt。纯声明没有网络、人员权限或旧租约。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from purrcept_engine.execution.catalog import CapabilityDeclaration, ObjectDeclaration
from purrcept_engine.execution.providers import (
    CallContext,
    CallOutcome,
    CapabilityInputError,
    FunctionCapability,
)
from purrcept_engine.plugins.base import (
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
    typed_factory,
)
from purrcept_engine.plugins.panels import DevicePanel, PanelControl

from .asr import FunASRTranscriber
from .capture import SoundDeviceCapture
from .models import (
    CaptureDriver,
    InputMode,
    StreamingTranscriber,
    VoiceDetector,
    VoiceInputError,
    VoiceInputSettings,
)
from .runtime import VoiceInputRuntime
from .vad import SileroVad


class _Empty(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _Start(_Empty):
    mode: InputMode = "asr"
    device: int | None = Field(
        default=None, ge=0, description="本机输入设备 ID，省略使用系统默认输入。"
    )


def make_factory(
    capture_factory: Callable[[VoiceInputSettings], CaptureDriver],
    vad_factory: Callable[[VoiceInputSettings], VoiceDetector],
    transcriber_factory: Callable[[VoiceInputSettings], StreamingTranscriber] = FunASRTranscriber,
    *,
    required_modules: tuple[str, ...] = (),
) -> PluginFactory:
    """注入有限设备与转写实现，替身不绕过真实配置、接管或生命周期规则。"""

    def declare(instance_id: str, settings: VoiceInputSettings, base_dir: Path) -> PluginBlueprint:
        model = settings.vad_model_path
        if not model.is_absolute():
            settings = settings.model_copy(update={"vad_model_path": base_dir / model})
        object_id = instance_id + ":voice_input"
        devices = CapabilityDeclaration(
            instance_id + ":voice_input.devices",
            object_id,
            "devices",
            "枚举本机可用输入设备，不开始收音。",
            _Empty,
            20.0,
            "await {path}()",
        )
        status = CapabilityDeclaration(
            instance_id + ":voice_input.status",
            object_id,
            "status",
            "查询实际收音、识别及已接管输入处理状态。",
            _Empty,
            20.0,
            "await {path}()",
        )
        start = CapabilityDeclaration(
            instance_id + ":voice_input.start",
            object_id,
            "start",
            "开始本机收音，选择 ASR 文字或原生音频模式。",
            _Start,
            25.0,
            'await {path}(mode="asr")',
            "audio 模式要求生成模型显式声明支持音频；已收音时先 stop。"
            "持续人声确认直接抢占，最终材料不重复抢占。",
            control=True,
        )
        stop = CapabilityDeclaration(
            instance_id + ":voice_input.stop",
            object_id,
            "stop",
            "停止本机收音并释放尚未接管的录音。",
            _Empty,
            20.0,
            "await {path}()",
            "停止收音不停止已有输出；停音使用 me.microphone.interrupt。",
            control=True,
        )

        async def build(ports: PluginPorts) -> PluginRuntime:
            runtime = VoiceInputRuntime(
                instance_id,
                settings,
                ports.inputs,
                lambda: capture_factory(settings),
                lambda: vad_factory(settings),
                transcriber_factory(settings),
                native_audio_enabled=ports.native_audio_enabled,
            )

            async def run_devices(parameters: _Empty, context: CallContext) -> CallOutcome:
                del parameters, context
                return CallOutcome("confirmed", await runtime.devices())

            async def run_status(parameters: _Empty, context: CallContext) -> CallOutcome:
                del parameters, context
                return CallOutcome("confirmed", await runtime.status())

            async def run_start(parameters: _Start, context: CallContext) -> CallOutcome:
                del context
                try:
                    return CallOutcome(
                        "confirmed", await runtime.start(parameters.mode, parameters.device)
                    )
                except VoiceInputError as error:
                    raise CapabilityInputError(str(error)) from error

            async def run_stop(parameters: _Empty, context: CallContext) -> CallOutcome:
                del parameters, context
                return CallOutcome("confirmed", await runtime.stop())

            return PluginRuntime(
                (
                    FunctionCapability(devices, run_devices),
                    FunctionCapability(status, run_status),
                    FunctionCapability(start, run_start),
                    FunctionCapability(stop, run_stop),
                ),
                stop_inputs=runtime.stop_inputs,
                close=runtime.close,
                snapshot=runtime.snapshot,
                panel=DevicePanel(
                    "voice_input",
                    (
                        PanelControl("devices", "列出输入设备", devices.capability_id),
                        PanelControl(
                            "start", "开始收音", start.capability_id, fields=("mode", "device")
                        ),
                        PanelControl("stop", "停止收音", stop.capability_id),
                    ),
                    lambda: str(runtime.revision),
                ),
            )

        return PluginBlueprint(
            (ObjectDeclaration(object_id, settings.voice_input_id, settings.label, "subject"),),
            (devices, status, start, stop),
            build,
            required_modules=required_modules,
        )

    return typed_factory(VoiceInputSettings, declare)


factory = make_factory(
    lambda settings: SoundDeviceCapture(settings.frame_queue_size),
    lambda settings: SileroVad(settings.vad_model_path),
    required_modules=("sounddevice", "av", "numpy", "onnxruntime", "websockets"),
)
