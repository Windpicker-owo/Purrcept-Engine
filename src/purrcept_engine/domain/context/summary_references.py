"""解析宿主元数据中的显式摘要依赖，不解释外部正文。"""

from __future__ import annotations

from purrcept_engine.domain.context.maintenance import (
    SummaryError,
    SummarySource,
)


def summary_references(source: SummarySource) -> tuple[str, ...]:
    """仅借用宿主消息metadata的明确引用，外界JSON正文不能增加摘要依赖。

    Args:
        source: 已冻结的历史范围及所属会话版本。

    Raises:
        SummaryError: 宿主摘要来源标记必须是身份数组；宿主摘要来源标记不是非空身份。
    """
    references: dict[str, None] = {}
    for message in source.messages:
        declared = message.metadata.get("evidence")
        if declared is None:
            continue
        if not isinstance(declared, tuple):
            raise SummaryError("宿主摘要来源标记必须是身份数组。")
        for reference in declared:
            if not isinstance(reference, str) or not reference:
                raise SummaryError("宿主摘要来源标记不是非空身份。")
            references[reference] = None
    return tuple(references)
