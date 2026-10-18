"""主动思考的运行期开关与空闲期限，不拥有第二个调度循环或模型。

SubjectService 创建本对象，唯一刺激协调者在等待前调用 tick。真实新输入按同库
接管游标复位；忙碌时不积累空闲刺激。页面切换与生成接管串行，关闭不撤回已提交
事实。重启只采用配置默认值，从启动重新计时，不补发停机期间错过的周期。
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
        self.deadline: float | None = None
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
                self.deadline = None
                self.sequence = None
                self.blocked = False
                self.last_error = None
            self.changed.set()

    async def tick(self, *, busy: bool) -> float | None:
        """协调者在等待前检查是否到期，返回下一次检查的剩余秒数。

        Args:
            busy: 本协调者是否仍持有前台或注意调用，包括尚未结束的准备阶段。

        Returns:
            正间隔或已接管后的0；关闭及忙碌时为 None，等待真实状态/控制变化。

        Raises:
            StorageError: 除正式接管容量不足以外的权威故障保持原异常。

        Note:
            输入容量不足时记录 last_error，等待完整间隔再尝试新的周期，不删输入、
            不放宽额度、不热循环。原观察冲突同样重新计时，不补发多个错过的周期。
        """
        async with self.lock:
            if not self.enabled:
                return None
            self.runtime.check()
            sequence, pending = await self.runtime.store.idle_input_state()
            now = asyncio.get_running_loop().time()
            blocked = busy or pending
            if self.deadline is None or sequence != self.sequence or blocked or self.blocked:
                self.deadline = now + self.seconds
            self.sequence, self.blocked = sequence, blocked
            if blocked:
                return None
            remaining = self.deadline - now
            if remaining > 0:
                return remaining

            occurred = datetime.now(UTC)
            submission = InputSubmission(
                InputSource("subject.idle", "subject", "self", "subject.idle", uuid4().hex),
                IDLE_STIMULUS_PROMPT.render({"seconds": self.seconds}),
                occurred,
                occurred,
                context=InputContext(minimum_wake=True),
            )
            self.runtime.check()
            try:
                admission = await self.runtime.commit(
                    self.runtime.store.admit_idle_input(submission, expected_sequence=sequence)
                )
            except StorageError as error:
                if error.code != "input_capacity":
                    raise
                self.last_error = error.code
                self.deadline = asyncio.get_running_loop().time() + self.seconds
                return self.seconds
            self.last_error = None
            self.deadline = asyncio.get_running_loop().time() + self.seconds
            if admission is not None:
                self.last_idle_at = time.time()
                return 0.0
            return self.seconds
