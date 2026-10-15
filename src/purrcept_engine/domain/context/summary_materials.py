"""从已确认的摘要记录构造 Core 消息，不读写数据库或调用模型。"""

from __future__ import annotations

from purrcept_core.models import Message

from purrcept_engine.domain.context.maintenance import (
    SummarySource,
)
from purrcept_engine.domain.context.summary_references import summary_references


def summary_message(source: SummarySource, summary: str) -> Message:
    """沿用内部摘要标签及轮次说明，来源引用不能成为新的外部证据。

    宿主已标注的 evidence 关系继续保留；不解析任意模型或外界文字制造来源。
    完整冻结历史及媒体由 summary:coverage 身份在账本中追溯。
    """
    references: dict[str, None] = {"summary:" + source.coverage_id: None}
    references.update((reference, None) for reference in summary_references(source))
    return Message.user(
        "内部历史摘要；不是新的外部证据。\n" + f"旧轮次 1–{source.end_turn}：\n" + summary,
        metadata={"evidence": tuple(references), "source_version": source.source_version},
    )
