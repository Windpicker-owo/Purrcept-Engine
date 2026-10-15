"""声明可选向量配置并拥有认识检索所借用的 HTTP 资源与固定索引工作器。

首批只实现与既有 openai/ embedding 路径一致的明确 HTTP 协议，不进入
LiteLLM 全局客户端、环境凭据或代理。配置校验和资源构造不发请求；关闭先
结束索引工作器和 HTTP 借用，再收回连接池。Reranker 是显式注入的扩展，
没有配置实现时不创建额外模型或端点。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from purrcept_engine.domain.knowledge.ports import Reranker
from purrcept_engine.domain.knowledge.semantic import (
    EmbeddingProvider,
    IndexChanges,
    KnowledgeIndexWorker,
    SemanticStore,
    VectorRetriever,
)
from purrcept_engine.domain.knowledge.service import KnowledgeService
from purrcept_engine.domain.knowledge.settings import EmbeddingSettings
from purrcept_engine.errors import HostStateError
from purrcept_engine.infrastructure.models.embedding import (
    OpenAIEmbeddingResource,
)


class KnowledgeResources:
    """Host 创建的认识资源所有者，借出同一 KnowledgeService 给前台和反思。

    embedding 配置缺省时不创建 HTTP 资源，只保留本地三路召回。注入的
    embedding/reranker 归调用者关闭；本对象只关闭自己创建的向量资源。
    Host 应先结束两条会话，再关闭本对象，最后关闭母 Store。
    """

    def __init__(
        self,
        store: SemanticStore,
        changes: IndexChanges,
        settings: EmbeddingSettings | None,
        *,
        page_chars: int,
        can_start: Callable[[], None],
        embedding: EmbeddingProvider | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        """取得明确所有权但不发请求。

        Args:
            store: 应用借出的领域存储端口，实际连接和关闭归资源所有者。
            changes: 同库提交完成后的变更通知端口。
            settings: 当前组件已校验的配置或纯声明。
            page_chars: 本实例统一的正文分页字符上限。
            can_start: 实际派发前重新检查的资格回调，等待后仍须复核。
            embedding: 显式借入的向量 Provider；连接释放仍归装配所有者。 None
                表示不使用语义向量路线。
            reranker: 可选重排器，只能对给定候选排序。 None 表示不重排。

        Raises:
            ValueError: 借入 Embedding 后端仍须显式声明向量配置；借入 Embedding
                后端的向量空间必须与显式配置一致。

        Note:
            没有配置时不接受隐含的向量后端。
        """
        if settings is None and embedding is not None:
            raise ValueError("借入 Embedding 后端仍须显式声明向量配置。")
        if (
            settings is not None
            and embedding is not None
            and embedding.model_id != settings.model_id
        ):
            raise ValueError("借入 Embedding 后端的向量空间必须与显式配置一致。")
        # Ordering: 先完成纯服务参数校验，避免页长错误发生在自有 HTTP 池取得之后。
        self.knowledge = KnowledgeService(store, page_chars=page_chars, reranker=reranker)
        self._owned_embedding = (
            OpenAIEmbeddingResource(settings)
            if settings is not None and embedding is None
            else None
        )
        provider = embedding if embedding is not None else self._owned_embedding
        if provider is not None:
            self.knowledge.semantic = VectorRetriever(store, provider)
        self.indexer = KnowledgeIndexWorker(store, changes, provider, can_start=can_start)
        self._close_task: asyncio.Task[None] | None = None
        self._resources_released = False

    @property
    def failure(self) -> BaseException | None:
        """返回索引工作器真实故障。

        Note:
            检索调用的故障直接返回其调用者。
        """
        return self.indexer.failure

    @property
    def pending_resources(self) -> int:
        """汇总固定工作器、提交和自有向量传输的真实借用。"""
        return self.indexer.pending_resources + (
            0 if self._owned_embedding is None else self._owned_embedding.pending_resources
        )

    @property
    def resources_released(self) -> bool:
        """确认索引借用及自有 HTTP 池已归还，原提交故障仍由 close 报告。

        Note:
            pending_resources 为零只说明没有在途请求，不能证明连接池已经关闭。
            Host 仅在此事实成立后释放本所有者及母 Store，不根据错误类型推测
            传输寿命，也不把借入的调用者资源纳入关闭责任。
        """
        return self._resources_released

    async def start(self) -> None:
        """在 Host 允许新派发后启动一个固定工作器。

        Raises:
            HostStateError: 认识资源已开始关闭。
        """
        if self._close_task is not None:
            raise HostStateError("认识资源已开始关闭。")
        await self.indexer.start()

    async def close(self) -> None:
        """等待唯一关闭任务，不把调用者借入的资源当作自身连接池关闭。"""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="knowledge-resources-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        """先结束索引网络/提交借用，再释放自身创建的向量传输。"""
        errors: list[BaseException] = []
        try:
            await self.indexer.close()
        except BaseException as error:
            # 此处只收集资源清理原型。indexer.close 已等全部接管提交真正结束，
            # 即使它们报告失败，HTTP 池仍属于 app 必须归还的独立资源。
            errors.append(error)
        transport_released = self._owned_embedding is None
        if self._owned_embedding is not None:
            try:
                await self._owned_embedding.close()
            except BaseException as error:
                errors.append(error)
            else:
                transport_released = True
        # 索引提交失败在工作器全部归还后报告；HTTP 关闭失败则仍不能证明
        # 连接池释放。把资源事实登记在抛错之前，供 Host 继续独立清理。
        self._resources_released = self.indexer.pending_resources == 0 and transport_released
        if errors:
            raise BaseExceptionGroup("认识资源收尾失败。", errors)
