"""装配 Notebook 的命名模型和派生检索资源，业务任务仍由插件持有。

每个实例有隔离账本、向量空间及可选重排借用。模型请求通过现有证据/重试包装，
先检查完整编译容量，再发给后端；本模块不调度整理、不修改笔记。
"""

from __future__ import annotations

import math
from collections.abc import Callable

from purrcept_core.models import (
    Message,
    ModelBackend,
    ModelRequest,
    ModelResponse,
    ModelSettings,
    SystemInstruction,
)

from purrcept_engine.app.config import EngineConfig, GenerationModelSettings
from purrcept_engine.domain.context.capacity import GenerationCapacity, check_capacity
from purrcept_engine.domain.evidence.models import (
    EvidenceCandidate,
    EvidenceReference,
    EvidenceSnapshot,
)
from purrcept_engine.domain.model_requests.models import RequestScope
from purrcept_engine.domain.model_requests.ports import RetryPolicy
from purrcept_engine.domain.model_requests.service import (
    ModelGenerationBackend,
    RequestBudget,
    prepare_off_loop,
)
from purrcept_engine.domain.notebook.contracts import Document, Publication, Work
from purrcept_engine.domain.notebook.embedding import EmbeddingProvider, Reranker, validate_vectors
from purrcept_engine.domain.notebook.settings import EmbeddingSettings
from purrcept_engine.infrastructure.models.compiler import compile_materials
from purrcept_engine.infrastructure.models.embedding import OpenAIEmbeddingResource
from purrcept_engine.infrastructure.sqlite.evidence_port import EvidenceLedger
from purrcept_engine.infrastructure.sqlite.notebook import NotebookLedger
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.values import JsonObject, decode_object


class PluginModel:
    """为插件别名绑定一个生成配置，客户端由宿主共享，单次包装由调用者归还。"""

    def __init__(
        self,
        store: Store,
        backend: ModelBackend,
        settings: GenerationModelSettings,
        config: EngineConfig,
        owner: str,
        can_start: Callable[[], None],
        retry: RetryPolicy,
    ) -> None:
        """借入命名后端及明确容量，不创建会话或定时任务。"""
        self.store, self.backend, self.settings = store, backend, settings
        self.config, self.owner, self.can_start = config, owner, can_start
        self.retry = retry

    async def generate(
        self,
        instructions: tuple[SystemInstruction, ...],
        messages: tuple[Message, ...],
        *,
        batch_id: str,
        sources: tuple[str, ...],
        budget: RequestBudget,
    ) -> ModelResponse:
        """检查完整请求后记录全部尝试；关闭包装等待其权威提交实际归还。"""
        budget.check()
        request = ModelRequest(
            messages,
            instructions=instructions,
            settings=ModelSettings(max_output_tokens=self.settings.max_output_tokens),
        )

        def assess() -> None:
            materials, _ = compile_materials(request, model=self.settings.model)
            payload = decode_object(materials.encode())["payload"]
            if not isinstance(payload, dict):
                raise TypeError("模型编译缺少完整 payload。")
            check_capacity(
                payload,
                GenerationCapacity(
                    self.settings.context_window,
                    self.settings.max_output_tokens,
                    self.settings.input_limit,
                ),
                reserve_fraction=self.config.context.input_reserve_fraction,
                trigger=self.config.context.summary_trigger,
                target=self.config.context.summary_target,
            )

        await prepare_off_loop(assess)
        wrapper = ModelGenerationBackend(
            self.backend,
            self.store,
            compiler=compile_materials,
            scope=RequestScope(self.owner, batch_id, material_refs=sources),
            budget=budget,
            policy=self.retry,
            diagnostic_bytes=self.config.diagnostics.max_bytes,
            can_start=self.can_start,
        )
        try:
            return await wrapper.generate(request, model=self.settings.model)
        finally:
            await wrapper.close()


