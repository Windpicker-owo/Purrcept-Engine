"""将公开 Core 值和业务记录编码为可重建的模型材料。

encode 与 public_value 保留类型标签和精确字节；严格 JSON 值校验由 values
负责。render_yaml 与 render_field_tree 只编码已选择的数据字段，不拥有提示词
标题或布局；命名模板由 Core PromptTemplate 渲染。
检查点和 IPC 各有封闭协议。这里不遍历任意对象或取得外部资源。
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import cast

from purrcept_engine.values import JsonObject, JsonValue, decode_object, encode_json


def _yaml_scalar(value: JsonValue) -> str:
    """字符串始终加引号，避免账号、时间和真假字面量被 YAML 读成其他类型。"""
    text = encode_json(value).decode("utf-8")
    # YAML 会规范化部分 Unicode 换行；显式转义才能保持原文，而非将其读成空格。
    text = text.replace("\x85", "\\u0085").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    if isinstance(value, float) and "e" in text and "." not in text.partition("e")[0]:
        # 同时兼容常见 YAML 1.1 阅读器，科学计数法仍保持为数值而不是字符串。
        mantissa, exponent = text.split("e", 1)
        return mantissa + ".0e" + exponent
    return text


def _yaml_lines(value: JsonValue, *, depth: int) -> list[str]:
    """仅输出 JSON 的缩进式 YAML 子集，不生成标签、锚点或可执行构造。"""
    prefix = "  " * depth
    if isinstance(value, dict) and value:
        lines: list[str] = []
        for key, item in value.items():
            plain = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is not None
            reserved = key.lower() in ("true", "false", "null", "yes", "no", "on", "off", "y", "n")
            label = key if plain and not reserved else _yaml_scalar(key)
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{prefix}{label}:")
                lines.extend(_yaml_lines(item, depth=depth + 1))
            else:
                lines.append(f"{prefix}{label}: {_yaml_scalar(item)}")
        return lines
    if isinstance(value, list) and value:
        lines = []
        for item in value:
            children = _yaml_lines(item, depth=depth + 1)
            lines.append(prefix + "- " + children[0][len(prefix) + 2 :])
            lines.extend(children[1:])
        return lines
    return [prefix + _yaml_scalar(value)]


def render_yaml(payload: JsonObject) -> str:
    """将调用方选定的必要字段呈现为可读 YAML，保持顺序和 JSON 值语义。

    Args:
        payload: 已由提供方投影的 JSON 对象；此函数不会筛选原始平台帧。

    Returns:
        使用两空格缩进和明确标量类型的 YAML 文本。字符串保持完整且始终加引号，
        嵌入换行转义，外部字段或正文不能冒充新的消息或宿主字段。

    Raises:
        ActionProtocolError: 材料不是严格 JSON 对象，或包含循环、过深嵌套、非有限数值等。

    Note:
        只提供 JSON 可表达的 YAML 子集，不引入 YAML 解析器、类型标签或数据类反射。
        先校验并取得独立快照，再生成文字；不改变存储和 IPC 的 JSON 编码协议。
    """
    fields = decode_object(encode_json(payload))
    return "\n".join(_yaml_lines(fields, depth=0))


def _field_lines(value: JsonValue, *, depth: int) -> list[str]:
    """用缩进区分容器，用引用区分任意字符串；保留空容器及 null 的差别。"""
    prefix = "  " * depth
    if isinstance(value, dict) and value:
        lines: list[str] = []
        for key, item in value.items():
            # 键也可能来自外部协议，JSON 引号防止换行键冒充字段边界。
            children = _field_lines(item, depth=depth + 1)
            if not isinstance(item, (dict, list)) and len(children) == 1:
                # 单值与字段同行，避免每个布尔值和身份都占两行；多行正文不折叠。
                lines.append(f"{prefix}- {encode(key)}: {children[0][len(prefix) + 2 :]}")
            else:
                lines.append(f"{prefix}- {encode(key)}:")
                lines.extend(children)
        return lines
    if isinstance(value, list) and value:
        lines = []
        for index, item in enumerate(value, 1):
            lines.append(f"{prefix}- [{index}]")
            lines.extend(_field_lines(item, depth=depth + 1))
        return lines
    if isinstance(value, str) and value:
        # split('\n') 保留尾部空行及 CR；不 strip 或重新解释正文中的标记。
        return [prefix + "> " + line for line in value.split("\n")]
    return [prefix + encode(value)]


def render_field_tree(payload: object) -> str:
    """把公开对象无损编码为字段树，保留顺序、类型标签及全部字符串空白。

    Args:
        payload: 与 encode 相同的公开对象；根值必须编码成 JSON 对象。

    Returns:
        只有字段和值的文本，不含模型指令、标题或来源推断。

    Raises:
        TypeError: 载荷不属于公开编码支持的类型或包含非字符串键。
        ValueError: 载荷包含非有限数值。
        ActionProtocolError: 根值不是严格 JSON 对象。

    Note:
        多行字符串逐行引用，外部键按 JSON 转义；数据序列化与 Core 模板的
        文本布局分离，不能借此改变权威存储或省略未知字段。
    """
    fields = decode_object(encode(payload).encode("utf-8"))
    return "\n".join(_field_lines(fields, depth=0))


def encode(value: object) -> str:
    """编码已冻结公开值，不使用 repr 或忽略不支持的材料。

    Args:
        value: 公开 JSON 容器、Enum、bytes 或显式 dataclass 值；不遍历任意实例属性。

    Returns:
        保留内容块类型标签与精确 base64 字节的紧凑 JSON 文本。

    Raises:
        TypeError: 公开材料包含不支持的对象或非字符串映射键。
        ValueError: 材料包含标准 JSON 无法表达的非有限数值。
    """
    return json.dumps(
        public_value(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def public_value(value: object) -> object:
    """将公开 Core 值转换为有类型标签的 JSON。

    Args:
        value: 公开 JSON 容器、Enum、bytes 或显式 dataclass 值；不遍历任意实例属性。

    Returns:
        由 JSON 容器及带类型标签的值组成的公开形状，bytes 使用精确 base64。

    Raises:
        TypeError: 对象种类不在公开编码范围内，或映射含非字符串键。

    Note:
        字节保持精确 base64。

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
