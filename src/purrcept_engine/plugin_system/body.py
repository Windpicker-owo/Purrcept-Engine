"""定义声音与 Live2D 共用的不可变身体语义值。

BodyIntent 只表达情绪、视线和离散动作；播放身份、期限与采用资格由宿主
及身体所有者绑定。这里不创建驱动、不关联播放资源或发送 VTS 参数。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BodyIntent(BaseModel):
    """共享纯语义契约，模型不能给出宿主身份、绝对时钟或播放关联。

    app/音频在实际播放开始后交给身体；独立 apply 由身体分配有期限的身份。
    情绪与注视字段缺省或 None 表示不更新，也不延长旧字段期限；显式零值恢复中性
    目标。各字段分别到期回落。gesture 缺省不打断动作，none 明确取消，
    其他手势在首次发送确认后启动一次。播放结束只撤销该播放仍拥有的字段。
    effort 为本次注视/手势的力度，0 放松、1 强调；缺省采用本地柔和基线。
    它不是持续情绪，单独更新不会改变正在执行的动作，也不继承上次力度。
    delivery 只用于 speak(body=...)，选择与实际音频韵律配合的交流方式：
    neutral 不自动摆头，explain 解释，question 疑问，deny 否定，emphasize 强调。
    省略时采用 neutral；它不从文本或音量推断含义，也不用于独立 apply。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    valence: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    arousal: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    tension: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    gaze_x: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    gaze_y: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    gesture: Literal["none", "nod", "shake", "tilt"] | None = None
    effort: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    delivery: Literal["neutral", "explain", "question", "deny", "emphasize"] | None = None
