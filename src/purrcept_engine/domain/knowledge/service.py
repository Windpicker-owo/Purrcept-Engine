"""主体的认识读取与有限检索端口，不取得任意 SQL 或另开数据库连接。

KnowledgeStore 由 app 注入的 SQLite 账本实现；KnowledgeService 借用已提交记录，融合
关键词/关系/时间与显式配置的可选语义路线，异步等待后再次核验来源和版本。
可选服务错误原样报告，不偷偷把已配置语义路线改成关键词路线。模型容量由
请求装配核验，本模块不按字符数猜 token 或静默裁剪检索结果。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import cast

from purrcept_engine.domain.knowledge.models import (
    EvidencePage,
    EvidenceSnapshot,
    KnowledgeCandidate,
    KnowledgeInputError,
    MemoryRecord,
)
from purrcept_engine.domain.knowledge.ports import KnowledgeStore, Reranker, SemanticRetriever


@dataclass(frozen=True, slots=True)
class RecallResult:
    """有限检索结果及实际路线状态，不把禁用或尚未索引解释为没有事实。

    Attributes:
        items: 经过融合和权威版本复核的有限认识候选。
        semantic: 可选语义召回路线；未配置时只使用本地检索。
        reranked: 是否实际执行了已配置的候选重排。
        index_pending: 当前有限读取发现的关键词索引积压数量。
        index_pending_is_lower_bound: 关键词积压数量是否仅为有界扫描下界。
        embedding_pending: 当前有限读取发现的语义索引积压数量。
        embedding_pending_is_lower_bound: 语义积压数量是否仅为有界扫描下界。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    items: tuple[KnowledgeCandidate, ...]
    semantic: bool
    reranked: bool
    index_pending: int
    index_pending_is_lower_bound: bool
    embedding_pending: int = 0
    embedding_pending_is_lower_bound: bool = False


def recall_payload(result: RecallResult) -> str:
    """前台与反思共用实际召回形状，默认摘要层不授予原文已读资格。

    Args:
        result: 已经融合并复核版本有效性的召回结果。

    Note:
        保留模型可见的 id/text/version/sources/score/ranks，另明确当前资格和
        启用路线。有限积压预览不能伪装成全部待办数量；容量由实际请求装配核验。
    """
    return json.dumps(
        {
            "items": [_recall_item(item) for item in result.items],
            "coverage": {
                "semantic": "enabled" if result.semantic else "disabled",
                "reranker": "enabled" if result.reranked else "disabled",
                "index_pending": result.index_pending,
                "index_pending_is_lower_bound": result.index_pending_is_lower_bound,
                "embedding_pending": result.embedding_pending,
                "embedding_pending_is_lower_bound": result.embedding_pending_is_lower_bound,
            },
        },
        ensure_ascii=False,
        allow_nan=False,
    )


def _recall_item(candidate: KnowledgeCandidate) -> dict[str, object]:
    """默认交付摘要及未决版本引用，不自动展开对方正文或授予来源资格。"""
    record = candidate.record
    if isinstance(record, EvidenceSnapshot):
        text = record.summary or record.items_json or record.text
        sources = (candidate.reference.id,)
        active = record.valid
        reason = None if active else "source_revoked"
    else:
        text = record.patch.summary or record.patch.content
        sources = record.patch.sources
        active = record.active
        reason = record.inactive_reason
    item: dict[str, object] = {
        "id": candidate.reference.id,
        "text": text,
        "version": candidate.reference.version,
        "sources": sources,
        "active": active,
        "inactive_reason": reason,
        "reference": candidate.reference.model_dump(mode="json"),
        "ranks": dict(candidate.ranks),
        "score": sum(1 / (60 + rank) for _, rank in candidate.ranks),
    }
    if isinstance(record, MemoryRecord):
        item["conflicts"] = [relation.model_dump(mode="json") for relation in record.conflicts]
    return item


