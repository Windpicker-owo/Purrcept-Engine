"""从已调音 PCM 提取稀疏韵律候选，并按实际播放时钟选择表演。

SpeechPerformance 由 Live2DRuntime 独占；feed 可先于播放开始，只保存有界
候选，不触发动作。begin 绑定真实播放身份、语义和期限，sample 只交付当前
媒体时刻附近的一次机会。finish/reset 撤销材料，迟到帧不能复活旧发言。

分析仅使用 20 ms 单声道 s16le 的能量包络、相对上升和停顿，是保守的表演
启发式，不识别语言重音、音高或情绪。没有音频回压、网络、线程或原生资源；
口型仍归运行时，最终动作优先级和连续收势归 MotionController。
"""

from __future__ import annotations

import math
import struct
from collections import deque
from dataclasses import dataclass
from typing import Literal

from purrcept_engine.plugin_system.body import BodyIntent
from purrcept_engine.plugins.live2d.models import BodyStyle


@dataclass(frozen=True, slots=True)
class SpeechCue:
    """一次已到播放时刻的机会；接收方可以因显式动作占用而丢弃，不得排队补演。"""

    gesture: Literal["nod", "shake", "tilt"] | None
    effort: float
    scale: float
    pause: bool


@dataclass(frozen=True, slots=True)
class _Event:
    """只保存候选发生的媒体秒数，不保存原始声音或单调墙钟。"""

    at: float
    kind: Literal["accent", "pause"]


