"""把专业驱动的步骤、确认和反馈限定到同一个活动所有者。

ActivityService 创建 ActivityPort；驱动只能借出与当前活动关联的有限步骤，
不能直接改控制状态、增加额度或取得数据库。取消观察不撤销已经开始的真实
能力调用，ActivityStep 的结束只归还本次步骤资格。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from purrcept_engine.domain.activities.models import (
    ActivityCommand,
    ActivityFeedback,
    ActivityRecord,
    ActivityState,
    FeedbackKind,
)
from purrcept_engine.domain.execution.models import ActionRecord
from purrcept_engine.values import JsonObject

if TYPE_CHECKING:
    from purrcept_engine.domain.activities.service import ActivityService


@dataclass(frozen=True, slots=True)
class ActivityStep:
    """宿主执行服务取得的步骤身份，直到真实收尾才归还并发事实。

    此值只用于本服务所属循环。finish 是幂等的等待，取消外部等待不会把仍在途
    的步骤当作已结束；宿主执行所有者须在取得真实结果之后调用它。
    """

    activity_id: str
    run_generation: int
    step_id: str
    _service: ActivityService

    async def finish(self) -> None:
        """报告宿主实际步骤已结束，不改变其独立回执。"""
        await self._service.finish_step(self)


class ActivityPort:
    """宿主为单项活动创建的驱动端口，没有通用状态或兄弟活动写权限。

    驱动取得当前快照后携命令身份、运行代次和观察版本提交事实。控制状态仅由
    服务核验后提交。checkpoint 保存专业工作，不把局部产物提升为长期认识。
    """

    def __init__(self, service: ActivityService, activity_id: str) -> None:
        """绑定一项不可变活动身份，不启动驱动或建立后台任务。"""
        self._activity_id = activity_id
        self._service = service

    @property
    def activity_id(self) -> str:
        """返回创建时固定的委托身份，公开端口不能改挂到兄弟活动。"""
        return self._activity_id

    async def snapshot(self) -> ActivityRecord:
        """读取本活动的当前权威快照，不产生恢复或控制副作用。"""
        return await self._service.activity(self.activity_id)

    async def begin_step(self, *, run_generation: int) -> ActivityStep:
        """在本活动进入专业步骤前复核资格，并取得直到真实收尾才归还的身份。

        端口固定活动身份，不能借此为兄弟活动派发。驱动保存真实结果后调用
        ActivityStep.finish；取消观察或停止请求不代替实际步骤结束。
        """
        return await self._service.begin_step(self.activity_id, run_generation)

    async def invoke(
        self,
        path: str,
        arguments: JsonObject,
        *,
        run_generation: int,
        timeout_seconds: float | None = None,
    ) -> ActionRecord:
        """本活动经真实执行服务调用能力，首版 unknown 不归还仍在途步骤。

        驱动可在观察超时后停止并报告 needs_decision；实际所有者仍保存迟到
        回执。观察取消不撤销已接管调用，不能借此直接运行 Provider 或脚本。
        """
        return await self._service.invoke(
            self.activity_id,
            path,
            arguments,
            run_generation=run_generation,
            timeout_seconds=timeout_seconds,
        )

    async def confirm(
        self,
        command_id: str,
        *,
        run_generation: int,
        version: int,
        state: ActivityState,
    ) -> ActivityCommand:
        """提交实际控制结果；version 是原命令版本，迟到结果仍保留但不复活。"""
        return await self._service.confirm(
            self.activity_id,
            command_id,
            run_generation=run_generation,
            version=version,
            state=state,
        )

    async def checkpoint(
        self, *, run_generation: int, version: int, work_json: str
    ) -> ActivityRecord:
        """按当前观察版本保存局部工作，旧代次或旧版本不能覆盖新工作。"""
        return await self._service.checkpoint_work(
            self.activity_id,
            run_generation=run_generation,
            version=version,
            work_json=work_json,
        )

    async def feedback(
        self,
        feedback_id: str,
        kind: FeedbackKind,
        payload_json: str,
        *,
        run_generation: int,
        version: int,
        command_id: str | None = None,
    ) -> ActivityFeedback:
        """原始反馈可靠保存；旧版本进展不覆盖当前状态，关键事实仍交付。"""
        return await self._service.feedback(
            self.activity_id,
            feedback_id,
            kind,
            payload_json,
            run_generation=run_generation,
            version=version,
            command_id=command_id,
        )