class KnowledgeService:
    """主体借用的只读检索/阅读入口，认识写入仍只通过反思批次提交口。"""

    def __init__(
        self,
        store: KnowledgeStore,
        *,
        page_chars: int = 8000,
        semantic: SemanticRetriever | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        """固定显式检索资源和页长，不寻找自动替代模型。

        Args:
            store: 应用借出的领域存储端口，实际连接和关闭归资源所有者。
            page_chars: 本实例统一的正文分页字符上限。 默认 8000。
            semantic: 可选语义召回路线；未配置时只使用本地检索。
            reranker: 可选重排器，只能对给定候选排序。 None 表示不重排。

        Raises:
            ValueError: 原文页长必须是正整数。
        """
        if isinstance(page_chars, bool) or not isinstance(page_chars, int) or page_chars < 1:
            raise ValueError("原文页长必须是正整数。")
        self.store = store
        self.page_chars = page_chars
        self.semantic = semantic
        self.reranker = reranker

    async def read_evidence(
        self,
        identity: str,
        level: str = "evidence",
        cursor: str | None = None,
        max_chars: int | None = None,
        version: int | None = None,
    ) -> EvidencePage:
        """展开指定版本或当前版本。

        Args:
            identity: 需要展开的原文或认识稳定身份；游标续读仍核对原身份与版本。
            level: evidence 展开正文，summary 或 overview 只读对应概要。 默认 'evidence'。
            cursor: 宿主签发并保存的不透明续读游标，续读同一快照版本。 None 表示首次读取。
            max_chars: 本页正文的字符上限；分页游标继续绑定原快照。 None
                使用本实例配置的统一页长。
            version: 目标记录的确切版本，区别于可变的当前版本。 None 首读当前版本，
                续读仍绑定游标的原版本。

        Returns:
            当前版本的一页正文、确切范围和下一游标；取得页面不等于已呈现给模型。

        Raises:
            KnowledgeInputError: 来源、版本、层级、游标或页长不满足读取要求。
            StorageError: 权威存储不可用或保存的材料损坏。

        Note:
            游标始终绑定原版本，取得不登记实际呈现。
        """
        return await self.store.read_evidence(
            identity,
            level=level,
            cursor=cursor,
            max_chars=self.page_chars if max_chars is None else max_chars,
            version=version,
        )

    async def recall(
        self,
        query: str,
        entities_json: str = "[]",
        start: float | None = None,
        end: float | None = None,
        at: float | None = None,
        include_inactive: bool = False,
        *,
        limit: int = 12,
    ) -> RecallResult:
        """融合本地与已配置的可选路线，返回经版本核验的认识候选。

        Args:
            query: 用于关键词或语义检索的意图文本。
            entities_json: 已编码为 JSON 字符串数组的实体过滤条件。 默认 '[]'。
            start: 按发生时间筛选的起点；None 表示不限制此端。
            end: 按发生时间筛选的终点；None 表示不限制此端。
            at: 核验记录有效性的 Unix 时间戳；None 使用当前时间。
            include_inactive: 是否同时返回已失效的历史记录，并保留失效原因。 默认 False。
            limit: 本次读取或更新的最大条目数。 默认 12。

        Returns:
            融合并复核版本后的候选，以及实际启用路线与索引积压信息。

        Raises:
            KnowledgeInputError: 来源、版本、时间范围或材料引用不满足当前操作要求。
            RuntimeError: 语义检索返回重复版本身份；重排返回未知或重复候选版本。

        Note:
            RRF 融合保留三条本地路线，语义/重排等待后再次查权威资格。
        """
        try:
            entities: object = json.loads(entities_json)
        except json.JSONDecodeError as error:
            raise KnowledgeInputError("entities_json 必须是身份字符串数组。") from error
        if not isinstance(entities, list):
            raise KnowledgeInputError("entities_json 必须是身份字符串数组。")
        raw_entities = cast(list[object], entities)
        identifiers_list: list[str] = []
        for item in raw_entities:
            if not isinstance(item, str) or not item:
                raise KnowledgeInputError("entities_json 必须是身份字符串数组。")
            identifiers_list.append(item)
        identifiers = tuple(identifiers_list)
        for value in (start, end, at):
            if value is not None and (isinstance(value, bool) or not math.isfinite(value)):
                raise KnowledgeInputError("查询时间必须是有限数值。")
        if (start is None) != (end is None) or (
            start is not None and end is not None and end < start
        ):
            raise KnowledgeInputError("发生时间查询需要有序 start/end。")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise KnowledgeInputError("检索条数必须是正整数。")

        await self.store.refresh_keyword_index(limit=100)
        candidates = await self.store.search_knowledge(
            query,
            entities=identifiers,
            start=start,
            end=end,
            at=at,
            include_inactive=include_inactive,
            limit=40,
        )
        by_reference = {item.reference: item for item in candidates}
        if self.semantic is not None:
            references = await self.semantic.retrieve(query)
            if len(references) != len(set(references)):
                raise RuntimeError("语义检索返回重复版本身份。")
            rank = 0
            for reference in references[:40]:
                record = await self.store.get_candidate(
                    reference, include_inactive=include_inactive, at=at
                )
                if record is None:
                    continue
                rank += 1
                prior = by_reference.get(reference)
                ranks = () if prior is None else prior.ranks
                by_reference[reference] = KnowledgeCandidate(
                    reference=reference, record=record, ranks=(*ranks, ("semantic", rank))
                )
        ordered = tuple(
            sorted(
                by_reference.values(),
                key=lambda item: (
                    -sum(1 / (60 + rank) for _, rank in item.ranks),
                    item.reference.kind,
                    item.reference.id,
                    item.reference.version,
                ),
            )
        )
        if self.reranker is not None:
            proposed = await self.reranker.rank(query, ordered)
            if len(proposed) != len(set(proposed)) or not set(proposed) <= set(by_reference):
                raise RuntimeError("重排返回未知或重复候选版本。")
            ordered = tuple(
                by_reference[reference]
                for reference in (
                    *proposed,
                    *(item.reference for item in ordered if item.reference not in proposed),
                )
            )

        result: list[KnowledgeCandidate] = []
        for candidate in ordered:
            current = await self.store.get_candidate(
                candidate.reference, include_inactive=include_inactive, at=at
            )
            if current is not None:
                result.append(candidate.model_copy(update={"record": current}))
            if len(result) == limit:
                break
        pending = await self.store.pending_index(route="keyword", limit=100)
        embedding_pending = (
            await self.store.pending_index(route="semantic", limit=100)
            if self.semantic is not None
            else ()
        )
        return RecallResult(
            tuple(result),
            self.semantic is not None,
            self.reranker is not None,
            len(pending),
            len(pending) == 100,
            len(embedding_pending),
            len(embedding_pending) == 100,
        )
