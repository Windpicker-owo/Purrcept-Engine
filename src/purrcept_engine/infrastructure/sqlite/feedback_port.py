"""把后台反馈事务接到 Store 唯一队列，保持原文和消费者交接的所有权。

Ledger 不打开连接。先只读取得有限工作和真实字节数，再申请控制写入额度；
空扫描不发布变化通知。取消等待沿 Store 契约保留已接管的事务，宿主后台
持有对应任务直至完成。SQL 和事实判断均在 feedback 模块。
"""

import math
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Protocol

import purrcept_engine.domain.subject.feedback_models as subject_feedback_models
from purrcept_engine.domain.knowledge.models import EvidencePage, SupervisionRecord
from purrcept_engine.infrastructure.sqlite import feedback as sql
from purrcept_engine.infrastructure.sqlite.time import AdmitInput
from purrcept_engine.serialization import encode


def _nonnegative(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("反馈代次和游标序号必须是非负整数。")


def _limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 32:
        raise ValueError("反馈扫描限额必须在 1 到 32 之间。")


class Submit(Protocol):
    """Store 注入的有界队列；连接仅在数据库线程执行回调时借出。"""

    def __call__[Result](
        self,
        operation: Callable[[sqlite3.Connection], Result],
        *,
        logical_bytes: int = 0,
        control: bool = False,
        writing: bool = False,
    ) -> Awaitable[Result]:
        """等待实际提交结果；取消观察不撤销已经接管的事务。"""
        ...


class FeedbackLedger:
    """Store 创建的有限后台端口，与母 Store 同循环及同资源寿命。

    app 借出本对象和独立的知识端口。派生前台输入仍通过 Store 的全局输入
    接管边界；额度满保留可靠 outbox，内部或权威故障原样传播。
    """

    def __init__(self, submit: Submit, admit: AdmitInput) -> None:
        """借入队列和同事务接管函数，不建立后台任务或第二连接。"""
        self._submit = submit
        self._admit = admit

    async def bridge(
        self, *, now: float, limit: int = 16
    ) -> subject_feedback_models.FeedbackBridge:
        """桥接实际回执、活动双消费者、前台失败及到期计划和有效监管。"""
        _limit(limit)
        if isinstance(now, bool) or not math.isfinite(now):
            raise ValueError("反馈观察时刻必须是有限时间。")
        work = await self._submit(lambda connection: sql.prepare(connection, now=now, limit=limit))
        if not work.available:
            return subject_feedback_models.FeedbackBridge()
        return await self._submit(
            lambda connection: sql.bridge(connection, self._admit, work, now=now, limit=limit),
            logical_bytes=len(
                encode(
                    (
                        work.calls,
                        work.activity,
                        work.failures,
                        tuple(item.model_dump(mode="json") for item in work.plans),
                        tuple(item.model_dump(mode="json") for item in work.supervision),
                    )
                ).encode("utf-8")
            ),
            control=True,
            writing=True,
        )

    async def dispatch_supervision(
        self, record: SupervisionRecord, *, observed_generation: int
    ) -> subject_feedback_models.FeedbackBridge:
        """即时交接提交前已观察代次的新意见，迟到扫描不能重绑当前代次。"""
        _nonnegative(observed_generation)
        return await self._submit(
            lambda connection: sql.dispatch_supervision(
                connection, self._admit, record, observed_generation=observed_generation
            ),
            logical_bytes=len(record.model_dump_json().encode("utf-8")),
            control=True,
            writing=True,
        )

    async def reflection_state(self) -> subject_feedback_models.ReflectionState:
        """读取独立来源和真正成功轮数，不改变复核资格。"""
        return await self._submit(sql.reflection_state)

    async def reflection_sources(
        self, *, epoch: int, limit: int = 32, after: int = 0
    ) -> tuple[subject_feedback_models.ReflectionSource, ...]:
        """分页读取具体版本；已耗尽的相同覆盖不会反复排到第一批。"""
        _limit(limit)
        _nonnegative(epoch)
        _nonnegative(after)
        return await self._submit(
            lambda connection: sql.reflection_sources(
                connection,
                epoch=epoch,
                limit=limit,
                after=after,
            )
        )

    async def reserve_reflection(
        self, *, batch_id: str, epoch: int, pages: tuple[EvidencePage, ...], successful_turns: int
    ) -> bool:
        """在实际模型请求前持久接管原文范围；失败不退回自动重跑。"""
        _nonnegative(epoch)
        _nonnegative(successful_turns)
        if not batch_id.strip() or not pages:
            raise ValueError("反思接管必须有批次身份和真实页面。")
        _limit(len(pages))
        values = tuple(page.model_dump(mode="json") for page in pages)
        return await self._submit(
            lambda connection: sql.reserve_reflection(
                connection,
                batch_id=batch_id,
                epoch=epoch,
                pages=pages,
                successful_turns=successful_turns,
            ),
            logical_bytes=len(encode(values).encode("utf-8")),
            control=True,
            writing=True,
        )
