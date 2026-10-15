"""在真实会话入口维护旧历史，复用 Core 首个 Generate 的公开请求布局。

HistoryPreparation 在 Session 开始主动预算但尚未 begin_turn 时运行。预览
facade 只推进到第一个未执行的模型 effect，取得完整目录、schema、历史、
当前输入与冻结尾部；不会调用模型、执行工具或采用临时历史。候选由同一
布局重新计量，安装及原文归档仍归 Store 的空闲版本事务。
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

from purrcept_engine.app.config import ContextSettings
from purrcept_engine.context.capacity import CapacityAssessment, CapacityError
from purrcept_engine.context.checkpoints import decode_checkpoint
from purrcept_engine.context.maintenance import SummaryError, freeze_summary_source
from purrcept_engine.domain.subject.models import SessionRecord
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.subject.delivery import ToolDeliveryGate
from purrcept_engine.subject.history import HistoryMaintenance
from purrcept_engine.subject.model_requests import (
    RequestBudget,
    compile_materials,
    prepare_off_loop,
)
from purrcept_engine.subject.session import CapacityCheck, DynamicReminder, DynamicState
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
    """取得 Core 真正首个请求，关闭未执行流程，不复制编译器或工具循环。"""
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
        store: Store,
        maintenance: HistoryMaintenance,
        *,
        backend: ModelBackend,
        model: str,
        assess: CapacityCheck,
        context: ContextSettings,
    ) -> None:
        """借用有限模型与同库账本，构造不读库或启动摘要。"""
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
        """只在超阈值且存在未受保护旧前缀时摘要，返回实际权威版本。"""
        budget.check()
        checkpoint = (
            None if session.checkpoint_json is None else decode_checkpoint(session.checkpoint_json)
        )

        def layout(candidate: ConversationCheckpoint | None) -> ModelRequest:
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
            return ToolDeliveryGate().prepare(
                preview,
                model=self.model,
                capacity=self.assess,
                host_state=dynamic.host_payload,
                reminder_key=DynamicReminder.source_id,
            )

        def measure() -> CapacityAssessment:
            """首轮完整布局及分词可能耗时数秒，不能阻塞麦克风采集。"""
            materials, _ = compile_materials(layout(checkpoint), model=self.model)
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
