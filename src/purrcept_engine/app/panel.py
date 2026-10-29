"""本机面板的有限控制所有者，借用真实宿主调用与同库身份账本。

页面提交显式快捷操作或针对真实行动的人工评价，不能发送 Python 或模型参数。控制身份先
持久接管，再交到原能力、活动或主体选项所有者；断开/取消等待不撤销已接受工作。
世界回执仍由执行器和活动服务保存。人工评价只交给可靠反馈，不改写执行状态；
主体选项只改变本次运行的调度，不伪造行动回执。
面板保存控制关联及观察终态，不拥有模型或空闲计时。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from purrcept_engine.domain.activities.contracts import ActivityError
from purrcept_engine.domain.activities.service import ActivityService
from purrcept_engine.domain.controls.models import ControlError, ControlRecord
from purrcept_engine.domain.execution.catalog import Catalog
from purrcept_engine.domain.execution.errors import ExecutionBusy, ExecutionLimitError
from purrcept_engine.domain.execution.models import ActionRecord
from purrcept_engine.domain.execution.providers import CapabilityInputError
from purrcept_engine.domain.subject.human_feedback import HumanFeedback
from purrcept_engine.domain.subject.proactive import ProactiveThinking
from purrcept_engine.errors import HostStateError
from purrcept_engine.infrastructure.sqlite.controls import ControlLedger
from purrcept_engine.infrastructure.sqlite.human_feedback import HumanFeedbackLedger
from purrcept_engine.plugin_system.contracts import PluginRuntime
from purrcept_engine.plugin_system.panels import PanelControl, PreviewFrame
from purrcept_engine.values import JsonObject, JsonValue, encode_json


class ControlRequest(BaseModel):
    """本机 HTTP/嵌入边界，页面版本与宿主实例不能由默认值补齐。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    instance_id: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=128)
    operation: str = Field(min_length=1, max_length=64)
    expected_revision: str = Field(min_length=1, max_length=256)
    arguments: JsonObject = Field(default_factory=dict)


class _Invoke(Protocol):
    """Host 注入的唯一结构化执行入口；身份回调在首次 await 前发生。"""

    async def __call__(
        self,
        path: str,
        arguments: JsonObject,
        *,
        input_id: str | None = None,
        can_start: Callable[[], bool] | None = None,
        on_admitted: Callable[[str], None] | None = None,
    ) -> ActionRecord:
        """共用真实能力/schema/Provider 和原回执，不先请求语言模型。

        Args:
            path: 此操作定位的明确路径。
            arguments: 按已装配参数模型校验的能力关键字参数。
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。 None
                表示无单一输入因果关联。
            can_start: 实际派发前重新检查的资格回调，等待后仍须复核。 None
                表示不附加调用方资格条件，宿主固定门槛仍生效。
            on_admitted: 首次等待前接收执行器分配身份的同步回调，不授予重投资格。 None
                表示不向调用方另行通知接管身份。

        Returns:
            已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。
        """
        ...


