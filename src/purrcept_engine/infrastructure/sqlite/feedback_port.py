"""把后台反馈事务接到 Store 唯一队列，保持原文和消费者交接的所有权。

Ledger 不打开连接。先只读取得有限工作和真实字节数，再申请控制写入额度；
空扫描不发布变化通知。取消等待沿 Store 契约保留已接管的事务，宿主后台
持有对应任务直至完成。SQL 和事实判断均在 feedback 模块。
"""

import math
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

import purrcept_engine.domain.subject.feedback_models as subject_feedback_models
from purrcept_engine.domain.knowledge.models import EvidencePage, SupervisionRecord
from purrcept_engine.infrastructure.sqlite import analysis_work, behavior_review
from purrcept_engine.infrastructure.sqlite import feedback as sql
from purrcept_engine.infrastructure.sqlite.time import AdmitInput
from purrcept_engine.serialization import encode
from purrcept_engine.values import JsonObject


def _nonnegative(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("反馈代次和游标序号必须是非负整数。")


def _limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 32:
        raise ValueError("反馈扫描限额必须在 1 到 32 之间。")


def _validate_purpose(value: str) -> None:
    if value not in ("memory", "behavior"):
        raise ValueError("后台用途只能为 memory 或 behavior。")


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
        """等待实际提交结果。

        Args:
            operation: 在专属数据库线程执行的同步短事务，返回值交回原事件循环。
            logical_bytes: 本次操作占用的逻辑载荷字节数，用于接管前背压。 默认 0。
            control: 是否使用独立控制名额，避免普通输入阻塞真实回执收尾。 默认 False。
            writing: 是否属于写操作，以便关闭或权威故障时撤销派发资格。 默认 False。

        Returns:
            当前所有者持有的工作观察入口，等待取消不等于实际资源已归还。

        Note:
            取消观察不撤销已经接管的事务。
        """
        ...


class FeedbackLedger:
    """Store 创建的有限后台端口，与母 Store 同循环及同资源寿命。

    app 借出本对象和独立的知识端口。派生前台输入仍通过 Store 的全局输入
    接管边界；额度满保留可靠 outbox，内部或权威故障原样传播。
    """

    def __init__(self, submit: Submit, admit: AdmitInput) -> None:
        """借入队列和同事务接管函数，不建立后台任务或第二连接。

        Args:
            submit: 借用同一 Store 有界队列执行同步短事务的入口。
            admit: 在调用方当前事务中正式接管输入的同步入口，不另开事务。
        """
        self._submit = submit
        self._admit = admit

    async def bridge(
        self, *, now: float, limit: int = 16
    ) -> subject_feedback_models.FeedbackBridge:
        """桥接实际回执、活动双消费者、前台失败及到期计划和有效监管。

        Args:
            now: 本次规则计算采用的时刻，便于同一操作保持一致。
            limit: 本次读取或更新的最大条目数。 默认 16。

        Returns:
            实际提交的消费者交接与仍需等待容量的状态。

        Raises:
            ValueError: 反馈观察时刻必须是有限时间。
        """
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
        """即时交接提交前已观察代次的新意见，迟到扫描不能重绑当前代次。

        Args:
            record: 带原始依据与版本约束的监管记录。
            observed_generation: 接管或判断开始时观察到的前台生成代次。

        Returns:
            实际提交的消费者交接与仍需等待容量的状态。
        """
        _nonnegative(observed_generation)
        return await self._submit(
            lambda connection: sql.dispatch_supervision(
                connection, self._admit, record, observed_generation=observed_generation
            ),
            logical_bytes=len(record.model_dump_json().encode("utf-8")),
            control=True,
            writing=True,
        )

    async def reflection_state(
        self, *, purpose: Literal["memory", "behavior"] = "memory"
    ) -> subject_feedback_models.ReflectionState:
        """读取独立来源和真正成功轮数，不改变复核资格。

        Returns:
            反思来源序号与实际成功前台轮数。
        """
        _validate_purpose(purpose)
        return await self._submit(
            lambda connection: sql.reflection_state(connection, purpose=purpose)
        )

    async def reflection_sources(
        self,
        *,
        epoch: int,
        limit: int = 32,
        after: int = 0,
        purpose: Literal["memory", "behavior"] = "memory",
    ) -> tuple[subject_feedback_models.ReflectionSource, ...]:
        """分页读取具体版本。

        Args:
            epoch: 后台可靠来源的已提交单调序号，不等同于模型输出次数。
            limit: 本次读取或更新的最大条目数。 默认 32。
            after: 已观察的变更序号。 默认 0。

        Returns:
            条目集合：待复核来源的确切版本与可靠游标。

        Note:
            已耗尽的相同覆盖不会反复排到第一批。
        """
        _validate_purpose(purpose)
        _limit(limit)
        _nonnegative(epoch)
        _nonnegative(after)
        return await self._submit(
            lambda connection: sql.reflection_sources(
                connection,
                epoch=epoch,
                limit=limit,
                after=after,
                purpose=purpose,
            )
        )

    async def reserve_reflection(
        self,
        *,
        batch_id: str,
        epoch: int,
        pages: tuple[EvidencePage, ...],
        successful_turns: int,
        purpose: Literal["memory", "behavior"] = "memory",
    ) -> bool:
        """在实际模型请求前持久接管原文范围。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            epoch: 后台可靠来源的已提交单调序号，不等同于模型输出次数。
            pages: 当前批次实际取得的完整原文页面。
            successful_turns: 已提交成功的前台轮数，用于后台调度判断。

        Raises:
            ValueError: 反思接管必须有批次身份和真实页面。

        Note:
            失败不退回自动重跑。
        """
        _validate_purpose(purpose)
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
                purpose=purpose,
            ),
            logical_bytes=len(encode(values).encode("utf-8")),
            control=True,
            writing=True,
        )

    async def postpone_preparation(
        self,
        sources: tuple[subject_feedback_models.ReflectionSource, ...],
        *,
        purpose: Literal["memory", "behavior"],
        reason: str,
    ) -> None:
        """将准备超时作为持久冷却提交，不让预检反复取得新的整批预算。"""
        _validate_purpose(purpose)
        _limit(len(sources))
        await self._submit(
            lambda connection: analysis_work.postpone_preparation(
                connection,
                tuple((source.evidence_id, source.version) for source in sources),
                purpose=purpose,
                reason=reason,
            ),
            logical_bytes=len(encode(sources).encode()),
            control=True,
            writing=True,
        )

    async def block_reflection(
        self,
        *,
        source_id: str,
        version: int,
        reason: str,
        purpose: Literal["memory", "behavior"] = "memory",
    ) -> None:
        """在同一提交队列保留容量拒绝，不将原文伪装为已复核。"""
        _validate_purpose(purpose)

        def block(connection: sqlite3.Connection) -> None:
            """短事务只改变尚未领取的单项责任。"""
            with connection:
                connection.execute(
                    "UPDATE analysis_work SET state='capacity_blocked',reason=? WHERE purpose=? "
                    "AND evidence_id=? AND version=? AND state IN('pending','retry_wait')",
                    (reason, purpose, source_id, version),
                )

        await self._submit(block, logical_bytes=len(source_id.encode()), control=True, writing=True)

    async def behavior_context(self, identities: tuple[str, ...]) -> JsonObject:
        """读取当前有限来源的上下文，不读取媒体 BLOB。"""
        return await self._submit(
            lambda connection: behavior_review.context(connection, identities)
        )

    async def configure_capacity(self, capacity_key: str) -> None:
        """为两个分析用途登记不含凭据的容量身份，仅变化时解开容量阻塞。"""
        if not capacity_key:
            raise ValueError("容量策略身份不能为空。")

        def configure(connection: sqlite3.Connection) -> None:
            """持久策略与重新排队共享一次事务，不清除重试或派发记录。"""
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                for purpose in ("memory", "behavior"):
                    row = connection.execute(
                        "SELECT capacity_key FROM analysis_policy WHERE purpose=?", (purpose,)
                    ).fetchone()
                    if row is None or row[0] != capacity_key:
                        connection.execute(
                            "INSERT INTO analysis_policy VALUES(?,?) ON CONFLICT(purpose) DO "
                            "UPDATE SET "
                            "capacity_key=excluded.capacity_key",
                            (purpose, capacity_key),
                        )
                        connection.execute(
                            "UPDATE analysis_work SET "
                            "state='pending',reason=NULL,attempts=0,next_attempt_at=0 WHERE "
                            "purpose=? AND "
                            "state='capacity_blocked'",
                            (purpose,),
                        )

        await self._submit(
            configure, writing=True, control=True, logical_bytes=len(capacity_key.encode())
        )
