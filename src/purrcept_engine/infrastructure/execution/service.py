"""接管一次受限行动，拥有 Worker、有限派发和独立真实回执的完整寿命。

ExecutionService 在脚本接管前预留 Worker 名额，结构化单次调用直接预留调用
名额；两者先保存意图和目录版本，再共用冻结能力的派发。普通等待按来源轮转，
控制调用使用独立名额；实际 Provider 返回前不释放名额或目标占用。
调用者取消只结束观察，关闭则撤销新
派发并终止本服务的 Worker，等待已经进入 Provider 的调用保存真实结果。

本模块不在宿主执行行动代码，不重放未知效果，也不拥有插件和数据库的关闭。
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from purrcept_engine.domain.execution.catalog import Catalog
from purrcept_engine.domain.execution.errors import (
    ExecutionBusy,
    ExecutionClosed,
    ExecutionLimitError,
)
from purrcept_engine.domain.execution.models import ActionOutcome, ActionRecord
from purrcept_engine.domain.execution.ports import ExecutionJournal
from purrcept_engine.domain.execution.providers import (
    CallContext,
    CapabilityImplementation,
    bind_implementations,
)
from purrcept_engine.domain.execution.settings import ExecutionSettings
from purrcept_engine.errors import ConfigError
from purrcept_engine.infrastructure.execution.call_dispatcher import CallDispatcher
from purrcept_engine.infrastructure.execution.state import (
    CapabilityCall,
    ScriptAction,
    StructuredAction,
)
from purrcept_engine.infrastructure.execution.worker_session import WorkerSession
from purrcept_engine.values import ActionProtocolError, JsonObject, decode_object, encode_json


class ExecutionService:
    """由 app 创建的执行资源所有者，所有公开异步操作使用同一事件循环。

    构造检查声明与实际实现精确对应，不打开设备。execute/invoke 接管后独立运行，
    调用方可通过 query 查询回执；取消该等待不会取消已开始的行动。close 可
    重复等待同一收尾任务，取消 close 等待不撤销收尾，不重置宿主关闭期限。
    插件与 Store 必须保留到本服务真正关闭，内部故障原样报告。
    """

    def __init__(
        self,
        catalog: Catalog,
        implementations: tuple[CapabilityImplementation, ...],
        store: ExecutionJournal,
        settings: ExecutionSettings,
        *,
        scratch_root: Path,
    ) -> None:
        """冻结实现对应关系。

        Args:
            catalog: 全部已启用插件共同生成的冻结目录。
            implementations: 与冻结目录逐项对应的宿主能力实现。
            store: 应用借出的领域存储端口，实际连接和关闭归资源所有者。
            settings: 当前组件已校验的配置或纯声明。
            scratch_root: 每次隔离行动建立临时目录时使用的根路径。

        Raises:
            ValueError: 行动临时根目录必须是绝对路径。
            ConfigError: 目录能力与实际实现缺失或多余；能力实现与冻结声明不一致。

        Note:
            临时目录必须位于显式给出的绝对根路径下。
        """
        if not scratch_root.is_absolute():
            raise ValueError("行动临时根目录必须是绝对路径。")
        self.implementations = bind_implementations(implementations)
        declarations = {
            binding.declaration.capability_id: binding.declaration
            for binding in catalog.bindings.values()
        }
        if set(declarations) != set(self.implementations):
            raise ConfigError("目录能力与实际实现缺失或多余。")
        if any(
            self.implementations[identifier].declaration != declaration
            for identifier, declaration in declarations.items()
        ):
            raise ConfigError("能力实现与冻结声明不一致。")

        self.catalog = catalog
        self.store = store
        self.settings = settings
        self.scratch_root = scratch_root
        self.actions: dict[str, ScriptAction | StructuredAction] = {}
        self.owned_actions: set[asyncio.Task[ActionRecord]] = set()
        self.owned_calls: set[asyncio.Task[None]] = set()
        self.invocation_releases: dict[str, asyncio.Future[None]] = {}
        self.pending_calls = 0
        self.pending_ordinary = 0
        self.pending_controls = 0
        self.ordinary: dict[str, deque[CapabilityCall]] = {}
        self.rotation: deque[str] = deque()
        self.controls: deque[CapabilityCall] = deque()
        self.ordinary_active = 0
        self.control_active = 0
        self.targets: set[str] = set()
        self.dispatch_timer: asyncio.TimerHandle | None = None
        self.closed = False
        self.close_task: asyncio.Task[None] | None = None
        self.failures: list[BaseException] = []

        self.worker_session = WorkerSession(self)
        self.call_dispatcher = CallDispatcher(self)

    @property
    def pending_resources(self) -> int:
        """返回行动所有者与未收尾调用数，供宿主关闭超时后报告资源状态。"""
        return len(self.actions) + self.pending_calls

    async def _finish_action(
        self,
        action_id: str,
        *,
        state: ActionOutcome,
        result_json: str | None,
        output: str,
        error: str | None,
    ) -> None:
        """提交真实终态后报告行动摘要；已有逐调用 ERROR 不在此重复升级。"""
        await self.store.finish_action(
            action_id, state=state, result_json=result_json, output=output, error=error
        )
        action = await self.store.get_action(action_id)
        if action is None:
            raise RuntimeError("已结束行动缺少权威结果。")
        failures = sum(call.state == "failed" for call in action.calls)
        unknown = sum(call.state == "unknown" for call in action.calls)
        level = logging.ERROR if state in ("failed", "timed_out") and not failures else logging.INFO
        if unknown and level == logging.INFO:
            level = logging.WARNING
        logging.getLogger(__name__).log(
            level,
            "行动已结束。",
            extra={
                "action_id": action_id,
                "outcome": state,
                "calls": len(action.calls),
                "failed_calls": failures,
                "unknown_calls": unknown,
                "duration_ms": max(
                    0.0, (action.finished_at or action.created_at) - action.created_at
                )
                * 1000,
            },
        )

    async def execute(
        self,
        source: str,
        *,
        input_id: str | None = None,
        can_start: Callable[[], bool] | None = None,
        presented_call_ids: frozenset[str] = frozenset(),
    ) -> ActionRecord:
        """接管完整源码后等待整体结果，等待取消不撤销该行动的所有权。

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
            ExecutionClosed: 执行服务已关闭接管入口。
            ExecutionBusy: 行动 Worker 名额已满。
            TypeError: 行动源码必须是字符串。
            ExecutionLimitError: 行动源码不是有效 UTF-8；行动源码超过字节额度。
            ValueError: 输入关联必须是非空身份；模型回执交付资格必须是不可变的非空调用身份集合。

        Note:
            can_start 只在实际 execute 帧之前核验生成资格。派发之后资格变化
            不终止已开始的独立行动；服务关闭与自身资源额度仍可以停止 Worker。
            presented_call_ids 冻结接管时的真实回执交付资格，脚本不能扩大此集合。
        """
        if self.closed:
            raise ExecutionClosed("执行服务已关闭接管入口。")
        if sum(isinstance(action, ScriptAction) for action in self.actions.values()) >= (
            self.settings.max_workers
        ):
            raise ExecutionBusy("行动 Worker 名额已满。")
        if not isinstance(source, str):
            raise TypeError("行动源码必须是字符串。")
        try:
            source_bytes = len(source.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ExecutionLimitError("行动源码不是有效 UTF-8。") from error
        if source_bytes > self.settings.source_bytes:
            raise ExecutionLimitError("行动源码超过字节额度。")
        if input_id is not None and (not isinstance(input_id, str) or not input_id):
            raise ValueError("输入关联必须是非空身份。")
        if not isinstance(presented_call_ids, frozenset) or any(
            not isinstance(identity, str) or not identity for identity in presented_call_ids
        ):
            raise ValueError("模型回执交付资格必须是不可变的非空调用身份集合。")

        loop = asyncio.get_running_loop()
        action = ScriptAction(
            uuid4().hex,
            source,
            input_id,
            loop.time() + self.settings.action_seconds,
            can_start,
            asyncio.Queue(maxsize=self.settings.max_calls + 2),
            presented_call_ids=presented_call_ids,
        )
        # 名额在第一次 await 前取得；两项并发接管不能同时看到最后一个空位。
        self.actions[action.action_id] = action
        task = asyncio.create_task(self.run_action(action), name=f"action:{action.action_id}")
        self.owned_actions.add(task)
        task.add_done_callback(self.action_done)
        return await asyncio.shield(task)

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
        """直接接管单能力意图，共用派发和账本，不启动 Worker 或宿主代码。

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
            ExecutionClosed: 执行服务已关闭接管入口。
            TypeError: 能力路径必须是字符串；能力参数必须是 JSON 对象。
            ExecutionLimitError: 能力参数超过字节额度。
            ValueError: 输入关联必须是非空身份；调用等待期限必须是有限正数。
            ExecutionBusy: 能力调用等待名额已满。
            BaseExceptionGroup: 调用身份交接与已接管所有者收尾均失败。

        Note:
            参数在接管前冻结为标准 JSON，业务 schema 仍仅在 Provider 边界解析。
            所有等待都计入固定期限；超时首版未知结束观察，迟到确认继续原回执链。
            调用方取消等待不改变本服务对已接管意图及实际 Provider 的所有权。
            before_start 使用当前权威资格收紧期限；on_admitted 在首次等待前交出
            实际身份，使观察失败的所有者仍能等待真正归还，而不包装原型故障。
        """
        if self.closed:
            raise ExecutionClosed("执行服务已关闭接管入口。")
        if not isinstance(path, str):
            raise TypeError("能力路径必须是字符串。")
        binding = self.catalog.resolve(path)
        if not isinstance(arguments, dict):
            raise TypeError("能力参数必须是 JSON 对象。")
        encoded = encode_json(arguments)
        if len(encoded) > self.settings.call_arguments_bytes:
            raise ExecutionLimitError("能力参数超过字节额度。")
        frozen_arguments = decode_object(encoded)
        if input_id is not None and (not isinstance(input_id, str) or not input_id):
            raise ValueError("输入关联必须是非空身份。")
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("调用等待期限必须是有限正数。")

        loop = asyncio.get_running_loop()
        seconds = min(self.settings.action_seconds, binding.declaration.timeout_seconds)
        if timeout_seconds is not None:
            seconds = min(seconds, timeout_seconds)
        action = StructuredAction(
            uuid4().hex,
            encode_json(
                {"kind": "structured_call", "path": path, "arguments": frozen_arguments}
            ).decode("utf-8"),
            input_id,
            loop.time() + seconds,
            can_start,
            loop.create_future(),
            before_start=before_start,
            start_ready=None if before_start is None else loop.create_future(),
        )
        call = CapabilityCall(
            action,
            1,
            binding,
            frozen_arguments,
            CallContext(f"{action.action_id}:1", action.action_id, action.deadline, input_id),
            not binding.declaration.control,
        )
        # 在首次 await 前取得名额，不能把每项未受理意图变成隐藏等待任务。
        if not self.call_dispatcher.reserve_call(call):
            raise ExecutionBusy("能力调用等待名额已满。")
        self.invocation_releases[action.action_id] = loop.create_future()
        self.actions[action.action_id] = action
        task = asyncio.create_task(
            self.run_structured(call, encoded.decode("utf-8")), name=f"call:{action.action_id}"
        )
        self.owned_actions.add(task)
        task.add_done_callback(self.action_done)
        if on_admitted is not None:
            try:
                on_admitted(action.action_id)
            except BaseException as callback_error:
                # 身份回调是所有者交接，故障不能让已预留的行动继续产生效果。
                # 在首次 await 前撤销资格，收回已接管资源后保留原始回调异常。
                self.stop(action, "AdmissionCallbackFailed")
                try:
                    await asyncio.shield(task)
                except BaseException as cleanup_error:
                    raise BaseExceptionGroup(
                        "调用身份交接与已接管所有者收尾均失败。", [callback_error, cleanup_error]
                    ) from None
                raise
        return await asyncio.shield(task)

    async def wait_invocation_released(self, action_id: str) -> None:
        """只等待真实结构化资源，不依赖可能失败的权威查询证明归还。

        Args:
            action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。

        Raises:
            ValueError: 调用归还查询必须使用非空行动身份。

        Note:
            身份必须来自本实例的 invoke/on_admitted；插件不能直接访问此端口。
            Future 仅在资源实际占用时登记，归还即移除。没有登记不推断效果或
            入账成功，特别是意图提交失败时可能尚无 SQL 行，也须能收回活动步骤。
        """
        if not isinstance(action_id, str) or not action_id:
            raise ValueError("调用归还查询必须使用非空行动身份。")
        released = self.invocation_releases.get(action_id)
        if released is not None:
            await asyncio.shield(released)

    async def run_structured(self, call: CapabilityCall, arguments_json: str) -> ActionRecord:
        """先入账意图再排队。

        Args:
            call: 当前真实能力或 Core 工具调用的冻结材料。
            arguments_json: 已编码为严格 JSON 对象的原始能力参数。

        Returns:
            已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。

        Raises:
            RuntimeError: 结构化行动所有者类型错误；已接管结构化行动缺少权威记录。
            BaseExceptionGroup: 结构化行动原始故障与权威结束提交均失败。

        Note:
            首版真实回执结束行动，Provider 所有权独立保留。
        """
        action = call.action
        if not isinstance(action, StructuredAction):
            raise RuntimeError("结构化行动所有者类型错误。")
        admitted = False
        queued = False
        try:
            await self.store.begin_action(
                action.action_id,
                catalog_version=self.catalog.version,
                source=action.source,
                input_id=action.input_id,
            )
            admitted = True
            await self.store.begin_call(
                call.context.call_id,
                action_id=action.action_id,
                sequence=1,
                path=call.binding.path,
                target_id=call.binding.declaration.object_id,
                arguments_json=arguments_json,
            )
            self.call_dispatcher.enqueue(call)
            queued = True
            self.call_dispatcher.dispatch()
            receipt = await action.receipt

            state: ActionOutcome = "completed" if receipt.state == "confirmed" else "failed"
            error = receipt.error
            if action.stop_error is not None:
                state = (
                    "cancelled"
                    if action.stop_error in {"ServiceClosed", "GenerationRevoked"}
                    else "failed"
                )
                error = action.stop_error
            elif action.timed_out or (
                receipt.state == "not_started"
                and asyncio.get_running_loop().time() >= action.deadline
            ):
                state = "timed_out"
            await self._finish_action(
                action.action_id,
                state=state,
                result_json=receipt.result_json,
                output="",
                error=error,
            )
        except BaseException as error:
            # 这是已接管所有者的清理边界。权威故障原样停止后续派发，不能用
            # 一个内存失败结果伪装成已经成功入账的行动。
            self.closed = True
            for owner in self.actions.values():
                self.stop(owner, "ExecutionInternalFailure")
            self.call_dispatcher.dispatch()
            if admitted:
                try:
                    await self._finish_action(
                        action.action_id,
                        state="failed",
                        result_json=None,
                        output="",
                        error=f"{type(error).__name__}: {error}",
                    )
                except BaseException as record_error:
                    raise BaseExceptionGroup(
                        "结构化行动原始故障与权威结束提交均失败。", [error, record_error]
                    ) from None
            raise
        finally:
            action.eligible = False
            self.actions.pop(action.action_id, None)
            if not queued:
                self.call_dispatcher.release_call(call)
        record = await self.store.get_action(action.action_id)
        if record is None:
            raise RuntimeError("已接管结构化行动缺少权威记录。")
        return record

    async def query(self, action_id: str) -> ActionRecord | None:
        """查询权威行动与调用回执，不从 Worker 的自报状态补造效果。

        Args:
            action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。

        Returns:
            已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。
            未取得对应记录或素材时返回 None；读取故障仍抛出。
        """
        return await self.store.get_action(action_id)

    def stop_dispatch(self) -> None:
        """立即撤新派发资格，已开始 Provider 的真实效果仍独立收尾。

        Note:
            只改变当前循环上的资格，不等待数据库、不操作进程；Host 可在
            发布 closing 的同一同步段完成撤销。队列、Worker 和 Provider
            的资源归还仍由唯一 close 所有者执行。
        """
        self.closed = True

    async def close(self) -> None:
        """停止新入口与新调用，等待本服务持有的实际进程和 Provider 收尾。

        Note:
            宿主拥有总体关闭期限。本方法不另开 30 秒预算，不关闭仍被调用使用
            的插件或 Store；等待取消后同一后台收尾继续，后续 close 可再次观察。
        """
        if self.close_task is None:
            self.stop_dispatch()
            for action in self.actions.values():
                self.stop(action, "ServiceClosed")
            self.call_dispatcher.dispatch()
            self.close_task = asyncio.create_task(self.finish_close())
        await asyncio.shield(self.close_task)

    async def finish_close(self) -> None:
        """先收回 Worker，再等真实调用。

        Raises:
            BaseExceptionGroup: 执行资源收尾期间的原始故障。

        Note:
            任何原始失败在全部资源收尾后报告。
        """
        while self.owned_actions or self.owned_calls:
            if self.owned_actions:
                await asyncio.gather(*self.owned_actions, return_exceptions=True)
            if self.owned_calls:
                await asyncio.gather(*self.owned_calls, return_exceptions=True)
        if self.dispatch_timer is not None:
            self.dispatch_timer.cancel()
            self.dispatch_timer = None
        if self.failures:
            raise BaseExceptionGroup("执行资源收尾期间的原始故障。", self.failures)

    def action_done(self, task: asyncio.Task[ActionRecord]) -> None:
        """独立任务即使观察者已取消仍取回异常，关闭时保留同一真实失败。

        Args:
            task: 当前所有者创建并负责回收结果的任务。
        """
        self.owned_actions.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self.failures.append(error)

    async def run_action(self, action: ScriptAction) -> ActionRecord:
        """保存意图、启动/核验、处理帧、保存整体结果并按真实资源顺序释放。

        Args:
            action: 本次执行服务接管的冻结脚本或结构化调用。

        Returns:
            已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。

        Raises:
            ActionProtocolError: Worker ready 身份或目录版本不一致。
            BaseExceptionGroup: 行动原始故障与权威结束提交均失败。
            RuntimeError: 已接管行动缺少权威记录。
        """
        monitor: asyncio.Task[None] | None = None
        admitted = False
        try:
            await self.store.begin_action(
                action.action_id,
                catalog_version=self.catalog.version,
                source=action.source,
                input_id=action.input_id,
            )
            admitted = True
            if self.closed or asyncio.get_running_loop().time() >= action.deadline:
                action.stop_error = "ServiceClosed" if self.closed else "ActionWallLimit"
            else:
                action.worker = await asyncio.to_thread(self.worker_session.launch)
                self.worker_session.start_reader(action)
                monitor = asyncio.create_task(self.worker_session.monitor(action))
                await self.worker_session.bootstrap(action)
                ready = await action.messages.get()
                if isinstance(ready, BaseException):
                    raise ready
                if ready != {
                    "kind": "ready",
                    "action_id": action.action_id,
                    "catalog_version": self.catalog.version,
                }:
                    raise ActionProtocolError("Worker ready 身份或目录版本不一致。")

                if (
                    self.closed
                    or not action.eligible
                    or asyncio.get_running_loop().time() >= action.deadline
                    or (action.can_start is not None and not action.can_start())
                ):
                    self.stop(action, action.stop_error or "GenerationRevoked")
                else:
                    # 发送前最后核验之后不再读取生成资格；已派发行动独立收尾。
                    # ready 已证明子进程正在等这份短帧，管道没有其他写者。
                    # 此处不 await，防止关闭/生成撤销插入最终核验和实际派发之间。
                    action.worker.channel.send({"kind": "execute", "action_id": action.action_id})
                    await self.worker_session.frames(action)
        except (ActionProtocolError, EOFError, OSError) as error:
            if action.stop_error is None:
                action.stop_error = f"{type(error).__name__}: {error}"
            if action.worker is not None:
                self.stop(action, action.stop_error)
        except BaseException as error:
            # 这是独立资源所有者的清理边界；原始故障在真实资源收回后重新抛出。
            action.failure = error
            self.stop(action, "ExecutionInternalFailure")
        finally:
            action.eligible = False
            self.call_dispatcher.dispatch()
            if action.worker is not None:
                cleanup_error = await self.worker_session.release_worker(
                    action.worker, action, monitor
                )
                if cleanup_error is not None:
                    action.failure = (
                        cleanup_error
                        if action.failure is None
                        else BaseExceptionGroup(
                            "行动及实际资源回收失败。", [action.failure, cleanup_error]
                        )
                    )
                else:
                    self.actions.pop(action.action_id, None)
            else:
                self.actions.pop(action.action_id, None)

        if action.failure is not None:
            if admitted:
                try:
                    await self._finish_action(
                        action.action_id,
                        state="failed",
                        result_json=None,
                        output="" if action.terminal is None else action.terminal.output,
                        error=f"{type(action.failure).__name__}: {action.failure}",
                    )
                except BaseException as record_error:
                    raise BaseExceptionGroup(
                        "行动原始故障与权威结束提交均失败。",
                        [action.failure, record_error],
                    ) from None
            raise action.failure
        if action.stop_error is not None and admitted:
            state: Literal["failed", "timed_out", "cancelled"] = "failed"
            if action.stop_error in {"ActionWallLimit", "WorkerCpuLimit"}:
                state = "timed_out"
            elif action.stop_error in {"ServiceClosed", "GenerationRevoked"}:
                state = "cancelled"
            await self._finish_action(
                action.action_id, state=state, result_json=None, output="", error=action.stop_error
            )
        elif action.terminal is not None:
            terminal = action.terminal
            await self._finish_action(
                action.action_id,
                state=terminal.state,
                result_json=terminal.result_json,
                output=terminal.output,
                error=terminal.error,
            )
        record = await self.store.get_action(action.action_id)
        if record is None:
            raise RuntimeError("已接管行动缺少权威记录。")
        return record

    def stop(self, action: ScriptAction | StructuredAction, reason: str) -> None:
        """同步撤销尚未开始的调用资格。

        Args:
            action: 本次执行服务接管的冻结脚本或结构化调用。
            reason: 供后续核对的明确处置原因。

        Note:
            已进入 Provider 的效果继续独立收尾。
        """
        action.eligible = False
        action.stop_error = action.stop_error or reason
        if (
            isinstance(action, ScriptAction)
            and action.worker is not None
            and action.worker.process.poll() is None
        ):
            action.worker.process.terminate()


__all__ = ["ExecutionBusy", "ExecutionClosed", "ExecutionLimitError", "ExecutionService"]
