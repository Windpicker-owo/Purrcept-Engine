"""内置时钟及一个有所有者的闹铃等待任务，不持有另一份可写闹铃状态。

factory纯声明me.clock三项能力，ClockRuntime在start恢复每日安排并启动唯一
timer。定义和正式发生由TimePort权威提交，输入接管与消耗同事务；私有缓存
仅是标有观察时间的展示快照。stop_inputs停止新timer工作并等待已受理交接，
close收回实际提交任务，不取消已经发生的事实或关闭宿主Store。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from purrcept_engine.domain.time.models import Alarm, TimeError
from purrcept_engine.errors import ConfigError, HostStateError, StorageError
from purrcept_engine.execution.catalog import CapabilityDeclaration, ObjectDeclaration
from purrcept_engine.execution.providers import (
    CallContext,
    CallOutcome,
    CapabilityInputError,
    FunctionCapability,
)
from purrcept_engine.infrastructure.sqlite.time import parse_alarm_at, resolve_timezone
from purrcept_engine.infrastructure.sqlite.time_port import TimePort
from purrcept_engine.plugin_system.contracts import (
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
    typed_factory,
)
from purrcept_engine.values import JsonObject

_MAX_SLEEP_SECONDS = 60.0


class ClockSettings(BaseModel):
    """内置时钟配置，只保留时区、可见路径和标签，不建立主人身份。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    timezone: str = "Asia/Shanghai"
    clock_id: str = Field(default="clock", pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    label: str = Field(default="世界时钟", min_length=1, pattern=r"\S")

    @field_validator("timezone")
    @classmethod
    def timezone_known(cls, value: str) -> str:
        """纯配置边界核验已安装IANA数据，不静默采用机器默认时区。"""
        try:
            resolve_timezone(value)
        except TimeError as error:
            raise ValueError(str(error)) from error
        return value


class _Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Empty(_Parameters):
    pass


class _SetAlarm(_Parameters):
    at: str = Field(
        min_length=1,
        pattern=r"\S",
        description="触发时间；可填HH:MM表示下次到达该墙钟时间，或填ISO日期时间。",
    )
    message: str = Field(default="", max_length=500, description="闹铃触发时随输入发送的留言。")
    repeat: Literal["once", "daily"] = Field(
        default="once", description="重复方式：once为单次，daily为每天重复。"
    )

    @field_validator("message", mode="before")
    @classmethod
    def trimmed_message(cls, value: object) -> object:
        """沿用先trim后限制500字符的真实边界，不把多余外侧空白计为正文。"""
        return value.strip() if isinstance(value, str) else value


class _CancelAlarm(_Parameters):
    alarm_id: str = Field(
        min_length=1,
        pattern=r"\S",
        description="要取消的alarm_id，必须来自read或set_alarm的返回值。",
    )


class ClockRuntime:
    """一个时钟实例的timer及有限提交所有者，TimePort仍拥有全部业务状态。

    now供宿主或隔离测试提供带时区墙钟，默认UTC系统时间；payload再按配置
    时区展示。存储拒收额度时保留待响定义并显示waiting_capacity，每次最长
    60秒重新核对墙钟或等定义变化，不制造隐藏待投队列/模型重试。
    """

    def __init__(
        self,
        clock_id: str,
        settings: ClockSettings,
        port: TimePort,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """借用权威端口，不在构造时创建任务或接管输入。"""
        self.clock_id, self.settings, self.port = clock_id, settings, port
        self.now = now or (lambda: datetime.now(UTC))
        self._wakeup = asyncio.Event()
        self._timer: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._commits: set[asyncio.Future[object]] = set()
        self._stopping = False
        self._closed = False
        self._failure: JsonObject | None = None
        self._cached: tuple[Alarm, ...] = ()
        self._observed_at: datetime | None = None

    async def _commit[Result](self, operation: Awaitable[Result]) -> Result:
        """等待取消不取消实际提交，close仍持有并收取所有已开始的数据库操作。"""
        task = asyncio.ensure_future(operation)
        owned = cast(asyncio.Future[object], task)
        self._commits.add(owned)
        owned.add_done_callback(self._commit_done)
        return await asyncio.shield(task)

    def _commit_done(self, task: asyncio.Future[object]) -> None:
        """收回无人观察提交的异常，故障仍由原调用方或timer按真实类别报告。"""
        self._commits.discard(task)
        if not task.cancelled():
            task.exception()

    def _check(self) -> None:
        if self._closed:
            raise HostStateError("时钟资源已关闭。")

    async def start(self) -> None:
        """完整宿主装配后恢复定义并启动唯一timer，重复start不再次恢复每日。"""
        self._check()
        if self._stopping:
            raise HostStateError("时钟已停止接入。")
        if self._timer is None:
            await self._commit(self.port.recover_daily(self.clock_id, self.now()))
            self._timer = asyncio.create_task(self._run(), name="clock:" + self.clock_id)
            self._timer.add_done_callback(self._timer_done)

    def _timer_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            self._failure = {"error_type": type(error).__name__, "state": "failed"}

    async def _refresh(self) -> tuple[Alarm, ...]:
        """展示只从已提交定义刷新，版本不会由此缓存反向写入。"""
        self._cached = await self.port.alarms(self.clock_id)
        self._observed_at = self.now()
        return self._cached

    async def read(self) -> JsonObject:
        """返回当前配置时区完整ISO时刻及实际待响安排。"""
        self._check()
        pending = await self._refresh()
        return {
            "iso": self.now().astimezone(resolve_timezone(self.settings.timezone)).isoformat(),
            "timezone": self.settings.timezone,
            "alarms": [alarm.to_json() for alarm in pending],
        }

    async def set_alarm(
        self, at: str, message: str, repeat: Literal["once", "daily"], call_id: str
    ) -> JsonObject:
        """保存未来安排后才叫醒timer；业务拒绝不吞掉权威错误。"""
        self._check()
        now = self.now()
        when = parse_alarm_at(at, timezone=self.settings.timezone, now=now)
        alarm = Alarm(
            id="alarm_" + uuid4().hex,
            clock_id=self.clock_id,
            at=when,
            timezone=self.settings.timezone,
            message=message,
            repeat=repeat,
            occurrence_id=str(uuid4()),
            call_id=call_id,
        )
        committed = await self._commit(self.port.set_alarm(alarm, now))
        self._wakeup.set()
        return committed.to_json()

    async def cancel_alarm(self, alarm_id: str) -> JsonObject:
        """明确取消当前安排，唤醒timer重新读权威表。"""
        self._check()
        committed = await self._commit(self.port.cancel_alarm(self.clock_id, alarm_id.strip()))
        self._wakeup.set()
        return {**committed.to_json(), "cancelled": True}

    async def _wait(self, seconds: float | None) -> None:
        """关闭及定义变化即时唤醒，墙钟检查最长60秒，睡眠不占数据库线程。"""
        if seconds is None:
            await self._wakeup.wait()
        else:
            try:
                await asyncio.wait_for(
                    self._wakeup.wait(), timeout=min(seconds, _MAX_SLEEP_SECONDS)
                )
            except TimeoutError:
                pass

    async def _run(self) -> None:
        """先清唤醒标记再读定义，避免并发set/cancel发生后丢掉重新核对机会。"""
        while not self._stopping:
            self._wakeup.clear()
            try:
                pending = await self._refresh()
                if self._stopping:
                    break
                if not pending:
                    await self._wait(None)
                    continue
                alarm = pending[0]
                now = self.now()
                remaining = (alarm.at - now).total_seconds()
                if remaining > 0:
                    await self._wait(remaining)
                    continue
                await self._commit(
                    self.port.fire_alarm(self.clock_id, alarm.id, alarm.occurrence_id, now)
                )
                self._failure = None
            except StorageError as error:
                if error.code not in ("input_capacity", "store_busy"):
                    raise
                # 拒收不会消耗闹铃。保留明确容量诊断，不把失败改成已投或立即忙转。
                self._failure = {
                    "error_type": type(error).__name__,
                    "code": error.code,
                    "state": "waiting_capacity",
                }
                await self._wait(_MAX_SLEEP_SECONDS)

    def snapshot(self) -> JsonObject:
        """返回带观察时刻的展示快照，不声称缓存是此刻全部正式状态。"""
        return {
            "timezone": self.settings.timezone,
            "state": "stopped" if self._stopping else "running",
            "observed_at": None if self._observed_at is None else self._observed_at.isoformat(),
            "alarms": [alarm.to_json() for alarm in self._cached],
            "failure": self._failure,
        }

    async def prepare_request(self) -> JsonObject:
        """每次真实请求异步刷新，当前时间/闹铃放动态材料而非固定目录。"""
        clock = await self.read()
        # read 已包含同一次刷新的完整闹铃，不再复制 snapshot 中的第二份安排。
        return {**clock, "state": self.snapshot()["state"], "failure": self._failure}

    async def stop_inputs(self) -> None:
        """停止新的timer交接并等待当前提交，取消观察不取消后台清理。"""
        self._stopping = True
        self._wakeup.set()
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop(), name="clock-stop:" + self.clock_id)
        await asyncio.shield(self._stop_task)

    async def _stop(self) -> None:
        if self._timer is not None:
            await self._timer

    async def close(self) -> None:
        """execution结束借用后收回资源；不关闭共享Store或抹掉未响安排。"""
        self._closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(), name="clock-close:" + self.clock_id
            )
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        """所有收尾都实际等待，多个失败一起报告，观察者取消不跳过后续阶段。"""
        failures: list[BaseException] = []
        try:
            await self.stop_inputs()
        except BaseException as error:
            # 资源关闭边界保留原失败并继续收回已受理提交，不能因timer错误遗漏所有权。
            failures.append(error)
        if self._commits:
            results = await asyncio.gather(*tuple(self._commits), return_exceptions=True)
            failures.extend(result for result in results if isinstance(result, BaseException))
        if failures:
            raise BaseExceptionGroup("时钟资源收尾失败。", failures)


