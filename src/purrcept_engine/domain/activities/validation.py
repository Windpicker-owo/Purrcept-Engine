"""校验活动标识与版本化记录中的严格 JSON 字段。

入口供活动契约和服务在采用外部值前调用；这里只拒绝无效值，不写入账本
或以校验成功代替活动运行资格。
"""

from __future__ import annotations

import math

from purrcept_engine.domain.activities.contracts import (
    ActivityError,
)


def validate_text(value: object, name: str) -> str:
    """拒绝空白或不可编码的活动标识及文字，不替调用方补入默认值。

    Args:
        value: 必须非空白且可编码为 UTF-8 的业务标识或说明。
        name: 用于指出无效字段的稳定字段名。

    Raises:
        ActivityError: 参数、材料或当前状态不满足此入口的约束。
    """
    if not isinstance(value, str) or not value.strip():
        raise ActivityError(f"{name} 必须是非空字符串。", code="activity_arguments")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ActivityError(f"{name} 不能表示为 UTF-8。", code="activity_arguments") from error
    return value


def positive_seconds(value: object, name: str) -> float:
    """只接受正有限秒数，布尔值不代表运行额度。

    Args:
        value: 必须是正有限数值的秒数，明确拒绝 bool。
        name: 用于指出预算错误的字段名。

    Raises:
        ActivityError: 参数、材料或当前状态不满足此入口的约束。
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ActivityError(f"{name} 必须是正有限数。", code="activity_arguments")
    return float(value)
