"""在真实请求前协调历史容量、保护范围与一次受控摘要维护。

借用同一会话的已提交版本及保护身份，生成布局与正式派发共用编译端口。
这里只协调领域步骤，不取得 SQLite 连接、模型凭据或客户端；容量不足明确
失败，内部故障不能伪装为一个空历史。
"""

import logging

from purrcept_core.models import (
    ConversationCheckpoint,
    Message,
    Model,
    ModelBackend,
    ModelError,
    ModelRequest,
    ModelSettings,
    SystemInstruction,
    ToolSet,
)
from purrcept_core.models.effects import Generate

from purrcept_engine.domain.context.capacity import CapacityAssessment, CapacityError
from purrcept_engine.domain.context.checkpoints import decode_checkpoint
from purrcept_engine.domain.context.delivery import ToolDeliveryGate
from purrcept_engine.domain.context.history import HistoryMaintenance
from purrcept_engine.domain.context.history_ports import HistoryContext, HistoryStore
from purrcept_engine.domain.context.maintenance import SummaryError, freeze_summary_source
from purrcept_engine.domain.model_requests.ports import MaterialCompiler
from purrcept_engine.domain.model_requests.service import RequestBudget, prepare_off_loop
from purrcept_engine.domain.subject.models import SessionRecord
from purrcept_engine.domain.subject.session import CapacityCheck, DynamicReminder, DynamicState
from purrcept_engine.values import decode_object

_logger = logging.getLogger(__name__)


def preview_request(
    backend: ModelBackend,
    model: str,
    checkpoint: ConversationCheckpoint | None,
    message: Message,
    *,
    instructions: tuple[SystemInstruction, ...],
    tools: ToolSet,
    settings: ModelSettings,
    dynamic: DynamicState,
    max_requests: int,
) -> ModelRequest:
    """取得 Core 真正首个请求，关闭未执行流程，不复制编译器或工具循环。

    Args:
        backend: 显式注入的模型后端，资源取得和关闭归装配层。
        model: 本次调用使用的显式模型名，不在领域层推断替代模型。
        checkpoint: 完整 Core 检查点；None 表示尚无已提交历史。
        message: 当前请求中实际存在的完整 Core 消息。
        instructions: 稳定 system 指令序列，实时状态不得混入缓存前缀。
        tools: 本会话允许使用的固定 Core 工具集合。
        settings: 当前组件已校验的配置或纯声明。
        dynamic: 当前逻辑请求实际采用的动态状态材料。
        max_requests: 本轮逻辑模型请求总上限，传输重试不重复占名额。

    Returns:
        按实际消息顺序构造的完整 Core 请求。

    Raises:
        TypeError: Core 首个公开请求预览没有返回 Generate。
    """
    reminder = DynamicReminder(dynamic)
    conversation = Model(backend, model).conversation(
        instructions=instructions,
        tools=tools,
        settings=settings,
        reminder_sources=(reminder,),
        max_model_rounds=max_requests,
        continuation_policy="client_managed",
        request_budget=None,
    )
    if checkpoint is not None:
        conversation.restore(checkpoint)
    flow = conversation.ask(message)
    try:
        effect = next(flow)
        if not isinstance(effect, Generate):
            raise TypeError("Core 首个公开请求预览没有返回 Generate。")
        return effect.request
    finally:
        flow.close()


