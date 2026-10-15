"""现场语音的冻结参数和真实阶段，不拥有设备、队列或主体生成代次。

参数只在能力边界校验。播放身份由 CallContext 分配，结果分别说明合成、
播放器开始和终止，不能由 accepted 推断已经发声。身体语义沿用 Live2D
共享值类型，省略整个 body 与全零意图保持不同。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from purrcept_engine.plugin_system import BodyIntent
from purrcept_engine.values import JsonObject


class AudioSettings(BaseModel):
    """纯声明阶段冻结的输出配置，构造不打开网络或播放器。

    api_key 只来自显式配置。ffplay/ffmpeg 是明确外部程序；streaming 使用
    同一独立音频播放器，不借用 Live2D 连接或原生进程。队列只数候播项。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    microphone_id: str = Field(default="microphone", pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    label: str = Field(default="麦克风", min_length=1, pattern=r"\S")
    api_key: SecretStr = SecretStr("")
    api_base: str = "https://fishaudio.org/api/open/v3"
    model: str = Field(default="fishaudio-s21pro-flash", min_length=1, pattern=r"\S")
    voice_id: str = Field(
        default="00a1b221-6137-4b73-ad62-b0cbce134167", min_length=1, pattern=r"\S"
    )
    timeout_seconds: float = Field(default=30.0, gt=0, le=180, allow_inf_nan=False)
    playback_timeout_seconds: float = Field(default=120.0, gt=0, le=180, allow_inf_nan=False)
    playback_backend: Literal["streaming", "ffplay"] = "streaming"
    playback_executable: str = Field(default="ffplay", min_length=1)
    processing_executable: str = Field(default="ffmpeg", min_length=1)
    audio_format: Literal["mp3", "wav"] = "mp3"
    speed: float = Field(default=1.0, ge=0.5, le=2.0, allow_inf_nan=False)
    sample_rate: Literal[16000, 24000, 32000, 44100] = 32000
    max_pending: int = Field(default=4, ge=0, le=32)
    output_dir: str = Field(default=".purrcept/microphone", min_length=1, pattern=r"\S")
    postprocess_profile: str = ""

    @model_validator(mode="after")
    def explicit_endpoint(self) -> AudioSettings:
        """纯配置阶段拒绝相对端点及隐藏URL凭据，不等到付费前才猜地址。

        Returns:
            本次声线与外部程序路径的已校验设置。

        Raises:
            ValueError: Fish Audio需要不带URL凭据/查询串的完整http(s)根端点。
        """
        url = urlsplit(self.api_base)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Fish Audio需要不带URL凭据/查询串的完整http(s)根端点。")
        return self


# 维护者属性契约（类说明参与 schema，保持原 docstring）。
# Attributes:
#     text: 实际发给合成端点的完整发声文本。
#     speed: 显式语速；None 沿当前声音配置，不静默忽略。
#     body: 可选语义姿态，不使身体取得音频资源所有权。
class SpeechParameters(BaseModel):
    """保留公开说话参数；body 只携带语义，不允许模型分配播放身份或时钟。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    text: str = Field(min_length=1, max_length=10000, pattern=r"\S")
    voice_id: str = ""
    speed: float | None = Field(default=None, ge=0.5, le=2.0, allow_inf_nan=False)
    volume: float = Field(default=0.0, ge=-20, le=20, allow_inf_nan=False)
    format: Literal["", "mp3", "wav", "pcm"] = ""
    body: BodyIntent | None = None


class RecordParameters(BaseModel):
    """只生成文件/base64，不加入现场队列，也不消费 BodyIntent。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    text: str = Field(min_length=1, max_length=10000, pattern=r"\S")
    save_as: str = Field(default="", max_length=512)
    voice_id: str = ""
    speed: float | None = Field(default=None, ge=0.5, le=2.0, allow_inf_nan=False)
    volume: float = Field(default=0.0, ge=-20, le=20, allow_inf_nan=False)
    format: Literal["", "mp3", "wav"] = ""


@dataclass(frozen=True, slots=True)
class SpeechAudio:
    """一次合成取得的完整字节；未知时长不以配置或字节数猜测。

    Attributes:
        data: 实际合成或解码处理完成的完整编码音频字节。
        format: data 实际采用的 mp3 或 wav 编码。
        duration_seconds: 实际可确定的音频时长（秒）；未取得时保留 None。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    data: bytes
    format: Literal["mp3", "wav"]
    duration_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class PlaybackResult:
    """播放器确认的终态，elapsed 是媒体时钟，不冒充精确 DAC 样本位置。

    Attributes:
        status: 播放器实际报告的完成、失败、中断、超时或未开始结论。
        started: 播放器是否确实开始，区别于合成或接管成功。
        elapsed_seconds: 实际播放器报告的已播放秒数。
        error: 当前流程已经取得的真实故障或失败说明。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    status: Literal["completed", "failed", "interrupted", "timed_out", "not_started"]
    started: bool
    elapsed_seconds: float = 0.0
    error: str | None = None

    def json(self) -> JsonObject:
        """保留部分播放边界。

        Returns:
            业务对象，按实际结果提供 status、started、completed、partial_playback_possible、
            elapsed_seconds、progress_basis、error 字段。

        Note:
            正常退出不证明物理扬声器或听者已听到。
        """
        return {
            "status": self.status,
            "started": self.started,
            "completed": self.status == "completed",
            "partial_playback_possible": self.started and self.status != "completed",
            "elapsed_seconds": self.elapsed_seconds,
            "progress_basis": "player_media_clock",
            "error": self.error,
        }
