"""保存公开值及其语义校验，不打开数据库或调用设备。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
