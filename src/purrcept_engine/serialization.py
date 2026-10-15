"""保存公开值及其语义校验，不打开数据库或调用设备。"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import cast


def encode(value: object) -> str:
    """编码已冻结公开值，不使用 repr 或忽略不支持的材料。"""
    return json.dumps(
        public_value(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def public_value(value: object) -> object:
    """将公开 Core 值转换为有类型标签的 JSON；字节保持精确 base64。

    类型标签用于区分 refusal、概率及不同内容块。此函数不遍历异常、客户端或
    任意实例属性；只有 JSON 容器及显式 dataclass 值可进入权威材料。
    """
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, bytes):
        return {"type": "bytes", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        result: dict[str, object] = {}
        for key, item in mapping.items():
            if not isinstance(key, str):
                raise TypeError("公开 JSON 对象只接受字符串键。")
            result[key] = public_value(item)
        return result
    if isinstance(value, Sequence):
        return [public_value(item) for item in cast(Sequence[object], value)]
    if is_dataclass(value) and not isinstance(value, type):
        result: dict[str, object] = {"type": type(value).__name__}
        for item in fields(value):
            field_value: object = getattr(value, item.name)
            result[item.name] = public_value(field_value)
        return result
    raise TypeError(f"公开模型材料无法编码：{type(value).__name__}")
