"""将已分类的模型输入错误交回当前工具轮，保留内部和权威错误原型。

Core 已负责工具名和 schema 参数校验；这里只处理 Engine 业务输入产生的
明确冲突、范围和行动源码错误。装配故障、存储失败及取消仍沿原调用链传播，
不会通过宽泛错误策略让模型反复尝试修复内部系统。
"""

from collections.abc import Awaitable, Callable
from functools import wraps

from purrcept_core.models import TextBlock
from purrcept_core.models.tools import ToolResult
from pydantic import ValidationError

from purrcept_engine.execution.validation import ActionSourceError
from purrcept_engine.storage.knowledge import KnowledgeInputError
from purrcept_engine.storage.model_requests import encode


def model_input_boundary[**P, R](
    function: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R | ToolResult]]:
    """保留公开签名及工具说明，仅转译有限可纠正错误，不重置本轮预算。"""

    @wraps(function)
    async def invoke(*args: P.args, **kwargs: P.kwargs) -> R | ToolResult:
        try:
            return await function(*args, **kwargs)
        except (KnowledgeInputError, ActionSourceError, ValidationError) as error:
            if isinstance(error, ValidationError):
                issues = error.errors(include_input=False, include_context=False, include_url=False)
                description = "; ".join(
                    f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
                    for issue in issues
                )
            else:
                description = str(error)
            return ToolResult(
                content=(
                    TextBlock(encode({"error_type": type(error).__name__, "error": description})),
                ),
                is_error=True,
            )

    return invoke
