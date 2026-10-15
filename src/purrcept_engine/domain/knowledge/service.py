"""主体的认识读取与有限检索端口，不取得任意 SQL 或另开数据库连接。

KnowledgeStore 由 app 的 Store 实现；KnowledgeService 借用已提交记录，融合
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
    """有限检索结果及实际路线状态，不把禁用或尚未索引解释为没有事实。"""

    items: tuple[KnowledgeCandidate, ...]
    semantic: bool
    reranked: bool
    index_pending: int
    index_pending_is_lower_bound: bool
    embedding_pending: int = 0
    embedding_pending_is_lower_bound: bool = False


def recall_payload(result: RecallResult) -> str:
    """前台与反思共用实际召回形状，默认摘要层不授予原文已读资格。

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
        """固定显式检索资源和页长，不寻找自动替代模型。"""
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
        """展开指定版本或当前版本；游标始终绑定原版本，取得不登记实际呈现。"""
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
        """RRF 融合保留三条本地路线，语义/重排等待后再次查权威资格。"""
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
