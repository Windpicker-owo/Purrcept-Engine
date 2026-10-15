"""保存公开值及其语义校验，不打开数据库或调用设备。"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import cast


def payload_bytes(*values: str | None) -> int:
    """在异步提交边界核验 UTF-8 文本，并计入控制队列实际载荷。"""
    total = 0
    for value in values:
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError("执行记录字段必须是字符串或 None。")
        try:
            total += len(value.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("执行记录不能表示为 UTF-8。") from error
    return total


def validate_json(value: str | None, *, arguments: bool = False) -> None:
    """只校验真实 JSON 边界；参数要求对象，结果允许任意标准 JSON 值。"""
    if value is None:
        if arguments:
            raise ValueError("调用参数 JSON 不能缺失。")
        return
    if not isinstance(value, str):
        raise ValueError("执行记录 JSON 必须是字符串。")
    try:
        parsed: object = json.loads(value, parse_constant=_reject_constant)
        _finite_json(parsed)
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError("执行记录不是标准有限 JSON。") from error
    if arguments and not isinstance(parsed, Mapping):
        raise ValueError("调用参数 JSON 必须表示对象。")


def _reject_constant(value: str) -> object:
    raise ValueError(f"非标准 JSON 常量：{value}")


def _finite_json(value: object) -> None:
    """拒绝解析为无穷大的合法数字文本，不只拦截 NaN/Infinity 扩展词。"""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON 数字必须有限。")
    if isinstance(value, Mapping):
        for item in cast(Mapping[str, object], value).values():
            _finite_json(item)
    elif isinstance(value, list):
        for item in cast(list[object], value):
            _finite_json(item)