class SpeechPerformance:
    """在宿主事件循环串行使用的单发言分析器和调度器。

    feed 输入由 BodyPort 校验格式；同身份采样顺序递增，重复块被忽略，缺口
    重置包络以免跨缺口制造强调。至多保留 512 个候选，满时丢新候选而不
    挤掉尚未播放的开头。一次发言最多选择六个头部动作，其他机会自然略过。
    """

    def __init__(self, style: BodyStyle) -> None:
        """绑定本地动作密度与幅度，不读取外部时钟或拥有播放资源。

        Args:
            style: 发言动作间隔、幅度与缺省力度。
        """
        self._style = style
        self.reset()

    def reset(self) -> None:
        """丢弃连接之前的全部准备与采用状态；新连接不得复活旧发言。"""
        self._identity: str | None = None
        self._retired: str | None = None
        self._active = False
        self._events: deque[_Event] = deque()
        self._intent = BodyIntent()
        self._deadline = 0.0
        self._cursor = -1.0
        self._next_sample = 0
        self._selected = 0
        self._last_gesture = -math.inf
        self._dropped = 0
        self._reset_envelope()

    def _reset_envelope(self) -> None:
        """连续性中断后重新积累证据，不能把缺失音频当作停顿或能量突变。"""
        self._fast = self._baseline = 0.0
        self._voiced = self._quiet = 0.0
        self._last_accent = -math.inf

    def feed(self, playback: str, sample_rate: int, sample: int, pcm: bytes) -> bool:
        """登记一块已校验的 20 ms PCM，成功接管时返回 True。

        Args:
            playback: 宿主串行播放器身份，不能来自模型文本。
            sample_rate: 与身体校准一致的采样率。
            sample: 本块首样本在当前发言中的位置。
            pcm: 单声道小端 16 位采样，长度必须为 sample_rate/50 个样本。

        Returns:
            是否接管；旧身份、重复块或没有起点的新材料返回 False。
            调用只更新本地有限状态，不证明已经实际播放。
        """
        if playback == self._retired or (self._active and playback != self._identity):
            return False
        if playback != self._identity:
            if sample != 0:
                return False
            self._identity = playback
            self._events.clear()
            self._next_sample = 0
            self._reset_envelope()
        if sample < self._next_sample:
            return False
        gap = sample != self._next_sample
        if gap:
            self._reset_envelope()
        values = struct.unpack("<" + "h" * (len(pcm) // 2), pcm)
        energy = math.sqrt(sum(value * value for value in values) / len(values)) / 32768
        self._next_sample = sample + len(values)
        at = self._next_sample / sample_rate

        previous = self._fast
        if gap:
            self._fast = self._baseline = energy
        else:
            self._fast += (energy - self._fast) * (-math.expm1(-0.02 / 0.04))
        if energy >= 0.012:
            self._voiced += 0.02
            self._quiet = 0.0
            # 相对包络突出且仍在上升才是候选；恒定的大音量不会持续触发动作。
            if (
                self._voiced >= 0.12
                and self._fast > max(0.025, self._baseline * 1.65)
                and self._fast - previous > 0.003
                and at - self._last_accent >= 0.8
            ):
                self._enqueue(_Event(at, "accent"))
                self._last_accent = at
        else:
            self._quiet += 0.02
            if self._quiet >= 0.28:
                if self._voiced >= 0.18:
                    self._enqueue(_Event(at, "pause"))
                self._voiced = 0.0
        self._baseline += (energy - self._baseline) * (-math.expm1(-0.02 / 0.8))
        return True

    def _enqueue(self, event: _Event) -> None:
        """保留即将播放的早期候选；超过上限只丢视觉机会，不影响声音。"""
        if len(self._events) < 512:
            self._events.append(event)
        else:
            self._dropped += 1

    def begin(self, playback: str, intent: BodyIntent | None, deadline: float) -> None:
        """在实际播放开始后绑定语义；保留同身份已经准备的 PCM 候选。

        Args:
            playback: 当前真实播放身份。
            intent: 本次发言语义；缺省只提供停顿眨眼机会，不自动摆头。
            deadline: 宿主收紧后的单调截止时刻。
        """
        if playback != self._identity:
            self._events.clear()
            self._next_sample = 0
            self._reset_envelope()
        self._identity, self._active = playback, True
        self._intent, self._deadline = intent or BodyIntent(), deadline
        self._cursor = -1.0
        self._selected = 0
        self._last_gesture = -math.inf

    def sample(self, elapsed: float, now: float) -> SpeechCue | None:
        """取当前媒体时刻附近的一次机会；时钟不前进则不重复交付。

        Args:
            elapsed: 实际播放器秒数；不能由墙钟外推。
            now: 用于截止判定的单调时间。

        Returns:
            最多一项已消费的机会；超过 250 ms 的旧事件直接丢弃。
            手势选择仍需 MotionController 检查显式动作占用。
        """
        if not self._active or now >= self._deadline or elapsed <= self._cursor:
            return None
        self._cursor = elapsed
        latest: _Event | None = None
        while self._events and self._events[0].at <= elapsed:
            event = self._events.popleft()
            if elapsed - event.at <= 0.25:
                latest = event
        if latest is None:
            return None

        delivery = self._intent.delivery or "neutral"
        gesture: Literal["nod", "shake", "tilt"] | None = None
        if (
            self._selected < 6
            and elapsed - self._last_gesture >= self._style.speech_gesture_interval_seconds
            and self._intent.gesture is None
        ):
            if latest.kind == "pause" and delivery == "question":
                gesture = "tilt"
            elif latest.kind == "accent":
                if delivery in ("explain", "emphasize"):
                    gesture = "nod"
                elif delivery == "deny":
                    gesture = "shake"
        if gesture is not None:
            self._last_gesture = elapsed
            self._selected += 1
        effort = self._intent.effort
        return SpeechCue(
            gesture,
            self._style.default_effort if effort is None else effort,
            self._style.speech_gesture_scale * (0.7 if delivery == "explain" else 1.0),
            latest.kind == "pause",
        )

    def finish(self, playback: str) -> None:
        """撤销同身份材料与资格；旧结束不能清除后来发言的候选。

        Args:
            playback: 已结束或中断的实际播放身份。
        """
        if playback == self._identity:
            self._retired = playback
            self._identity, self._active = None, False
            self._events.clear()

    @property
    def deadline(self) -> float:
        """返回本次发言动作不得超过的宿主单调期限。"""
        return self._deadline

    @property
    def pending_events(self) -> int:
        """返回有界候选数，仅供诊断，不代表已经采用的动作。"""
        return len(self._events)

    @property
    def dropped_events(self) -> int:
        """返回连接期间因容量限制略过的候选数。"""
        return self._dropped
