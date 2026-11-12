"""身体语义与 VTS 校准值，不包含音频设备或自动情绪推断。

BodyIntent 是模型可写的纯语义。身份和失效时间由身体所有者加入；连接
恢复不会复活过期意图。校准映射只影响视觉通道，不能改变音频进度。
"""

from __future__ import annotations

from typing import Annotated

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


class BodyStyle(BaseModel):
    """本地动作习惯；motion_scale 控制手势和待机，不缩放明确注视或口型。

    head_turn_seconds 与 relaxed_head_turn_seconds 分别是强调和放松转头的
    完成时间，default_effort 是每次未注明力度的新动作的基线。身体
    response_seconds 是临界阻尼时间尺度，body_gaze_scale 是越过注视死区后
    的最大归一化身体转向幅度。主动头部动作不再经过第二层弹簧。当前版本
    不再提供音量驱动摆头和独立正弦微表情；眨眼间隔是随机分布的尺度。
    speech_gesture_scale 只缩放自动语音动作，speech_gesture_interval_seconds
    限制媒体时钟中的动作密度；交流意图不会自动把缺省力度提高到强调档。
    speech_expression_scale 控制交流眉眼/嘴角层，mouth_* 控制输出口型的
    开合惯性与短间隙保持；不改变音频、播放器进度或情绪字段的所有权。
    context_* 仅控制已采用注意判断的低优先级姿态：置信门槛不是正确率，
    focus_hold 是切换间隔，focus/emotion_seconds 是有效期上限，情绪的
    attack/decay 是进入和自然消退的指数响应尺度。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)
    baseline_valence: float = Field(default=0.0, ge=-1, le=1)
    baseline_arousal: float = Field(default=0.0, ge=0, le=1)
    motion_scale: float = Field(default=1.0, ge=0, le=2)
    blink_interval_seconds: float = Field(default=3.75, ge=1, le=10)
    gesture_cooldown_seconds: float = Field(default=2.0, ge=0.9, le=10)
    idle_motion_scale: float = Field(default=1.0, ge=0, le=2)
    head_turn_seconds: float = Field(default=0.24, ge=0.08, le=1)
    relaxed_head_turn_seconds: float = Field(default=0.70, ge=0.08, le=2)
    default_effort: float = Field(default=0.15, ge=0, le=1)
    body_response_seconds: float = Field(default=0.65, ge=0.1, le=3)
    body_gaze_scale: float = Field(default=0.50, ge=0, le=1)
    speech_gesture_scale: float = Field(default=0.80, ge=0, le=1)
    speech_gesture_interval_seconds: float = Field(default=3.0, ge=2, le=10)
    speech_expression_scale: float = Field(default=0.90, ge=0, le=1)
    mouth_open_scale: float = Field(default=0.75, ge=0, le=1)
    mouth_attack_seconds: float = Field(default=0.07, ge=0.02, le=0.3)
    mouth_release_seconds: float = Field(default=0.14, ge=0.03, le=0.4)
    mouth_gap_hold_seconds: float = Field(default=0.08, ge=0, le=0.2)
    gaze_hold_seconds: float = Field(default=0.35, ge=0, le=2)
    head_delay_seconds: float = Field(default=0.12, ge=0, le=1)
    body_delay_seconds: float = Field(default=0.55, ge=0, le=2)
    context_confidence: float = Field(default=0.65, ge=0, le=1)
    context_focus_hold_seconds: float = Field(default=1.2, ge=0, le=5)
    context_focus_seconds: float = Field(default=6.0, ge=0.5, le=15)
    context_emotion_seconds: float = Field(default=8.0, ge=0.5, le=15)
    context_emotion_attack_seconds: float = Field(default=1.2, ge=0.1, le=5)
    context_emotion_decay_seconds: float = Field(default=6.0, ge=0.2, le=20)


type FocusPoint = tuple[Annotated[float, Field(ge=-1, le=1)], Annotated[float, Field(ge=-1, le=1)]]


class FocusLayout(BaseModel):
    """语境对象在角色归一化注视空间中的位置，不代表实际屏幕坐标。

    模型校准文件可分别调整六个目标；audience 同时是无有效语境时的回正位置。
    方向仍经过该模型的眼头补偿及参数绑定，不从来源名称猜测屏幕布局。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)
    audience: FocusPoint = (0.0, 0.0)
    chat: FocusPoint = (-0.65, -0.1)
    game: FocusPoint = (0.5, 0.0)
    work: FocusPoint = (0.25, -0.45)
    speaker: FocusPoint = (0.0, 0.08)
    away: FocusPoint = (-0.35, 0.25)


class GazeCalibration(BaseModel):
    """将头身归一化输出换算为注视偏移，供眼球反向补偿。

    系数属于模型校准而非人体角度，默认值只是调试起点。符号应在完成
    ParameterBinding 和 VTS 输入映射后人工核对；零值关闭相应补偿。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)
    head_x: float = Field(default=0.8, ge=-3, le=3)
    head_y: float = Field(default=0.8, ge=-3, le=3)
    body_x: float = Field(default=0.3, ge=-3, le=3)
    body_y: float = Field(default=0.3, ge=-3, le=3)


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
        """拒绝错误校准，不用未知通道的零值掩盖拼写错误。

        Returns:
            明确绑定的 VTS 参数与数值映射。

        Raises:
            ValueError: 参数值或组合不满足本入口的明确约束，错误正文指出具体字段。
        """
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
        """围绕中性值非对称缩放，混合超幅时显式限幅。

        Args:
            channels: 原生音频的通道数或显式身体参数通道映射。
        """
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
    gaze: GazeCalibration = Field(default_factory=GazeCalibration)
    focus: FocusLayout = Field(default_factory=FocusLayout)

    @model_validator(mode="after")
    def unique_inputs(self) -> ModelProfile:
        """重复映射不能按列表先后静默覆盖。

        Returns:
            模型校准及人工参数绑定的已校验结果。

        Raises:
            ValueError: 身体校准输入重复。
        """
        if len({item.parameter for item in self.bindings}) != len(self.bindings):
            raise ValueError("身体校准输入重复。")
        return self