class NotebookAccess:
    """应用为一个 Notebook 创建的有限资源门面；工作器与文件仍由插件持有。

    所有提交通过实例账本。注入的向量/重排器由调用方关闭，本对象只释放自己
    创建的 HTTP 池。宿主在插件归还所有借用后调用 close，再关闭母 Store。
    """

    def __init__(
        self,
        ledger: NotebookLedger,
        evidence: EvidenceLedger,
        settings: EmbeddingSettings | None,
        *,
        embedding: EmbeddingProvider | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        """绑定命名空间与向量资源，配置不完整时明确拒绝而不猜测默认。"""
        if embedding is not None and (settings is None or embedding.model_id != settings.model_id):
            raise ValueError("Notebook 借入向量资源必须匹配显式 embedding 空间。")
        self.ledger, self.evidence, self.reranker = ledger, evidence, reranker
        self.owned = (
            OpenAIEmbeddingResource(settings)
            if settings is not None and embedding is None
            else None
        )
        self.embedding = embedding if embedding is not None else self.owned

    async def documents(self) -> tuple[Document, ...]:
        """读取本实例可重建的当前文件登记。"""
        return await self.ledger.documents()

    async def register(self, document: Document, *, needs_work: bool) -> None:
        """登记确认发布的文件，正文不会成为第二份可编辑权威。"""
        await self.ledger.register(document, needs_work=needs_work)

    async def remove(self, paths: tuple[str, ...]) -> None:
        """撤销明确不存在路径的当前索引，不删历史证据。"""
        await self.ledger.remove(paths)

    async def pending(self, *, limit: int = 32) -> tuple[Work, ...]:
        """读取当前实例的有限整理待办。"""
        return await self.ledger.pending(limit=limit)

    async def advance(self, work: Work, cursor: str | None) -> None:
        """为仍匹配的源版本推进实际处理范围。"""
        await self.ledger.advance(work, cursor)

    async def postpone(self, works: tuple[Work, ...], reason: str) -> None:
        """保留未完成范围，并持久保存失败冷却。"""
        await self.ledger.postpone(works, reason)

    async def publications(self) -> tuple[Publication, ...]:
        """读取本实例尚需文件核验的发布意图。"""
        return await self.ledger.publications()

    async def intend(self, publication: Publication) -> None:
        """先保存发布意图，再由 Notebook 进入实际文件写入。"""
        await self.ledger.intend(publication)

    async def settle(self, identity: str, state: str) -> None:
        """登记发布的实际核验结论，不重放文件操作。"""
        await self.ledger.settle(identity, state)

    async def maintain_index(self) -> None:
        """至多计算32个缺失的当前向量，网络等待在 SQL 事务之外。"""
        if self.embedding is None:
            return
        installed = {
            doc.path: doc.file_version
            for doc, _ in await self.ledger.vectors(self.embedding.model_id)
        }
        documents = tuple(
            doc
            for doc in await self.ledger.documents()
            if installed.get(doc.path) != doc.file_version
        )[:32]
        if not documents:
            return
        snapshots = tuple(
            [
                await self.evidence.get_evidence(doc.evidence_id, version=doc.version)
                for doc in documents
            ]
        )
        if any(snapshot is None for snapshot in snapshots):
            raise ValueError("Notebook 向量来源缺少确切快照。")
        vectors = validate_vectors(
            await self.embedding.embed(tuple(s.text for s in snapshots if s is not None)),
            len(documents),
        )
        for document, vector in zip(documents, vectors, strict=True):
            await self.ledger.install_vector(document, self.embedding.model_id, vector)

    async def search(
        self, query: str, *, path: str = "", limit: int = 40
    ) -> tuple[EvidenceSnapshot, ...]:
        """融合本实例关键词和可选向量路线，明确保留配置后端的真实失败。"""
        keywords = await self.ledger.search(query, path=path, limit=limit)
        scores = {(s.evidence_id, s.version): 1 / (60 + rank) for rank, s in enumerate(keywords, 1)}
        snapshots = {(s.evidence_id, s.version): s for s in keywords}
        if self.embedding is not None:
            vector = validate_vectors(await self.embedding.embed((query,)), 1)[0]

            def cosine(other: tuple[float, ...]) -> float:
                if len(other) != len(vector):
                    raise ValueError("Notebook 同空间向量维度不一致。")
                return sum(a * b for a, b in zip(vector, other, strict=True)) / (
                    math.hypot(*vector) * math.hypot(*other)
                )

            ranked = sorted(
                (
                    item
                    for item in await self.ledger.vectors(self.embedding.model_id)
                    if not path or item[0].path == path or item[0].path.startswith(path + "/")
                ),
                key=lambda item: (-cosine(item[1]), item[0].path),
            )[:40]
            for rank, (document, _) in enumerate(ranked, 1):
                snapshot = await self.evidence.get_evidence(
                    document.evidence_id, version=document.version
                )
                if snapshot is not None:
                    key = (snapshot.evidence_id, snapshot.version)
                    snapshots[key] = snapshot
                    scores[key] = scores.get(key, 0) + 1 / (60 + rank)
        ordered = sorted(snapshots, key=lambda key: (-scores[key], key))
        if self.reranker is not None:
            candidates = tuple(
                EvidenceCandidate(
                    reference=EvidenceReference(kind="evidence", id=key[0], version=key[1]),
                    record=snapshots[key],
                )
                for key in ordered
            )
            references = await self.reranker.rank(query, candidates)
            proposed = [(ref.id, ref.version) for ref in references]
            if len(set(proposed)) != len(proposed) or not set(proposed) <= set(snapshots):
                raise ValueError("Notebook 重排返回未知或重复版本。")
            ordered = [*proposed, *(key for key in ordered if key not in proposed)]
        return tuple(snapshots[key] for key in ordered[:limit])

    async def status(self) -> JsonObject:
        """报告任务及向量积压，启用语义路线不等于所有当前版本已经建好向量。"""
        missing = 0
        if self.embedding is not None:
            installed = {
                doc.path: doc.file_version
                for doc, _ in await self.ledger.vectors(self.embedding.model_id)
            }
            missing = sum(
                installed.get(doc.path) != doc.file_version for doc in await self.ledger.documents()
            )
        return {
            **await self.ledger.status(),
            "semantic_enabled": self.embedding is not None,
            "semantic_pending": missing,
        }

    async def close(self) -> None:
        """在插件归还借用后关闭本对象创建的向量 HTTP 池。"""
        if self.owned is not None:
            await self.owned.close()
