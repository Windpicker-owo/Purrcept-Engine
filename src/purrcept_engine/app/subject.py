"""把已取得模型、账本和插件状态装配为实际主体入口，不拥有 Core 循环。

固定指令与能力目录只在此构造一次。每个请求异步读取提交后的计划、活动、
插件状态及技能，再交给同步 user 尾部。前台与反思各自维护历史与预算；
客户端和插件资源的最终关闭仍归 EngineHost。
"""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from purrcept_core.models import ContentBlock, DecisionBackend, ModelBackend

from purrcept_engine.app.config import DecisionModelSettings, EngineConfig, GenerationModelSettings
from purrcept_engine.domain.context.capacity import (
    CapacityAssessment,
    CapacityRule,
    DecisionCapacity,
    GenerationCapacity,
    assess_capacity,
    check_capacity,
)
from purrcept_engine.domain.context.history import HistoryMaintenance
from purrcept_engine.domain.context.history_preparation import HistoryPreparation
from purrcept_engine.domain.context.materials import media_blocks
from purrcept_engine.domain.execution.catalog import Catalog
from purrcept_engine.domain.inputs.models import MediaReference
from purrcept_engine.domain.knowledge.service import KnowledgeService
from purrcept_engine.domain.model_requests.service import RequestBudget
from purrcept_engine.domain.subject.attention import AttentionModel
from purrcept_engine.domain.subject.background import BackgroundService
from purrcept_engine.domain.subject.background_ports import BackgroundSettings
from purrcept_engine.domain.subject.foreground_tools import foreground_instructions
from purrcept_engine.domain.subject.ports import ActionCallback
from purrcept_engine.domain.subject.reflection_tools import reflection_instructions
from purrcept_engine.domain.subject.service import SubjectService
from purrcept_engine.domain.subject.session import DynamicState
from purrcept_engine.domain.subject.settings import CoordinatorSettings, SubjectModel
from purrcept_engine.infrastructure.models.compiler import compile_materials
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system.contracts import PluginRuntime
from purrcept_engine.values import JsonObject


@dataclass(frozen=True, slots=True)
class SubjectAssembly:
    """宿主持有的主体及摘要资源，客户端仅借用，不在此重复构造或关闭。"""

    service: SubjectService
    foreground_history: HistoryPreparation
    knowledge: KnowledgeService
    background: BackgroundService
    reflection_history: HistoryPreparation


