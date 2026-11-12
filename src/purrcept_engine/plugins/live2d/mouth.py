"""将媒体时钟上的口型目标变成有惯性的嘴部运动。

MouthController 由 Live2DRuntime 独占，在取得能量或 OVR 视素后 sample，
不改音频时间轴、不缓存声音。短促能量凹口可保持上次开度，开合使用不同
时间常数和速度限制，细小目标抖动不立即重定向。视素同样平滑，避免映射
视素的模型绕过嘴部动态。结束释放保持，重连 reset 清除旧口型。
"""

from __future__ import annotations

import math

from purrcept_engine.plugins.live2d.models import VISEMES, BodyStyle
from purrcept_engine.plugins.live2d.preferences import Preferences


def viseme_mouth_form(weights: tuple[float, ...]) -> float:
    """将已平滑 OVR 权重投影为单一展唇/圆唇轴，不能替代模型专用视素绑定。

    Args:
        weights: VISEMES 顺序的平滑权重；无分析器时全零。

    Returns:
        [-1,1] 的嘴型倾向；E/ih 展唇为正，oh/ou 圆唇为负，闭口音不添开度。

    Note:
        这是两参数模型的有损投影。权重不除以音量，低置信/低能量不能放大成
        满幅横向动作；最终方向仍须按 mouth_form 绑定人工确认。
    """
    if len(weights) != len(VISEMES):
        raise ValueError("口型视素数量无效。")
    return max(-1.0, min(1.0, weights[11] + weights[12] - weights[13] - weights[14]))


class MouthController:
    """单事件循环内使用的口型状态；输入已经过运行时的媒体身份和时效校验。"""

    def __init__(self, style: BodyStyle) -> None:
        """冻结开合习惯，不读取设备或系统时钟。

        Args:
            style: 开度上限、响应时间与短间隙保持时间。
        """
        self._style = style
        self._preferences: Preferences | None = None
        self.reset(0.0)

    def configure(self, preferences: Preferences) -> None:
        """改变后续目标映射与开合时间，保留当前嘴部位置和间隙保持。

        Args:
            preferences: 已校验的模型偏好；增益在限幅和平滑之前采用。
        """
        self._preferences = preferences

    def reset(self, now: float) -> None:
        """新连接从闭口开始，不能带入断开前的元音或保持期限。

        Args:
            now: 与 sample 相同的单调时钟秒数。
        """
        self._time = now
        self._opening = self._target = self._held = 0.0
        self._voiced_at = -math.inf
        self._weights = (0.0,) * len(VISEMES)

    def release(self) -> None:
        """播放结束或被替换时立即撤销间隙保持，当前开度仍可连续收回。"""
        self._held = 0.0
        self._voiced_at = -math.inf

    def sample(
        self, now: float, opening: float, weights: tuple[float, ...], *, active: bool
    ) -> tuple[float, tuple[float, ...]]:
        """生成有开合惯性的最终口型；active=False 会闭口而不是延长发言。

        Args:
            now: 有限、非递减的单调秒数。
            opening: 当前媒体位置的归一化开度目标。
            weights: 按 VISEMES 顺序的原生视素权重；无 OVR 时全部为零。
            active: 当前是否仍有实际播放身份。

        Returns:
            最终嘴部开度和平滑后的视素；权重总和不超过一。

        Raises:
            ValueError: 时间倒退、非有限或视素数量不符合内部契约。
        """
        if not math.isfinite(now) or now < self._time or len(weights) != len(VISEMES):
            raise ValueError("口型时钟或视素数量无效。")
        delta, self._time = min(0.25, now - self._time), now
        style = self._style
        preferences = self._preferences
        timing = preferences or style
        target = max(0.0, min(1.0, opening)) if active else 0.0
        if not active:
            self.release()
        elif target >= 0.07:
            self._held, self._voiced_at = target, now
        elif target < 0.035:
            target = self._held if now - self._voiced_at <= timing.mouth_gap_hold_seconds else 0.0

        target *= style.mouth_open_scale
        if preferences is not None:
            target = min(1.0, target**preferences.mouth_gamma * preferences.mouth_gain)
        # 零目标必须采用，不能被死区留下常开嘴；微小非零变化合并为同一开度。
        if target == 0 or abs(target - self._target) >= 0.045:
            self._target = target
        response = (
            timing.mouth_attack_seconds
            if self._target > self._opening
            else timing.mouth_release_seconds
        )
        change = (self._target - self._opening) * (-math.expm1(-delta / response))
        self._opening += max(-3.5 * delta, min(4.5 * delta, change))
        if self._target == 0 and self._opening < 0.003:
            self._opening = 0.0

        smoothed: list[float] = []
        for old, raw in zip(self._weights, weights, strict=True):
            goal = max(0.0, min(1.0, raw)) if active else 0.0
            response = timing.mouth_attack_seconds if goal > old else timing.mouth_release_seconds
            value = old + (goal - old) * (-math.expm1(-delta / response))
            smoothed.append(0.0 if goal == 0 and value < 0.003 else value)
        total = max(1.0, sum(smoothed))
        self._weights = tuple(value / total for value in smoothed)
        return self._opening, self._weights
