"""CallDispatcher 的执行资源协作；不创建另一份行动资格或权威记录。"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from dataclasses import replace
from typing import TYPE_CHECKING

from purrcept_engine.domain.execution.providers import (
    CallOutcome,
    CapabilityImplementation,
    CapabilityInputError,
)
from purrcept_engine.infrastructure.execution.state import (
    CapabilityCall,
    ScriptAction,
    StructuredAction,
)
from purrcept_engine.values import JsonObject, encode_json

if TYPE_CHECKING:
    from purrcept_engine.infrastructure.execution.service import ExecutionService


class CallDispatcher:
    """由执行器构造并独占，借用同一运行状态；资源关闭仍归执行器。"""

    def __init__(self, runtime: ExecutionService) -> None:
        """固定唯一执行器借用，不取得额外 Provider、账本或后台任务。

        Args:
            runtime: 创建此协作者的领域或执行运行实例，不跨实例共享状态。
        """
        self.runtime = runtime

    def reserve_call(self, call: CapabilityCall) -> bool:
        """预留等待及实际收尾位置，普通满额不能挤掉宿主登记的控制类别。

        Args:
            call: 当前真实能力或 Core 工具调用的冻结材料。

        Note:
            两类都以既有 Worker 数乘每行动调用数限定未完成记录；这不是额外的
            Provider 并发额度。已结束观察但仍在返回的 Provider 继续占用原位置。
        """
        capacity = self.runtime.settings.max_workers * self.runtime.settings.max_calls
        pending = self.runtime.pending_ordinary if call.ordinary else self.runtime.pending_controls
        if pending >= capacity:
            return False
        self.runtime.pending_calls += 1
        if call.ordinary:
            self.runtime.pending_ordinary += 1
        else:
            self.runtime.pending_controls += 1
        return True

    def release_call(self, call: CapabilityCall) -> None:
        """只在未接管失败或真实调用任务结束时归还其分类位置。

        Args:
            call: 当前真实能力或 Core 工具调用的冻结材料。
        """
        self.runtime.pending_calls -= 1
        if call.ordinary:
            self.runtime.pending_ordinary -= 1
        else:
            self.runtime.pending_controls -= 1
        if isinstance(call.action, StructuredAction):
            released = self.runtime.invocation_releases.pop(call.action.action_id)
            released.set_result(None)

    def enqueue(self, call: CapabilityCall) -> None:
        """进入共用队列。

        Args:
            call: 当前真实能力或 Core 工具调用的冻结材料。

        Note:
            来源内保持接管次序，控制不进入普通轮转。
        """
        if call.ordinary:
            source_id = call.source_id
            if source_id not in self.runtime.ordinary:
                self.runtime.ordinary[source_id] = deque()
                self.runtime.rotation.append(source_id)
            self.runtime.ordinary[source_id].append(call)
        else:
            self.runtime.controls.append(call)

    def dispatch(self) -> None:
        """普通请求按来源轮转且保留来源内次序，控制请求不等待普通名额。"""
        if self.runtime.dispatch_timer is not None:
            self.runtime.dispatch_timer.cancel()
            self.runtime.dispatch_timer = None
        now = asyncio.get_running_loop().time()
        for _ in range(len(self.runtime.controls)):
            call = self.runtime.controls[0]
            expired = (
                self.runtime.closed or not call.action.eligible or now >= call.context.deadline
            )
            if (
                not expired
                and self.runtime.control_active >= self.runtime.settings.max_provider_calls
            ):
                break
            self.runtime.controls.popleft()
            if not expired:
                self.runtime.control_active += 1
            self.start_call(call, reserved=not expired)

        stalled = 0
        while self.runtime.rotation and stalled < len(self.runtime.rotation):
            source_id = self.runtime.rotation.popleft()
            queue = self.runtime.ordinary[source_id]
            call = queue[0]
            expired = (
                self.runtime.closed or not call.action.eligible or now >= call.context.deadline
            )
            serial = call.binding.declaration.concurrency == "target_serial"
            available = (
                self.runtime.ordinary_active < self.runtime.settings.max_provider_calls
                and (not serial or call.binding.declaration.object_id not in self.runtime.targets)
            )
            if expired or available:
                queue.popleft()
                if not expired:
                    self.runtime.ordinary_active += 1
                    if serial:
                        call.target_reserved = True
                        self.runtime.targets.add(call.binding.declaration.object_id)
                self.start_call(call, reserved=not expired)
                stalled = 0
            else:
                stalled += 1
            if queue:
                self.runtime.rotation.append(source_id)
            else:
                del self.runtime.ordinary[source_id]

        waiting = (
            *self.runtime.controls,
            *(call for queue in self.runtime.ordinary.values() for call in queue),
        )
        if waiting:
            deadline = min(call.context.deadline for call in waiting)
            self.runtime.dispatch_timer = asyncio.get_running_loop().call_at(
                deadline, self.dispatch
            )

    def start_call(self, call: CapabilityCall, *, reserved: bool) -> None:
        """把已分配名额的调用交给独立所有者，回调在真实结束后归还额度。

        Args:
            call: 当前真实能力或 Core 工具调用的冻结材料。
            reserved: 是否占用独立控制名额，具体计数由所属所有者维护。
        """
        task = asyncio.create_task(self.invoke_call(call, reserved=reserved))
        self.runtime.owned_calls.add(task)
        task.add_done_callback(lambda task: self.call_done(task, call))

    def call_done(self, task: asyncio.Task[None], call: CapabilityCall) -> None:
        """内部故障撤销服务派发而保留原始异常，不能让后台任务静默失踪。

        Args:
            task: 当前所有者创建并负责回收结果的任务。
            call: 当前真实能力或 Core 工具调用的冻结材料。
        """
        self.runtime.owned_calls.discard(task)
        self.release_call(call)
        if task.cancelled() and isinstance(call.action, StructuredAction):
            # 资格端口自身取消时还未进入 Provider；仍须唤醒已接管行动所有者，
            # 让原取消沿其持久收尾传播，不能留下永远等待的首版 Future。
            if not call.action.receipt.done():
                call.action.receipt.cancel()
        if not task.cancelled() and (error := task.exception()) is not None:
            if isinstance(call.action, StructuredAction) and not call.action.receipt.done():
                call.action.receipt.set_exception(error)
            self.runtime.failures.append(error)
            self.runtime.closed = True
            for action in self.runtime.actions.values():
                if isinstance(action, ScriptAction):
                    action.failure = error
                self.runtime.stop(action, "ExecutionInternalFailure")
            self.dispatch()

    async def invoke_call(self, call: CapabilityCall, *, reserved: bool) -> None:
        """持久启动门槛之后再次核验。

        Args:
            call: 当前真实能力或 Core 工具调用的冻结材料。
            reserved: 是否占用独立控制名额，具体计数由所属所有者维护。

        Raises:
            BaseExceptionGroup: 未开始回执提交与资格收尾均失败；
                未知回执提交与迟到真实结果提交均失败。

        Note:
            超时保留真实任务并独立澄清未知回执。
        """
        try:
            if not reserved or not self.call_eligible(call):
                await self.not_started(call)
                return
            await self.runtime.store.mark_call_started(call.context.call_id)
            if not self.call_eligible(call):
                await self.not_started(call)
                return
            implementation = self.runtime.implementations[call.binding.declaration.capability_id]
            provider = asyncio.create_task(self.enter_provider(call, implementation))
            ready = call.action.start_ready if isinstance(call.action, StructuredAction) else None
            if ready is not None:
                # 资格异步核验与实际效果属于不同阶段。未完成资格时超时不生成
                # unknown；它从未进入 Provider。实际开始后使用收紧后的期限。
                remaining = max(0.0, call.context.deadline - asyncio.get_running_loop().time())
                qualified, _ = await asyncio.wait(
                    (provider, ready), timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
                if not qualified and not call.provider_started:
                    if isinstance(call.action, StructuredAction):
                        call.action.timed_out = True
                    try:
                        await self.not_started(call, reason="启动资格核验未在期限内完成。")
                    except BaseException as receipt_error:
                        # 权威失败立即关闭派发，但资格任务的实际取消清理仍归
                        # 本次调用；不能先归还位置，再遗留一个无所有者 Task。
                        self.runtime.closed = True
                        for owner in self.runtime.actions.values():
                            self.runtime.stop(owner, "ExecutionInternalFailure")
                        self.dispatch()
                        try:
                            await provider
                        except BaseException as qualification_error:
                            raise BaseExceptionGroup(
                                "未开始回执提交与资格收尾均失败。",
                                [receipt_error, qualification_error],
                            ) from None
                        raise
                    await provider
                    return
            remaining = max(0.0, call.context.deadline - asyncio.get_running_loop().time())
            done, _ = await asyncio.wait((provider,), timeout=remaining)
            timed_out = not done
            receipt_failure: BaseException | None = None
            if timed_out:
                if isinstance(call.action, StructuredAction):
                    call.action.timed_out = True
                try:
                    await self.record(
                        call,
                        CallOutcome(
                            "unknown", None, "TimeoutError", "能力期限耗尽，效果尚未确认。"
                        ),
                    )
                except BaseException as error:
                    # 权威提交失败立即阻止新派发，但不能提前归还实际 Provider
                    # 仍借用的目标和名额。先保留原故障，等真实返回后原样报告。
                    receipt_failure = error
                    self.runtime.closed = True
                    for owner in self.runtime.actions.values():
                        self.runtime.stop(owner, "ExecutionInternalFailure")
                    self.dispatch()
            outcome = await provider
            if receipt_failure is not None:
                if outcome is not None:
                    try:
                        await self.record(call, outcome, deliver=False)
                    except BaseException as late_error:
                        raise BaseExceptionGroup(
                            "未知回执提交与迟到真实结果提交均失败。", [receipt_failure, late_error]
                        ) from None
                raise receipt_failure
            if outcome is None:
                await self.not_started(call)
                return
            await self.record(call, outcome, deliver=not timed_out)
        finally:
            if reserved:
                if call.ordinary:
                    self.runtime.ordinary_active -= 1
                else:
                    self.runtime.control_active -= 1
                if call.target_reserved:
                    self.runtime.targets.remove(call.binding.declaration.object_id)
            self.dispatch()

    async def enter_provider(
        self, call: CapabilityCall, implementation: CapabilityImplementation
    ) -> CallOutcome | None:
        """异步资格与真实 Provider 分界。

        Args:
            call: 当前真实能力或 Core 工具调用的冻结材料。
            implementation: 本次真实调用绑定的能力实现。

        Returns:
            实际确认、失败或未知结论，以及可 JSON 化的业务值和原生媒体引用。
            未取得对应记录或素材时返回 None；读取故障仍抛出。

        Raises:
            ValueError: 启动资格必须返回正有限剩余秒数或 None。

        Note:
            权威故障不能被翻译成业务失败。
        """
        if not self.call_eligible(call):
            return None
        action = call.action
        if isinstance(action, StructuredAction) and action.before_start is not None:
            deadline = asyncio.timeout_at(call.context.deadline)
            try:
                async with deadline:
                    remaining = await action.before_start()
            except TimeoutError:
                if not deadline.expired():
                    raise
                return None
            if remaining is None:
                self.runtime.stop(action, "GenerationRevoked")
                return None
            if (
                isinstance(remaining, bool)
                or not isinstance(remaining, (int, float))
                or not math.isfinite(remaining)
                or remaining <= 0
            ):
                raise ValueError("启动资格必须返回正有限剩余秒数或 None。")
            call.context = replace(
                call.context,
                deadline=min(call.context.deadline, asyncio.get_running_loop().time() + remaining),
            )
        # 资格 await 后复核服务、宿主同步门槛与收紧的 deadline。从这里到
        # invoke 不再交还事件循环，暂停/结束不能夹在旧资格与实际启动之间。
        if not self.call_eligible(call):
            return None
        call.provider_started = True
        if isinstance(action, StructuredAction) and action.start_ready is not None:
            action.start_ready.set_result(None)
        try:
            return await implementation.invoke(call.arguments, call.context)
        except asyncio.CancelledError:
            # 此时实际 Provider 已开始。其自身取消不能证明外部效果撤销。
            return CallOutcome("unknown", None, "CancelledError", "Provider 已取消，效果未确认。")
        except CapabilityInputError as error:
            call.diagnostic_error = error
            return CallOutcome("failed", None, type(error).__name__, str(error))
        except Exception as error:
            # 只在真实扩展边界保留程序失败；资格/权威异常不进入此翻译范围。
            call.diagnostic_error = error
            return CallOutcome("failed", None, type(error).__name__, str(error))

    def call_eligible(self, call: CapabilityCall) -> bool:
        """脚本已派发后独立运行。

        Args:
            call: 当前真实能力或 Core 工具调用的冻结材料。

        Note:
            结构化资格仍在真实 Provider 前复核。
        """
        if (
            self.runtime.closed
            or not call.action.eligible
            or asyncio.get_running_loop().time() >= call.context.deadline
        ):
            return False
        if isinstance(call.action, StructuredAction):
            can_start = call.action.can_start
            if can_start is not None and not can_start():
                self.runtime.stop(call.action, "GenerationRevoked")
                return False
        return True

    async def not_started(
        self,
        call: CapabilityCall,
        *,
        error_type: str = "CallNotStarted",
        reason: str = "调用在开始前失去资格或期限。",
    ) -> None:
        """保存未进入 Provider 的事实，再向原观察者交付。

        Args:
            call: 当前真实能力或 Core 工具调用的冻结材料。
            error_type: 回执中稳定的错误类别，无错误时为 None。 默认 'CallNotStarted'。
            reason: 供后续核对的明确处置原因。 默认 '调用在开始前失去资格或期限。'。

        Note:
            不产生未知效果。
        """
        receipt = await self.runtime.store.record_call_result(
            call.context.call_id,
            state="not_started",
            result_json=None,
            error_type=error_type,
            error=reason,
        )
        if isinstance(call.action, StructuredAction):
            if not call.action.receipt.done():
                call.action.receipt.set_result(receipt)
            return
        await self.runtime.worker_session.deliver(
            call, {"ok": False, "error_type": error_type, "error": reason}
        )

    async def record(
        self, call: CapabilityCall, outcome: CallOutcome, *, deliver: bool = True
    ) -> None:
        """原始结果先完整入账。

        Args:
            call: 当前真实能力或 Core 工具调用的冻结材料。
            outcome: 此次操作的真实结论，不由期望效果推断成功。
            deliver: 当前观察是否仍允许把结果交回 Worker，不影响真实回执保存。 默认 True。

        Raises:
            TypeError: Provider 未返回声明的 CallOutcome。

        Note:
            交付超限只改变交付结果，不改变效果或自动重做。
        """
        if not isinstance(outcome, CallOutcome) or outcome.state not in {
            "confirmed",
            "failed",
            "unknown",
        }:
            raise TypeError("Provider 未返回声明的 CallOutcome。")
        encoded = encode_json(outcome.value)
        receipt = await self.runtime.store.record_call_result(
            call.context.call_id,
            state=outcome.state,
            result_json=encoded.decode("utf-8"),
            error_type=outcome.error_type,
            error=outcome.error,
            media=outcome.media,
        )
        logging.getLogger(__name__).log(
            logging.WARNING
            if receipt.state == "unknown"
            else logging.ERROR
            if receipt.state == "failed"
            else logging.DEBUG,
            "能力执行结果未知，需要核对。"
            if receipt.state == "unknown"
            else "能力执行失败。"
            if receipt.state == "failed"
            else "能力执行已确认。",
            extra={
                "action_id": receipt.action_id,
                "call_id": receipt.call_id,
                "path": receipt.path,
                "outcome": receipt.state,
                "error_type": receipt.error_type,
                "receipt_revision": receipt.receipt_revision,
            },
            exc_info=None
            if call.diagnostic_error is None
            else (
                type(call.diagnostic_error),
                call.diagnostic_error,
                call.diagnostic_error.__traceback__,
            ),
        )
        call.diagnostic_error = None

        if isinstance(call.action, StructuredAction):
            # 结构化观察直接读取已提交事实，没有 IPC 交付上限或虚构 Worker。
            # 超时已交付首版未知时，迟到回执仅追加原调用，不能复活旧观察。
            if deliver and not call.action.receipt.done():
                call.action.receipt.set_result(receipt)
            return
        if not deliver:
            return
        if outcome.state == "confirmed":
            response: JsonObject = {"ok": True, "value": outcome.value}
            size = len(encoded)
        else:
            response = {
                "ok": False,
                "error_type": outcome.error_type
                or ("CapabilityFailed" if outcome.state == "failed" else "UnknownResult"),
                "error": outcome.error
                or ("能力返回明确失败。" if outcome.state == "failed" else "能力效果无法确认。"),
            }
            size = len(
                encode_json({"error_type": response["error_type"], "error": response["error"]})
            )
        if size > self.runtime.settings.call_result_bytes:
            response = {
                "ok": False,
                "error_type": "ResultDeliveryLimit",
                "error": f"结果已完整保存，交付字节超限；call_id={call.context.call_id}",
            }
            error_bytes = len(
                encode_json({"error_type": response["error_type"], "error": response["error"]})
            )
            if error_bytes > self.runtime.settings.call_result_bytes:
                # 极小配置连错误及身份也不能交付；明确终止并保留原回执，
                # 不能为了报错再次发送超额度数据，让 Worker 误报协议损坏。
                self.runtime.stop(
                    call.action, f"ResultDeliveryLimit: call_id={call.context.call_id}"
                )
                return
        await self.runtime.worker_session.deliver(call, response)
