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
        """校验配置与插件声明，创建尚未启动的宿主实例。

        Args:
            config: 已校验的宿主配置声明，不在构造时连接外部服务。
            plugin_factories: 显式插件工厂表；None 使用内置登记，不扫描外部包。
            executor_factory: 可替换隔离执行器的工厂；None 使用默认执行器。
            console_output: 显式控制台能力使用的文字输出回调。
            generation_backends: 按配置名注入的生成后端，由嵌入调用者负责最终关闭。
            decision_backends: 按配置名注入的判断后端，由嵌入调用者负责最终关闭。
            capacity_rules: 按规则名显式登记的纯容量计量函数。
            embedding_provider: 嵌入调用者注入的向量 Provider；None 按显式配置装配。
            reranker: 可选重排器，只能对给定候选排序。 None 表示不重排。

        Raises:
            ConfigError: 配置或插件纯声明不成立，尚未取得外部资源。

        Note:
            构造阶段不打开设备。执行器工厂须遵守 ActionExecutor 的隔离与资源归还契约；
            显式注入的模型后端由调用者最终关闭。
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
        """返回本实例启动快照。

        Note:
            运行期间不能替换它来改变资源和关闭边界。
        """
        return self._state.config

    @property
    def catalog(self) -> Catalog:
        """返回同一冻结能力目录。

        Note:
            查询不开始插件、网络或 Worker。
        """
        return self._state.prepared.catalog

    async def start(self) -> HostSnapshot:
        """启动本实例的核心资源和已配置插件，返回宿主状态。

        Returns:
            当前实例、生命周期阶段和未归还资源的不可变快照；closing 表示收尾仍在继续。

        Raises:
            HostStateError: 当前实例已关闭或阶段不允许启动。
            InstanceLocked: 同一数据目录仍由另一实例持有。
            StorageError: 权威库初始化或格式核验失败。

        Note:
            重复调用观察同一次启动。失败时仍由宿主持有已取得资源的收尾责任；
            外围连接状态以快照为准。
        """
        return await self._lifecycle.start()

    async def submit(self, submission: InputSubmission) -> Admission:
        """向引擎正式提交外部输入，取得可查询的接管凭证。

        Args:
            submission: 待正式接管的来源、原文、媒体与可信信号。

        Returns:
            正式接管凭证，包含 input_id 与重复来源标记；不表示模型已处理或对外回复。

        Raises:
            HostStateError: 宿主尚未就绪或已经进入关闭阶段。
            StorageError: 接管容量已满、来源冲突、输入无效或权威存储故障。

        Note:
            宿主在首次等待前持有完整接管任务。取消本次观察不会撤销已接管原文，
            以及随后必要的 VAD 停音或其他显式原代次抢占交接。
            VAD 只保底通知下一轮，不取消当前生成。后续处理用 query_processing 查询。
        """
        return await self._inputs.submit(submission)

    async def query_input(self, input_id: str) -> InputRecord | None:
        """按正式输入身份查询原文及接管事实。

        Args:
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。

        Returns:
            首次接管的完整输入原文、来源与媒体引用。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。

        Raises:
            HostStateError: 权威库尚未取得或宿主已完全关闭。
            StorageError: 权威读取失败或已保存记录损坏。

        Note:
            查询已接管输入；关闭收尾期间仍可使用尚未释放的存储。
        """
        return await self._queries.query_input(input_id)

    async def query_native_input(self, source: InputSource) -> InputRecord | None:
        """按原生来源读取首次接管事实，供重放恢复。

        Args:
            source: 含适配器、账号、会话和原生事件身份的可信来源。

        Returns:
            首次接管的完整输入原文、来源与媒体引用。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。

        Raises:
            HostStateError: 权威库尚未取得或宿主已完全关闭。
            StorageError: 权威读取失败或已保存记录损坏。

        Note:
            不开始新的输入处理。
        """
        return await self._queries.query_native_input(source)

    async def query_processing(self, input_id: str) -> InputProcessing | None:
        """查询输入实际处置阶段及其失败原因。

        Args:
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。

        Returns:
            输入实际处理阶段、版本和关联轮次。 未取得对应记录或素材时返回 None；读取故障仍抛出。

        Raises:
            HostStateError: 权威库尚未取得或宿主已完全关闭。
            StorageError: 权威读取失败或已保存记录损坏。

        Note:
            读取宿主实际输入阶段，取得、接管及处理结束分别表达。
        """
        return await self._queries.query_processing(input_id)

    async def execute(
        self,
        source: str,
        *,
        input_id: str | None = None,
        can_start: Callable[[], bool] | None = None,
        presented_call_ids: frozenset[str] = frozenset(),
    ) -> ActionRecord:
        """在隔离环境执行明确的 Python 行动，返回已保存的逐次回执。

        Args:
            source: 待执行的受限 Python 源码。
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。 None
                表示无单一输入因果关联。
            can_start: 实际派发前重新检查的资格回调，等待后仍须复核。 None
                表示不附加调用方资格条件，宿主固定门槛仍生效。
            presented_call_ids: 主体交付门槛确认已完整呈现的调用身份；空集合不授予恢复资格。

        Returns:
            已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。

        Raises:
            HostStateError: 宿主当前不允许派发或执行服务尚未装配。
            StorageError: 接管容量不足或权威记录无法提交。
            ExecutionBusy: 行动 Worker 接管名额已满。
            ExecutionClosed: 执行器已停止接管新行动。
            ExecutionLimitError: 源码编码或字节大小超过执行器边界。

        Note:
            与前台 play_action 共用冻结目录和执行器。取消观察不撤销已开始效果。input_id
            仅作因果关联；presented_call_ids 只能由可信交付门槛提供，不向 Worker 或模型开放。
        """
        self._lifecycle.check_dispatch()
        execution = self._state.execution
        if execution is None:
            raise HostStateError("执行服务尚未装配。")

        def eligible() -> bool:
            """在实际派发前复核宿主阶段与本次生成资格。"""
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
        """查询行动及其逐次真实调用历史。

        Args:
            action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。

        Returns:
            已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。
            未取得对应记录或素材时返回 None；读取故障仍抛出。

        Raises:
            HostStateError: 权威库尚未取得或宿主已完全关闭。
            StorageError: 权威读取失败或已保存记录损坏。

        Note:
            读取包括迟到调用结论的权威事实，关闭收尾期间仍可查询。
        """
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
        """按冻结目录直接调用单项能力，返回同一执行链的行动回执。

        Args:
            path: 冻结目录中的完整能力调用路径，例如 me.console.output。
            arguments: 按已装配参数模型校验的能力关键字参数。
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。 None
                表示无单一输入因果关联。
            can_start: 实际派发前重新检查的资格回调，等待后仍须复核。 None
                表示不附加调用方资格条件，宿主固定门槛仍生效。
            timeout_seconds: 本次操作的等待上限（秒），不得扩大所属行动或活动期限。 None
                使用所属能力或行动的既定期限。
            before_start: Provider 启动前的异步核验；返回 None 拒绝，正有限秒数只收紧期限。
                None 表示没有额外的异步资格核验。
            on_admitted: 首次等待前接收执行器分配身份的同步回调，不授予重投资格。 None
                表示不向调用方另行通知接管身份。

        Returns:
            已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。

        Raises:
            HostStateError: 宿主当前不允许派发或执行服务尚未装配。
            StorageError: 受理容量不足或权威记录无法提交。
            ExecutionBusy: 能力调用接管名额已满。
            ExecutionClosed: 执行器已停止接管。
            LookupError: 路径不在当前冻结能力目录中。

        Note:
            与脚本共用目录、并发和真实回执。观察取消或 unknown 不代表资源已经归还；
            活动所有者须用配对资源等待入口确认。接管身份不证明已发生效果。
        """
        self._lifecycle.check_dispatch()
        execution = self._state.execution
        if execution is None:
            raise HostStateError("执行服务尚未装配。")

        def eligible() -> bool:
            """在实际派发前复核宿主阶段与本次生成资格。"""
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
        """提交明确活动控制，返回受理与实际确认记录。

        Args:
            command: 明确的活动控制种类，目标与预算字段须满足对应命令。
            activity_id: 已创建活动的稳定身份。 None 只适用于不需要已有活动身份的控制。
            definition: 新活动使用的已装配定义名。 None 不指定新活动定义。
            goal: 明确委托给专业活动的目标文本。 None 不附加新目标。
            additional_seconds: 本次明确增加的活动预算秒数。 None 不增加预算。
            reason: 供后续核对的明确处置原因。 None 不附加原因。

        Returns:
            命令接管与实际确认记录；受理成功不等于驱动已完成。

        Raises:
            HostStateError: 宿主尚未就绪或活动服务未装配。
            ActivityError: 命令字段、当前活动状态或预算不满足控制要求。

        Note:
            独立受理明确活动控制，不等待普通行动名额，也不假报实际完成。
        """
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
        """读取真实控制、预算和核对状态，不自动启动或恢复驱动。

        Args:
            activity_id: 已创建活动的稳定身份。

        Returns:
            活动的控制、预算、运行代次与工作快照。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。

        Raises:
            HostStateError: 权威库尚未取得或宿主已完全关闭。
            StorageError: 权威读取失败或已保存记录损坏。
        """
        return await self._queries.query_activity(activity_id)

    async def query_request(self, request_id: str) -> RequestRecord | None:
        """读取可还原的模型材料及真实尝试，不发起新的模型调用。

        Args:
            request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。

        Returns:
            一个逻辑模型请求的冻结材料及按实际顺序保存的尝试。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。

        Raises:
            HostStateError: 权威库尚未取得或宿主已完全关闭。
            StorageError: 权威读取失败或已保存记录损坏。
        """
        return await self._queries.query_request(request_id)

    async def query_panel(self) -> JsonObject:
        """读取有限真实概况与设备缓存状态。

        Note:
            多页面查询不启动模型或设备。
        """
        return await self._queries.query_panel()

    async def submit_control(self, request: ControlRequest) -> ControlRecord:
        """提交允许的本机快捷控制，取得稳定受理记录。

        Args:
            request: 包含稳定请求身份、目标和操作参数的显式控制。

        Returns:
            稳定请求身份的受理记录与当前实际业务关联。

        Raises:
            HostStateError: 宿主当前不允许控制或面板服务未装配。
            ControlError: 请求身份冲突、实例过期或目标操作不在面板允许范围内。

        Note:
            提交面板允许的显式控制，共用原能力、活动或主体选项所有者，不经过模型。
        """
        self._lifecycle.check_dispatch()
        if self._state.panel is None:
            raise HostStateError("面板控制尚未装配。")
        return await self._state.panel.submit(request)

    async def query_control(self, request_id: str) -> JsonObject | None:
        """查询接管记录及当前真实业务回执，未知/迟到确认仍关联原身份。

        Args:
            request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。

        Raises:
            HostStateError: 权威库尚未取得或宿主已完全关闭。
            StorageError: 权威读取失败或已保存记录损坏。
        """
        return await self._queries.query_control(request_id)

    def query_preview(self, target: str) -> PreviewFrame | None:
        """只读已取得画面。

        Args:
            target: 本次操作明确指定的业务目标身份。

        Returns:
            当前已经取得的完整预览画面，不触发额外采集。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。

        Note:
            查询不额外截图或延长设备会话。
        """
        return self._queries.query_preview(target)

    def query(self) -> HostSnapshot:
        """返回生命周期快照，不触发存储读取、设备或模型调用。

        Returns:
            当前实例、生命周期阶段和未归还资源的不可变快照；closing 表示收尾仍在继续。
        """
        return self._lifecycle.query()

    async def close(self) -> HostSnapshot:
        """停止接收新工作，并在总关闭期限内等待实际资源归还。

        Returns:
            当前实例、生命周期阶段和未归还资源的不可变快照；closing 表示收尾仍在继续。

        Note:
            重复调用等待同一收尾任务。超时只返回 closing，目录锁与未归还依赖继续持有。
        """
        return await self._lifecycle.close()

    async def wait_closed(self, *, wait_seconds: float | None = None) -> HostSnapshot:
        """继续观察已发起的关闭过程，返回当前宿主状态。

        Args:
            wait_seconds: 本次观察关闭进度的等待秒数；None 表示等待实际结束。

        Returns:
            当前实例、生命周期阶段和未归还资源的不可变快照；closing 表示收尾仍在继续。

        Note:
            本次观察期限不重置宿主原关闭期限，也不重新创建清理任务。
        """
        return await self._lifecycle.wait_closed(wait_seconds=wait_seconds)
