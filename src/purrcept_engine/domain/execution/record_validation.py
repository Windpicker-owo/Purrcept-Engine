"""校验执行记录的数值、身份和已编码 JSON 字段。

调用者在接管外部记录前使用这些纯校验；它们不修改记录、不提交账本，
也不将字段合法性当作真实效果或派发资格。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import cast


def payload_bytes(*values: str | None) -> int:
    """在异步提交边界核验 UTF-8 文本，并计入控制队列实际载荷。

    Args:
        values: 待校验、编码或构造数组的原始值集合。

    Raises:
        ValueError: 执行记录字段必须是字符串或 None；执行记录不能表示为 UTF-8。
    """
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
    """只校验真实 JSON 边界。

    Args:
        value: 已编码 JSON 文本；解析后必须仍符合执行记录的值边界。
        arguments: 是否要求顶层为参数对象；False 允许结果为任意支持的 JSON 值。 默认 False。

    Raises:
        ValueError: 参数值或组合不满足本入口的明确约束，错误正文指出具体字段。

    Note:
        参数要求对象，结果允许任意标准 JSON 值。
    """
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
