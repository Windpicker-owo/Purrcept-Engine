"""把已取得模型、账本和插件状态装配为实际主体入口，不拥有 Core 循环。

固定指令与能力目录只在此构造一次。每个请求异步读取提交后的计划、活动、
插件状态及技能，再交给同步 user 尾部。前台与反思各自维护历史与预算；
客户端和插件资源的最终关闭仍归 EngineHost。
"""

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from purrcept_core.models import (
    ContentBlock,
    DecisionBackend,
    MessageRole,
    ModelBackend,
    ModelError,
    ModelSettings,
    ToolSet,
)

from purrcept_engine.app.config import DecisionModelSettings, EngineConfig, GenerationModelSettings
from purrcept_engine.domain.context.capacity import (
    CapacityAssessment,
    CapacityError,
    DecisionCapacity,
    GenerationCapacity,
    assess_capacity,
    check_capacity,
)
from purrcept_engine.domain.context.history import HistoryMaintenance
from purrcept_engine.domain.context.history_preparation import HistoryPreparation
from purrcept_engine.domain.context.maintenance import SummaryError
from purrcept_engine.domain.context.materials import media_blocks
from purrcept_engine.domain.execution.catalog import Catalog
from purrcept_engine.domain.inputs.models import MediaReference
from purrcept_engine.domain.knowledge.service import KnowledgeService
from purrcept_engine.domain.model_requests.service import RequestBudget
from purrcept_engine.domain.subject.attention import AttentionModel
from purrcept_engine.domain.subject.background import BackgroundService
from purrcept_engine.domain.subject.background_ports import BackgroundSettings
from purrcept_engine.domain.subject.foreground_tools import (
    foreground_instructions,
    format_character_card,
)
from purrcept_engine.domain.subject.ports import ActionCallback
from purrcept_engine.domain.subject.prompts import SLEEP_HISTORY_PREVIEW_PROMPT
from purrcept_engine.domain.subject.reflection_tools import reflection_instructions
from purrcept_engine.domain.subject.service import SubjectService
from purrcept_engine.domain.subject.session import DynamicState
from purrcept_engine.domain.subject.settings import CoordinatorSettings, SubjectModel
from purrcept_engine.infrastructure.models.compiler import compile_materials
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system.contracts import PluginRuntime
from purrcept_engine.values import JsonObject

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SubjectAssembly:
    """宿主持有的主体及摘要资源，客户端仅借用，不在此重复构造或关闭。

    Attributes:
        service: 已装配的业务服务，能力入口借用其已有状态。
        foreground_history: 前台已提交历史的独立维护所有者。
        knowledge: 原文展开与认识召回服务，修订仍经过反思批次契约。
        background: 同一主体的独立反馈和反思所有者。
        reflection_history: 反思已提交历史的独立维护所有者。
        sleep_history: 睡眠期间的一次前台压缩所有者，不与正常首发预检共享在途状态。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    service: SubjectService
    foreground_history: HistoryPreparation
    knowledge: KnowledgeService
    background: BackgroundService
    reflection_history: HistoryPreparation
    sleep_history: HistoryPreparation


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
    wakeup_committed: Callable[[], None],
    knowledge: KnowledgeService,
) -> SubjectAssembly:
    """按配置 discriminator 选择命名资源，构造不查询库或发起模型请求。

    Args:
        config: 已校验的宿主配置声明，不在构造时连接外部服务。
        store: 应用借出的领域存储端口，实际连接和关闭归资源所有者。
        catalog: 全部已启用插件共同生成的冻结目录。
        generation: 按配置名注入的生成后端集合。
        decisions: 按配置名注入的专用判断后端集合。
        execute: 当前主体执行受限行动的有限入口。
        plugins: 本实例已装配的插件运行资源，不执行隐式发现。
        can_start: 实际派发前重新检查的资格回调，等待后仍须复核。
        wakeup_committed: 计划提交后唤醒调度器的回调，不直接执行计划。
        knowledge: 原文展开与认识召回服务，修订仍经过反思批次契约。

    Returns:
        已连接前台、后台、认识及各自历史维护所有者的主体装配结果。

    Raises:
        TypeError: 已校验主体用途没有生成模型声明。

    Note:
        所有模型共用完整请求的多模态 token 估算，不根据模型名猜窗口或选择公式。
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
        """为一个模型声明绑定完整请求容量评估或强制核验。"""
        declaration = (
            GenerationCapacity(
                settings.context_window,
                settings.max_output_tokens,
                settings.input_limit,
            )
            if isinstance(settings, GenerationModelSettings)
            else DecisionCapacity(settings.input_limit)
        )

        def measure(payload: JsonObject) -> CapacityAssessment:
            """按绑定的模型容量声明评估这份完整请求。"""
            operation = check_capacity if enforce else assess_capacity
            return operation(
                payload,
                declaration,
                reserve_fraction=config.context.input_reserve_fraction,
                trigger=config.context.summary_trigger,
                target=config.context.summary_target,
            )

        return measure

    ledger = store.knowledge
    diagnostic_bytes = config.diagnostics.max_bytes

    def history_for(name: str, settings: GenerationModelSettings) -> HistoryPreparation:
        """各维护用途独占在途状态，底层借用相同摘要资源与权威 CAS。"""
        return HistoryPreparation(
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
            backend=generation[name],
            model=settings.model,
            assess=capacity_for(settings, enforce=False),
            context=config.context,
            compiler=compile_materials,
        )

    history = history_for(foreground_name, foreground_settings)
    reflection_history = history_for(reflection_name, reflection_settings)
    sleep_history = history_for(foreground_name, foreground_settings)

    async def native(media: tuple[MediaReference, ...]) -> tuple[ContentBlock, ...]:
        """从权威媒体存储展开已接管的原生内容。"""
        return await media_blocks(store, media)

    async def attention_work() -> JsonObject:
        """注意只取得有限相关背景，完整未决状态保留给前台/反思真实请求。"""
        session = await store.get_session("foreground")
        turn = None if session.active_turn is None else await store.get_turn(session.active_turn)
        plans = await ledger.pending_plans(limit=32)
        activities = await store.activity_records()
        # 相关工作背景有明确范围，不能把当前原文不存在的关系补成已知内容。
        return {
            "coverage": "当前前台输入、至多32项待办计划与活动；不含完整前台历史。",
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
        """在原轮次预算内读取最新已提交工作，构建动态尾部。"""
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
        # Ordering: 插件准备可能等待 I/O；时钟取在等待结束后。每次逻辑请求刷新，
        # 同一请求的传输重试仍使用已冻结材料，不把旧状态冒充实时采样。
        now = datetime.now().astimezone()
        return DynamicState.from_payload(
            {
                "material_kind": "engine_committed_state",
                "request_time": {
                    "current_time": now.isoformat(),
                    "unix_seconds": now.timestamp(),
                    "meaning": "本次逻辑模型请求组装时的宿主时间，含本地时区偏移；"
                    "不是模型完成或外部效果发生时刻。同一请求重试保留此时间。"
                    "后续刺激留到下一批，历史消息位置不保证实际发生顺序。",
                },
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

    async def compact_sleep_history() -> None:
        """在主脑空闲时尝试一次旧历史摘要，实际安装仍遵守原保护与版本门槛。

        此布局只用于睡眠维护前后容量比较，不提交新的主脑输入，不借行动工具。
        下一次真实唤醒仍以完整工具目录和实际新输入再次预检，不能借此绕过容量。
        """
        budget = RequestBudget(
            max_requests=1,
            deadline=asyncio.get_running_loop().time() + config.subject.summary_retry.total_seconds,
        )
        try:
            session = await store.get_session("foreground")
            dynamic = await refresh(budget)
            await sleep_history(
                session,
                SLEEP_HISTORY_PREVIEW_PROMPT.message(MessageRole.USER),
                instructions=foreground_instructions(config.subject.character.record(), catalog),
                tools=ToolSet(),
                settings=ModelSettings(max_output_tokens=foreground_settings.max_output_tokens),
                dynamic=dynamic,
                budget=budget,
                force=True,
            )
        except (CapacityError, ModelError, SummaryError) as error:
            # 无可用容量/模型不应把睡眠变成宿主故障；记录真实失败且不赠送重试。
            # 权威库、内部程序故障及取消不在此转换。
            sleep_history.last_failure = type(error).__name__ + ": " + str(error)
            _logger.warning("睡眠历史维护保留原版本：%s", sleep_history.last_failure)

    attention: AttentionModel | None = None
    attention_name = config.subject.attention
    if attention_name is not None:
        settings = config.models[attention_name]
        # 与前台使用同一角色配置和 Core 角色卡模板；不从聊天正文或平台昵称猜身份。
        attention_profile = format_character_card(config.subject.character.record())
        if isinstance(settings, DecisionModelSettings):
            attention = AttentionModel(
                decisions[attention_name],
                settings.model,
                capacity_for(settings, enforce=True),
                config.subject.attention_retry,
                kind="decision",
                compiler=compile_materials,
                profile=attention_profile,
                supports_contents=settings.supports_contents,
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
                profile=attention_profile,
                supports_contents=settings.supports_contents,
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
            supports_contents=foreground_settings.supports_contents,
        ),
        attention=attention,
        settings=CoordinatorSettings(
            diagnostic_bytes,
            config.subject.attention_batch_seconds,
            config.subject.attention_batch_size,
            config.subject.foreground_turn.max_requests,
            config.subject.foreground_turn.total_seconds,
            config.subject.proactive_thinking,
            config.subject.proactive_idle_seconds,
        ),
        instructions=foreground_instructions(config.subject.character.record(), catalog),
        execute=execute,
        media=native,
        refresh=refresh,
        attention_context=attention_work,
        wakeup_committed=wakeup_committed,
        can_start=can_start,
        prepare_history=history,
        compact_sleep_history=compact_sleep_history,
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
            supports_contents=reflection_settings.supports_contents,
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
    return SubjectAssembly(
        subject, history, knowledge, background, reflection_history, sleep_history
    )
