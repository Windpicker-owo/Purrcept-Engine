"""冻结本机收音配置与扩展边界，不加载原生库或建立 ASR 连接。

设备只交付有序 16 kHz mono S16 帧，VAD 决定人声窗口，转写器独占一次
发言的网络状态。来源显示名只是本次输入事实，不建立人员权限或跨通道关联。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from purrcept_engine.errors import EngineError


class VoiceInputError(EngineError):
    """外部设备、VAD、识别协议或有界缓存失败，失败发言不会提交残句。"""


class VoiceInputSettings(BaseModel):
    """声明期校验的采样与识别配置，ASR 凭据只来自显式配置。

    原生音频能力由装配端口按生成模型声明提供，不由模型别名猜测。
    起音确认与打断门槛分别控制缓存和可信 speech_started，不累计长停顿。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)
    voice_input_id: str = Field(default="voice_input", pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    label: str = Field(default="本机收音", min_length=1, pattern=r"\S")
    source_name: str = Field(default="本机麦克风", min_length=1, pattern=r"\S")
    vad_model_path: Path = Field(
        default=Path(".purrcept/voice-input/silero_vad.onnx"), strict=False
    )
    asr_api_key: SecretStr = SecretStr("")
    asr_url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    asr_model: str = Field(default="fun-asr-realtime", min_length=1)
    workspace_id: str = ""
    speech_threshold: float = Field(default=0.5, gt=0, lt=1)
    pre_roll_ms: int = Field(default=300, ge=100, le=1000)
    start_ms: int = Field(default=96, ge=32, le=500)
    end_ms: int = Field(default=500, ge=200, le=2000)
    min_speech_ms: int = Field(default=160, ge=32, le=1000)
    interrupt_ms: int = Field(default=2000, ge=32, le=10000)
    interrupt_gap_ms: int = Field(default=96, ge=0, le=500)
    asr_timeout_seconds: float = Field(default=10.0, ge=1, le=60)
    max_pending_utterances: int = Field(default=4, ge=1, le=8)
    frame_queue_size: int = Field(default=64, ge=8, le=256)

    @model_validator(mode="after")
    def validate_order(self) -> VoiceInputSettings:
        """前置缓存覆盖起音。

        Returns:
            已校验并固定 VAD 等资源路径的收音配置。

        Raises:
            ValueError: 参数值或组合不满足本入口的明确约束，错误正文指出具体字段。

        Note:
            连续人声确认不能早于起音或跨越分段长停顿。
        """
        if self.pre_roll_ms < self.start_ms:
            raise ValueError("前置缓存必须覆盖起音确认。")
        if self.interrupt_ms < self.start_ms or self.interrupt_gap_ms >= self.end_ms:
            raise ValueError("持续人声确认与停顿分段顺序无效。")
        if not self.asr_url.startswith("wss://"):
            raise ValueError("ASR 必须使用显式安全 WebSocket 地址。")
        return self


@dataclass(frozen=True, slots=True)
class InputDevice:
    """设备枚举事实；枚举不打开输入流，ID 仅在本机当前 PortAudio 列表内有效。

    Attributes:
        device_id: 捕获库枚举的原生输入设备序号。
        name: 当前声明、字段或协议操作的明确名称。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    device_id: int
    name: str


class CaptureDriver(Protocol):
    """运行时每次开始收音创建的设备所有者，内置实现为 SoundDeviceCapture。

    frames 按顺序交付完整 512 样本帧；队列满或设备溢出必须失败而非继续
    拼接缺口。取消迭代器释放输入流，调用方不借用回调缓冲区。
    """

    async def devices(self) -> tuple[InputDevice, ...]:
        """只枚举设备名称与 ID，不采集音频。

        Returns:
            捕获库实际枚举的输入设备及采样信息。
        """
        ...

    def frames(self, device: int | None) -> AsyncGenerator[bytes]:
        """打开一次输入流，取消或失败归还原生资源。

        Args:
            device: 显式选定的输入设备序号；None 交由设备适配器解析。

        Returns:
            按原始采集、解码或播放顺序逐块交付的完整字节流。
        """
        ...


class VoiceDetector(Protocol):
    """单次收音会话独占的有状态 VAD；内置 SileroVad 采用本机 ONNX 模型。

    runtime 在回调之外顺序调用 probability，每帧恰好 32 ms；不同会话
    不复用循环状态，异常终止当前采集并标明缺口。
    """

    def probability(self, pcm: bytes) -> float:
        """返回当前帧人声概率，不把语义、音量或身份当作人声判断。

        Args:
            pcm: 已解码的 PCM 字节或按播放顺序产生的字节流。
        """
        ...


@dataclass(frozen=True, slots=True)
class TranscriptUpdate:
    """一次识别任务内的句子修订，final 不等同于整个任务完成。

    Attributes:
        sentence_id: 同一捕获会话内本次发言的稳定身份。
        text: 本次明确传输、输入或呈现的完整文本。
        final: 此次转写是否为端点确认的最终结果。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    sentence_id: str
    text: str
    final: bool


class StreamingTranscriber(Protocol):
    """runtime 为每次发言调用的流式识别边界，默认 FunASRTranscriber。

    帧序列有界、有序，EOF 表示 VAD 正常结束。供应商 task-finished 后才
    正常退出；失败不重传、不采用临时文本，取消关闭网络连接。实例可复用，
    每次调用的句子表和任务身份必须独立。
    """

    def transcribe(self, frames: AsyncIterator[bytes]) -> AsyncIterator[TranscriptUpdate]:
        """并行发送音频与接收修订，正常结束代表最终结果完整。

        Args:
            frames: 当前捕获块的帧数或顺序产生的 PCM 帧流。

        Returns:
            按真实接收顺序产生的转写更新，保留临时与最终文本区别。
        """
        ...


type InputMode = Literal["asr", "audio"]
