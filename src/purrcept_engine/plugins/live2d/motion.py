"""将有期限的局部身体意图变成协调的连续姿态，不拥有设备或声音。

Live2DRuntime 为每个连接创建/重置 MotionController，先 submit 更新字段，
实际发送确认后 confirm 启动一次动作，再用 sample 生成最新帧。情绪字段、
注视目标与动作各自失效；revoke 只撤销同播放拥有的状态。
ContextController 提供低优先级语境基线，显式字段覆盖同名语义；语境的期限
在被遮挡时照常推进，语音结束不会将它一并清空。

每次动作冻结自己的力度：放松动作平滑往返，强调动作才加入起势与短暂停留。
主动头部动作使用有限时长轨迹，身体使用临界阻尼跟随；不把主动动作再次
送入弹簧过滤。内部以固定 120 Hz 推进运动和事件抽样，输出频率不改变随机序列。
顺序是期限、注意调度、姿态与手势、头身动力学、眼球补偿、表情与眨眼。
speech_cue 只接受已经到达播放器时刻的机会；自动手势阶段由 advance_speech
推进，媒体暂停时保持，显式动作优先。终止后的收势重新使用本地时间。
超过半秒的停顿跳过旧事件，最多追赶半秒，不在恢复后补演历史动作。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from purrcept_engine.plugin_system import ExpressionAssessment
from purrcept_engine.plugin_system.body import BodyIntent
from purrcept_engine.plugins.live2d.context import ContextController
from purrcept_engine.plugins.live2d.models import BodyStyle, FocusLayout, GazeCalibration
from purrcept_engine.plugins.live2d.preferences import Preferences
from purrcept_engine.plugins.live2d.prosody import SpeechCue

_DT = 1 / 120

# 各阶段给出结束时间和归一化目标。短主动作与明确停留用于表演节奏，
# 这些是可调的角色动作设计，不声称模拟真实肌肉力学。
_GESTURE_STAGES = {
    "nod": ((0.08, -0.025), (0.21, 0.16), (0.29, 0.16), (0.48, -0.025), (0.64, 0.0)),
    "shake": ((0.08, 0.025), (0.22, -0.16), (0.30, -0.16), (0.48, 0.13), (0.54, 0.13), (0.74, 0.0)),
    "tilt": ((0.20, 0.18), (0.80, 0.18), (1.08, 0.0)),
}

# 相同阶段索引便于连续混合。重复结束时间表示放松动作省去准备、刹停或
# 回弹阶段；不能用微小的反向动作冒充放松运动。
_RELAXED_GESTURE_STAGES = {
    "nod": ((0.0, 0.0), (0.50, 0.12), (0.50, 0.12), (1.10, 0.0), (1.10, 0.0)),
    "shake": ((0.0, 0.0), (0.45, -0.12), (0.45, -0.12), (1.15, 0.10), (1.15, 0.10), (1.70, 0.0)),
    "tilt": ((0.60, 0.16), (0.85, 0.16), (1.50, 0.0)),
}

# 交流方式的眉眼/嘴角姿态是角色表演选择，不将“否定”等同于愤怒。
# 按左眉、右眉、眼睑偏移、笑眼、嘴角排列；持续情绪仍独立参与合成。
_SPEECH_EXPRESSIONS = {
    "neutral": (0.0, 0.0, 0.0, 0.0, 0.0),
    "explain": (0.14, 0.14, -0.08, 0.20, 0.20),
    "question": (0.45, 0.12, -0.06, 0.0, -0.08),
    "deny": (-0.32, -0.32, -0.12, 0.0, -0.25),
    "emphasize": (0.32, 0.32, -0.04, 0.08, 0.12),
}


def _gesture_stages(kind: str, effort: float) -> tuple[tuple[float, float], ...]:
    """将力度解析为冻结的动作谱；低力度只改变速度，不添加反向预备动作。"""
    accent = max(0.0, (effort - 0.45) / 0.55)
    relaxed_time = 1 - 0.25 * effort
    return tuple(
        (
            soft_end * relaxed_time * (1 - accent) + strong_end * accent,
            soft_value * (1 - accent) + strong_value * accent,
        )
        for (soft_end, soft_value), (strong_end, strong_value) in zip(
            _RELAXED_GESTURE_STAGES[kind], _GESTURE_STAGES[kind], strict=True
        )
    )


@dataclass(slots=True)
class _Stroke:
    """有限时长的五次轨迹，重定向保持当前位置、速度和加速度。

    终点速度和加速度归零，所以主动动作能按时刹停并真正保持。目标不变时
    不重启轨迹；这与每帧不断靠近目标的跟随系统有不同的时间契约。
    """

    position: float = 0.0
    velocity: float = 0.0
    acceleration: float = 0.0
    target: float = 0.0
    elapsed: float = 0.0
    duration: float = 1.0
    coefficients: tuple[float, float, float, float, float, float] = (0, 0, 0, 0, 0, 0)

    def aim(self, target: float, duration: float) -> None:
        """在目标改变时重新规划，继承当前导数，避免中断动作时瞬移或反向跳速。"""
        if target == self.target:
            return
        self.target, self.elapsed, self.duration = target, 0.0, duration
        start = self.position
        speed = self.velocity * duration
        half_accel = self.acceleration * duration**2 / 2
        distance = target - start
        self.coefficients = (
            start,
            speed,
            half_accel,
            10 * distance - 6 * speed - 3 * half_accel,
            -15 * distance + 8 * speed + 3 * half_accel,
            6 * distance - 3 * speed - half_accel,
        )

    def advance(self) -> float:
        """推进到本次采样位置，到达期限后精确保持终点。"""
        self.elapsed = min(self.duration, self.elapsed + _DT)
        if self.elapsed >= self.duration:
            self.position, self.velocity, self.acceleration = self.target, 0.0, 0.0
            return self.position
        u = self.elapsed / self.duration
        a, b, c, d, e, f = self.coefficients
        self.position = a + u * (b + u * (c + u * (d + u * (e + u * f))))
        self.velocity = (b + u * (2 * c + u * (3 * d + u * (4 * e + u * 5 * f)))) / self.duration
        self.acceleration = (2 * c + u * (6 * d + u * (12 * e + u * 20 * f))) / self.duration**2
        return self.position


@dataclass(slots=True)
class _Axis:
    """保存一个运动自由度，目标改变时保留速度以保持动作连续。"""

    position: float = 0.0
    velocity: float = 0.0

    def advance(self, target: float, response: float) -> float:
        """用临界阻尼解析解推进一小步；response 是响应时间尺度，单位秒。"""
        omega = 2 / response
        offset = self.position - target
        impulse = self.velocity + omega * offset
        decay = math.exp(-omega * _DT)
        self.position = target + (offset + impulse * _DT) * decay
        self.velocity = (self.velocity - omega * impulse * _DT) * decay
        if abs(self.position) > 1:
            self.position = max(-1.0, min(1.0, self.position))
            # 限位时消除向外速度，不能在隐藏越界状态中积蓄下一次回弹。
            if self.position * self.velocity > 0:
                self.velocity = 0.0
        return self.position


@dataclass(frozen=True, slots=True)
class _Target:
    """字段的最新所有者及期限；其他字段更新不能续期或撤销它。"""

    value: float
    expires: float
    playback: str | None
    effort: float
    submitted: float


@dataclass(frozen=True, slots=True)
class _Gesture:
    """首次确认启动的有限动作；恢复运动不属于继续执行该语义动作。"""

    kind: str
    started: float
    expires: float
    playback: str | None
    stages: tuple[tuple[float, float], ...]
    scale: float = 1.0
    automatic: bool = False


@dataclass(frozen=True, slots=True)
class _SpeechExpression:
    """当前发言持有的交流姿态；播放撤销只回收这一层，不清除独立情绪。"""

    delivery: str
    expires: float
    playback: str


def _ease(value: float) -> float:
    """端点速度和加速度为零的五次过渡，输入按阶段进度限幅。"""
    value = max(0.0, min(1.0, value))
    return value**3 * (10 + value * (-15 + value * 6))


class MotionController:
    """由视觉运行时独占的同步动作状态机，可用注入时间与种子离线回放。

    所有方法在同一事件循环串行调用，不等待网络。sample 返回独立字典，
    口型由运行时在其后加入。实例不保存完整事件历史；每个字段至多一个目标，
    手势至多一个。连接恢复必须 reset，不能继承旧速度或过期表达。
    """

    def __init__(
        self,
        style: BodyStyle,
        gaze: GazeCalibration,
        *,
        seed: int | None = None,
        focus: FocusLayout | None = None,
    ) -> None:
        """冻结风格与模型补偿参数，不读取系统时钟。

        Args:
            style: 动作幅度、响应时间与基线。
            gaze: 模型归一化坐标中的眼头补偿系数。
            seed: 固定时可重放同一事件序列；None 使用独立随机序列。
            focus: 语境目标坐标，未提供时采用默认虚拟布局。
        """
        self._style, self._calibration = style, gaze
        self._preferences = Preferences(model_id="unbound")
        self._context = ContextController(style, focus or FocusLayout())
        self._random = random.Random(seed)
        self._time: float | None = None
        self._targets: dict[str, _Target] = {}
        self._axes: dict[str, _Axis] = {}
        self._head_axes: tuple[_Stroke, _Stroke, _Stroke] = (_Stroke(), _Stroke(), _Stroke())
        self._gesture_axes: tuple[_Stroke, _Stroke, _Stroke] = (_Stroke(), _Stroke(), _Stroke())
        self._gesture_stage = -1
        self._speech_time = self._speech_advanced = 0.0
        self._values: dict[str, float] = {}
        self._gesture: _Gesture | None = None
        self._last_gesture = -math.inf
        self._gaze = (0.0, 0.0)
        self._gaze_since = -math.inf
        self._gaze_effort = style.default_effort
        self._head_duration = style.relaxed_head_turn_seconds
        self._head_goal = (0.0, 0.0)
        self._body_goal = (0.0, 0.0)
        self._idle_goal = (0.0, 0.0)
        self._idle_gaze = (0.0, 0.0)
        self._idle_at = math.inf
        self._blink_at = math.inf
        self._blink_started: float | None = None
        self._last_blink = -math.inf
        self._speech_expression: _SpeechExpression | None = None
        self._expression_values = [0.0] * 5
        self._context_weight = 0.0
        self._origin = 0.0

    def reset(self, now: float) -> None:
        """从新连接的基线开始，撤销所有旧字段、动作、速度和事件期限。

        Args:
            now: 与 sample 相同的单调时钟秒数。
        """
        self._time = self._origin = now
        self._context.reset(now)
        self._targets.clear()
        self._gesture = None
        self._last_gesture = -math.inf
        self._gaze, self._head_goal, self._body_goal = (0.0, 0.0), (0.0, 0.0), (0.0, 0.0)
        self._gaze_since = now - self._style.gaze_hold_seconds
        self._gaze_effort = self._style.default_effort
        self._head_duration = self._style.relaxed_head_turn_seconds
        self._idle_goal = (0.0, 0.0)
        self._idle_gaze = (0.0, 0.0)
        self._idle_at = now + self._idle_interval()
        self._blink_at = now + self._blink_interval()
        self._blink_started = None
        self._last_blink = now
        self._speech_expression = None
        self._expression_values = [0.0] * 5
        self._context_weight = 0.0
        self._head_axes = (_Stroke(), _Stroke(), _Stroke())
        self._gesture_axes = (_Stroke(), _Stroke(), _Stroke())
        self._gesture_stage = -1
        self._speech_time = self._speech_advanced = 0.0
        self._axes = {
            name: _Axis()
            for name in (
                "body_x",
                "body_y",
                "body_z",
                "gaze_x",
                "gaze_y",
            )
        }
        self._values = dict.fromkeys(self._axes, 0.0)
        self._values.update(
            head_x=0.0,
            head_y=0.0,
            head_z=0.0,
            valence=self._style.baseline_valence,
            arousal=self._style.baseline_arousal,
            tension=0.0,
            eye_open=1.0,
            eye_l=1.0,
            eye_r=1.0,
            brow_l=0.0,
            brow_r=0.0,
            smile=self._style.baseline_valence * 0.4,
            mouth_form=self._style.baseline_valence * 0.2,
            breath=0.0,
        )

    def configure(self, preferences: Preferences) -> None:
        """采用新偏好，保留当前速度、语义期限与随机序列。

        Args:
            preferences: 已按当前模型校验的姿态和空闲调度偏好。

        Note:
            下一固定步长重定向目标；不 reset，避免滑块使动作跳回正面。
        """
        self._preferences = preferences

    def observe(
        self, assessment: ExpressionAssessment, *, now: float, deadline: float, urgent: bool
    ) -> None:
        """更新低优先级语境，不撤销显式字段、手势或播放所有权。

        Args:
            assessment: 已采用批次附带的身体判断。
            now: 与帧循环一致的单调时刻。
            deadline: 宿主限定的语义失效时间。
            urgent: 是否允许提前切换语境注视，力度仍使用放松基线。
        """
        self.sample(now)
        self._context.observe(assessment, now=now, deadline=deadline, urgent=urgent)

    def submit(
        self, intent: BodyIntent, *, now: float, expires: float, playback: str | None
    ) -> None:
        """更新明确给出的字段，其他字段和已经启动的动作保持自己的期限。

        Args:
            intent: 局部更新；gesture=none 明确取消当前动作。
            now: 受理时的单调时钟。
            expires: 宿主收紧后的绝对截止；已到期更新不采用。
            playback: 关联播放身份，独立调用为 None。
        """
        self.sample(now)
        if expires <= now:
            return
        fields = {
            "valence": intent.valence,
            "arousal": intent.arousal,
            "tension": intent.tension,
            "gaze_x": intent.gaze_x,
            "gaze_y": intent.gaze_y,
        }
        for name, value in fields.items():
            if value is not None:
                effort = self._style.default_effort if intent.effort is None else intent.effort
                self._targets[name] = _Target(value, expires, playback, effort, now)
        if intent.gesture == "none":
            self._gesture = None
        if playback is not None and intent.delivery is not None:
            self._speech_expression = _SpeechExpression(intent.delivery, expires, playback)

    def confirm(
        self, intent: BodyIntent, *, now: float, expires: float, playback: str | None
    ) -> None:
        """在首次发送确认时启动手势；冷却中的新手势丢弃，不延期补演。

        Args:
            intent: 运行时仍确认有效的原始意图；每次身份只能调用一次。
            now: 首次确认的单调时刻。
            expires: 动作不得超过的宿主期限。
            playback: 动作所属播放；None 表示独立动作。
        """
        if intent.gesture in (None, "none") or expires <= now:
            return
        automatic = self._gesture is not None and self._gesture.automatic
        if not automatic and now - self._last_gesture < self._style.gesture_cooldown_seconds:
            return
        self.sample(now)
        effort = self._style.default_effort if intent.effort is None else intent.effort
        stages = _gesture_stages(intent.gesture, effort)
        self._gesture = _Gesture(intent.gesture, now, expires, playback, stages)
        self._gesture_stage = -1
        self._last_gesture = now

    def advance_speech(self, playback: str, elapsed: float) -> None:
        """只推进同身份自动动作的媒体时钟；没有新进度时姿态保持。

        Args:
            playback: 当前真实播放身份。
            elapsed: 播放器报告的非递减秒数，不由墙钟外推。
        """
        gesture = self._gesture
        if gesture is not None and gesture.automatic and gesture.playback == playback:
            self._speech_time = max(self._speech_time, elapsed)

    def speech_cue(
        self, cue: SpeechCue, *, playback: str, elapsed: float, now: float, expires: float
    ) -> None:
        """采用当前语音机会，显式手势占用时直接略过自动动作。

        Args:
            cue: 调度器已消费的一次候选，不允许因占用而重新排队。
            playback: 当前实际播放身份，用于结束时撤销。
            elapsed: 机会被采用时的实际媒体秒数。
            now: 当前单调时刻，用于截止、冷却和眨眼调度。
            expires: 本次发言允许表演的绝对截止。

        Note:
            语音动作使用媒体时钟，停止后转为本地连续收势。停顿仅提高眨眼
            机会，并保留两秒最短间隔，不把每个停顿都转换成眨眼。
        """
        self.sample(now)
        if now >= expires:
            return
        if (
            cue.pause
            and self._blink_started is None
            and now - self._last_blink >= 2.0
            and self._random.random() < 0.45
        ):
            self._blink_at = now
        if (
            cue.gesture is None
            or cue.scale == 0
            or self._gesture is not None
            or now - self._last_gesture < self._style.gesture_cooldown_seconds
        ):
            return
        self._gesture = _Gesture(
            cue.gesture,
            elapsed,
            expires,
            playback,
            _gesture_stages(cue.gesture, cue.effort),
            cue.scale,
            True,
        )
        self._gesture_stage = -1
        self._speech_time, self._speech_advanced = elapsed, 0.0
        self._last_gesture = now

    def revoke(self, playback: str, now: float) -> None:
        """撤销指定播放仍拥有的目标与动作，保留当前位置和速度用于平滑回落。

        Args:
            playback: 已结束或被替换的播放身份。
            now: 撤销时单调时刻；旧播放不能清除后来独立更新的字段。
        """
        self.sample(now)
        self._targets = {
            key: value for key, value in self._targets.items() if value.playback != playback
        }
        if self._gesture is not None and self._gesture.playback == playback:
            self._gesture = None
        if self._speech_expression is not None and self._speech_expression.playback == playback:
            self._speech_expression = None

    def sample(self, now: float) -> dict[str, float]:
        """按真实经过时间生成一帧，调用本身不证明设备采用。

        Args:
            now: 有限、非递减的单调秒数。

        Returns:
            情绪、头身、眼球、眼睑和表情通道，不含口型开合或视素。

        Raises:
            ValueError: 时钟非有限或倒退。
        """
        if not math.isfinite(now) or (self._time is not None and now < self._time - 1e-8):
            raise ValueError("动作时钟必须有限且单调。")
        if self._time is None:
            self.reset(now)
        assert self._time is not None
        if now - self._time > 0.5:
            # 长暂停不追赶历史眨眼或姿态抽样；旧动作按绝对年龄自然失效。
            self._time = now - 0.5
            self._blink_started = None
            self._blink_at = now + self._blink_interval()
            self._idle_at = now + self._idle_interval()
        while self._time + _DT <= now + 1e-8:
            self._time += _DT
            self._advance(self._time)
        return dict(self._values)

    def _blink_interval(self) -> float:
        return self._style.blink_interval_seconds * self._random.uniform(0.65, 1.45)

    def _idle_interval(self) -> float:
        return self._preferences.idle_interval_seconds * self._random.uniform(0.55, 1.55)

    def _advance(self, now: float) -> None:
        """按固定顺序合成，眼球补偿必须晚于手势和头身动态更新。"""
        style = self._style
        gaze_expired = any(
            key in {"gaze_x", "gaze_y"} and value.expires <= now
            for key, value in self._targets.items()
        )
        self._targets = {key: value for key, value in self._targets.items() if value.expires > now}
        context = self._context.sample(now)
        preferences = self._preferences
        if now >= self._idle_at:
            # 虚拟注意目标有停留和回看；不移动系统鼠标，也不逐帧注入噪声。
            # 纵向转移形成轻微低头/抬头，避免无语境地反复点头表示同意。
            self._idle_gaze = (
                (self._random.uniform(-0.8, 0.8), self._random.uniform(-0.48, 0.4))
                if self._random.random() < 0.72
                else (0.0, 0.0)
            )
            self._idle_goal = (self._random.uniform(-0.08, 0.08), self._random.uniform(-0.12, 0.12))
            self._idle_at = now + self._idle_interval()
        idle_scale = style.motion_scale * style.idle_motion_scale * float(preferences.idle_enabled)
        idle_free = not self._context.has_focus(now) and not any(
            name in self._targets for name in ("gaze_x", "gaze_y")
        )
        baseline = {
            key: context[key] for key in ("valence", "arousal", "tension", "gaze_x", "gaze_y")
        }
        if idle_free:
            for index, name in enumerate(("gaze_x", "gaze_y")):
                baseline[name] += self._idle_gaze[index] * idle_scale * preferences.idle_gaze_scale
        targets = {
            key: self._targets[key].value if key in self._targets else value
            for key, value in baseline.items()
        }
        for name in ("valence", "arousal", "tension"):
            self._values[name] += (targets[name] - self._values[name]) * (-math.expm1(-_DT / 0.3))

        desired = tuple(
            max(-1.0, min(1.0, targets[name] + getattr(preferences, name)))
            for name in ("gaze_x", "gaze_y")
        )
        distance = math.hypot(desired[0] - self._gaze[0], desired[1] - self._gaze[1])
        # 小变动保持当前目标；显著变化可打断最短注视。期限失效必须允许回正。
        returning = desired == (0.0, 0.0) or gaze_expired
        if distance > 1e-6 and (
            returning
            or distance >= 0.65
            or (distance >= 0.025 and now - self._gaze_since >= style.gaze_hold_seconds)
        ):
            # 注视向量采用最近一次实际改变坐标的请求力度；情绪或单独力度更新
            # 不重定时。到期/撤销后的回正采用柔和基线，不延续旧强调力度。
            gaze_targets = [
                self._targets[key]
                for key, old, new in zip(("gaze_x", "gaze_y"), self._gaze, desired, strict=True)
                if old != new and key in self._targets
            ]
            latest = max(gaze_targets, key=lambda target: target.submitted, default=None)
            self._gaze_effort = (
                style.default_effort if latest is None or gaze_expired else latest.effort
            )
            self._gaze, self._gaze_since = desired, now
        age = now - self._gaze_since
        if age >= style.head_delay_seconds:
            head_goal = tuple(value * 0.3 for value in self._gaze)
            if head_goal != self._head_goal:
                self._head_goal = head_goal
                self._head_duration = (
                    style.relaxed_head_turn_seconds * (1 - self._gaze_effort)
                    + style.head_turn_seconds * self._gaze_effort
                )
        if age >= style.body_delay_seconds:
            # 仅持续的偏侧注视带动躯干；死区边缘连续，避免轻微扫视触发全身摇摆。
            self._body_goal = tuple(
                math.copysign(max(0.0, abs(value) - 0.20) / 0.80, value) * style.body_gaze_scale
                for value in self._gaze
            )

        gesture = self._gesture_targets(now)
        idle_scale *= preferences.idle_posture_scale
        if self._gesture is not None:
            idle_scale *= 0.25
        if not idle_free:
            idle_scale *= 0.2
        body_targets = (
            self._body_goal[0] + self._idle_goal[0] * idle_scale,
            self._body_goal[1],
            self._body_goal[0] * 0.25 + self._idle_goal[1] * idle_scale,
        )
        head_targets = (self._head_goal[0], self._head_goal[1], self._idle_goal[1] * idle_scale)
        for index, axis in enumerate(("x", "y", "z")):
            body_name = "body_" + axis
            self._values[body_name] = self._axes[body_name].advance(
                body_targets[index] + getattr(preferences, body_name), style.body_response_seconds
            )
            # 主动转头按本次力度到位，待机偏斜较慢。手势已拥有完整的阶段节奏，
            # 必须直接合成，不能再次送进头部弹簧把发力与刹停磨平。
            stroke = self._head_axes[index]
            stroke.aim(
                head_targets[index] + getattr(preferences, "head_" + axis),
                self._head_duration if index < 2 else 0.9,
            )
            self._values["head_" + axis] = max(-1.0, min(1.0, stroke.advance() + gesture[index]))

        # 补偿使用已叠加手势的最终头身姿态，否则点头也会把注视目标拖走。
        calibration = self._calibration
        for axis, head_gain, body_gain in (
            ("x", calibration.head_x, calibration.body_x),
            ("y", calibration.head_y, calibration.body_y),
        ):
            index = 0 if axis == "x" else 1
            name = "gaze_" + axis
            fixation = self._axes[name].advance(self._gaze[index], 0.035)
            # 注意转移平滑，头动补偿直接作用于本帧。把补偿再平滑会在快速点头时
            # 引入额外相位延迟，使角色短暂看离原目标。
            compensated = fixation - head_gain * self._values["head_" + axis]
            compensated -= body_gain * self._values["body_" + axis]
            self._values[name] = max(-1.0, min(1.0, compensated))

        # 交流姿态与情绪保持不同所有权。结束后平滑撤回交流层，独立微笑仍保留。
        expression = self._speech_expression
        if expression is not None and expression.expires <= now:
            self._speech_expression = expression = None
        expression_goal = _SPEECH_EXPRESSIONS[expression.delivery if expression else "neutral"]
        response = 0.20 if expression is not None else 0.45
        for index, target in enumerate(expression_goal):
            target *= style.speech_expression_scale
            self._expression_values[index] += (target - self._expression_values[index]) * (
                -math.expm1(-_DT / response)
            )
        brow_l, brow_r, eyelid, smile, mouth_form = self._expression_values
        # 显式情绪拥有完整面部倾向，不叠加被遮挡语境的额外眉眼。
        # 语境计时仍继续，显式姿态结束后只能露出尚有效的状态或自然衰减尾部。
        context_weight = float(
            not any(name in self._targets for name in ("valence", "arousal", "tension"))
        )
        self._context_weight += (context_weight - self._context_weight) * (-math.expm1(-_DT / 0.2))
        brow_l += context["context_brow_l"] * self._context_weight
        brow_r += context["context_brow_r"] * self._context_weight
        eyelid += context["context_lid"] * self._context_weight

        # 眨眼最后乘在合成表情上，睁眼后恢复当前情绪与交流姿态。
        closure = self._blink_closure(now)
        valence, tension = self._values["valence"], self._values["tension"]
        eye_base = max(0.0, min(1.0, preferences.eye_open - 0.12 * max(0.0, valence) + eyelid))
        self._values["eye_open"] = self._values["eye_l"] = self._values["eye_r"] = eye_base * (
            1 - closure
        )
        self._values["brow_l"] = max(-1.0, min(1.0, -0.3 * tension + brow_l))
        self._values["brow_r"] = max(-1.0, min(1.0, -0.3 * tension + brow_r))
        self._values["smile"] = max(0.0, min(1.0, valence * 0.4 + smile))
        self._values["mouth_form"] = max(
            -1.0,
            min(
                1.0,
                (valence * 0.2 + mouth_form) * preferences.mouth_form_gain + preferences.mouth_form,
            ),
        )
        self._values["breath"] = (1 - math.cos((now - self._origin) * 1.7)) * 0.05

    def _gesture_targets(self, now: float) -> tuple[float, float, float]:
        """显式动作走单调时间，语音动作只消耗已确认的媒体进度；取消后本地收势。"""
        gesture = self._gesture
        if gesture is not None:
            age = (self._speech_time if gesture.automatic else now) - gesture.started
            stalled = gesture.automatic and age - self._speech_advanced > 0.25
            if now >= gesture.expires or age >= gesture.stages[-1][0] or stalled:
                self._gesture = gesture = None
            elif gesture.automatic:
                # 单次最多推进 250 ms。媒体时钟跳跃时收势，不快放历史动作；
                # 正常进度用同一 120 Hz 子步跨越阶段，暂停时不继续推进轨迹。
                while self._speech_advanced + _DT <= age + 1e-8:
                    self._speech_advanced += _DT
                    self._aim_gesture_stage(gesture, self._speech_advanced)
                    for stroke in self._gesture_axes:
                        stroke.advance()
                x, y, z = self._gesture_axes
                return x.position, y.position, z.position
            else:
                self._aim_gesture_stage(gesture, age)
        if gesture is None:
            for stroke in self._gesture_axes:
                stroke.aim(0.0, 0.40)
        x, y, z = self._gesture_axes
        return x.advance(), y.advance(), z.advance()

    def _aim_gesture_stage(self, gesture: _Gesture, age: float) -> None:
        """仅在进入新阶段时重定向；语音幅度在此合成，不能再叠加另一套头部振荡。"""
        stage = next(index for index, (end, _) in enumerate(gesture.stages) if age < end)
        if stage == self._gesture_stage:
            return
        self._gesture_stage = stage
        end, value = gesture.stages[stage]
        active_axis = {"shake": 0, "nod": 1, "tilt": 2}[gesture.kind]
        for index, stroke in enumerate(self._gesture_axes):
            target = (
                value * self._style.motion_scale * gesture.scale if index == active_axis else 0.0
            )
            stroke.aim(target, max(_DT, end - age))

    def _blink_closure(self, now: float) -> float:
        """闭合、保持、睁开使用不同阶段；从本次结束开始抽下一次间隔。"""
        if self._blink_started is None:
            if now < self._blink_at:
                return 0.0
            self._blink_started = now
            self._last_blink = now
        age = now - self._blink_started
        if age < 0.075:
            return _ease(age / 0.075)
        if age < 0.11:
            return 1.0
        if age < 0.26:
            return 1 - _ease((age - 0.11) / 0.15)
        self._blink_started = None
        self._blink_at = now + self._blink_interval()
        return 0.0