class HistoryPreparation:
    """app 为每份连续历史创建的维护所有者，不共享在途摘要状态。

    assess 允许返回超额比例，真正请求使用独立 check 门槛。已分类模型或
    候选失败保留旧历史及实际请求证据；同覆盖不会重试，旧布局仍超额时由
    正常请求门槛明确拒绝。内部和权威故障不降级成正常摘要失败。
    """

    def __init__(
        self,
        store: HistoryStore,
        maintenance: HistoryMaintenance,
        *,
        compiler: MaterialCompiler,
        backend: ModelBackend,
        model: str,
        assess: CapacityCheck,
        context: HistoryContext,
    ) -> None:
        """借用有限模型与同库账本，构造不读库或启动摘要。

        Args:
            store: 应用借出的领域存储端口，实际连接和关闭归资源所有者。
            maintenance: 当前会话唯一的历史摘要计算所有者。
            compiler: 按实际 SDK 布局冻结完整请求材料的纯编译器。
            backend: 显式注入的模型后端，资源取得和关闭归装配层。
            model: 本次调用使用的显式模型名，不在领域层推断替代模型。
            assess: 只生成完整请求容量评估的函数。
            context: 宿主或 Core 注入的可信调用上下文，不由模型自行填写。
        """
        self.compiler = compiler
        self.store, self.maintenance = store, maintenance
        self.backend, self.model, self.assess, self.context = backend, model, assess, context
        self.last_failure: str | None = None

    async def __call__(
        self,
        session: SessionRecord,
        message: Message,
        *,
        instructions: tuple[SystemInstruction, ...],
        tools: ToolSet,
        settings: ModelSettings,
        dynamic: DynamicState,
        budget: RequestBudget,
    ) -> SessionRecord:
        """只在超阈值且存在未受保护旧前缀时摘要，返回实际权威版本。

        Args:
            session: 当前已提交会话版本及完整检查点。
            message: 当前请求中实际存在的完整 Core 消息。
            instructions: 稳定 system 指令序列，实时状态不得混入缓存前缀。
            tools: 本会话允许使用的固定 Core 工具集合。
            settings: 当前组件已校验的配置或纯声明。
            dynamic: 当前逻辑请求实际采用的动态状态材料。
            budget: 当前连续批次共享的请求数量与单调截止预算。

        Returns:
            会话已提交版本与完整检查点，不包含尚未提交的临时历史。

        Raises:
            TypeError: Engine 历史来源元数据格式无效。
        """
        budget.check()
        checkpoint = (
            None if session.checkpoint_json is None else decode_checkpoint(session.checkpoint_json)
        )

        def layout(candidate: ConversationCheckpoint | None) -> ModelRequest:
            """用当前完整请求布局核验候选历史的真实交付容量。"""
            preview = preview_request(
                self.backend,
                self.model,
                candidate,
                message,
                instructions=instructions,
                tools=tools,
                settings=settings,
                dynamic=dynamic,
                max_requests=budget.max_requests,
            )
            # 首发预览与真实请求使用同一尾部投影，不能因已经完整交付的回执
            # 正文副本提前触发摘要；没有待工具结果，因此这里只复用纯投影。
            return ToolDeliveryGate(compiler=self.compiler).prepare(
                preview,
                model=self.model,
                capacity=self.assess,
                host_state=dynamic.host_payload,
                reminder_key=DynamicReminder.source_id,
            )

        def measure() -> CapacityAssessment:
            """首轮完整布局及分词可能耗时数秒，不能阻塞麦克风采集。"""
            materials, _ = self.compiler(layout(checkpoint), model=self.model)
            payload = decode_object(materials.encode("utf-8")).get("payload")
            if not isinstance(payload, dict):
                raise TypeError("Core 实际布局缺少 SDK payload。")
            return self.assess(payload)

        before = await prepare_off_loop(measure)
        budget.check()
        if not before.maintenance_due or checkpoint is None:
            return session

        # 未决保护来自数据库中的因果关系；外界正文或工具 JSON 同名字段不能
        # 授予保护或完成资格。保护命中前缀时只缩短覆盖，不越过它挑后面的轮次。
        identities: list[str] = []
        sources: list[tuple[str, ...]] = []
        for item in checkpoint.history.messages:
            evidence = item.metadata.get("evidence", ())
            if not isinstance(evidence, (tuple, list)):
                raise TypeError("Engine 历史来源元数据格式无效。")
            selected: list[str] = []
            for value in evidence:
                if not isinstance(value, str):
                    raise TypeError("Engine 历史来源元数据格式无效。")
                selected.append(value)
            sources.append(tuple(selected))
            identities.extend(selected)
        protected = await self.store.protected_input_ids(tuple(identities))
        positions = tuple(
            index
            for index, source_ids in enumerate(sources)
            if any(identity in protected for identity in source_ids)
        )
        source = freeze_summary_source(
            checkpoint,
            owner_id=session.scope,
            source_version=session.version,
            protected_turns=self.context.protected_turns,
            protected_message_indices=positions,
        )
        if source is None:
            return session
        budget.check()
        try:
            await self.maintenance.run(
                source,
                source_layout=layout,
                source_model=self.model,
                source_capacity=self.assess,
                deadline=budget.deadline,
            )
        except (SummaryError, CapacityError, ModelError) as error:
            self.last_failure = type(error).__name__ + ": " + str(error)
            _logger.warning("历史维护保留原版本：%s", self.last_failure)
        else:
            self.last_failure = None
        budget.check()
        return await self.store.get_session(session.scope)

    async def close(self) -> None:
        """在会话撤销后收回真实摘要借用，不关闭 app 拥有的客户端。"""
        await self.maintenance.close()
