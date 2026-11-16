"""声明 Notebook 的可选向量与候选重排契约。

应用按插件实例创建或借入资源，Notebook 工作器推动索引；网络等待不进入
数据库事务。模型空间必须明确，配置后端失败保持原型，不静默退回另一条路线。
"""

from __future__ import annotations

import math
from typing import Protocol

from purrcept_engine.domain.evidence.models import EvidenceCandidate, EvidenceReference


class EmbeddingProtocolError(ValueError):
    """配置的向量Provider返回不完整或无效响应，停止当前查询而不伪造召回。

    内置HTTP边界验证序号、维度、有限非零向量；公开错误不携带凭据或响应
    原文。主体将它视为该批实际外部失败，内部算法异常仍保留原型并停止工作器。
    """


class EmbeddingProvider(Protocol):
    """app 创建或显式借入的向量资源，仅供当前 Notebook 的检索和索引流程借用。

    model_id 标识决定向量空间的明确版本，不是展示名称；变更后必须重建。
    embed 保留输入顺序，返回同维、非空、非零和有限向量。网络与取消原样
    传出，不写权威事实。实现可以复用连接池，但每次请求保持独立材料。
    内置实现见 infrastructure.models.embedding；资源关闭由创建它的 app 所有者负责。
    """

    @property
    def model_id(self) -> str:
        """返回冻结的向量空间版本，查询与索引只能使用这一空间。"""
        ...

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """为至多 32 个完整文本取得对应向量，缺项或异常维度不能伪造成功。

        Args:
            texts: 按原顺序交给 embedding 的非空文本批次。

        Returns:
            按文本输入顺序取得、数量和维度已核验的有限向量。
        """
        ...


def validate_vectors(
    vectors: tuple[tuple[float, ...], ...], expected: int
) -> tuple[tuple[float, ...], ...]:
    """核验内置和注入 Provider 的同一契约，不能以零向量补缺或截断维度。

    Args:
        vectors: 按输入顺序返回的向量序列，数量与维度必须一致。
        expected: 此次比较或提交必须仍匹配的原始观察值。

    Returns:
        按文本输入顺序取得、数量和维度已核验的有限向量。

    Raises:
        ValueError: 参数值或组合不满足本入口的明确约束，错误正文指出具体字段。
    """
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


class Reranker(Protocol):
    """显式配置的可选重排，只能排列给出的冻结候选，不添加未知来源。"""

    async def rank(
        self, query: str, candidates: tuple[EvidenceCandidate, ...]
    ) -> tuple[EvidenceReference, ...]:
        """返回唯一候选身份序列，可省略候选而不能制造另一版本。

        Args:
            query: 用于关键词或语义检索的意图文本。
            candidates: 已通过权威核验的冻结候选，重排不得新增来源。

        Returns:
            按相关性排列的确切版本引用，使用前仍须复查权威资格。
        """
        ...
