"""身体语义与 VTS 校准值，不包含音频设备或自动情绪推断。

BodyIntent 是模型可写的纯语义。身份和失效时间由身体所有者加入；连接
恢复不会复活过期意图。校准映射只影响视觉通道，不能改变音频进度。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from purrcept_engine.errors import EngineError

VISEMES = ("sil", "PP", "FF", "TH", "DD", "kk", "CH", "SS", "nn", "RR", "aa", "E", "ih", "oh", "ou")


class BodyError(EngineError):
    """外部身体故障的终止结果，音频必须继续独立运行。

    驱动在认证拒绝、协议错误、模型不符或本地令牌文件错误时抛出本类；
    身体所有者结束当前恢复轮次。仅 BodyUnavailable 允许有限网络恢复，
    程序错误和取消不转换成身体离线。
    """


class BodyUnavailable(BodyError):
    """明确的连接或请求暂时不可用，由身体所有者扣除恢复次数。

    本类不触发驱动内部重发，不表示上一帧未产生效果。重新连接后只发送
    当时的中性或新姿态，不补发故障前的请求及原生授权对话框。
    """


class BodyIntent(BaseModel):
    """共享纯语义契约，模型不能给出宿主身份、绝对时钟或播放关联。

    app/音频在实际播放开始后交给身体；独立 apply 由身体分配最多配置
    时长的身份。数值连续姿态随有效期采用，离散 gesture 每次身份只执行一次。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    valence: float = Field(default=0.0, ge=-1, le=1, allow_inf_nan=False)
    arousal: float = Field(default=0.0, ge=0, le=1, allow_inf_nan=False)
    tension: float = Field(default=0.0, ge=0, le=1, allow_inf_nan=False)
    gaze_x: float = Field(default=0.0, ge=-1, le=1, allow_inf_nan=False)
    gaze_y: float = Field(default=0.0, ge=-1, le=1, allow_inf_nan=False)
    gesture: Literal["none", "nod", "shake", "tilt"] = "none"


class BodyStyle(BaseModel):
    """本地自然动作习惯，配置零值只关闭相应程序运动，不删除语义或口型。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)
    baseline_valence: float = Field(default=0.0, ge=-1, le=1)
    baseline_arousal: float = Field(default=0.0, ge=0, le=1)
    motion_scale: float = Field(default=1.0, ge=0, le=2)
    blink_interval_seconds: float = Field(default=3.75, ge=1, le=10)
    gesture_cooldown_seconds: float = Field(default=2.0, ge=0.9, le=10)
    idle_motion_scale: float = Field(default=1.0, ge=0, le=2)
    speech_motion_scale: float = Field(default=1.0, ge=0, le=2)
    micro_expression_scale: float = Field(default=1.0, ge=0, le=2)


class ParameterBinding(BaseModel):
    """人工校准的归一化通道映射；VTS 自定义输入不代表模型已实际绑定。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    parameter: str = Field(pattern=r"^Purrcept[A-Za-z0-9]{1,24}$")
    sources: dict[str, float] = Field(min_length=1)
    minimum: float = Field(default=-1.0, allow_inf_nan=False)
    maximum: float = Field(default=1.0, allow_inf_nan=False)
    neutral: float = Field(default=0.0, allow_inf_nan=False)
    default_value: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def check_range(self) -> ParameterBinding:
        """拒绝错误校准，不用未知通道的零值掩盖拼写错误。"""
        allowed = {
            "valence",
            "arousal",
            "tension",
            "gaze_x",
            "gaze_y",
            "head_x",
            "head_y",
            "head_z",
            "eye_open",
            "mouth_open",
            "mouth_form",
            "breath",
            "body_x",
            "body_y",
            "body_z",
            "eye_l",
            "eye_r",
            "brow_l",
            "brow_r",
            "smile",
            *("viseme_" + name for name in VISEMES),
        }
        if not self.minimum <= self.neutral <= self.maximum or self.minimum == self.maximum:
            raise ValueError("身体参数范围必须包含中性值并有非零宽度。")
        if not self.sources.keys() <= allowed:
            raise ValueError("身体参数含未知通道。")
        for value in self.sources.values():
            if not -10 <= value <= 10:
                raise ValueError("身体映射系数必须在[-10,10]。")
        if (
            self.default_value is not None
            and not self.minimum <= self.default_value <= self.maximum
        ):
            raise ValueError("身体参数默认值必须在校准范围内。")
        return self

    def map(self, channels: dict[str, float]) -> float:
        """围绕中性值非对称缩放，混合超幅时显式限幅。"""
        value = max(
            -1.0, min(1.0, sum(channels[key] * scale for key, scale in self.sources.items()))
        )
        span = self.maximum - self.neutral if value >= 0 else self.neutral - self.minimum
        return self.neutral + value * span


class ModelProfile(BaseModel):
    """显式模型身份和唯一自定义输入，加载不替操作者猜测模型语义。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    model_id: str = Field(min_length=1)
    bindings: tuple[ParameterBinding, ...] = Field(min_length=1, max_length=100)
    lip_sync_offset_ms: float = Field(default=0.0, ge=-200, le=200, allow_inf_nan=False)

    @model_validator(mode="after")
    def unique_inputs(self) -> ModelProfile:
        """重复映射不能按列表先后静默覆盖。"""
        if len({item.parameter for item in self.bindings}) != len(self.bindings):
            raise ValueError("身体校准输入重复。")
        return self
