"""定义主体调度所需的同库端口及唯一受限行动入口。

SubjectService 在同一事件循环借用这些端口，注意选择按原观察版本提交；
请求与真实效果独立入账。协议不提供 SQL、设备对象或通用事件订阅。
"""

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
        """等待新事务通知，数据库关闭按 Store 契约结束等待。

        Args:
            after_revision: 查询待办前取得的通知序号，避免查询与等待之间漏唤醒。
        """
        ...

    async def get_input(self, input_id: str) -> InputRecord | None:
        """读取完整接管事实，不存在的已关联身份属于权威损坏。

        Args:
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。

        Returns:
            首次接管的完整输入原文、来源与媒体引用。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
        ...

    async def input_processing(self, input_id: str) -> InputProcessing | None:
        """取得注意及前台处置的当前版本，不产生新交接。

        Args:
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。

        Returns:
            输入实际处理阶段、版本和关联轮次。 未取得对应记录或素材时返回 None；读取故障仍抛出。
        """
        ...

    async def staged_inputs(self, stage: InputStage, *, limit: int = 8) -> tuple[InputRecord, ...]:
        """查询指定阶段的待处理输入。

        Args:
            stage: 输入当前或待查询的处理阶段。
            limit: 本次读取或更新的最大条目数。 默认 8。

        Returns:
            条目集合：首次接管的完整输入原文、来源与媒体引用。
        """
        ...

    async def submit_inputs(
        self, expected: tuple[InputProcessing, ...], *, minimum_wake: bool
    ) -> bool:
        """按原版本原子提交整批输入；minimum_wake 同时登记持久唤醒意图。

        Args:
            expected: 尚未交接的 attention/priority 批次快照。
            minimum_wake: 必须与本批是否包含可信 priority 输入一致。

        Returns:
            是否采用整批；冲突时不产生部分提交。
        """
        ...

    async def take_foreground_inputs(self) -> tuple[InputRecord, ...]:
        """有持久唤醒请求时原子取尽已提交收件箱，后来输入留到下一轮。

        Returns:
            本轮独占的输入快照；未提交的缓存输入不在其中。
        """
        ...

    async def buffered_inputs(self) -> tuple[InputRecord, ...]:
        """读取当前全部未提交缓存，供高价值输入一次性合批。

        Returns:
            attention/priority 输入的接管顺序快照，上限由正式接管额度限定。

        Note:
            查询不接管输入；提交时仍须核对整批版本。
        """
        ...

    async def decide_inputs(
        self,
        expected: tuple[InputProcessing, ...],
        level: AttentionLevel,
        reason: str,
        *,
        request_id: str | None = None,
    ) -> bool:
        """按原批次版本采用注意结果；observe 保留输入但不唤醒。

        Args:
            expected: 已提交的整批版本。
            level: observe、wake 或 urgent。
            reason: 实际处置原因。
            request_id: 实际请求证据，程序处置为 None。

        Returns:
            是否整批采用；迟到结果不更改已取走输入。
        """
        ...

    async def register_minimum_wake_assessment(
        self, expected: tuple[InputProcessing, ...], *, owner_id: str, observed_generation: int
    ) -> bool:
        """给已保底唤醒的整批输入绑定一次旧生成升级观察。

        Args:
            expected: 本批提交后的完整 foreground 版本。
            owner_id: 当前宿主实例身份。
            observed_generation: 本批提交时正在处理的旧生成。

        Returns:
            是否首次登记；不会增加第二份主脑待办。
        """
        ...

    async def minimum_wake_assessments(
        self, owner_id: str, *, limit: int = 8
    ) -> tuple[MinimumWakeAssessment, ...]:
        """查询本宿主的有限批次升级待办。

        Args:
            owner_id: 当前宿主实例身份。
            limit: 最大批次数，默认八批。

        Returns:
            冻结的批次及原代次；重启不继承旧资格。
        """
        ...

    async def finish_minimum_wake_assessment(
        self,
        assessment: MinimumWakeAssessment,
        level: AttentionLevel,
        reason: str,
        *,
        request_id: str | None = None,
    ) -> bool:
        """结案判断并核对整批版本，任何结果都不能撤销最低唤醒。

        Args:
            assessment: 原批次和原生成观察。
            level: 模型实际选择。
            reason: 判断原因或过期说明。
            request_id: 实际请求证据，未调用时为 None。

        Returns:
            是否仍可采用到原收件箱批次，供协调者判断抢占资格。
        """
        ...

    async def requests_for_turn(self, owner_id: str, turn_id: str) -> tuple[RequestRecord, ...]:
        """关联实际逻辑请求及材料，不能自造注意成功请求身份。

        Args:
            owner_id: 发起本次工作并持有提交资格的所有者身份。
            turn_id: 连续会话中本轮处理的身份。

        Returns:
            条目集合：一个逻辑模型请求的冻结材料及按实际顺序保存的尝试。
        """
        ...

    async def failed_turns(self, scope: SessionScope) -> tuple[TurnRecord, ...]:
        """查询尚未由新成功交互承接的失败事实，不自行重试其输入。

        Args:
            scope: 目标连续会话，foreground 与 reflection 拥有独立历史。

        Returns:
            条目集合：本轮接管身份、来源输入、生成代次与当前结论。
        """
        ...

    async def actions_for_inputs(self, input_ids: tuple[str, ...]) -> tuple[ActionRecord, ...]:
        """读取已持久因果关联的真实行动和调用，不从文字或 ID 前缀猜归属。

        Args:
            input_ids: 本轮接管的输入身份集合，保留真实因果关联。

        Returns:
            条目集合：已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。
        """
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
        """接管受限脚本，不在宿主 exec 或直接调用插件实现。

        Args:
            source: 待执行的受限 Python 源码。
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。
            can_start: 实际派发前重新检查的资格回调，等待后仍须复核。
            presented_call_ids: 主体交付门槛确认已完整呈现的调用身份；空集合不授予恢复资格。

        Returns:
            当前所有者持有的工作观察入口，等待取消不等于实际资源已归还。
        """
        ...
