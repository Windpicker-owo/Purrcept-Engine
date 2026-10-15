"""AttentionService 的固定工作流程；共享协调事实只借用同一领域运行时。"""

from __future__ import annotations

import asyncio
import time
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
)
from purrcept_engine.domain.subject.attention import attention_request
from purrcept_engine.domain.subject.models import (
    AttentionLevel,
    InputProcessing,
    MinimumWakeAssessment,
)
from purrcept_engine.errors import HostStateError, StorageError
from purrcept_engine.serialization import encode
from purrcept_engine.values import ActionProtocolError, decode_object

if TYPE_CHECKING:
    from purrcept_engine.domain.subject.service import SubjectService


class AttentionService:
    """领域运行时创建的工作所有者，启动一次；取消和汇总关闭由运行时统一协调。"""

    def __init__(self, runtime: SubjectService) -> None:
        """借用同一协调状态，不复制会话、生成资格或失败事实。"""
        self.runtime = runtime
        self.task: asyncio.Task[None] | None = None

    def start(self) -> asyncio.Task[None]:
        """创建唯一固定任务并交给运行时汇总关闭；重复启动明确拒绝。"""
        if self.task is not None:
            raise HostStateError("领域工作器已经启动。")
        self.task = asyncio.create_task(self.attention(), name="subject-attention")
        return self.task

    async def attention(self) -> None:
        """空闲时判断普通输入；主脑活动期间保持缓冲开放，由请求边界取尽。"""
        while True:
            self.runtime.check()
            revision = self.runtime.store.change_revision
            upgrades = await self.runtime.store.minimum_wake_assessments(
                self.runtime.attention_owner, limit=1
            )
            if upgrades:
                await self.upgrade_minimum_wake(upgrades[0])
                continue
            if self.runtime.session.busy:
                # 已有主脑交互时，新刺激不再被后台 observe/defer 提前取走。
                await self.runtime.wait_foreground(revision)
                continue
            if await self.runtime.store.staged_inputs("foreground", limit=1):
                await self.runtime.wait(revision)
                continue
            records = await self.runtime.store.staged_inputs(
                "attention", limit=self.runtime.settings.attention_batch
            )
            if not records:
                await self.runtime.wait(revision)
                continue
            expected = await self.runtime.processing(records)
            first = min(item.admitted_at for item in expected)
            remaining = min(
                self.runtime.settings.coalesce_seconds,
                max(0.0, first + self.runtime.settings.coalesce_seconds - time.time()),
            )
            deadline = asyncio.get_running_loop().time() + remaining
            upgrade_ready = False
            while len(records) < self.runtime.settings.attention_batch and remaining > 0:
                await self.runtime.wait(revision, seconds=remaining)
                self.runtime.check()
                revision = self.runtime.store.change_revision
                if await self.runtime.store.minimum_wake_assessments(
                    self.runtime.attention_owner, limit=1
                ):
                    upgrade_ready = True
                    break
                records = await self.runtime.store.staged_inputs(
                    "attention", limit=self.runtime.settings.attention_batch
                )
                if not records:
                    break
                remaining = deadline - asyncio.get_running_loop().time()
            if upgrade_ready:
                continue
            if not records:
                continue
            if self.runtime.session.busy or await self.runtime.store.staged_inputs(
                "foreground", limit=1
            ):
                continue
            expected = await self.runtime.processing(records)
            if any(item.stage != "attention" for item in expected):
                continue
            if self.runtime.attention is None:
                await self.runtime.commit(
                    self.runtime.store.decide_inputs(expected, "wake", "attention.not_configured")
                )
                continue
            observed = self.runtime.session.generation
            background = await self.runtime.attention_context()
            while records:
                try:
                    request = await attention_request(
                        records,
                        background=background,
                        media=self.runtime.media,
                        model=self.runtime.attention,
                        defer_seconds=self.runtime.settings.defer_seconds,
                    )
                except (CapacityError, UnsupportedMaterial) as error:
                    if len(records) > 1:
                        records = records[:-1]
                        expected = expected[:-1]
                        continue
                    reason = encode(
                        {
                            "attention": "not_completed",
                            "error_type": type(error).__name__,
                            "reason": str(error),
                            "code": error.code if isinstance(error, CapacityError) else None,
                        }
                    )
                    await self.runtime.commit(
                        self.runtime.store.decide_inputs(expected, "wake", reason)
                    )
                    break
                await self.assess(records, expected, request, observed)
                break

    async def upgrade_minimum_wake(self, assessment: MinimumWakeAssessment) -> None:
        """仅评估仍在旧生成后排队的直接唤起，失效观察结案而不换代续做。"""
        record = await self.runtime.store.get_input(assessment.input_id)
        processing = await self.runtime.store.input_processing(assessment.input_id)
        if record is None or processing is None:
            raise StorageError("最低唤起升级引用缺少原文或处置身份。", code="invalid_record")
        if (
            processing.stage != "foreground"
            or processing.version != assessment.input_version
            or not self.runtime.session.eligible(assessment.observed_generation)
        ):
            await self.runtime.commit(
                self.runtime.store.finish_minimum_wake_assessment(
                    assessment, "wake", "attention.minimum_wake_observation_expired"
                )
            )
            return
        if self.runtime.attention is None:
            raise RuntimeError("最低唤起升级缺少装配的注意模型。")
        background = await self.runtime.attention_context()
        try:
            request = await attention_request(
                (record,),
                background=background,
                media=self.runtime.media,
                model=self.runtime.attention,
                defer_seconds=self.runtime.settings.defer_seconds,
            )
        except (CapacityError, UnsupportedMaterial) as error:
            await self.runtime.commit(
                self.runtime.store.finish_minimum_wake_assessment(
                    assessment,
                    "wake",
                    encode(
                        {
                            "attention": "not_completed",
                            "minimum_level": "wake",
                            "error_type": type(error).__name__,
                            "reason": str(error),
                            "code": error.code if isinstance(error, CapacityError) else None,
                        }
                    ),
                )
            )
            return
        # 材料组装也可能等待存储。正式请求仍只针对原观察，不因为生成已经
        # 前进而借新代次继续评估；第一次逻辑请求前无需伪造失败模型证据。
        current = await self.runtime.store.input_processing(assessment.input_id)
        if current != processing or not self.runtime.session.eligible(
            assessment.observed_generation
        ):
            await self.runtime.commit(
                self.runtime.store.finish_minimum_wake_assessment(
                    assessment, "wake", "attention.minimum_wake_observation_expired"
                )
            )
            return
        await self.assess(
            (record,),
            (processing,),
            request,
            assessment.observed_generation,
            minimum_wake=assessment,
        )

    async def assess(
        self,
        records: tuple[InputRecord, ...],
        expected: tuple[InputProcessing, ...],
        request: DecisionRequest | ModelRequest,
        observed_generation: int,
        *,
        minimum_wake: MinimumWakeAssessment | None = None,
    ) -> None:
        """判断证据独立入账，处置 CAS 后才抢占，有限服务失败交前台一次。"""
        if self.runtime.attention is None:
            raise RuntimeError("注意工作器缺少装配的模型。")
        identity = uuid4().hex
        budget = RequestBudget(
            max_requests=1,
            deadline=asyncio.get_running_loop().time() + self.runtime.attention.retry.total_seconds,
        )

        def can_assess() -> None:
            self.runtime.check()
            if minimum_wake is not None and not self.runtime.session.eligible(observed_generation):
                # 旧生成结束后不重试升级请求。已启动尝试仍按原证据/借用机制
                # 收尾，不能把迟到的 urgent 采用到正在处理该输入的新轮次。
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
            )
        self.runtime.attention_backends.add(backend)
        level: AttentionLevel = "wake"
        reason = "typesafe.batch_judgment"
        failed = False
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
                        "defer",
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
                        "defer",
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
                reason = encode({"attention": "not_completed", "error_type": type(error).__name__})
            requests = await self.runtime.store.requests_for_turn("attention", identity)
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
            if minimum_wake is None:
                applied = await self.runtime.commit(
                    self.runtime.store.decide_inputs(
                        expected,
                        level,
                        reason,
                        request_id=requests[0].request.request_id,
                        defer_until=time.time() + self.runtime.settings.defer_seconds
                        if level == "defer"
                        else None,
                    )
                )
            else:
                applied = await self.runtime.commit(
                    self.runtime.store.finish_minimum_wake_assessment(
                        minimum_wake,
                        level,
                        encode(
                            {
                                "attention": "minimum_wake_assessment",
                                "minimum_level": "wake",
                                "assessed_level": level,
                                "reason": reason,
                                "observed_generation": observed_generation,
                            }
                        ),
                        request_id=requests[0].request.request_id,
                    )
                )
            if applied and level == "urgent":
                self.runtime.session.interrupt(observed_generation)
        except Exception as error:
            # 内部/权威故障必须先锁存再异步归还后端；关闭取消可能发生在
            # finally 的清理等待中，不能用那次取消覆盖已经发生的原始故障。
            self.runtime.failure_error = self.runtime.failure_error or error
            raise
        finally:
            budget.revoke()
            await backend.close()
            self.runtime.attention_backends.discard(backend)