def _declare(instance_id: str, settings: ClockSettings, base_dir: Path) -> PluginBlueprint:
    """声明稳定me路径和旧三能力，不打开设备、启动timer或建立身份映射。"""
    del base_dir
    object_id = instance_id + ":clock"
    path = "me." + settings.clock_id
    read = CapabilityDeclaration(
        instance_id + ":clock.read",
        object_id,
        "read",
        "获取当前时区的时间及待触发的闹铃列表。",
        _Empty,
        5.0,
        f"await {path}.read()",
        "用于查询当前具体时间、日期或确认已设置的闹铃安排。",
        concurrency="target_serial",
    )
    set_alarm = CapabilityDeclaration(
        instance_id + ":clock.set_alarm",
        object_id,
        "set_alarm",
        "设置一个定时闹铃，到达指定时间时提醒并附带留言。",
        _SetAlarm,
        5.0,
        f'await {path}.set_alarm(at="22:00", message="该睡觉了")',
        "需要在特定时间接收提醒时使用。HH:MM(:SS)取下一次；"
        "ISO完整时刻须未来；单次用once，每日用daily。",
        concurrency="target_serial",
    )
    cancel = CapabilityDeclaration(
        instance_id + ":clock.cancel_alarm",
        object_id,
        "cancel_alarm",
        "取消尚未触发的指定闹铃。",
        _CancelAlarm,
        5.0,
        f'await {path}.cancel_alarm(alarm_id="alarm_ab12")',
        "当不再需要特定提醒或设置有误时使用。已响完的单次闹铃无需重复取消。",
        concurrency="target_serial",
    )

    async def build(ports: PluginPorts) -> PluginRuntime:
        port = ports.time
        if port is None:
            raise ConfigError("clock插件需要宿主时间业务端口。")
        clock = ClockRuntime(instance_id, settings, port)

        async def run_read(parameters: _Empty, context: CallContext) -> CallOutcome:
            del parameters, context
            return CallOutcome("confirmed", await clock.read())

        async def run_set(parameters: _SetAlarm, context: CallContext) -> CallOutcome:
            try:
                result = await clock.set_alarm(
                    parameters.at, parameters.message, parameters.repeat, context.call_id
                )
            except TimeError as error:
                raise CapabilityInputError(str(error)) from error
            return CallOutcome("confirmed", result)

        async def run_cancel(parameters: _CancelAlarm, context: CallContext) -> CallOutcome:
            del context
            try:
                result = await clock.cancel_alarm(parameters.alarm_id)
            except TimeError as error:
                raise CapabilityInputError(str(error)) from error
            return CallOutcome("confirmed", result)

        return PluginRuntime(
            (
                FunctionCapability(read, run_read),
                FunctionCapability(set_alarm, run_set),
                FunctionCapability(cancel, run_cancel),
            ),
            start=clock.start,
            stop_inputs=clock.stop_inputs,
            close=clock.close,
            snapshot=clock.snapshot,
            prepare_request=clock.prepare_request,
        )

    return PluginBlueprint(
        (ObjectDeclaration(object_id, settings.clock_id, settings.label.strip(), "subject"),),
        (read, set_alarm, cancel),
        build,
        required_modules=() if settings.timezone in ("UTC", "GMT") else ("tzdata",),
    )


factory: PluginFactory = typed_factory(ClockSettings, _declare)
