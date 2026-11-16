"""SQLite 权威记录公共字段解码与参数边界校验辅助函数。"""

from datetime import UTC, datetime

from purrcept_engine.errors import StorageError


def as_text(value: object, message: str = "权威记录中的字符串字段无效。") -> str:
    """校验并返回数据库行中的字符串字段。"""
    if not isinstance(value, str):
        raise StorageError(message, code="invalid_record")
    return value


def as_opt_text(value: object, message: str = "权威记录中的字符串字段无效。") -> str | None:
    """校验并返回可空字符串字段。"""
    return None if value is None else as_text(value, message)


def as_int(
    value: object,
    message: str = "权威记录中的非负整数字段无效。",
    *,
    minimum: int | None = 0,
) -> int:
    """校验数据库整数字段（排除 bool，默认要求非负整数）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError(message, code="invalid_record")
    if minimum is not None and value < minimum:
        raise StorageError(message, code="invalid_record")
    return value


def as_bool(value: object, message: str = "权威记录中的布尔字段无效。") -> bool:
    """校验 SQLite 0/1 整数表示的布尔字段。"""
    if value not in (0, 1) or isinstance(value, float):
        raise StorageError(message, code="invalid_record")
    return bool(value)


def as_datetime(value: object) -> datetime:
    """解析带时区的持久化 ISO 时间戳。"""
    try:
        result = datetime.fromisoformat(as_text(value))
    except ValueError as error:
        raise StorageError("权威记录中的时间无效。", code="invalid_record") from error
    if result.utcoffset() is None:
        raise StorageError("权威记录中的时间缺少时区。", code="invalid_record")
    return result


def to_timestamp(value: datetime) -> str:
    """统一转换为 UTC ISO-8601 字符串入库。"""
    return value.astimezone(UTC).isoformat()


def require_positive(value: int, field: str) -> None:
    """校验正整数调用参数。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} 必须是正整数。")


def validate_id(value: str) -> None:
    """校验非空字符串身份参数。"""
    if not isinstance(value, str) or not value:
        raise ValueError("记录身份必须是非空字符串。")
