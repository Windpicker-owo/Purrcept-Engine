"""严格 JSON 值和编码边界；不持有 IPC 端点或接受任意 Python 对象。"""

import json
import math
from collections.abc import Mapping, Sequence
from typing import TypeGuard

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None


type JsonObject = dict[str, JsonValue]


_MAX_DEPTH = 64


class ActionProtocolError(ValueError):
    """帧、JSON 或管道所有权边界失败；code 可区分截断、容量及 I/O。"""

    def __init__(self, message: str, *, code: str = "invalid_payload") -> None:
        """保留可判断的错误类别，原生错误通过异常链保留。"""
        super().__init__(message)
        self.code = code


def _valid_text(value: str) -> None:
    """JSON 转义可能产生孤立代理项，必须保证最终值仍可表示为 UTF-8。"""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ActionProtocolError("JSON 字符串包含孤立代理项。") from error


def _is_container(value: object) -> TypeGuard[Mapping[object, object] | Sequence[object]]:
    """运行时容器成员尚未校验，先以只读 object 视图逐项建立 JSON 约束。"""
    return isinstance(value, (Mapping, Sequence))


def _to_pod(value: object, active: set[int], depth: int) -> JsonValue:
    """只接受明确 JSON 值，将有序序列和字符串键映射冻结为传输 POD。

    active 只记录当前祖先链，因此共享的非循环子对象可以重复出现；容器深度
    在递归前限制，避免 Python 递归限制成为实际协议边界。
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        _valid_text(value)
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ActionProtocolError("JSON 不接受非有限数值。")
        return value

    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ActionProtocolError("二进制内容必须通过媒体引用交付。")
    if not _is_container(value):
        raise ActionProtocolError("值不是受支持的 JSON 数据类型。")
    if depth >= _MAX_DEPTH:
        raise ActionProtocolError("JSON 容器嵌套过深。", code="max_depth")
    identity = id(value)
    if identity in active:
        raise ActionProtocolError("JSON 数据包含循环引用。")

    active.add(identity)
    try:
        if isinstance(value, Mapping):
            mapping: Mapping[object, object] = value
            result: JsonObject = {}
            for key, item in mapping.items():
                if not isinstance(key, str):
                    raise ActionProtocolError("JSON 对象的键必须是字符串。")
                _valid_text(key)
                result[key] = _to_pod(item, active, depth + 1)
            return result
        sequence: Sequence[object] = value
        return [_to_pod(item, active, depth + 1) for item in sequence]
    finally:
        active.remove(identity)


def encode_json(value: object) -> bytes:
    """编码紧凑 UTF-8 JSON，不转换未知对象或用 repr 猜测内容。

    Mapping/Sequence 的合法内容可交接为普通 dict/list；字符串键、有限数值、
    非循环结构及最多 64 层容器在编码前核验。保留声明的成员顺序；调用者在
    编码期间不得并发修改源容器，返回字节不再借用源对象。
    """
    pod = _to_pod(value, set(), 0)
    try:
        return json.dumps(pod, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (ValueError, RecursionError) as error:
        raise ActionProtocolError("JSON 数据无法编码。") from error


def _check_depth(text: str) -> None:
    """在 JSON 解析器分配嵌套容器前检查深度，正确跳过字符串中的括号。"""
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > _MAX_DEPTH:
                raise ActionProtocolError("JSON 容器嵌套过深。", code="max_depth")
        elif character in "]}":
            depth -= 1


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """拒绝重复字段，避免校验器和执行者对同一字段采用不同值。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ActionProtocolError("JSON 对象包含重复字段。")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ActionProtocolError("JSON 不接受 NaN 或 Infinity。")


def decode_object(data: bytes) -> JsonObject:
    """解析完整 UTF-8 JSON 对象，拒绝重复字段和不安全的值表示。

    不接受数组等顶层值、非法 UTF-8、孤立代理项、非有限数值或过深结构。
    返回短生命周期 POD；身份及业务字段语义仍由调用者验证。
    """
    if not isinstance(data, bytes):
        raise ActionProtocolError("JSON 正文必须是不可变 bytes。")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ActionProtocolError("JSON 正文不是完整的 UTF-8。") from error
    _check_depth(text)
    try:
        decoded: object = json.loads(
            text, object_pairs_hook=_object_pairs, parse_constant=_reject_constant, strict=True
        )
    except ActionProtocolError:
        raise
    except (ValueError, RecursionError) as error:
        raise ActionProtocolError("JSON 正文格式无效。") from error
    # JSON 语法有效仍可能产生溢出的浮点数或转义的孤立代理项；解析后核验
    # 实际值，不能仅靠 parse_constant 或输入字节的 UTF-8 合法性判断。
    result = _to_pod(decoded, set(), 0)
    if not isinstance(result, dict):
        raise ActionProtocolError("帧正文必须是 JSON 对象。")
    return result
