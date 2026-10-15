"""Engine 的嵌入门面；从这里定位装配、输入、查询和关闭。

公开方法保留原接管与观察语义，生命周期交给 HostLifecycle，装配交给
EngineAssembly。具体模型循环、执行器和账本分别由下层所有者推进。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from uuid import uuid4

from purrcept_core.models import DecisionBackend, ModelBackend

from purrcept_engine.app.bootstrap import EngineAssembly
from purrcept_engine.app.config import EngineConfig
from purrcept_engine.app.host_state import EngineState
from purrcept_engine.app.host_types import ExecutorFactory, HostSnapshot
from purrcept_engine.app.ingress import InputAdmissionService
from purrcept_engine.app.lifecycle import HostLifecycle
from purrcept_engine.app.panel import ControlRequest
from purrcept_engine.app.queries import HostQueries
from purrcept_engine.domain.activities.models import ActivityCommand, ActivityRecord, CommandKind
from purrcept_engine.domain.context.capacity import CapacityRule
from purrcept_engine.domain.controls.models import ControlRecord
from purrcept_engine.domain.execution.catalog import Catalog
from purrcept_engine.domain.execution.models import ActionRecord
from purrcept_engine.domain.inputs.models import (
    Admission,
    InputRecord,
    InputSource,
    InputSubmission,
)
from purrcept_engine.domain.knowledge.ports import Reranker
from purrcept_engine.domain.knowledge.semantic import EmbeddingProvider
from purrcept_engine.domain.model_requests.models import RequestRecord
from purrcept_engine.domain.subject.models import InputProcessing
from purrcept_engine.errors import HostStateError
from purrcept_engine.plugin_system.contracts import PluginFactory
from purrcept_engine.plugin_system.panels import PreviewFrame
from purrcept_engine.values import JsonObject


class EngineHost:
    """由 CLI 或嵌入调用者创建并独占的宿主，所有方法在同一事件循环使用。

    start 和 close 都合并重复调用；关闭后不能重新启动同一个实例。submit
    只确认权威接管，query_input 读取其实际状态。输入等待超时不回滚接管。
    进程退出属于 CLI，库入口始终保留调用者对进程的处置权。
    """

    def __init__(
        self,
        config: EngineConfig,
        *,
        plugin_factories: Mapping[str, PluginFactory] | None = None,
        executor_factory: ExecutorFactory | None = None,
        console_output: Callable[[str], None] = print,
        generation_backends: Mapping[str, ModelBackend] | None = None,
        decision_backends: Mapping[str, DecisionBackend] | None = None,
        capacity_rules: Mapping[str, CapacityRule] | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        """保存配置并核验纯声明；具体资源与可替换执行器仅在 start 装配。

        plugin_factories 显式登记可信工厂，不触发自动扫描。executor_factory
        可替换平台执行器，但仍借用有限 ExecutionJournal 并满足 ActionExecutor
        的资格、真实效果与资源寿命契约，不能降级成宿主 exec。
        """
        self._state = EngineState(
            config,
            plugin_factories=plugin_factories,
            executor_factory=executor_factory,
            console_output=console_output,
            generation_backends=generation_backends,
            decision_backends=decision_backends,
            capacity_rules=capacity_rules,
            embedding_provider=embedding_provider,
            reranker=reranker,
        )
        self._lifecycle = HostLifecycle(self._state)
        self._inputs = InputAdmissionService(self._state, self._lifecycle)
        self._queries = HostQueries(self._state, self._lifecycle)
        self._assembly = EngineAssembly(self._state, self, self._lifecycle)
        self._lifecycle.bind_start(self._assembly.start)

    @property
    def config(self) -> EngineConfig:
        """返回本实例启动快照；运行期间不能替换它来改变资源和关闭边界。"""
        return self._state.config

    @property
    def catalog(self) -> Catalog:
        """返回同一冻结能力目录；查询不开始插件、网络或 Worker。"""
        return self._state.prepared.catalog

    async def start(self) -> HostSnapshot:
        """取得单实例与核心存储；失败清理自身资源，重复调用等待同次启动。"""
        return await self._lifecycle.start()

    async def submit(self, submission: InputSubmission) -> Admission:
        """接管原文与可信信号；取消等待不撤销提交后必要的抢占和停音交接。

        宿主在首次等待前持有完整接管所有者，不只持有SQLite事务。否则来源
        等待取消可能留下已接管VAD，却丢失停音通知。返回后仍须查询真实处理。
        """
        return await self._inputs.submit(submission)

    async def query_input(self, input_id: str) -> InputRecord | None:
        """查询已接管输入；关闭收尾期间仍可使用尚未释放的存储。"""
        return await self._queries.query_input(input_id)

    async def query_native_input(self, source: InputSource) -> InputRecord | None:
        """按原生来源读取首次接管事实，供重放恢复；不开始新的输入处理。"""
        return await self._queries.query_native_input(source)

    async def query_processing(self, input_id: str) -> InputProcessing | None:
        """读取宿主实际输入阶段，取得、接管及处理结束分别表达。"""
        return await self._queries.query_processing(input_id)

    async def execute(
        self,
        source: str,
        *,
        input_id: str | None = None,
        can_start: Callable[[], bool] | None = None,
        presented_call_ids: frozenset[str] = frozenset(),
    ) -> ActionRecord:
        """程序式执行明确行动，接管后的观察取消不撤销真实效果。

        主脑 play_action 使用同一入口和冻结目录，不另建平行执行路径。
        input_id 表示因果来源，不授予能力权限；can_start 撤销未派发生成。
        presented_call_ids 仅供主体交付门槛传入已呈现的完整回执身份，程序式
        默认调用没有此资格；它不会作为模型参数或Worker协议字段下发。
        """
        self._lifecycle.check_dispatch()
        execution = self._state.execution
        if execution is None:
            raise HostStateError("执行服务尚未装配。")

        def eligible() -> bool:
            if self._state.phase in ("closing", "closed"):
                return False
            self._lifecycle.check_dispatch()
            return can_start is None or can_start()

        return await execution.execute(
            source,
            input_id=input_id,
            can_start=eligible,
            presented_call_ids=presented_call_ids,
        )

    async def query_action(self, action_id: str) -> ActionRecord | None:
        """读取包括迟到调用结论的权威事实，关闭收尾期间仍可查询。"""
        return await self._queries.query_action(action_id)

    async def invoke(
        self,
        path: str,
        arguments: JsonObject,
        *,
        input_id: str | None = None,
        can_start: Callable[[], bool] | None = None,
        timeout_seconds: float | None = None,
        before_start: Callable[[], Awaitable[float | None]] | None = None,
        on_admitted: Callable[[str], None] | None = None,
    ) -> ActionRecord:
        """宿主明确调用单能力，共用目录、并发和真实回执，不先生成 Python。

        before_start 为专业活动的异步资格核验，只能拒绝或缩短原期限。
        on_admitted 交出真实关联身份，观察取消/未知后资源仍由执行器持有。
        接管身份不证明已发生效果；停止与关闭等待另有实际资源归还语义。
        """
        self._lifecycle.check_dispatch()
        execution = self._state.execution
        if execution is None:
            raise HostStateError("执行服务尚未装配。")

        def eligible() -> bool:
            if self._state.phase in ("closing", "closed"):
                return False
            self._lifecycle.check_dispatch()
            return can_start is None or can_start()

        return await execution.invoke(
            path,
            arguments,
            input_id=input_id,
            can_start=eligible,
            timeout_seconds=timeout_seconds,
            before_start=before_start,
            on_admitted=on_admitted,
        )

    async def control_activity(
        self,
        command: CommandKind,
        *,
        activity_id: str | None = None,
        definition: str | None = None,
        goal: str | None = None,
        additional_seconds: float | None = None,
        reason: str | None = None,
    ) -> ActivityCommand:
        """独立受理明确活动控制，不等待普通行动名额，也不假报实际完成。"""
        self._lifecycle.running_store()
        service = self._state.activities
        if service is None:
            raise HostStateError("活动服务尚未装配。")
        return await service.command(
            uuid4().hex,
            command,
            activity_id=activity_id,
            definition=definition,
            goal=goal,
            additional_seconds=additional_seconds,
            reason=reason,
        )

    async def query_activity(self, activity_id: str) -> ActivityRecord | None:
        """读取真实控制、预算和核对状态，不自动启动或恢复驱动。"""
        return await self._queries.query_activity(activity_id)

    async def query_request(self, request_id: str) -> RequestRecord | None:
        """读取可还原的模型材料及真实尝试，不发起新的模型调用。"""
        return await self._queries.query_request(request_id)

    async def query_panel(self) -> JsonObject:
        """读取有限真实概况与设备缓存状态；多页面查询不启动模型或设备。"""
        return await self._queries.query_panel()

    async def submit_control(self, request: ControlRequest) -> ControlRecord:
        """提交面板允许的显式控制，共用原能力与活动状态，不经过模型。"""
        self._lifecycle.check_dispatch()
        if self._state.panel is None:
            raise HostStateError("面板控制尚未装配。")
        return await self._state.panel.submit(request)

    async def query_control(self, request_id: str) -> JsonObject | None:
        """查询接管记录及当前真实业务回执，未知/迟到确认仍关联原身份。"""
        return await self._queries.query_control(request_id)

    def query_preview(self, target: str) -> PreviewFrame | None:
        """只读已取得画面；查询不额外截图或延长设备会话。"""
        return self._queries.query_preview(target)

    def query(self) -> HostSnapshot:
        """返回生命周期快照，不触发存储读取、设备或模型调用。"""
        return self._lifecycle.query()

    async def close(self) -> HostSnapshot:
        """关闭新入口并等待同一总期限；超时返回 closing，继续持有收尾资源。"""
        return await self._lifecycle.close()

    async def wait_closed(self, *, wait_seconds: float | None = None) -> HostSnapshot:
        """继续等待已发起的关闭；新的观察期限不重置原关闭期限或清理任务。"""
        return await self._lifecycle.wait_closed(wait_seconds=wait_seconds)
