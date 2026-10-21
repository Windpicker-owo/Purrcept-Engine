"""主动思考的运行期开关、静默期限与主脑唤醒期限，不拥有额外调度循环。

SubjectService 创建本对象，唯一刺激协调者在等待前调用 tick。真实新输入按同库
接管游标只复位静默期限；主脑实际取批才复位唤醒期限，注意处理不能推迟后者。
任一期限到达且主脑空闲时产生一次刺激；明确睡眠暂停两个期限，起床后重新计时。
页面切换与接管串行，重启从配置重新计时，不补发停机或忙碌期间错过的多个周期。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from purrcept_engine.domain.controls.models import ControlError
from purrcept_engine.domain.inputs.models import InputContext, InputSource, InputSubmission
from purrcept_engine.domain.subject.prompts import IDLE_STIMULUS_PROMPT
from purrcept_engine.errors import StorageError
from purrcept_engine.values import JsonObject

if TYPE_CHECKING:
    from purrcept_engine.domain.subject.service import SubjectService


class ProactiveThinking:
    """主体持有的运行期选项，所有调用限定在同一事件循环。

    配置给出默认开关及正间隔；控制只改变本次运行。changed 唤醒协调者，revision
    只随开关更改递增，不因页面轮询或时间流逝失效。tick 与 set_enabled 共用锁，
    禁用返回后不再接受新空闲刺激，已经接管的事实仍按正常队列处理。
    """

    def __init__(self, runtime: SubjectService) -> None:
        """借用主体的提交、资格与配置，不在构造时创建任务或读取数据库。

        Args:
            runtime: 创建并关闭本对象的主体所有者。
        """
        self.runtime = runtime
        self.enabled = runtime.settings.proactive_thinking
        self.seconds = runtime.settings.proactive_idle_seconds
        self.revision: int = 0
        self.changed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.quiet_deadline: float | None = None
        self.wake_deadline: float | None = None
        self.next_attempt: float | None = None
        self.sequence: int | None = None
        self.blocked = False
        self.last_idle_at: float | None = None
        self.last_error: str | None = None

    def snapshot(self) -> JsonObject:
        """返回有限运行状态；读取不推进计时、发刺激或改变控制版本。

        Returns:
            开关、控制版本、间隔及最近一次生成/背压事实，不泄露单调时钟原值。
        """
        return {
            "enabled": self.enabled,
            "revision": str(self.revision),
            "idle_seconds": self.seconds,
            "waiting_for_idle": self.blocked,
            "last_idle_at": self.last_idle_at,
            "last_error": self.last_error,
        }

    async def set_enabled(self, enabled: bool, *, expected_revision: str) -> None:
        """按页面观察版本修改开关，开启时从完整间隔重新计时。

        Args:
            enabled: 本次运行是否允许周期性空闲刺激。
            expected_revision: 页面最后看到的控制版本，等待后仍须复查。

        Raises:
            ControlError: 布尔参数无效或控制版本过期。
            HostStateError: 主体已开始关闭。
        """
        if not isinstance(enabled, bool):
            raise ControlError("主动思考开关必须是布尔值。")
        async with self.lock:
            self.runtime.check()
            if expected_revision != str(self.revision):
                raise ControlError("主动思考设置已变化，请刷新。", code="stale_revision")
            if self.enabled != enabled:
                self.enabled = enabled
                self.revision += 1
                self.quiet_deadline = None
                self.wake_deadline = None
                self.next_attempt = None
                self.sequence = None
                self.blocked = False
                self.last_error = None
            self.changed.set()

    def foreground_started(self) -> None:
        """协调者成功取出主脑快照后重置期限，工具请求和注意判断不调用此入口。

        Note:
            与 tick 在同一协调任务内串行调用，无 await。一次唤醒只重置一次，
            不因轮内多次模型请求、重试或注意处理而刷新主脑期限。
        """
        now = asyncio.get_running_loop().time()
        self.quiet_deadline = now + self.seconds
        self.wake_deadline = now + self.seconds
        self.next_attempt = None

    async def tick(
        self, *, foreground_busy: bool, attention_busy: bool, sleeping: bool = False
    ) -> float | None:
        """协调者在等待前检查是否到期，返回下一次检查的剩余秒数。

        Args:
            foreground_busy: 是否仍持有前台调用，包括尚未结束的准备阶段。
            attention_busy: 是否仍持有注意调用；只阻止静默触发，不推迟主脑期限。
            sleeping: 已提交的休息事实；暂停而不改开关，起床后重新等待完整间隔。

        Returns:
            最近期限的剩余秒数或已接管后的0；禁用及主脑忙碌时为 None。

        Raises:
            StorageError: 除正式接管容量不足以外的权威故障保持原异常。

        Note:
            输入容量不足时记录 last_error，等待完整间隔再尝试新的周期，不删输入、
            不放宽额度、不热循环。观察冲突同样等待完整间隔，但新输入和注意处理
            不能复位主脑期限。忙碌期间到期只合并为完成后的一次唤醒。
        """
        async with self.lock:
            if sleeping:
                self.quiet_deadline = None
                self.wake_deadline = None
                self.next_attempt = None
                self.sequence = None
                self.blocked = True
                return None
            if not self.enabled:
                return None
            self.runtime.check()
            sequence, pending = await self.runtime.store.idle_input_state()
            now = asyncio.get_running_loop().time()
            if self.wake_deadline is None:
                self.wake_deadline = now + self.seconds
            blocked = foreground_busy or attention_busy or pending
            if self.quiet_deadline is None or sequence != self.sequence or blocked or self.blocked:
                self.quiet_deadline = now + self.seconds
            self.sequence, self.blocked = sequence, blocked
            if foreground_busy:
                return None
            # 两个期限独立推进：新背景刺激和 observe 不能让主脑无限饥饿。
            # 接管失败的退避独立于这两个期限，避免已到期时反复撞满缓存。
            if self.next_attempt is not None and self.next_attempt > now:
                return self.next_attempt - now
            remaining = self.wake_deadline - now
            if not blocked:
                remaining = min(remaining, self.quiet_deadline - now)
            if remaining > 0:
                return remaining

            foreground_overdue = self.wake_deadline <= now
            occurred = datetime.now(UTC)
            submission = InputSubmission(
                InputSource("subject.idle", "subject", "self", "subject.idle", uuid4().hex),
                IDLE_STIMULUS_PROMPT.render(
                    {
                        "seconds": self.seconds,
                        "condition": "没有激活主脑"
                        if foreground_overdue
                        else "没有新的刺激且注意处理空闲",
                    }
                ),
                occurred,
                occurred,
                context=InputContext(minimum_wake=True),
            )
            self.runtime.check()
            try:
                admission = await self.runtime.commit(
                    self.runtime.store.admit_idle_input(
                        submission,
                        expected_sequence=sequence,
                        foreground_overdue=foreground_overdue,
                    )
                )
            except StorageError as error:
                if error.code != "input_capacity":
                    raise
                self.last_error = error.code
                self.next_attempt = asyncio.get_running_loop().time() + self.seconds
                return self.seconds
            self.last_error = None
            self.next_attempt = asyncio.get_running_loop().time() + self.seconds
            if admission is not None:
                self.last_idle_at = time.time()
                return 0.0
            return self.seconds
