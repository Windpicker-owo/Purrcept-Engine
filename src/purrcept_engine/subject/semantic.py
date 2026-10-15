"""以显式向量资源补充认识召回，并在一个固定工作器中维护派生索引。

认识和原文仍由 KnowledgeStore 决定资格。这里在数据库事务之外计算向量，
返回或安装前重新核验确切版本；向量空间改变时只重建派生任务，不改写事实。
未装配 embedding 时工作器只维护关键词，查询明确报告语义路线禁用。
网络及 Provider 故障保留原型和未完成任务，不自动更换模型或退回另一条路线。
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from typing import Protocol

from purrcept_engine.domain.knowledge.models import (
    EvidenceSnapshot,
    IndexJob,
    KnowledgeReference,
    MemoryRecord,
)
from purrcept_engine.errors import HostStateError
from purrcept_engine.subject.knowledge import KnowledgeStore


class EmbeddingProtocolError(ValueError):
    """配置的向量Provider返回不完整或无效响应，停止当前查询而不伪造召回。

    内置HTTP边界验证序号、维度、有限非零向量；公开错误不携带凭据或响应
    原文。主体将它视为该批实际外部失败，内部算法异常仍保留原型并停止工作器。
    """


class EmbeddingProvider(Protocol):
    """app 创建或显式借入的向量资源，前台、反思与一个索引工作器共同借用。

    model_id 标识决定向量空间的明确版本，不是展示名称；变更后必须重建。
    embed 保留输入顺序，返回同维、非空、非零和有限向量。网络与取消原样
    传出，不写权威事实。实现可以复用连接池，但每次请求保持独立材料。
    内置实现见 app.knowledge_resources；资源关闭由创建它的 app 所有者负责。
    """

    @property
    def model_id(self) -> str:
        """返回冻结的向量空间版本，查询与索引只能使用这一空间。"""
        ...

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """为至多 32 个完整文本取得对应向量，缺项或异常维度不能伪造成功。"""
        ...


class IndexChanges(Protocol):
    """母 Store 提供提交变化等待，工作器不能另外打开连接或周期空转。"""

    @property
    def change_revision(self) -> int:
        """取得查询前的提交序号，避免查询与等待之间漏掉新来源。"""
        ...

    async def wait_for_change(self, after_revision: int) -> int:
        """等待后于该序号的新提交，不把唤醒当作索引已完成。"""
        ...


class SemanticStore(KnowledgeStore, Protocol):
    """同一 Store 队列的向量重建接缝，不公开 SQL 或连接。

    只有当前有效且缺少所需向量空间的版本才排队；空扫描不能自己发布提交
    通知。安装仍由 apply_index 的同事务版本检查负责，重建不删除权威版本。
    """

    async def queue_semantic_rebuild(self, model_id: str, *, limit: int = 32) -> int:
        """有限补齐当前空间缺少的任务，返回真正新增数，不重排已有身份。"""
        ...

    async def discard_stale_index(self, reference: KnowledgeReference, *, route: str) -> bool:
        """仅确认当前仍失效的确切版本任务；不能伪造向量或吞掉有效新版本。"""
        ...


def validate_vectors(
    vectors: tuple[tuple[float, ...], ...], expected: int
) -> tuple[tuple[float, ...], ...]:
    """核验内置和注入 Provider 的同一契约，不能以零向量补缺或截断维度。"""
    if len(vectors) != expected:
        raise ValueError("Embedding 返回数量与请求不一致。")
    dimension: int | None = None
    for vector in vectors:
        if not vector or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in vector
        ):
            raise ValueError("Embedding 向量必须非空且只含有限数值。")
        if dimension is None:
            dimension = len(vector)
        elif len(vector) != dimension:
            raise ValueError("Embedding 同一响应的向量维度不一致。")
        norm = math.hypot(*vector)
        if norm == 0 or not math.isfinite(norm):
            raise ValueError("Embedding 向量必须具有有限非零方向。")
    return vectors


def index_text(record: EvidenceSnapshot | MemoryRecord) -> str:
    """索引完整原文或认识正文，摘要和 overview 不代替这一版本的实际内容。"""
    if isinstance(record, EvidenceSnapshot):
        return record.items_json or record.text
    return record.patch.content


class VectorRetriever:
    """KnowledgeService 借用的语义路线，排名来自当前空间的真实向量。"""

    def __init__(self, store: KnowledgeStore, embedding: EmbeddingProvider) -> None:
        """借入同库账本和明确资源，不触发网络或重建任务。"""
        self.store = store
        self.embedding = embedding

    async def retrieve(self, query: str) -> tuple[KnowledgeReference, ...]:
        """计算查询向量后回同库读取排名；上层融合和返回前还会再次复查资格。"""
        vectors = validate_vectors(await self.embedding.embed((query,)), 1)
        return await self.store.semantic_search(vectors[0], self.embedding.model_id, limit=40)


class KnowledgeIndexWorker:
    """app 持有的一个固定索引工作器，数据库保存全部待办，不为每版本创建 Task。

    一个批次最多维护 100 个关键词版本和 32 个向量版本。只有有新提交或剩余
    有限待办时继续，不因无工作重复调用模型。Provider 故障停止本工作器并保留
    failure 和待办；宿主可报告实际失败，重启仍由同一可靠任务继续。
    """

    def __init__(
        self,
        store: SemanticStore,
        changes: IndexChanges,
        embedding: EmbeddingProvider | None,
        *,
        can_start: Callable[[], None],
    ) -> None:
        """借入明确端口；构造不创建任务，也不读取任何当前内容。"""
        self.store, self.changes, self.embedding = store, changes, embedding
        self.can_start = can_start
        self._task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closing = False
        self._failure: BaseException | None = None
        self._commits: set[asyncio.Task[object]] = set()

    @property
    def failure(self) -> BaseException | None:
        """返回停止索引派发的原故障，不把它解释成语义路线没有结果。"""
        return self._failure

    @property
    def pending_resources(self) -> int:
        """报告实际工作器和取消后仍须等待的权威派生提交。"""
        return int(self._task is not None and not self._task.done()) + len(self._commits)

    async def start(self) -> None:
        """建立唯一工作器；重复开始或已经关闭不能新增派发。"""
        if self._closing or self._task is not None:
            raise HostStateError("认识索引工作器不能重复启动。")
        self.can_start()
        self._task = asyncio.create_task(self._run(), name="knowledge-index")
        self._task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[None]) -> None:
        """保存已观测原型，避免无人取回异常；保留数据库中的未完成版本。"""
        if not task.cancelled():
            self._failure = task.exception()

    async def _run(self) -> None:
        """只在有可靠版本待办时进入 Provider；网络等待始终在 SQL 事务之外。"""
        while not self._closing:
            self.can_start()
            revision = self.changes.change_revision
            keywords = await self.store.pending_index(route="keyword", limit=100)
            if keywords:
                await self._commit(self.store.refresh_keyword_index(limit=100))
            vectors: tuple[IndexJob, ...] = ()
            if self.embedding is not None:
                await self._commit(
                    self.store.queue_semantic_rebuild(self.embedding.model_id, limit=32)
                )
                vectors = await self.store.pending_index(route="semantic", limit=32)
                if vectors:
                    await self._index_vectors(vectors, self.embedding)
            if keywords or vectors:
                continue
            await self.changes.wait_for_change(revision)

    async def _index_vectors(
        self, jobs: tuple[IndexJob, ...], embedding: EmbeddingProvider
    ) -> None:
        """固定版本与正文后计算一批；回包期间失效的版本由安装事务拒绝。"""
        current: list[tuple[IndexJob, str]] = []
        for job in jobs:
            record = await self.store.get_candidate(job.reference)
            if record is None:
                # 已过期任务仍按原版本确认；不能给新版本安装一个猜测向量。
                await self._commit(self.store.discard_stale_index(job.reference, route="semantic"))
            else:
                current.append((job, index_text(record)))
        if not current:
            return
        self.can_start()
        vectors = validate_vectors(
            await embedding.embed(tuple(text for _, text in current)), len(current)
        )
        for (job, text), vector in zip(current, vectors, strict=True):
            await self._commit(
                self.store.apply_index(
                    job.reference, terms=text, vector=vector, model=embedding.model_id
                )
            )

    async def _commit[Result](self, operation: Awaitable[Result]) -> Result:
        """持有已经接管的派生提交，取消观察不让真实事务失去所有者。"""
        task = asyncio.ensure_future(operation)
        self._commits.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._commits.discard(task)

    async def close(self) -> None:
        """取消外部计算并等待同一收尾，已经接管的事务必须实际归还。"""
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._close(), name="knowledge-index-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        """工作器先结束，残留提交结束后才允许 app 释放向量资源和母 Store。"""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._commits:
            results = await asyncio.gather(*tuple(self._commits), return_exceptions=True)
            self._commits.clear()
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                self._failure = self._failure or errors[0]
                raise BaseExceptionGroup("认识索引提交收尾失败。", errors)
