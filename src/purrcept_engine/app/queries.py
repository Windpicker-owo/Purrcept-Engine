"""宿主权威事实与有限缓存的只读查询，不启动模型、设备或行动。

生命周期快照来自 HostLifecycle；其他查询借用尚未释放的账本与面板。
收尾期间仍可查真实结果，查询不会延长设备会话或重放未知外部效果。
"""

from __future__ import annotations

import logging

from purrcept_engine.app.host_state import EngineState
from purrcept_engine.app.lifecycle import HostLifecycle
from purrcept_engine.app.panel import PanelController
from purrcept_engine.domain.activities.models import ActivityRecord
from purrcept_engine.domain.execution.models import ActionRecord
from purrcept_engine.domain.inputs.models import (
    InputRecord,
    InputSource,
)
from purrcept_engine.domain.model_requests.models import RequestRecord
from purrcept_engine.domain.subject.models import InputProcessing
from purrcept_engine.errors import HostStateError
from purrcept_engine.infrastructure.sqlite.projections import action_projection
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system.panels import PreviewFrame
from purrcept_engine.values import JsonObject, JsonValue

_logger = logging.getLogger("purrcept_engine.app.host")


class HostQueries:
    """为宿主提供原文、回执、控制和面板的只读查询。

    Note:
        与宿主共用资源台账和生命周期；关闭期间仅借用尚未释放的库。查询
        不获取行动资格、不启动模型或设备，权威读取失败不会伪装成缺失结果。
    """

    def __init__(self, state: EngineState, lifecycle: HostLifecycle) -> None:
        """固定应用实例借用。

        Args:
            state: 此宿主唯一的内部资源台账，不跨实例或插件共享。
            lifecycle: 同一宿主的阶段和资源归还所有者。

        Note:
            资源创建、观察和归还不跨宿主共享。
        """
        self.state = state
        self.lifecycle = lifecycle

    async def query_input(self, input_id: str) -> InputRecord | None:
        """查询已接管输入。

        Args:
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。

        Returns:
            首次接管的完整输入原文、来源与媒体引用。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。

        Raises:
            HostStateError: 权威库尚未取得或宿主已完全关闭。
            StorageError: 权威读取失败或已保存记录损坏。

        Note:
            关闭收尾期间仍可使用尚未释放的存储。
        """
        store = self.state.store
        if store is None or self.state.phase == "closed":
            raise HostStateError("宿主没有可查询的权威存储。")
        return await store.get_input(input_id)

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
        store = self.state.store
        if store is None or self.state.phase == "closed":
            raise HostStateError("宿主没有可查询的权威存储。")
        return await store.get_native_input(source)

    async def query_processing(self, input_id: str) -> InputProcessing | None:
        """读取宿主实际输入阶段，取得、接管及处理结束分别表达。

        Args:
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。

        Returns:
            输入实际处理阶段、版本和关联轮次。 未取得对应记录或素材时返回 None；读取故障仍抛出。

        Raises:
            HostStateError: 权威库尚未取得或宿主已完全关闭。
            StorageError: 权威读取失败或已保存记录损坏。
        """
        store = self.state.store
        if store is None:
            raise HostStateError("宿主没有可查询的权威存储。")
        return await store.input_processing(input_id)

    async def query_action(self, action_id: str) -> ActionRecord | None:
        """读取包括迟到调用结论的权威事实，关闭收尾期间仍可查询。

        Args:
            action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。

        Returns:
            已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。
            未取得对应记录或素材时返回 None；读取故障仍抛出。

        Raises:
            HostStateError: 权威库尚未取得或宿主已完全关闭。
            StorageError: 权威读取失败或已保存记录损坏。
        """
        store = self.state.store
        if store is None:
            raise HostStateError("宿主没有可查询的权威存储。")
        return await store.get_action(action_id)

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
        store = self.state.store
        if store is None:
            raise HostStateError("宿主没有可查询的权威存储。")
        return await store.get_activity(activity_id)

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
        store = self.state.store
        if store is None:
            raise HostStateError("宿主没有可查询的权威存储。")
        return await store.get_request(request_id)

    async def query_panel(self) -> JsonObject:
        """读取有限真实概况与设备缓存状态。

        Returns:
            业务对象，按实际结果提供 instance_id、host_version、phase、pending_resources、
            authority_available、input_handoff_failure、devices、foreground、reflection、
            knowledge 等 字段。

        Raises:
            HostStateError: 宿主面板尚未就绪或已收回。

        Note:
            多页面查询不启动模型或设备。
        """
        store, panel = self.state.store, self.state.panel
        if store is None or panel is None:
            raise HostStateError("宿主面板尚未就绪或已收回。")
        facts = await store.runtime_projection()
        devices = await self.panel_devices(panel, store)
        snapshot = self.lifecycle.query()
        subject = self.state.subject
        return {
            **facts,
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
            "reflection": None if subject is None else subject.background.session.snapshot(),
            "knowledge": None
            if self.state.knowledge_resources is None
            else {
                "semantic_enabled": self.state.config.knowledge.embedding is not None,
                "pending_resources": self.state.knowledge_resources.pending_resources,
                "index_failure": None
                if self.state.knowledge_resources.failure is None
                else type(self.state.knowledge_resources.failure).__name__,
            },
            "pending_limits": {
                "count": self.state.config.execution.max_pending_inputs,
                "logical_bytes": self.state.config.execution.pending_input_bytes,
            },
            "diagnostics": self.state.diagnostic_cleanup,
        }

    async def panel_devices(self, panel: PanelController, store: Store) -> list[JsonValue]:
        """为最近一次收音补权威处理状态，不调用插件能力或重新投递声音。

        Args:
            panel: 本机有限控制与预览的投影服务。
            store: 应用借出的领域存储端口，实际连接和关闭归资源所有者。

        Note:
            每个收音插件只查最近一项，保持轮询成本有界。插件历史中的 admitted
            仅是当时的接管事实，不能据此声称当前仍在等待或已经回应。
        """
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
        """查询接管记录及当前真实业务回执，未知/迟到确认仍关联原身份。

        Args:
            request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。

        Raises:
            HostStateError: 权威库尚未取得或宿主已完全关闭。
            StorageError: 权威读取失败或已保存记录损坏。
        """
        store = self.state.store
        if store is None:
            raise HostStateError("宿主没有可查询的权威存储。")
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

    def query_preview(self, target: str) -> PreviewFrame | None:
        """只读已取得画面。

        Args:
            target: 本次操作明确指定的业务目标身份。

        Returns:
            当前已经取得的完整预览画面，不触发额外采集。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。

        Raises:
            HostStateError: 面板尚未就绪。

        Note:
            查询不额外截图或延长设备会话。
        """
        if self.state.panel is None:
            raise HostStateError("面板尚未就绪。")
        return self.state.panel.preview(target)
