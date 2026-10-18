"""电脑纯配置与六项稳定模型能力，真实驱动只由显式connect创建。

电脑是主体下的设备父节点，浏览器作为独立子应用由自己的工厂装配。react
及旧宿主租约/内部配置入口不进入目录。测试通过make_factory注入离线驱动，
生产factory采用独立Windows进程，两者使用同一参数和资源契约。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from purrcept_engine.errors import ConfigError
from purrcept_engine.plugin_system import (
    CallContext,
    CallOutcome,
    CapabilityDeclaration,
    CapabilityInputError,
    DevicePanel,
    FunctionCapability,
    ObjectDeclaration,
    PanelControl,
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
    PreviewFrame,
    typed_factory,
)
from purrcept_engine.plugins.computer.driver import ComputerDriver, ProcessDriver
from purrcept_engine.plugins.computer.models import ComputerError, Target
from purrcept_engine.plugins.computer.runtime import ComputerRuntime, load_policy


class ComputerSettings(BaseModel):
    """本机设备路径与业务偏好位置，不建立人员身份或隐式连接。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    computer_id: str = Field(default="computer", pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    label: str = Field(default="宿主实体机", min_length=1, pattern=r"\S")
    preferences: Path = Field(default=Path(".purrcept/computer.json"), strict=False)
    screenshot_dir: Path = Field(default=Path(".purrcept/computer-screenshots"), strict=False)


class _Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Empty(_Parameters):
    pass


class _Screenshot(_Parameters):
    target: Target | None = Field(default=None, description="foreground_monitor、monitor或window。")
    monitor_id: str | None = Field(default=None, description="monitor目标的Windows显示器设备名。")


class _Observe(_Parameters):
    operation: Literal["start", "pause", "status"] = "status"


def make_factory(driver_factory: Callable[[], ComputerDriver]) -> PluginFactory:
    """按同一生产契约注入实际驱动，调用方不取得主体或任意数据库。

    Args:
        driver_factory: 为当前插件实例创建独立设备驱动的工厂。

    Returns:
        接收实例身份、原始配置及基准目录的纯配置工厂。
    """

    def declare(instance_id: str, settings: ComputerSettings, base_dir: Path) -> PluginBlueprint:
        """解析已校验配置，生成对象、能力和资源构造声明。"""
        preferences = settings.preferences
        if not preferences.is_absolute():
            preferences = base_dir / preferences
        screenshot_dir = settings.screenshot_dir
        if not screenshot_dir.is_absolute():
            screenshot_dir = base_dir / screenshot_dir
        object_id, path = instance_id + ":computer", "me." + settings.computer_id
        status = CapabilityDeclaration(
            instance_id + ":computer.status",
            object_id,
            "status",
            "查询电脑连接及观察状态。",
            _Empty,
            20.0,
            f"await {path}.status()",
            concurrency="parallel",
        )
        connect = CapabilityDeclaration(
            instance_id + ":computer.connect",
            object_id,
            "connect",
            "连接运行Engine的真实宿主电脑，读取真实应用与屏幕，不是虚拟桌面。",
            _Empty,
            25.0,
            f"await {path}.connect()",
            "必须显式调用后才能读取前台或截图。重复连接不创建新采集进程。",
        )
        disconnect = CapabilityDeclaration(
            instance_id + ":computer.disconnect",
            object_id,
            "disconnect",
            "断开宿主实体机并撤销连续观察。",
            _Empty,
            20.0,
            f"await {path}.disconnect()",
            "停止感知，不关闭电脑应用。",
            control=True,
        )
        foreground = CapabilityDeclaration(
            instance_id + ":computer.foreground",
            object_id,
            "foreground",
            "读取已连接宿主实体机的前台窗口、进程与显示器。",
            _Empty,
            20.0,
            f"await {path}.foreground()",
            "不隐式连接，不自动取图。",
        )
        screenshot = CapabilityDeclaration(
            instance_id + ":computer.screenshot",
            object_id,
            "screenshot",
            "取得已连接宿主实体机的当前截图，保存 JPEG 文件并交付原生图像。",
            _Screenshot,
            20.0,
            f'await {path}.screenshot(target="foreground_monitor")',
            "target=monitor时必须提供monitor_id；单次截图保留原始分辨率。"
            "回执 path 可交给 QQ send 的 images 或 files；自定义保存目录须在 QQ 媒体允许目录内。"
            "保存失败仍保留已取得图像和 evidence_ref，但不能声称文件已保存。",
        )
        observe = CapabilityDeclaration(
            instance_id + ":computer.observe",
            object_id,
            "observe",
            "开始、暂停或查询已连接宿主实体机的连续观察。",
            _Observe,
            20.0,
            f'await {path}.observe(operation="start")',
            "start创建新观察代次；pause暂停；status不采集。失败后须明确start继续。",
            control=True,
        )

        async def build(ports: PluginPorts) -> PluginRuntime:
            """借用本实例的有限端口，创建能力实现与插件资源所有者。"""
            evidence = ports.evidence
            if evidence is None:
                raise ConfigError("computer插件需要宿主原文/媒体端口。")
            policy = await asyncio.to_thread(load_policy, preferences)
            computer = ComputerRuntime(
                instance_id,
                driver_factory,
                ports.inputs,
                evidence,
                policy,
                preferences,
                screenshot_dir=screenshot_dir,
            )

            async def run_status(parameters: _Empty, context: CallContext) -> CallOutcome:
                """只读当前插件已取得的业务状态。"""
                del parameters, context
                return CallOutcome("confirmed", computer.snapshot())

            async def run_connect(parameters: _Empty, context: CallContext) -> CallOutcome:
                """明确取得电脑驱动连接，不自动开始持续观察。"""
                del parameters, context
                return CallOutcome("confirmed", await computer.connect())

            async def run_disconnect(parameters: _Empty, context: CallContext) -> CallOutcome:
                """明确停止当前电脑连接及相关观察借用。"""
                del parameters, context
                return CallOutcome("confirmed", await computer.disconnect())

            async def run_foreground(parameters: _Empty, context: CallContext) -> CallOutcome:
                """查询当前连接实际取得的前台窗口信息。"""
                del parameters, context
                try:
                    result = await computer.foreground()
                except ComputerError as error:
                    raise CapabilityInputError(str(error)) from error
                return CallOutcome("confirmed", result)

            async def run_screenshot(parameters: _Screenshot, context: CallContext) -> CallOutcome:
                """取得同库截图及已完成保存的本地文件路径。"""
                del context
                try:
                    return await computer.screenshot(parameters.target, parameters.monitor_id)
                except (ComputerError, ValidationError) as error:
                    raise CapabilityInputError(str(error)) from error

            async def run_observe(parameters: _Observe, context: CallContext) -> CallOutcome:
                """按明确策略开始持续观察或采用指定观察配置。"""
                del context
                try:
                    result = await computer.observe(parameters.operation)
                except ComputerError as error:
                    raise CapabilityInputError(str(error)) from error
                return CallOutcome("confirmed", result)

            def revision() -> str:
                """返回当前已取得业务状态的版本标记，供面板观察变化。"""
                state = computer.snapshot()
                return f"{state['connection_id']}:{state['generation']}:{state['observing']}"

            def preview() -> PreviewFrame | None:
                """读取已有设备画面，缺失时返回明确的不可用响应。"""
                frame = computer.preview()
                return (
                    None
                    if frame is None
                    else PreviewFrame(frame.data, "image/jpeg", frame.captured_at)
                )

            return PluginRuntime(
                (
                    FunctionCapability(status, run_status),
                    FunctionCapability(connect, run_connect),
                    FunctionCapability(disconnect, run_disconnect),
                    FunctionCapability(foreground, run_foreground),
                    FunctionCapability(screenshot, run_screenshot),
                    FunctionCapability(observe, run_observe),
                ),
                stop_inputs=computer.stop_inputs,
                close=computer.close,
                snapshot=computer.snapshot,
                prepare_request=computer.prepare_request,
                panel=DevicePanel(
                    "computer",
                    (
                        PanelControl("connect", "连接电脑", connect.capability_id),
                        PanelControl("disconnect", "断开电脑", disconnect.capability_id),
                        PanelControl(
                            "observe_start",
                            "开始观察",
                            observe.capability_id,
                            '{"operation":"start"}',
                        ),
                        PanelControl(
                            "observe_pause",
                            "暂停观察",
                            observe.capability_id,
                            '{"operation":"pause"}',
                        ),
                    ),
                    revision,
                    preview,
                ),
            )

        return PluginBlueprint(
            (
                ObjectDeclaration(
                    object_id, settings.computer_id, settings.label.strip(), "subject"
                ),
            ),
            (status, connect, disconnect, foreground, screenshot, observe),
            build,
            required_modules=("PIL",),
        )

    return typed_factory(ComputerSettings, declare)


factory = make_factory(ProcessDriver)
