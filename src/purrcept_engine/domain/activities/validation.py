"""活动输入的明确校验边界；不读取当前状态或申请控制资格。"""

from __future__ import annotations

import math

from purrcept_engine.domain.activities.contracts import (
    ActivityError,
)


def validate_text(value: object, name: str) -> str:
    """拒绝空白或不可编码的活动标识及文字，不替调用方补入默认值。"""
    if not isinstance(value, str) or not value.strip():
        raise ActivityError(f"{name} 必须是非空字符串。", code="activity_arguments")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ActivityError(f"{name} 不能表示为 UTF-8。", code="activity_arguments") from error
    return value


def positive_seconds(value: object, name: str) -> float:
    """只接受正有限秒数，布尔值不代表运行额度。"""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ActivityError(f"{name} 必须是正有限数。", code="activity_arguments")
    return float(value)
