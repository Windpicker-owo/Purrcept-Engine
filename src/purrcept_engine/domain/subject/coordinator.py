"""刺激的唯一调度者：合批、提交收件箱、采用判断、唤醒与抢占。

主脑和注意模型都是被调用方。协调者最多持有一个前台调用和一个注意调用，
等待模型时仍响应输入提交。唤醒意图存在权威库，任务完成通知只促使重新查询。
observe 留下的输入不自行触发下一轮；一次唤醒原子取走所有已提交输入。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from purrcept_engine.domain.inputs.models import InputRecord
from purrcept_engine.domain.subject.attention_service import AttentionResult
from purrcept_engine.domain.subject.models import InputProcessing, MinimumWakeAssessment
from purrcept_engine.errors import StorageError
from purrcept_engine.serialization import encode

if TYPE_CHECKING:
    from purrcept_engine.domain.subject.service import SubjectService


class StimulusCoordinator:
    """由 SubjectService 创建并关闭的唯一刺激调度流程。

    Note:
        同一事件循环串行采用状态。模型等待在独立的有限任务中进行；输入积压
        仍受 Store 接管额度限制，不为每条刺激创建模型任务或内存等待对象。
    """

    def __init__(self, runtime: SubjectService) -> None:
        """借用同一主体状态和资源所有权。

        Args:
            runtime: 当前主体实例，不跨实例共享调度状态。
        """
        self.runtime = runtime
        self.foreground: asyncio.Task[None] | None = None
        self.evaluation: asyncio.Task[AttentionResult] | None = None
        self.expected: tuple[InputProcessing, ...] = ()
        self.observed = 0
        self.minimum_wake: MinimumWakeAssessment | None = None
        self.batch_anchor: str | None = None
        self.batch_deadline = 0.0

    async def run(self) -> None:
        """持续协调输入；前台每次完成后由这里检查持久唤醒请求。

        Raises:
            Exception: 内部或权威故障保持原型，由主体锁存并停止新派发。

        Note:
            关闭只取消本所有者的调用；实际行动和真实回执仍由执行器收尾。
        """
        try:
            while True:
                self.runtime.check()
                revision = self.runtime.store.change_revision

                if self.evaluation is not None and self.evaluation.done():
                    result = self.evaluation.result()
                    self.evaluation = None
                    await self.adopt(result)
                if self.foreground is not None and self.foreground.done():
                    self.foreground.result()
                    self.foreground = None

                # 高价值输入先把当前缓存整批提交。即使唯一注意调用仍在等待，
                # 保底唤醒也必须立刻持久化，不能被模型延迟挡住。
                priority = await self.runtime.store.staged_inputs("priority", limit=1)
                if priority:
                    records = await self.runtime.store.buffered_inputs()
                    expected = await self.runtime.processing(records)
                    if await self.runtime.commit(
                        self.runtime.store.submit_inputs(expected, minimum_wake=True)
                    ):
                        submitted = await self.runtime.processing(records)
                        observed = self.runtime.session.generation
                        if self.runtime.attention is not None and self.runtime.session.eligible(
                            observed
                        ):
                            await self.runtime.commit(
                                self.runtime.store.register_minimum_wake_assessment(
                                    submitted,
                                    owner_id=self.runtime.attention_owner,
                                    observed_generation=observed,
                                )
                            )
                    continue

                # 取批先于异步材料组装，claimed 在同一事务消耗唤醒资格，
                # 本轮运行期间的新提交不能被 Core 工具循环悄悄拉入。
                if self.foreground is None:
                    ready = await self.runtime.store.staged_inputs("foreground", limit=1)
                    if ready:
                        records = await self.runtime.commit(
                            self.runtime.store.take_foreground_inputs()
                        )
                        if records:
                            self.runtime.check()
                            self.foreground = asyncio.create_task(
                                self.runtime.foreground_service.run(records),
                                name="subject-foreground",
                            )
                            self.runtime.tasks.add(self.foreground)
                            self.foreground.add_done_callback(self.runtime.worker_done)
                        continue

                remaining: float | None = None
                if self.evaluation is None:
                    assessments = await self.runtime.store.minimum_wake_assessments(
                        self.runtime.attention_owner, limit=1
                    )
                    if assessments:
                        assessment = assessments[0]
                        records = await self.records_for(assessment)
                        if not self.runtime.session.eligible(assessment.observed_generation) or (
                            await self.runtime.processing(records) != assessment.expected
                        ):
                            await self.runtime.commit(
                                self.runtime.store.finish_minimum_wake_assessment(
                                    assessment, "wake", "attention.minimum_wake_observation_expired"
                                )
                            )
                            continue
                        self.launch(records, assessment.expected, assessment=assessment)
                        continue

                    records = await self.runtime.store.staged_inputs(
                        "attention", limit=self.runtime.settings.attention_batch
                    )
                    if records:
                        expected = await self.runtime.processing(records)
                        # 墙钟只在观察到新批次时换算一次，之后用单调时钟等待。
                        # 后来输入和系统时间回拨都不能刷新已建立的批次期限。
                        if self.batch_anchor != records[0].input_id:
                            self.batch_anchor = records[0].input_id
                            first = expected[0].admitted_at
                            remaining = min(
                                self.runtime.settings.coalesce_seconds,
                                max(
                                    0.0,
                                    first + self.runtime.settings.coalesce_seconds - time.time(),
                                ),
                            )
                            self.batch_deadline = asyncio.get_running_loop().time() + remaining
                        remaining = max(
                            0.0, self.batch_deadline - asyncio.get_running_loop().time()
                        )
                        if len(records) == self.runtime.settings.attention_batch or remaining <= 0:
                            if await self.runtime.commit(
                                self.runtime.store.submit_inputs(expected, minimum_wake=False)
                            ):
                                self.launch(records, await self.runtime.processing(records))
                            continue

                await self.wait(revision, seconds=remaining)
        finally:
            # 生命周期边界：取消观察不能遗失注意后端的实际清理。前台任务同时
            # 登记在主体上，由 SubjectService 按其取消/结案规则统一等待。
            if self.evaluation is not None:
                if not self.evaluation.done() and not self.evaluation.cancelling():
                    self.evaluation.cancel()
                await asyncio.gather(self.evaluation, return_exceptions=True)

    async def records_for(self, assessment: MinimumWakeAssessment) -> tuple[InputRecord, ...]:
        """恢复完整批次原文；缺少已关联证据属于权威故障。

        Args:
            assessment: 当前宿主登记的冻结批次。

        Returns:
            与版本快照顺序一致的原始输入。
        """
        records: list[InputRecord] = []
        for item in assessment.expected:
            record = await self.runtime.store.get_input(item.input_id)
            if record is None:
                raise StorageError("最低唤起批次缺少原文。", code="invalid_record")
            records.append(record)
        return tuple(records)

    def launch(
        self,
        records: tuple[InputRecord, ...],
        expected: tuple[InputProcessing, ...],
        *,
        assessment: MinimumWakeAssessment | None = None,
    ) -> None:
        """在唯一注意槽位启动一次被调用评估，不等待其完成。

        Args:
            records: 本次评估的完整批次。
            expected: 采用结果时必须匹配的提交后版本。
            assessment: 高价值批次的原生成升级资格，普通批次为 None。
        """
        self.expected = expected
        self.minimum_wake = assessment
        self.observed = (
            assessment.observed_generation if assessment else self.runtime.session.generation
        )

        async def evaluate() -> AttentionResult:
            """背景读取也留在调用任务内，避免辅助查询延迟阻塞可信输入交接。"""
            try:
                background = await self.runtime.attention_context()
                observed = self.observed
                return await self.runtime.attention_service.evaluate(
                    records,
                    background=background,
                    eligible=(lambda: self.runtime.session.eligible(observed))
                    if assessment is not None
                    else None,
                )
            except Exception as error:
                # 关闭可在调用完成与结果采用之间发生，原始故障必须先锁存。
                self.runtime.failure_error = self.runtime.failure_error or error
                raise

        self.evaluation = asyncio.create_task(evaluate(), name="subject-attention")

    async def adopt(self, result: AttentionResult) -> None:
        """采用完整批次版本后，才尝试抢占被观察的旧生成。

        Args:
            result: 被调用评估器返回的真实判断与请求证据。

        Note:
            高价值分支不能降级；迟到普通判断不能更新已取走的批次或新代次。
        """
        if self.minimum_wake is None:
            applied = await self.runtime.commit(
                self.runtime.store.decide_inputs(
                    self.expected, result.level, result.reason, request_id=result.request_id
                )
            )
        else:
            applied = await self.runtime.commit(
                self.runtime.store.finish_minimum_wake_assessment(
                    self.minimum_wake,
                    result.level,
                    encode(
                        {
                            "attention": "minimum_wake_assessment",
                            "minimum_level": "wake",
                            "assessed_level": result.level,
                            "reason": result.reason,
                            "observed_generation": self.observed,
                        }
                    ),
                    request_id=result.request_id,
                )
            )
        if applied and result.level == "urgent":
            self.runtime.session.interrupt(self.observed)

    async def wait(self, revision: int, *, seconds: float | None) -> None:
        """同时观察输入、调用完成和批次期限，取消等待不传播给模型调用。

        Args:
            revision: 查询前读取的存储通知序号。
            seconds: 普通批次剩余窗口，None 表示没有定时期限。
        """
        changed = asyncio.create_task(
            self.runtime.store.wait_for_change(revision), name="stimulus-change"
        )
        calls = [task for task in (self.foreground, self.evaluation) if task is not None]
        try:
            done, _ = await asyncio.wait(
                [changed, *calls], timeout=seconds, return_when=asyncio.FIRST_COMPLETED
            )
            if changed in done:
                changed.result()
        finally:
            changed.cancel()
            await asyncio.gather(changed, return_exceptions=True)
            if not changed.cancelled() and (error := changed.exception()) is not None:
                self.runtime.failure_error = self.runtime.failure_error or error
