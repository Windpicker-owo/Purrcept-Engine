"""宿主权威事实与设备缓存的只读查询服务，不启动模型、设备或外部行动。"""

from __future__ import annotations

import logging

from purrcept_engine.app.host_state import EngineState
from purrcept_engine.app.lifecycle import HostLifecycle
from purrcept_engine.app.panel import PanelController
from purrcept_engine.domain.activities.models import ActivityRecord
from purrcept_engine.domain.execution.models import ActionRecord
from purrcept_engine.domain.inputs.models import InputRecord, InputSource
from purrcept_engine.domain.model_requests.models import RequestRecord
from purrcept_engine.domain.subject.models import InputProcessing
from purrcept_engine.errors import HostStateError
from purrcept_engine.infrastructure.sqlite.projections import action_projection
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system.panels import PreviewFrame
from purrcept_engine.values import JsonObject, JsonValue

_logger = logging.getLogger("purrcept_engine.app.host")


class HostQueries:
    """为宿主提供原文、回执、控制和面板的只读查询。"""

    def __init__(self, state: EngineState, lifecycle: HostLifecycle) -> None:
        self.state = state
        self.lifecycle = lifecycle

    def _require_store(self, *, reject_closed: bool = False) -> Store:
        store = self.state.store
        if store is None or (reject_closed and self.state.phase == "closed"):
            raise HostStateError("宿主没有可查询的权威存储。")
        return store

    async def query_input(self, input_id: str) -> InputRecord | None:
        """查询已接管输入记录。"""
        return await self._require_store(reject_closed=True).get_input(input_id)

    async def query_native_input(self, source: InputSource) -> InputRecord | None:
        """按原生来源读取首次接管事实。"""
        return await self._require_store(reject_closed=True).get_native_input(source)

    async def query_processing(self, input_id: str) -> InputProcessing | None:
        """读取指定输入的当前处理阶段与版本。"""
        return await self._require_store().input_processing(input_id)

    async def query_action(self, action_id: str) -> ActionRecord | None:
        """读取包含迟到调用结论的完整行动记录。"""
        return await self._require_store().get_action(action_id)

    async def query_activity(self, activity_id: str) -> ActivityRecord | None:
        """读取活动控制、预算和核对状态。"""
        return await self._require_store().get_activity(activity_id)

    async def query_request(self, request_id: str) -> RequestRecord | None:
        """读取可还原的模型请求材料及传输尝试记录。"""
        return await self._require_store().get_request(request_id)

    async def query_model_usage(self, *, since: float) -> JsonObject:
        """读取当前宿主账本中从指定 Unix 时间戳开始的模型用量汇总。"""
        return await self._require_store(reject_closed=True).model_usage_statistics(since=since)

    def query_health(self) -> JsonObject:
        """读取当前处理资格和各组件故障快照。"""
        state = self.state
        owners = [state.startup_health, state.input_health, state.maintenance_health]
        subject = state.subject
        owners.extend(
            runtime.health for runtime in state.plugins.values() if runtime.health is not None
        )
        if subject is not None:
            owners.extend(
                (
                    subject.service.health,
                    subject.background.health,
                    subject.background.behavior_service.health,
                )
            )
        can_process = (
            state.phase == "running"
            and state.startup_stage is None
            and state.store is not None
            and state.store.authority_available
            and state.input_failure is None
            and state.maintenance_failure is None
            and (
                subject is None
                or (subject.service.failure is None and subject.background.failure is None)
            )
        )
        components: list[JsonValue] = [owner.snapshot() for owner in owners]
        if state.store is not None:
            components.append(state.store.health_snapshot())
        return {
            "instance_id": state.instance_id,
            "phase": state.phase,
            "can_process": can_process,
            "components": components,
            "startup_stage": state.startup_failed_stage or state.startup_stage,
            "startup_timings": dict(state.startup_timings),
        }

    async def query_chat(self, *, limit: int = 40) -> JsonObject:
        """读取 Web 后台对话调试台的近期消息与能力输出流水。"""
        store = self.state.store
        if store is None or self.state.startup_stage is not None:
            raise HostStateError("宿主尚未就绪，暂时无法读取对话流水。")
        feed = await store.chat_projection(limit=limit)
        subject = self.state.subject
        fg = None if subject is None else subject.service.session.snapshot()
        return {
            **feed,
            "console_enabled": self.state.config.console,
            "character_name": self.state.config.subject.character.name or "主体",
            "foreground_busy": bool(fg and fg.get("busy")),
            "can_process": bool(self.query_health().get("can_process")),
        }

    async def query_panel(self) -> JsonObject:
        """读取 Web 面板所需的运行概况与设备状态快照。"""
        store, panel = self.state.store, self.state.panel
        if store is None or panel is None or self.state.startup_stage is not None:
            raise HostStateError("宿主面板尚未就绪或已收回。")
        facts = await store.runtime_projection()
        devices = await self.panel_devices(panel, store)
        snapshot = self.lifecycle.query()
        subject = self.state.subject
        return {
            **facts,
            "health": self.query_health(),
            "instance_id": snapshot.instance_id,
            "host_version": snapshot.version,
            "phase": snapshot.phase,
            "pending_resources": list(snapshot.pending_resources),
            "authority_available": store.authority_available,
            "input_handoff_failure": None
            if self.state.input_failure is None
            else type(self.state.input_failure).__name__,
            "devices": devices,
            "foreground": None if subject is None else subject.service.session.snapshot(),
            "behavior_review": None
            if subject is None
            else subject.background.behavior_session.snapshot(),
            "analysis": None
            if subject is None
            else {
                "behavior_enabled": subject.background.settings.behavior_enabled,
                "behavior": subject.background.behavior_service.snapshot(),
            },
            "proactive_thinking": None if subject is None else subject.service.proactive.snapshot(),
            "notebooks": {
                name: runtime.snapshot()
                for name, runtime in self.state.plugins.items()
                if name in self.state.notebook_resources and runtime.snapshot is not None
            },
            "pending_limits": {
                "count": self.state.config.execution.max_pending_inputs,
                "logical_bytes": self.state.config.execution.pending_input_bytes,
            },
            "diagnostics": self.state.diagnostic_cleanup,
        }

    async def panel_devices(self, panel: PanelController, store: Store) -> list[JsonValue]:
        """为最近一次收音记录补充数据库内的实际处理阶段。"""
        devices = panel.devices()
        for device in devices:
            if not isinstance(device, dict) or device.get("kind") != "voice_input":
                continue
            state = device.get("state")
            if not isinstance(state, dict):
                continue
            history = state.get("history")
            if not isinstance(history, list) or not history:
                continue
            latest = history[-1]
            if not isinstance(latest, dict):
                continue
            input_id = latest.get("input_id")
            if not isinstance(input_id, str):
                continue
            processing = await store.input_processing(input_id)
            device["state"] = {
                **state,
                "latest_input": {
                    **latest,
                    "processing_stage": None if processing is None else processing.stage,
                    "processing_reason": None if processing is None else processing.reason,
                },
            }
        return devices

    async def query_control(self, request_id: str) -> JsonObject | None:
        """查询面板控制请求记录及关联行动或活动命令回执。"""
        store = self._require_store()
        record = await store.controls.get(request_id)
        if record is None:
            return None
        result = record.projection()
        if record.action_id is not None:
            action = await store.get_action(record.action_id)
            result["action"] = None if action is None else action_projection(action)
        if record.command_id is not None:
            command = await store.get_activity_command(record.command_id)
            result["activity_command"] = (
                None
                if command is None
                else {
                    "command_id": command.command_id,
                    "activity_id": command.activity_id,
                    "status": command.status,
                    "actual_state": command.actual_state,
                    "applied": command.applied,
                    "error": command.error,
                }
            )
        return result

    async def query_reviews(self, before: str | None = None, *, kind: str = "action") -> JsonObject:
        """分页读取可人工审查的行动列表。"""
        return await self._require_store().human_feedback.page(before, kind=kind)

    async def query_review(self, action_id: str) -> JsonObject | None:
        """读取单次行动的触发刺激、调用回执与人工评价历史。"""
        return await self._require_store().human_feedback.detail(action_id)

    async def query_review_media(self, action_id: str, media_id: str) -> tuple[bytes, str] | None:
        """读取指定审查关联的原始媒体字节与类型。"""
        return await self._require_store().human_feedback.media(action_id, media_id)

    def query_preview(self, target: str) -> PreviewFrame | None:
        """读取设备最新预览画面帧。"""
        if self.state.panel is None:
            raise HostStateError("面板尚未就绪。")
        return self.state.panel.preview(target)
