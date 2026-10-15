"""定义领域协作契约与不可变设置；调用与所有权由应用显式连接。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from purrcept_engine.domain.context.materials import MediaStore
from purrcept_engine.domain.execution.models import ActionRecord
from purrcept_engine.domain.inputs.models import InputRecord
from purrcept_engine.domain.model_requests.models import RequestRecord
from purrcept_engine.domain.model_requests.ports import RequestEvidenceSink
from purrcept_engine.domain.subject.models import (
    AttentionLevel,
    InputProcessing,
    InputStage,
    MinimumWakeAssessment,
    SessionScope,
    TurnRecord,
)
from purrcept_engine.domain.subject.session_ports import SessionStore


class CoordinatorStore(SessionStore, RequestEvidenceSink, MediaStore, Protocol):
    """Store 单连接提供的有限调度口，写确认后才采用选择或交接。

    app 在相同事件循环借出此端口。stage 查询不取得处理资格；decide_inputs
    按完整 InputProcessing 版本 CAS，begin_turn 再接管前台身份。change_revision
    只用于不漏通知的等待，不能替代数据库状态；取消观察不撤销已接管事务。
    """

    @property
    def change_revision(self) -> int:
        """返回已完成写入通知的单调序号，调用方先取序号再查询。"""
        ...

    async def wait_for_change(self, after_revision: int) -> int:
        """等待新事务通知，数据库关闭按 Store 契约结束等待。"""
        ...

    async def get_input(self, input_id: str) -> InputRecord | None:
        """读取完整接管事实，不存在的已关联身份属于权威损坏。"""
        ...

    async def input_processing(self, input_id: str) -> InputProcessing | None:
        """取得注意及前台处置的当前版本，不产生新交接。"""
        ...

    async def staged_inputs(self, stage: InputStage, *, limit: int = 8) -> tuple[InputRecord, ...]:
        """按接管顺序加载有限批次，不在内存建立全积压副本。"""
        ...

    async def take_foreground_inputs(self, turn_id: str | None = None) -> tuple[InputRecord, ...]:
        """取尽截至本事务的新刺激，活动轮次同时持久接管；不包含明确延期。"""
        ...

    async def decide_inputs(
        self,
        expected: tuple[InputProcessing, ...],
        level: AttentionLevel,
        reason: str,
        *,
        request_id: str | None = None,
        defer_until: float | None = None,
    ) -> bool:
        """同事务采用整批结果与延期；过时版本返回 False 而不重新投递。"""
        ...

    async def register_minimum_wake_assessment(
        self,
        expected: InputProcessing,
        *,
        owner_id: str,
        observed_generation: int,
    ) -> bool:
        """CAS 登记直接唤起的旧代次观察；同一输入只取得一次升级资格。"""
        ...

    async def minimum_wake_assessments(
        self, owner_id: str, *, limit: int = 8
    ) -> tuple[MinimumWakeAssessment, ...]:
        """查询本宿主的有限升级待办，不重建前台投递或猜测重启前代次。"""
        ...

    async def finish_minimum_wake_assessment(
        self,
        assessment: MinimumWakeAssessment,
        level: AttentionLevel,
        reason: str,
        *,
        request_id: str | None = None,
    ) -> bool:
        """结案判断并核对原前台版本，任何结果都不能撤销最低 WAKE。"""
        ...

    async def next_deferred_input(self) -> float | None:
        """返回最早未交接期限，单一定时等待服务全部延后身份。"""
        ...

    async def deliver_due_inputs(self, now: float) -> tuple[str, ...]:
        """原身份到期直投一次，不重新进入注意模型。"""
        ...

    async def requests_for_turn(self, owner_id: str, turn_id: str) -> tuple[RequestRecord, ...]:
        """关联实际逻辑请求及材料，不能自造注意成功请求身份。"""
        ...

    async def failed_turns(self, scope: SessionScope) -> tuple[TurnRecord, ...]:
        """查询尚未由新成功交互承接的失败事实，不自行重试其输入。"""
        ...

    async def actions_for_inputs(self, input_ids: tuple[str, ...]) -> tuple[ActionRecord, ...]:
        """读取已持久因果关联的真实行动和调用，不从文字或 ID 前缀猜归属。"""
        ...


class ActionCallback(Protocol):
    """app 绑定的唯一行动端口，仍由执行服务拥有实际资源和回执。

    只有本轮资格有效时可以开始新脚本。观察取消不能撤销已经开始的行动，
    实际 Provider 的未知或迟到结果由执行器独立保存，不写回临时 Core 历史。
    """

    def __call__(
        self,
        source: str,
        *,
        input_id: str | None,
        can_start: Callable[[], bool],
        presented_call_ids: frozenset[str],
    ) -> Awaitable[ActionRecord]:
        """接管受限脚本，不在宿主 exec 或直接调用插件实现。"""
        ...
