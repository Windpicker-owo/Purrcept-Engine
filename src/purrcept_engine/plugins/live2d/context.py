"""把低频语境判断保持为短期情绪和注视，不调用模型或控制设备。

ContextController 由 MotionController 独占，同一单调时钟上 observe 后 sample。
情绪连续进入并在期限后衰减；注视只保留一个当前目标和一个候选，通过停留
门槛抑制来回扫视。显式字段的优先级由 MotionController 合成，不在此抢占。
"""

import math
from dataclasses import dataclass

from purrcept_engine.plugin_system import ExpressionAssessment
from purrcept_engine.plugins.live2d.models import BodyStyle, FocusLayout

# 表演姿态按 valence、arousal、tension、左眉、右眉、眼睑排列。
# 它们是角色风格参数，不是对人的心理或肌肉活动的测量。
_EMOTIONS = {
    "neutral": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    "warm": (0.65, 0.15, 0.0, 0.08, 0.08, -0.03),
    "concerned": (-0.25, 0.25, 0.35, 0.14, 0.14, -0.08),
    "curious": (0.15, 0.3, 0.0, 0.3, 0.08, -0.03),
    "excited": (0.8, 0.7, 0.05, 0.28, 0.28, 0.0),
    "displeased": (-0.5, 0.3, 0.55, -0.16, -0.16, -0.10),
}
_CHANNELS = ("valence", "arousal", "tension", "context_brow_l", "context_brow_r", "context_lid")


@dataclass(frozen=True, slots=True)
class _Focus:
    """一个有限候选；等待停留门槛不延长事件本身的期限。"""

    name: str
    expires: float
    urgent: bool


class ContextController:
    """每个视觉连接一个实例；仅存有限状态，reset 撤销所有旧判断。

    observe 的时间与外层 sample 一致。置信度只作为可调接纳门槛，不解释为
    准确率；低置信字段既不改值也不续期。情绪与注视独立接纳和失效。
    """

    def __init__(self, style: BodyStyle, layout: FocusLayout) -> None:
        """冻结本地策略与语境坐标，构造不读取时钟或启动任务。

        Args:
            style: 置信门槛、停留时间和情绪响应尺度。
            layout: 人工可校准的六个语境目标。
        """
        self._style = style
        self._points = {
            "audience": layout.audience,
            "chat": layout.chat,
            "game": layout.game,
            "work": layout.work,
            "speaker": layout.speaker,
            "away": layout.away,
        }
        self._baseline = (style.baseline_valence, style.baseline_arousal, 0.0, 0.0, 0.0, 0.0)
        self.reset(0.0)

    def reset(self, now: float) -> None:
        """断开或重连时清空判断，恢复基线而不重演历史。

        Args:
            now: 当前连接的单调时钟秒数。
        """
        self._from = self._goal = self._baseline
        self._emotion_started = self._emotion_expires = now
        self._focus = _Focus("audience", now, False)
        self._focus_since = -math.inf
        self._pending: _Focus | None = None

    def observe(
        self, assessment: ExpressionAssessment, *, now: float, deadline: float, urgent: bool
    ) -> None:
        """分别接纳有依据的新字段，所有期限都收紧到宿主交付的 deadline。

        Args:
            assessment: 同一次评估的情绪与注意对象。
            now: 交付时刻，不是模型输出中的时间。
            deadline: 绝对单调失效时间；到期即拒绝。
            urgent: 只允许注视候选提前结束停留，不改变动作力度。
        """
        if deadline <= now:
            return
        style = self._style
        if (
            assessment.emotion != "unchanged"
            and assessment.emotion_confidence >= style.context_confidence
        ):
            # 先采样旧曲线再更新目标，连续评估不会从中性重新起步。
            self._from = self._emotion(now)
            self._goal = _EMOTIONS[assessment.emotion]
            self._emotion_started = now
            self._emotion_expires = min(deadline, now + style.context_emotion_seconds)
        if (
            assessment.focus != "unchanged"
            and assessment.focus_confidence >= style.context_confidence
        ):
            self._pending = _Focus(
                assessment.focus, min(deadline, now + style.context_focus_seconds), urgent
            )
        self._advance_focus(now)

    def sample(self, now: float) -> dict[str, float]:
        """返回当前低优先级姿态，时钟跳跃不补演候选切换。

        Args:
            now: 外层控制器固定步长使用的单调时刻。

        Returns:
            情绪、额外眉眼和目标坐标；调用方再覆盖显式字段并做头眼协调。
        """
        self._advance_focus(now)
        values: dict[str, float] = dict(zip(_CHANNELS, self._emotion(now), strict=True))
        values["gaze_x"], values["gaze_y"] = self._points[self._focus.name]
        return values

    def has_focus(self, now: float) -> bool:
        """返回当前是否有未失效的语境注视，包括明确看向观众。

        Args:
            now: 与 sample 一致的单调时刻。

        Note:
            调度器据此让空闲扫视让位，不用目标是否为零猜测所有权。
        """
        return self._focus.expires > now

    def _emotion(self, now: float) -> tuple[float, ...]:
        """期限前缓慢进入，期限后从当时姿态连续衰减到基线。"""
        age = max(0.0, min(now, self._emotion_expires) - self._emotion_started)
        attack = -math.expm1(-age / self._style.context_emotion_attack_seconds)
        decay = math.exp(
            -max(0.0, now - self._emotion_expires) / self._style.context_emotion_decay_seconds
        )
        return tuple(
            baseline + (start + (goal - start) * attack - baseline) * decay
            for baseline, start, goal in zip(self._baseline, self._from, self._goal, strict=True)
        )

    def _advance_focus(self, now: float) -> None:
        """丢弃过期候选；相同目标可续期但不重新开始停留窗口。"""
        pending = self._pending
        if pending is not None and pending.expires <= now:
            self._pending = pending = None
        if pending is not None and (
            pending.name == self._focus.name
            or pending.urgent
            or now - self._focus_since >= self._style.context_focus_hold_seconds
        ):
            if pending.name != self._focus.name:
                self._focus_since = now
            self._focus, self._pending = pending, None
        if self._focus.expires <= now:
            self._focus = _Focus("audience", now, False)
