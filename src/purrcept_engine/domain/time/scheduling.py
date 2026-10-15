"""解析明确时区与墙钟闹铃，计算下一次触发时刻；不读取账本或创建调度任务。"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, tzinfo
from functools import cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from purrcept_engine.domain.time.models import (
    TimeError,
    validate_aware_datetime,
)

_TIME_OF_DAY = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$")


@cache
def resolve_timezone(name: str) -> tzinfo:
    """解析明确IANA时区，UTC/GMT无需时区数据。

    Args:
        name: 非空 IANA 时区名；UTC 与 GMT 不要求额外时区数据。

    Returns:
        明确解析的 tzinfo；UTC 与 GMT 返回 UTC，不读取本机默认时区。

    Raises:
        TimeError: 时区名无效，或当前环境缺少该 IANA 时区的数据。

    Note:
        缺数据不改用机器时区。
    """
    if name in ("UTC", "GMT"):
        return UTC
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise TimeError("IANA时区不存在或缺少tzdata。", code="timezone") from error


def parse_alarm_at(value: str, *, timezone: str, now: datetime) -> datetime:
    """HH:MM(:SS)取下一次墙钟时刻，ISO无偏移按配置时区，过去ISO明确拒绝。

    Args:
        value: HH:MM 或 HH:MM:SS 墙钟时间，或 ISO 日期时间；无偏移 ISO 沿 timezone 解释。
        timezone: 显式 IANA 时区名称，或支持的 UTC/GMT 名称。
        now: 带明确时区的本次墙钟时刻。

    Returns:
        按明确时区规则计算的带时区触发时刻。

    Raises:
        TimeError: 时间文本或时区无效、缺少时区数据，或 ISO 目标已经过去。
    """
    validate_aware_datetime(now)
    if not isinstance(value, str) or not value.strip():
        raise TimeError("闹铃时间不能为空。", code="invalid_time")
    zone = resolve_timezone(timezone)
    current = now.astimezone(zone)
    text = value.strip()
    match = _TIME_OF_DAY.fullmatch(text)
    if match is not None:
        candidate = current.replace(
            hour=int(match[1]), minute=int(match[2]), second=int(match[3] or "0"), microsecond=0
        )
        return candidate + timedelta(days=1) if candidate <= current else candidate
    try:
        candidate = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        )
    except ValueError as error:
        raise TimeError("时间必须为HH:MM、HH:MM:SS或ISO日期时间。", code="invalid_time") from error
    candidate = (
        candidate.replace(tzinfo=zone)
        if candidate.utcoffset() is None
        else candidate.astimezone(zone)
    )
    if candidate <= current:
        raise TimeError("完整日期闹铃必须在未来。", code="invalid_time")
    return candidate


def next_daily_occurrence(at: datetime, *, timezone: str, now: datetime) -> datetime:
    """每日仅取严格晚于now的下次同一墙钟时刻，跨多日不逐条追补。

    Args:
        at: 带明确时区的目标触发时刻。
        timezone: 显式 IANA 时区名称，或支持的 UTC/GMT 名称。
        now: 带明确时区的本次墙钟时刻。

    Returns:
        按明确时区规则计算的带时区触发时刻。

    Raises:
        TimeError: 输入时刻无明确时区，或目标 IANA 时区无法解析。
    """
    validate_aware_datetime(at)
    validate_aware_datetime(now)
    zone = resolve_timezone(timezone)
    candidate, current = at.astimezone(zone), now.astimezone(zone)
    if candidate <= current:
        days = max(1, (current.date() - candidate.date()).days)
        candidate += timedelta(days=days)
        if candidate <= current:
            candidate += timedelta(days=1)
    return candidate
