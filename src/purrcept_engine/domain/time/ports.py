"""插件借用的有限业务端口；实现必须与宿主权威存储共享寿命。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

import purrcept_engine.domain.time.models as time_models


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
