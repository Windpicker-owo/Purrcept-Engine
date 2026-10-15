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
        """接管实际计量材料，不把排队确认当成已完成提交。

        Args:
            operation: 在专属数据库线程执行的同步短事务，返回值交回原事件循环。
            logical_bytes: 本次操作占用的逻辑载荷字节数，用于接管前背压。 默认 0。
            control: 是否使用独立控制名额，避免普通输入阻塞真实回执收尾。 默认 False。
            writing: 是否属于写操作，以便关闭或权威故障时撤销派发资格。 默认 False。

        Returns:
            当前所有者持有的工作观察入口，等待取消不等于实际资源已归还。
        """
        ...


class TimeLedger:
    """Store拥有的异步门面，与母Store同事件循环和同资源寿命。"""

    def __init__(self, submit: _Submit, admit: sql.AdmitInput) -> None:
        """借用唯一队列与原文接管，不能自行创建SQLite连接。

        Args:
            submit: 借用同一 Store 有界队列执行同步短事务的入口。
            admit: 在调用方当前事务中正式接管输入的同步入口，不另开事务。
        """
        self._submit, self._admit = submit, admit

    async def alarms(self, clock_id: str) -> tuple[time_models.Alarm, ...]:
        """只读正式定义，查询不产生新输入。

        Args:
            clock_id: 已装配时钟对象的身份。

        Returns:
            条目集合：当前已保存的闹铃定义与真实重复方式。
        """
        return await self._submit(
            lambda connection: sql.alarms(connection, clock_id),
            logical_bytes=len(clock_id.encode()),
        )

    async def set_alarm(self, alarm: time_models.Alarm, now: datetime) -> time_models.Alarm:
        """实际提交后返回，完整定义UTF8计入同一控制提交额度。

        Args:
            alarm: 带稳定身份、时区与重复方式的闹铃定义。
            now: 带明确时区的本次墙钟时刻。

        Returns:
            当前已保存的闹铃定义与真实重复方式。
        """
        return await self._submit(
            lambda connection: sql.set_alarm(connection, alarm, now),
            logical_bytes=len(encode((alarm.model_dump(mode="json"), now.isoformat())).encode()),
            control=True,
            writing=True,
        )

    async def cancel_alarm(self, clock_id: str, alarm_id: str) -> time_models.Alarm:
        """保存明确取消，不从模型文字推断闹铃已响。

        Args:
            clock_id: 已装配时钟对象的身份。
            alarm_id: 时钟下闹铃定义的身份。

        Returns:
            当前已保存的闹铃定义与真实重复方式。
        """
        return await self._submit(
            lambda connection: sql.cancel_alarm(connection, clock_id, alarm_id),
            logical_bytes=len(encode((clock_id, alarm_id)).encode()),
            control=True,
            writing=True,
        )

    async def recover_daily(self, clock_id: str, now: datetime) -> None:
        """启动恢复同一持久定义，不补发每日离线期间的世界效果。

        Args:
            clock_id: 已装配时钟对象的身份。
            now: 带明确时区的本次墙钟时刻。
        """
        await self._submit(
            lambda connection: sql.recover_daily(connection, clock_id, now),
            logical_bytes=len(encode((clock_id, now.isoformat())).encode()),
            control=True,
            writing=True,
        )

    async def fire_alarm(
        self, clock_id: str, alarm_id: str, occurrence_id: str, now: datetime
    ) -> time_models.AlarmFiring | None:
        """接管输入/消耗和回执共享同一事务，输入全局额度仍由admit核验。

        Args:
            clock_id: 已装配时钟对象的身份。
            alarm_id: 时钟下闹铃定义的身份。
            occurrence_id: 本次闹铃触发的稳定身份，用于同事务去重。
            now: 带明确时区的本次墙钟时刻。

        Returns:
            本次可靠触发及同事务正式接管的输入身份。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
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
        """读取唯一主体状态，不复制第二份可写实体组件。

        Returns:
            当前明确休息事实，预计期限不自动解除状态。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
        return await self._submit(sql.get_rest)

    async def rest(
        self, bed_id: str, minutes: int, now: datetime, call_id: str
    ) -> time_models.RestChange:
        """保存开始或更新及独立实际回执，已休息的开始时间保持。

        Args:
            bed_id: 提供本次休息状态的床对象身份。
            minutes: 明确休息的预计分钟数，到期不自动解除休息状态。
            now: 带明确时区的本次墙钟时刻。
            call_id: 实际能力调用的唯一身份，用于保存启动门槛及结果版本。

        Returns:
            此次明确休息或离开实际改变的状态。
        """
        return await self._submit(
            lambda connection: sql.rest(connection, bed_id, minutes, now, call_id),
            logical_bytes=len(encode((bed_id, minutes, now.isoformat(), call_id)).encode()),
            control=True,
            writing=True,
        )

    async def leave(self, now: datetime, call_id: str) -> time_models.RestChange:
        """明确起身或实际无副作用结果。

        Args:
            now: 带明确时区的本次墙钟时刻。
            call_id: 实际能力调用的唯一身份，用于保存启动门槛及结果版本。

        Returns:
            此次明确休息或离开实际改变的状态。
        """
        return await self._submit(
            lambda connection: sql.leave(connection, now, call_id),
            logical_bytes=len(encode((now.isoformat(), call_id)).encode()),
            control=True,
            writing=True,
        )
