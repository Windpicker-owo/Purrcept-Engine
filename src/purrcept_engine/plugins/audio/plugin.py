"""麦克风输出纯工厂与三项稳定能力，不装配旧 Runtime 兼容层。

声明阶段校验配置和模型可见路径。app 注入同库 AudioPort，全部 build
后显式接线 BodyPort，再启动声音接管。speak 使用 parallel 把现场排队
统一交给音频所有者，interrupt 使用独立控制名额，record 不进入播放队列。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from purrcept_engine.domain.execution.catalog import CapabilityDeclaration, ObjectDeclaration
from purrcept_engine.domain.execution.providers import CallContext, CallOutcome, FunctionCapability
from purrcept_engine.errors import ConfigError
from purrcept_engine.plugin_system.contracts import (
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
    typed_factory,
)
from purrcept_engine.plugin_system.panels import DevicePanel, PanelControl
from purrcept_engine.plugins.audio.dsp import check_program
from purrcept_engine.plugins.audio.models import (
    AudioError,
    AudioSettings,
    RecordParameters,
    SpeechParameters,
)
from purrcept_engine.plugins.audio.player import AudioPlayer, FFplayPlayer
from purrcept_engine.plugins.audio.runtime import AudioRuntime
from purrcept_engine.plugins.audio.tts import FishSource, SpeechSource


class _Empty(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def make_factory(
    source_factory: Callable[[AudioSettings], SpeechSource],
    player_factory: Callable[[AudioSettings], AudioPlayer],
    *,
    decode: bool = True,
) -> PluginFactory:
    """以同一参数/队列/回执契约注入传输与设备替身，不借通用宿主服务。"""

    def declare(instance_id: str, settings: AudioSettings, base_dir: Path) -> PluginBlueprint:
        if decode:
            # 启用的完整输出必须在构造设备/网络所有者前核验本地程序。
            # 这里只查PATH/文件，不运行播放器；离线替身显式decode=False。
            try:
                check_program(settings.processing_executable)
                check_program(settings.playback_executable)
            except AudioError as error:
                raise ConfigError(str(error)) from error
        object_id = instance_id + ":microphone"
        record = CapabilityDeclaration(
            instance_id + ":microphone.record",
            object_id,
            "record",
            "将完整文本合成为文件或标准base64音频，不现场播放。",
            RecordParameters,
            settings.timeout_seconds + 15,
            'audio = await {path}(text="[温柔地说]你好。")',
            "save_as 是沙箱内相对路径；省略返回标准base64。"
            "支持Fish S2方括号语气/停顿标签，如[happy]、[slight pause]；"
            "S1固定圆括号标签不能与S2混用。",
            concurrency="parallel",
        )
        speak = CapabilityDeclaration(
            instance_id + ":microphone.speak",
            object_id,
            "speak",
            "将文本合成为现场语音，并可按实际播放联动身体。",
            SpeechParameters,
            180.0,
            'result = await {path}(text="你好。", body={"valence": 0.5, "gesture": "nod"})',
            f"默认按宿主接管顺序排队，最多{settings.max_pending}项候播和1项正在合成/播放，"
            "队满拒绝。排队计入期限。accepted不代表播放开始；started来自播放器媒体时钟，"
            "completed才表示正常结束。failed/interrupted/timed_out可能已播放部分，不自动重播。"
            "身体离线不阻断声音；body省略不添加语义姿态。流式输出省略format或使用pcm；"
            "文件播放器使用mp3/wav。",
            concurrency="parallel",
        )
        interrupt = CapabilityDeclaration(
            instance_id + ":microphone.interrupt",
            object_id,
            "interrupt",
            "停止当前合成/播放，并取消本次停止接管时已有的全部候播。",
            _Empty,
            10.0,
            "result = await {path}()",
            "先撤播放资格再有限等待真实收尾，accepted与confirmed分开。同步合成不能撤销时迟到结果不播放；停止后新接管的speak仍可发声。停止收音与停止语音输出分开。",
            concurrency="parallel",
            control=True,
        )
        output_dir = (base_dir / settings.output_dir).resolve()
        profile_path = (
            None
            if not settings.postprocess_profile
            else (base_dir / settings.postprocess_profile).resolve()
        )

        async def build(ports: PluginPorts) -> PluginRuntime:
            ledger = ports.audio
            if ledger is None:
                raise ConfigError("microphone插件需要宿主同库音频事实端口。")
            source = source_factory(settings)
            try:
                player = player_factory(settings)
            except BaseException as failure:
                # build尚未交回Runtime时也必须收回已取得的合成连接所有者。
                try:
                    await source.close()
                except BaseException as cleanup:
                    raise BaseExceptionGroup(
                        "音频构造与资源清理均失败。", [failure, cleanup]
                    ) from None
                raise
            runtime = AudioRuntime(
                instance_id,
                settings,
                source,
                player,
                ledger,
                output_dir,
                profile_path,
                late_results=ports.late_results,
                decode=decode,
            )

            async def run_record(parameters: RecordParameters, context: CallContext) -> CallOutcome:
                return await runtime.record(parameters, context)

            async def run_speak(parameters: SpeechParameters, context: CallContext) -> CallOutcome:
                return await runtime.speak(parameters, context)

            async def run_interrupt(parameters: _Empty, context: CallContext) -> CallOutcome:
                del parameters
                return await runtime.interrupt(context, record_late=True)

            def revision() -> str:
                return str(runtime.snapshot()["revision"])

            return PluginRuntime(
                (
                    FunctionCapability(record, run_record),
                    FunctionCapability(speak, run_speak),
                    FunctionCapability(interrupt, run_interrupt),
                ),
                start=runtime.start,
                stop_inputs=runtime.stop_inputs,
                close=runtime.close,
                snapshot=runtime.snapshot,
                prepare_request=runtime.prepare_request,
                panel=DevicePanel(
                    "audio",
                    (PanelControl("stop_output", "停止语音输出", interrupt.capability_id),),
                    revision,
                ),
                audio_output=runtime,
                resources_released=lambda: runtime.resources_released,
            )

        return PluginBlueprint(
            (ObjectDeclaration(object_id, settings.microphone_id, settings.label, "subject"),),
            (record, speak, interrupt),
            build,
        )

    return typed_factory(AudioSettings, declare)


factory = make_factory(
    FishSource,
    lambda settings: FFplayPlayer(settings.playback_executable, sample_rate=settings.sample_rate),
)
