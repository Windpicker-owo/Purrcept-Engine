"""时钟/休息的有限异步门面，全部短事务提交到Store唯一队列。

插件只借用TimePort，不取得SQLite或任意事务回调。TimeLedger由Store创建，
同库admit函数只在数据线程的闹铃事务内使用。写入遵循已接管取消不撤销规则，
当前定义列表最多32条；没有第二个连接、消费者或常驻待投递队列。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol

import purrcept_engine.domain.time.models as time_models
from purrcept_engine.infrastructure.sqlite import time as sql
from purrcept_engine.serialization import encode


class _Submit(Protocol):
    """Store注入的有界同线程执行接缝，连接只交给本模块创建的短事务。"""

    def __call__[Result](
        self,
        operation: Callable[[sqlite3.Connection], Result],
        *,
        logical_bytes: int = 0,
        control: bool = False,
        writing: bool = False,
    ) -> Awaitable[Result]:
        """接管实际计量材料，不把排队确认当成已完成提交。"""
        ...


class TimePort(Protocol):
    """宿主提供的时间业务端口，不含模型、媒体或通用数据库访问。

    app创建TimeLedger并借给时钟和床插件；定义按唯一插件实例clock_id隔离，
    休息属于唯一主体。每次写入返回才意味着事务完成；取消等待不撤销已接受
    事务。业务TimeError可纠正，权威SQLite和内部异常原样传出。
    """

    async def alarms(self, clock_id: str) -> tuple[time_models.Alarm, ...]:
        """读取当前最多32条待响定义。"""
        ...

    async def set_alarm(self, alarm: time_models.Alarm, now: datetime) -> time_models.Alarm:
        """保存一个未来正式闹铃及持久发生身份。"""
        ...

    async def cancel_alarm(self, clock_id: str, alarm_id: str) -> time_models.Alarm:
        """取消当前尚未消耗的定义，不删除发生历史。"""
        ...

    async def recover_daily(self, clock_id: str, now: datetime) -> None:
        """重启每日仅推进下一次，单次逾期仍待补投。"""
        ...

    async def fire_alarm(
        self, clock_id: str, alarm_id: str, occurrence_id: str, now: datetime
    ) -> time_models.AlarmFiring | None:
        """同事务接管一次到期原文并消耗/推进定义，过期候选返回None。"""
        ...

    async def get_rest(self) -> time_models.RestState | None:
        """读取明确休息状态，到预计期限不自动醒来。"""
        ...

    async def rest(
        self, bed_id: str, minutes: int, now: datetime, call_id: str
    ) -> time_models.RestChange:
        """以实际调用身份提交休息开始或打算时长更新。"""
        ...

    async def leave(self, now: datetime, call_id: str) -> time_models.RestChange:
        """明确结束休息；醒着不创建新的状态变化。"""
        ...


class TimeLedger:
    """Store拥有的异步门面，与母Store同事件循环和同资源寿命。"""

    def __init__(self, submit: _Submit, admit: sql.AdmitInput) -> None:
        """借用唯一队列与原文接管，不能自行创建SQLite连接。"""
        self._submit, self._admit = submit, admit

    async def alarms(self, clock_id: str) -> tuple[time_models.Alarm, ...]:
        """只读正式定义，查询不产生新输入。"""
        return await self._submit(
            lambda connection: sql.alarms(connection, clock_id),
            logical_bytes=len(clock_id.encode()),
        )

    async def set_alarm(self, alarm: time_models.Alarm, now: datetime) -> time_models.Alarm:
        """实际提交后返回，完整定义UTF8计入同一控制提交额度。"""
        return await self._submit(
            lambda connection: sql.set_alarm(connection, alarm, now),
            logical_bytes=len(encode((alarm.model_dump(mode="json"), now.isoformat())).encode()),
            control=True,
            writing=True,
        )

    async def cancel_alarm(self, clock_id: str, alarm_id: str) -> time_models.Alarm:
        """保存明确取消，不从模型文字推断闹铃已响。"""
        return await self._submit(
            lambda connection: sql.cancel_alarm(connection, clock_id, alarm_id),
            logical_bytes=len(encode((clock_id, alarm_id)).encode()),
            control=True,
            writing=True,
        )

    async def recover_daily(self, clock_id: str, now: datetime) -> None:
        """启动恢复同一持久定义，不补发每日离线期间的世界效果。"""
        await self._submit(
            lambda connection: sql.recover_daily(connection, clock_id, now),
            logical_bytes=len(encode((clock_id, now.isoformat())).encode()),
            control=True,
            writing=True,
        )

    async def fire_alarm(
        self, clock_id: str, alarm_id: str, occurrence_id: str, now: datetime
    ) -> time_models.AlarmFiring | None:
        """接管输入/消耗和回执共享同一事务，输入全局额度仍由admit核验。"""
        return await self._submit(
            lambda connection: sql.fire_alarm(
                connection, clock_id, alarm_id, occurrence_id, now, self._admit
            ),
            logical_bytes=len(
                encode((clock_id, alarm_id, occurrence_id, now.isoformat())).encode()
            ),
            control=True,
            writing=True,
        )

    async def get_rest(self) -> time_models.RestState | None:
        """读取唯一主体状态，不复制第二份可写实体组件。"""
        return await self._submit(sql.get_rest)

    async def rest(
        self, bed_id: str, minutes: int, now: datetime, call_id: str
    ) -> time_models.RestChange:
        """保存开始或更新及独立实际回执，已休息的开始时间保持。"""
        return await self._submit(
            lambda connection: sql.rest(connection, bed_id, minutes, now, call_id),
            logical_bytes=len(encode((bed_id, minutes, now.isoformat(), call_id)).encode()),
            control=True,
            writing=True,
        )

    async def leave(self, now: datetime, call_id: str) -> time_models.RestChange:
        """明确起身或实际无副作用结果。"""
        return await self._submit(
            lambda connection: sql.leave(connection, now, call_id),
            logical_bytes=len(encode((now.isoformat(), call_id)).encode()),
            control=True,
            writing=True,
        )
