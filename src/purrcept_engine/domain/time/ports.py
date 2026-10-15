"""定义闹铃和休息状态的同库业务提交端口。

插件借用该端口保存定义、可靠触发与明确休息变更；触发输入须同事务接管。
协议不取得设备、不根据通知重放旧触发，也不拥有等待任务。
"""

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
        """读取当前最多32条待响定义。

        Args:
            clock_id: 已装配时钟对象的身份。

        Returns:
            条目集合：当前已保存的闹铃定义与真实重复方式。
        """
        ...

    async def set_alarm(self, alarm: time_models.Alarm, now: datetime) -> time_models.Alarm:
        """保存一个未来正式闹铃及持久发生身份。

        Args:
            alarm: 带稳定身份、时区与重复方式的闹铃定义。
            now: 带明确时区的本次墙钟时刻。

        Returns:
            当前已保存的闹铃定义与真实重复方式。
        """
        ...

    async def cancel_alarm(self, clock_id: str, alarm_id: str) -> time_models.Alarm:
        """取消当前尚未消耗的定义，不删除发生历史。

        Args:
            clock_id: 已装配时钟对象的身份。
            alarm_id: 时钟下闹铃定义的身份。

        Returns:
            当前已保存的闹铃定义与真实重复方式。
        """
        ...

    async def recover_daily(self, clock_id: str, now: datetime) -> None:
        """重启每日仅推进下一次，单次逾期仍待补投。

        Args:
            clock_id: 已装配时钟对象的身份。
            now: 带明确时区的本次墙钟时刻。
        """
        ...

    async def fire_alarm(
        self, clock_id: str, alarm_id: str, occurrence_id: str, now: datetime
    ) -> time_models.AlarmFiring | None:
        """同事务接管一次到期原文并消耗/推进定义，过期候选返回None。

        Args:
            clock_id: 已装配时钟对象的身份。
            alarm_id: 时钟下闹铃定义的身份。
            occurrence_id: 本次闹铃触发的稳定身份，用于同事务去重。
            now: 带明确时区的本次墙钟时刻。

        Returns:
            本次可靠触发及同事务正式接管的输入身份。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
        ...

    async def get_rest(self) -> time_models.RestState | None:
        """读取明确休息状态，到预计期限不自动醒来。

        Returns:
            当前明确休息事实，预计期限不自动解除状态。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
        ...

    async def rest(
        self, bed_id: str, minutes: int, now: datetime, call_id: str
    ) -> time_models.RestChange:
        """以实际调用身份提交休息开始或打算时长更新。

        Args:
            bed_id: 提供本次休息状态的床对象身份。
            minutes: 明确休息的预计分钟数，到期不自动解除休息状态。
            now: 带明确时区的本次墙钟时刻。
            call_id: 实际能力调用的唯一身份，用于保存启动门槛及结果版本。

        Returns:
            此次明确休息或离开实际改变的状态。
        """
        ...

    async def leave(self, now: datetime, call_id: str) -> time_models.RestChange:
        """明确结束休息。

        Args:
            now: 带明确时区的本次墙钟时刻。
            call_id: 实际能力调用的唯一身份，用于保存启动门槛及结果版本。

        Returns:
            此次明确休息或离开实际改变的状态。

        Note:
            醒着不创建新的状态变化。
        """
        ...