class PanelController:
    """Host 拥有的固定控制集合，查询不会启动设备或创建后台工作。

    最多16个控制观察任务，重复 request_id 合并等待；正式去重在同 Store
    事务内完成。首次接管的所有者负责取得执行器身份和保存终态。关闭只拒绝
    新控制，已接管调用继续依赖 execution 的实际收尾，然后归还提交任务。
    """

    def __init__(
        self,
        instance_id: str,
        catalog: Catalog,
        plugins: Mapping[str, PluginRuntime],
        ledger: ControlLedger,
        invoke: _Invoke,
        activities: ActivityService,
        proactive: ProactiveThinking,
        human_feedback: HumanFeedbackLedger,
    ) -> None:
        """绑定已构造插件及原业务端口，不另建能力表、设备连接或数据库。

        Args:
            instance_id: 本次装配中唯一的插件或宿主实例身份。
            catalog: 全部已启用插件共同生成的冻结目录。
            plugins: 本实例已装配的插件运行资源，不执行隐式发现。
            ledger: 当前业务专用账本，不提供任意数据库操作。
            invoke: 借用共享目录与执行器调用单能力的有限入口。
            activities: 此宿主唯一的活动状态提交服务。
            proactive: 主体拥有的空闲思考开关，不取得模型或世界能力。
            human_feedback: 同库人工评价端口，复用原反馈与反思消费者。

        Raises:
            ControlError: 插件面板重复操作或借用了其他插件能力；
                插件快捷控制参数未在能力契约中声明，或占用宿主主动思考控制身份。
        """
        self.instance_id, self.catalog = instance_id, catalog
        self.plugins, self.ledger = plugins, ledger
        self.invoke, self.activities = invoke, activities
        self.proactive = proactive
        self.human_feedback = human_feedback
        self._tasks: dict[str, tuple[str, asyncio.Task[ControlRecord]]] = {}
        self._closing = False
        paths = {item.declaration.capability_id: item for item in catalog.bindings.values()}
        self._controls: dict[tuple[str, str], tuple[str, PanelControl]] = {}
        for instance, runtime in plugins.items():
            panel = runtime.panel
            if panel is None:
                continue
            own = {item.declaration.capability_id for item in runtime.capabilities}
            for control in panel.controls:
                key = (instance, control.key)
                if key == ("subject", "set_proactive_thinking"):
                    raise ControlError("插件快捷控制与宿主主动思考选项冲突。")
                if key in self._controls or control.capability_id not in own:
                    raise ControlError("插件面板重复操作或借用了其他插件能力。")
                binding = paths[control.capability_id]
                fixed = control.arguments({})
                properties = binding.declaration.parameters.model_fields
                if any(name not in properties for name in (*fixed, *control.fields)):
                    raise ControlError("插件快捷控制参数未在能力契约中声明。")
                self._controls[key] = (binding.path, control)

    def devices(self) -> list[JsonValue]:
        """读取插件已提交状态及允许操作，不启动/恢复设备，也不主动采集画面。"""
        values: list[JsonValue] = []
        for instance, runtime in self.plugins.items():
            panel = runtime.panel
            state = {} if runtime.snapshot is None else runtime.snapshot()
            values.append(
                {
                    "instance_id": instance,
                    "kind": None if panel is None else panel.kind,
                    "revision": None if panel is None else panel.revision(),
                    "state": state,
                    "controls": []
                    if panel is None
                    else [
                        {"key": item.key, "title": item.title, "fields": list(item.fields)}
                        for item in panel.controls
                    ],
                    "has_preview": panel is not None and panel.preview is not None,
                }
            )
        return values

    def preview(self, target: str) -> PreviewFrame | None:
        """借出当前缓存，缺失返回 None。

        Args:
            target: 本次操作明确指定的业务目标身份。

        Returns:
            当前已经取得的完整预览画面，不触发额外采集。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。

        Raises:
            ControlError: 该设备没有缓存预览。

        Note:
            请求本身不打开设备或截图。
        """
        runtime = self.plugins.get(target)
        if runtime is None or runtime.panel is None or runtime.panel.preview is None:
            raise ControlError("该设备没有缓存预览。", code="control_target")
        return runtime.panel.preview()

    async def submit(self, request: ControlRequest) -> ControlRecord:
        """合并同身份观察并交给持久去重。

        Args:
            request: 包含稳定请求身份、目标和操作参数的显式控制。

        Returns:
            稳定请求身份的受理记录与当前实际业务关联。

        Raises:
            ControlError: 典型拒绝原因：宿主已停止面板控制接入；页面实例已过期，请刷新；
                控制身份对应不同请求；面板控制观察名额已满。

        Note:
            取消等待不取消控制所有者。
        """
        if self._closing:
            raise ControlError("宿主已停止面板控制接入。", code="host_closing")
        if request.instance_id != self.instance_id:
            raise ControlError("页面实例已过期，请刷新。", code="stale_instance")
        # 配置模型的 frozen 不冻结嵌套容器。接管前取得独立参数快照，不能让
        # HTTP/嵌入调用者跨 await 改写已记录意图或实际执行值。
        request = ControlRequest.model_validate_json(request.model_dump_json())
        payload = cast(
            JsonObject, request.model_dump(mode="json", exclude={"request_id", "instance_id"})
        )
        encoded = encode_json(payload).decode()
        pending = self._tasks.get(request.request_id)
        if pending is not None:
            if pending[0] != encoded:
                raise ControlError("控制身份对应不同请求。", code="control_conflict")
            return await asyncio.shield(pending[1])
        if len(self._tasks) >= 16:
            raise ControlError("面板控制观察名额已满。", code="control_busy")
        task = asyncio.create_task(self._run(request, payload), name="panel-control")
        self._tasks[request.request_id] = (encoded, task)

        def finished(owner: asyncio.Task[ControlRecord]) -> None:
            """收回已结束的工作所有者，并保留原任务中的真实故障。"""
            self._tasks.pop(request.request_id, None)
            if not owner.cancelled():
                owner.exception()

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def _run(self, request: ControlRequest, payload: JsonObject) -> ControlRecord:
        """先接管身份，再核验真实版本和交接；已接管关联提交必须实际收回。"""
        record, fresh = await self.ledger.admit(request.request_id, self.instance_id, payload)
        if not fresh:
            return record
        link_task: asyncio.Task[None] | None = None
        try:
            if self._closing:
                raise ControlError("宿主开始关闭，请查询已接管控制。", code="host_closing")
            if request.target == "subject" and request.operation == "set_proactive_thinking":
                if set(request.arguments) != {"enabled"}:
                    raise ControlError("主体面板只允许设置主动思考开关。", code="control_target")
                enabled = request.arguments["enabled"]
                if not isinstance(enabled, bool):
                    raise ControlError("主动思考开关必须是布尔值。")
                await self.proactive.set_enabled(
                    enabled, expected_revision=request.expected_revision
                )
            elif request.target.startswith("action:"):
                if request.operation != "review":
                    raise ControlError("行动审查只允许提交评价。", code="control_target")
                try:
                    value = HumanFeedback.model_validate(request.arguments)
                except ValidationError as error:
                    raise ControlError("评价须选择好或坏，留言最多 4000 字。") from error
                await self.human_feedback.submit(
                    request.request_id,
                    request.target.removeprefix("action:"),
                    request.expected_revision,
                    value,
                )
            elif request.target.startswith("activity:"):
                await self._activity(request)
            else:
                selected = self._controls.get((request.target, request.operation))
                runtime = self.plugins.get(request.target)
                if selected is None or runtime is None or runtime.panel is None:
                    raise ControlError("目标没有该快捷控制。", code="control_target")
                path, declaration = selected
                panel = runtime.panel
                if panel.revision() != request.expected_revision:
                    raise ControlError("设备状态已变化，请刷新。", code="stale_revision")
                try:
                    arguments = declaration.arguments(request.arguments)
                except ValueError as error:
                    raise ControlError(str(error)) from error
                stale = False

                def eligible() -> bool:
                    nonlocal stale
                    stale = panel.revision() != request.expected_revision
                    return not self._closing and not stale

                def admitted(action_id: str) -> None:
                    nonlocal link_task
                    link_task = asyncio.create_task(
                        self.ledger.link(request.request_id, action_id=action_id)
                    )

                await self.invoke(path, arguments, can_start=eligible, on_admitted=admitted)
                if link_task is not None:
                    await asyncio.shield(link_task)
                    link_task = None
                if stale:
                    raise ControlError(
                        "排队期间设备状态已变化；操作没有开始。", code="stale_revision"
                    )
            return await self.ledger.finish(request.request_id)
        except (
            ControlError,
            ActivityError,
            CapabilityInputError,
            ExecutionBusy,
            ExecutionLimitError,
        ) as error:
            return await self.ledger.finish(
                request.request_id,
                error_code=error.code
                if isinstance(error, (ControlError, ActivityError))
                else type(error).__name__,
                error=str(error),
            )
        except HostStateError:
            if not self._closing:
                raise
            return await self.ledger.finish(
                request.request_id, error_code="host_closing", error="宿主已停止新的派发。"
            )
        finally:
            if link_task is not None:
                await asyncio.shield(link_task)

    async def _activity(self, request: ControlRequest) -> None:
        """活动控制只允许暂停/恢复/结束，版本在原状态提交锁内核验。"""
        if request.operation not in ("pause", "resume", "end") or request.arguments:
            raise ControlError("面板只提供暂停、恢复和结束活动。")
        try:
            version = int(request.expected_revision)
        except ValueError:
            raise ControlError("活动版本无效。") from None
        command = await self.activities.command(
            "panel-" + request.request_id,
            request.operation,
            activity_id=request.target.removeprefix("activity:"),
            expected_version=version,
        )
        await self.ledger.link(request.request_id, command_id=command.command_id)

    async def close(self) -> None:
        """拒绝新控制并等待原业务观察/提交，不随页面断开取消实际工作。

        Raises:
            BaseExceptionGroup: 面板控制收尾失败。
        """
        self._closing = True
        results = await asyncio.gather(
            *(item[1] for item in tuple(self._tasks.values())), return_exceptions=True
        )
        errors = [item for item in results if isinstance(item, BaseException)]
        if errors:
            raise BaseExceptionGroup("面板控制收尾失败。", errors)