def build_subject(
    config: EngineConfig,
    store: Store,
    catalog: Catalog,
    generation: Mapping[str, ModelBackend],
    decisions: Mapping[str, DecisionBackend],
    *,
    execute: ActionCallback,
    plugins: Mapping[str, PluginRuntime],
    can_start: Callable[[], None],
    rules: Mapping[str, CapacityRule],
    wakeup_committed: Callable[[], None],
    knowledge: KnowledgeService,
) -> SubjectAssembly:
    """按配置 discriminator 选择命名资源，构造不查询库或发起模型请求。

    rules 是明确注册的纯计量依据，不根据模型名猜 Provider 窗口或媒体公式。
    外部模型不可用沿原预算失败，不从其余配置寻找替代模型。
    """
    foreground_name = config.subject.foreground
    foreground_settings = config.models[foreground_name]
    summary_name = config.subject.summary
    summary_settings = config.models[summary_name]
    reflection_name = config.subject.reflection
    reflection_settings = config.models[reflection_name]
    if (
        not isinstance(foreground_settings, GenerationModelSettings)
        or not isinstance(summary_settings, GenerationModelSettings)
        or not isinstance(reflection_settings, GenerationModelSettings)
    ):
        raise TypeError("已校验主体用途没有生成模型声明。")

    def capacity_for(
        settings: GenerationModelSettings | DecisionModelSettings,
        *,
        enforce: bool,
    ) -> Callable[[JsonObject], CapacityAssessment]:
        declaration = (
            GenerationCapacity(
                settings.context_window,
                settings.max_output_tokens,
                settings.capacity_rule,
                settings.media_rule,
                settings.input_limit,
            )
            if isinstance(settings, GenerationModelSettings)
            else DecisionCapacity(settings.input_limit, settings.capacity_rule, settings.media_rule)
        )

        def measure(payload: JsonObject) -> CapacityAssessment:
            operation = check_capacity if enforce else assess_capacity
            return operation(
                payload,
                declaration,
                reserve_fraction=config.context.input_reserve_fraction,
                trigger=config.context.summary_trigger,
                target=config.context.summary_target,
                rules=rules,
            )

        return measure

    ledger = store.knowledge
    diagnostic_bytes = config.diagnostics.max_bytes
    maintenance = HistoryMaintenance(
        store,
        store,
        generation[summary_name],
        model=summary_settings.model,
        summary_capacity=capacity_for(summary_settings, enforce=True),
        retry=config.subject.summary_retry,
        output_tokens=summary_settings.max_output_tokens,
        diagnostic_bytes=diagnostic_bytes,
        can_start=can_start,
        compiler=compile_materials,
    )
    history = HistoryPreparation(
        store,
        maintenance,
        backend=generation[foreground_name],
        model=foreground_settings.model,
        assess=capacity_for(foreground_settings, enforce=False),
        context=config.context,
        compiler=compile_materials,
    )
    reflection_history = HistoryPreparation(
        store,
        HistoryMaintenance(
            store,
            store,
            generation[summary_name],
            model=summary_settings.model,
            summary_capacity=capacity_for(summary_settings, enforce=True),
            retry=config.subject.summary_retry,
            output_tokens=summary_settings.max_output_tokens,
            diagnostic_bytes=diagnostic_bytes,
            can_start=can_start,
            compiler=compile_materials,
        ),
        backend=generation[reflection_name],
        model=reflection_settings.model,
        assess=capacity_for(reflection_settings, enforce=False),
        context=config.context,
        compiler=compile_materials,
    )

    async def native(media: tuple[MediaReference, ...]) -> tuple[ContentBlock, ...]:
        return await media_blocks(store, media)

    async def attention_work() -> JsonObject:
        """注意只取得有限相关背景，完整未决状态保留给前台/反思真实请求。"""
        session = await store.get_session("foreground")
        turn = None if session.active_turn is None else await store.get_turn(session.active_turn)
        plans = await ledger.pending_plans(limit=32)
        activities = await store.activity_records()
        # 相关工作背景有明确范围，不能把当前原文不存在的关系补成已知内容。
        return {
            "coverage": "current_foreground_identity; pending_plans_first_32; active_activities",
            "current_turn": None
            if turn is None
            else {
                "input_ids": list(turn.input_ids),
                "started_at": turn.started_at,
            },
            "plans": [item.model_dump(mode="json") for item in plans],
            "activities": [
                {
                    "activity_id": item.activity_id,
                    "version": item.version,
                    "goal": item.goal,
                    "state": item.state,
                    "remaining_seconds": item.remaining_seconds,
                    "reconciliation_required": item.needs_reconciliation,
                }
                for item in activities
            ],
        }

    async def refresh(budget: RequestBudget) -> DynamicState:
        budget.check()
        # Ordering: 当前未决材料全部重新核验，不能用注意的有限背景或已投递
        # 邮箱代替仍有效监管、未完成计划和真实调用事实。超额由完整请求拒绝。
        work = await store.subject_work()
        materials: JsonObject = {}
        for name, runtime in plugins.items():
            if runtime.prepare_request is not None:
                # 轮次预检和逻辑请求派发前刷新；同步 Reminder.resolve 只借用
                # 已完成材料，同一次传输重试不会再次扫文件或改变请求。
                materials[name] = await runtime.prepare_request()
        budget.check()
        return DynamicState.from_payload(
            {
                "material_kind": "engine_committed_state",
                "work": work,
                "request_materials": materials,
                "budget": {
                    "used_requests": budget.used,
                    "max_requests": budget.max_requests,
                    "remaining_seconds_at_assembly": max(
                        0, budget.deadline - asyncio.get_running_loop().time()
                    ),
                },
            }
        )

    attention: AttentionModel | None = None
    attention_name = config.subject.attention
    if attention_name is not None:
        settings = config.models[attention_name]
        if isinstance(settings, DecisionModelSettings):
            attention = AttentionModel(
                decisions[attention_name],
                settings.model,
                capacity_for(settings, enforce=True),
                config.subject.attention_retry,
                kind="decision",
                compiler=compile_materials,
            )
        else:
            attention = AttentionModel(
                generation[attention_name],
                settings.model,
                capacity_for(settings, enforce=True),
                config.subject.attention_retry,
                kind="generation",
                output_tokens=min(256, settings.max_output_tokens),
                compiler=compile_materials,
            )
    subject = SubjectService(
        store,
        knowledge,
        ledger,
        catalog,
        subject=SubjectModel(
            generation[foreground_name],
            foreground_settings.model,
            foreground_settings.max_output_tokens,
            capacity_for(foreground_settings, enforce=True),
            config.subject.foreground_retry,
            compiler=compile_materials,
        ),
        attention=attention,
        settings=CoordinatorSettings(
            diagnostic_bytes,
            config.subject.attention_batch_seconds,
            config.subject.attention_batch_size,
            config.subject.attention_defer_seconds,
            config.subject.foreground_turn.max_requests,
            config.subject.foreground_turn.total_seconds,
        ),
        instructions=foreground_instructions(config.subject.character.record(), catalog),
        execute=execute,
        media=native,
        refresh=refresh,
        attention_context=attention_work,
        wakeup_committed=wakeup_committed,
        can_start=can_start,
        prepare_history=history,
    )
    background = BackgroundService(
        store,
        store.feedback,
        knowledge,
        ledger,
        reflection=SubjectModel(
            generation[reflection_name],
            reflection_settings.model,
            reflection_settings.max_output_tokens,
            capacity_for(reflection_settings, enforce=True),
            config.subject.reflection_retry,
            compiler=compile_materials,
        ),
        instructions=reflection_instructions(),
        settings=BackgroundSettings(
            diagnostic_bytes,
            page_chars=config.context.page_chars,
            batch_seconds=config.subject.reflection_batch_seconds,
            successful_turns=config.subject.reflection_batch_turns,
            max_requests=config.subject.reflection_turn.max_requests,
            turn_seconds=config.subject.reflection_turn.total_seconds,
        ),
        media=native,
        refresh=refresh,
        prepare_history=reflection_history,
        foreground_generation=lambda: subject.generation,
        interrupt_foreground=subject.session.interrupt,
        can_start=can_start,
    )
    subject.bind_background(background)
    return SubjectAssembly(subject, history, knowledge, background, reflection_history)
