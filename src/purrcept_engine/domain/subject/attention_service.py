"""按调用方给出的完整批次评估注意级别，保存实际模型请求而不改变输入状态。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from purrcept_core.models import (
    ChoiceAnswer,
    DecisionBackend,
    DecisionRefusal,
    DecisionRequest,
    DecisionResponseError,
    ModelBackend,
    ModelError,
    ModelRequest,
    ToolCallBlock,
)

from purrcept_engine.domain.context.capacity import CapacityError
from purrcept_engine.domain.context.materials import UnsupportedMaterial
from purrcept_engine.domain.inputs.models import InputRecord
from purrcept_engine.domain.model_requests.models import RequestScope
from purrcept_engine.domain.model_requests.service import (
    ModelDecisionBackend,
    ModelGenerationBackend,
    RequestBudget,
    RequestBudgetExceeded,
)
from purrcept_engine.domain.subject.attention import attention_request
from purrcept_engine.domain.subject.models import (
    AttentionLevel,
)
from purrcept_engine.errors import StorageError
from purrcept_engine.serialization import encode
from purrcept_engine.values import ActionProtocolError, JsonObject, decode_object

if TYPE_CHECKING:
    from purrcept_engine.domain.subject.service import SubjectService


@dataclass(frozen=True, slots=True)
class AttentionResult:
    """被调用方返回的整批判断；是否采用、唤醒或抢占由协调者决定。

    Attributes:
        level: observe、wake 或 urgent；模型暂时失败时明确保底 wake。
        reason: 实际判断原因或未完成的完整证据说明。
        request_id: 已保存的逻辑请求身份，未发起请求时为 None。
    """

    level: AttentionLevel
    reason: str
    request_id: str | None = None


class AttentionService:
    """被刺激协调者串行调用的无状态批次评估器，不拥有定时或前台控制。

    Note:
        模型资源借用和请求证据仍由主体汇总；一次调用只有一个逻辑请求预算。
        输入交接、结果采用和旧生成抢占都留在调用方。
    """

    def __init__(self, runtime: SubjectService) -> None:
        """借用请求记账与模型声明，不启动工作器。

        Args:
            runtime: 创建评估器的唯一主体资源所有者。
        """
        self.runtime = runtime

    async def evaluate(
        self,
        records: tuple[InputRecord, ...],
        *,
        background: JsonObject,
        eligible: Callable[[], bool] | None = None,
    ) -> AttentionResult:
        """评估完整批次，返回判断及实际请求证据。

        Args:
            records: 调用方已经提交并冻结的完整刺激批次。
            background: 本次观察的有限工作状态，不读取连续前台历史。
            eligible: 最低唤起升级的旧代次资格；普通批次为 None。

        Returns:
            整批判断。容量、材料缺口和模型暂时失败明确返回未完成并保底 wake。

        Raises:
            StorageError: 请求证据缺失或权威记录故障。

        Note:
            不裁剪正文或拆换批次。内部故障保持原异常；取消仍归还实际后端借用。
        """
        if self.runtime.attention is None:
            return AttentionResult("wake", "attention.not_configured")
        try:
            request = await attention_request(
                records,
                background=background,
                media=self.runtime.media,
                model=self.runtime.attention,
            )
        except (CapacityError, UnsupportedMaterial) as error:
            return AttentionResult(
                "wake",
                encode(
                    {
                        "attention": "not_completed",
                        "error_type": type(error).__name__,
                        "reason": str(error),
                        "code": error.code if isinstance(error, CapacityError) else None,
                    }
                ),
            )
        return await self.assess(records, request, eligible=eligible)

    async def assess(
        self,
        records: tuple[InputRecord, ...],
        request: DecisionRequest | ModelRequest,
        *,
        eligible: Callable[[], bool] | None = None,
    ) -> AttentionResult:
        """在原预算内执行一次逻辑判断，保留所有真实尝试。

        Args:
            records: 请求对应的冻结输入批次。
            request: 已完整组装并预检的实际模型请求。
            eligible: 可选的原生成资格检查，重试前重新核对。

        Returns:
            判断及权威请求身份。

        Raises:
            StorageError: 缺少唯一请求证据。
            RuntimeError: 未装配注意模型。
        """
        if self.runtime.attention is None:
            raise RuntimeError("注意工作器缺少装配的模型。")
        identity = uuid4().hex
        budget = RequestBudget(
            max_requests=1,
            deadline=asyncio.get_running_loop().time() + self.runtime.attention.retry.total_seconds,
        )

        def can_assess() -> None:
            """复核当前注意判断仍拥有所观察前台代次的升级资格。"""
            self.runtime.check()
            if eligible is not None and not eligible():
                budget.revoke()
                budget.check()

        scope = RequestScope("attention", identity, tuple(record.input_id for record in records))
        if self.runtime.attention.kind == "decision":
            backend = ModelDecisionBackend(
                cast(DecisionBackend, self.runtime.attention.backend),
                self.runtime.store,
                scope=scope,
                budget=budget,
                policy=self.runtime.attention.retry,
                diagnostic_bytes=self.runtime.settings.diagnostic_bytes,
                can_start=can_assess,
                compiler=self.runtime.attention.compiler,
                observe_usage=self.runtime.attention.observe_usage,
            )
        else:
            backend = ModelGenerationBackend(
                cast(ModelBackend, self.runtime.attention.backend),
                self.runtime.store,
                scope=scope,
                budget=budget,
                policy=self.runtime.attention.retry,
                diagnostic_bytes=self.runtime.settings.diagnostic_bytes,
                can_start=can_assess,
                compiler=self.runtime.attention.compiler,
                observe_usage=self.runtime.attention.observe_usage,
            )
        self.runtime.attention_backends.add(backend)
        level: AttentionLevel = "wake"
        reason = "typesafe.batch_judgment"
        failed = False
        budget_stopped = False
        try:
            try:
                if isinstance(backend, ModelDecisionBackend):
                    if not isinstance(request, DecisionRequest):
                        raise TypeError("专用判断必须使用专用请求。")
                    response = await backend.decide(request, model=self.runtime.attention.model)
                    response.validate_for(request)
                    answer = response.answer("attention")
                    if isinstance(answer, DecisionRefusal):
                        raise DecisionResponseError("注意判断明确拒答。")
                    if not isinstance(answer, ChoiceAnswer) or answer.choice not in (
                        "observe",
                        "wake",
                        "urgent",
                    ):
                        raise DecisionResponseError("注意判断未返回声明的选择。")
                    level = answer.choice
                else:
                    if not isinstance(request, ModelRequest):
                        raise TypeError("生成注意必须使用生成请求。")
                    generated = await backend.generate(request, model=self.runtime.attention.model)
                    if any(isinstance(block, ToolCallBlock) for block in generated.message.content):
                        raise DecisionResponseError("无工具注意响应不能包含工具调用。")
                    try:
                        parsed = decode_object(generated.message.text.encode("utf-8"))
                    except ActionProtocolError as error:
                        raise DecisionResponseError("生成注意没有返回完整 JSON 对象。") from error
                    choice = parsed.get("attention")
                    if not isinstance(choice, str) or choice not in (
                        "observe",
                        "wake",
                        "urgent",
                    ):
                        raise DecisionResponseError("生成注意没有返回声明的选择。")
                    value = parsed.get("reason", "model.batch_judgment")
                    if not isinstance(value, str):
                        raise DecisionResponseError("生成注意理由必须是字符串。")
                    level, reason = choice, value
            except (ModelError, TimeoutError) as error:
                failed = True
                budget_stopped = isinstance(error, RequestBudgetExceeded)
                reason = encode({"attention": "not_completed", "error_type": type(error).__name__})
            requests = await self.runtime.store.requests_for_turn("attention", identity)
            if not requests and budget_stopped:
                # 资格可能在本地编译或背景准备期间到期，此时包装器尚未接管
                # 逻辑请求。没有实际请求不是证据损坏，也不能伪造请求成功记录。
                return AttentionResult(
                    "wake",
                    encode(
                        {
                            "attention": "not_completed",
                            "request_state": "not_started",
                            "cause": reason,
                        }
                    ),
                )
            if len(requests) != 1:
                raise StorageError("注意判断缺少唯一逻辑请求证据。", code="invalid_record")
            if failed:
                reason = encode(
                    {
                        "attention": "not_completed",
                        "cause": reason,
                        "request_state": requests[0].state,
                        "attempt_failures": tuple(
                            attempt.result.failure
                            for attempt in requests[0].attempts
                            if attempt.result is not None and attempt.result.failure is not None
                        ),
                    }
                )
            return AttentionResult(level, reason, requests[0].request.request_id)
        except Exception as error:
            # 内部/权威故障必须先锁存再异步归还后端；关闭取消可能发生在
            # finally 的清理等待中，不能用那次取消覆盖已经发生的原始故障。
            self.runtime.failure_error = self.runtime.failure_error or error
            raise
        finally:
            budget.revoke()
            await backend.close()
            self.runtime.attention_backends.discard(backend)
