"""保存公开值及其语义校验，不打开数据库或调用设备。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from purrcept_engine.errors import EngineError
from purrcept_engine.values import JsonObject

Text = Annotated[str, Field(min_length=1, pattern=r"\S", strict=True)]


Minutes = Annotated[int, Field(ge=1, le=20160, strict=True)]


Version = Annotated[int, Field(ge=1, strict=True)]


class TimeError(EngineError):
    """明确时间参数或业务拒绝，不把SQLite/内部故障改成可纠正参数错误。"""

    def __init__(self, message: str, *, code: str) -> None:
        """保留未知身份、额度及时间原因，供插件报告真实拒绝。"""
        super().__init__(message)
        self.code = code


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Alarm(_Record):
    """一个时钟的正式定义和当前发生身份，at保存配置时区中的下一应响时刻。"""

    id: Text
    clock_id: Text
    at: AwareDatetime
    timezone: Text
    message: Annotated[str, Field(max_length=500)] = ""
    repeat: Literal["once", "daily"] = "once"
    occurrence_id: Text
    version: Version = 1
    state: Literal["pending", "consumed", "cancelled"] = "pending"
    call_id: Text | None = None

    def to_json(self) -> JsonObject:
        """保留read/set/cancel的原有id/at/timezone/message/repeat字段。"""
        return {
            "id": self.id,
            "at": self.at.isoformat(),
            "timezone": self.timezone,
            "message": self.message,
            "repeat": self.repeat,
        }


class AlarmFiring(_Record):
    """已与正式输入同事务提交的闹铃发生事实，不依赖后续通知是否送达。"""

    clock_id: Text
    alarm_id: Text
    occurrence_id: Text
    input_id: Text
    scheduled_at: AwareDatetime
    fired_at: AwareDatetime
    next_at: AwareDatetime | None = None


class RestState(_Record):
    """主体明确休息的当前事实；elapsed按读取时刻投影，不持久化另一份时长。"""

    bed_id: Text
    expected_duration_minutes: Minutes
    started_at: AwareDatetime
    version: Version

    def to_json(self, now: datetime) -> JsonObject:
        """按客观UTC时差给整分钟数，下限0，到预计期限后仍resting。"""
        validate_aware_datetime(now)
        elapsed = max(
            0, int((now.astimezone(UTC) - self.started_at.astimezone(UTC)).total_seconds() // 60)
        )
        return {
            "resting": True,
            "expected_duration_minutes": self.expected_duration_minutes,
            "started_at": self.started_at.isoformat(),
            "elapsed_minutes": elapsed,
            "bed_id": self.bed_id,
        }


class RestChange(_Record):
    """一次休息提交的真实前后状态，重复call_id不产生第二次变化。"""

    at: AwareDatetime
    state: RestState | None
    previous: RestState | None
    changed: bool
    updated: bool = False

    def to_json(self) -> JsonObject:
        """沿用rest/leave结果，醒着leave只返回resting=false/changed=false。"""
        if self.state is not None:
            return {**self.state.to_json(self.at), "changed": self.changed, "updated": self.updated}
        if self.previous is not None:
            return {**self.previous.to_json(self.at), "resting": False, "changed": self.changed}
        return {"resting": False, "changed": False}


def validate_aware_datetime(value: datetime) -> None:
    """拒绝无明确时区的业务时间，避免本机时区改变持久化含义。"""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise TimeError("时间必须带明确时区。", code="invalid_time")
