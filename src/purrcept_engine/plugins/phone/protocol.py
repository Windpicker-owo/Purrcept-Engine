"""Phone v1 的严格 JSON 信封和用户消息，不接受任意能力或 Python 调用。

JSON 文本上限在网络入口先核验，Pydantic 再核验字段。设备身份来自认证，
操作会话由 Engine 分配；客户端不能通过消息正文授予唤醒或操作权限。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from purrcept_engine.values import JsonObject


class WireValue(BaseModel):
    """拒绝未知字段和类型猜测的网络值。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Frame(WireValue):
    """一帧协议消息；帧身份只关联应答，业务去重使用 payload 中的身份。"""

    v: Literal[1] = 1
    type: str = Field(min_length=1, max_length=64)
    id: str = Field(min_length=1, max_length=128)
    reply_to: str | None = None
    connection_id: str | None = None
    payload: JsonObject = Field(default_factory=dict)


class ChatSubmission(WireValue):
    """手机先保存原生身份和发生时间，重发必须沿用全部原始字段。"""

    message_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    text: str = Field(min_length=1, max_length=32768)
    occurred_at: str = Field(max_length=64)

    @field_validator("text")
    @classmethod
    def bounded_text(cls, value: str) -> str:
        """给 UTF-8 信封保留空间，中文和 emoji 也不能突破64 KiB帧上限。"""
        return validate_text(value)

    @field_validator("occurred_at")
    @classmethod
    def aware_time(cls, value: str) -> str:
        """拒绝缺时区的设备时间，不能用重试接收时间改写发生事实。"""
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("消息时间必须包含时区。")
        return value


class Pairing(WireValue):
    """配对请求持有一次性邀请，长期设备身份由服务端生成。"""

    invitation: str = Field(min_length=16, max_length=256)
    name: str = Field(min_length=1, max_length=80)


def validate_text(value: str) -> str:
    """校验网络文字的实际字节预算，字符上限由对应模型字段另行声明。"""
    if len(value.encode("utf-8")) > 60000:
        raise ValueError("消息超过 UTF-8 字节上限。")
    return value


class Cursor(WireValue):
    """手机确认自己已经落盘的有界事件前缀。"""

    through: int = Field(ge=0)


class DeviceResult(WireValue):
    """设备实际回执；副作用是否符合业务预期仍需后续观察。"""

    command_id: str = Field(min_length=1, max_length=128)
    state: Literal["confirmed", "failed", "unknown"]
    data: JsonObject = Field(default_factory=dict)
    error: str | None = Field(default=None, max_length=512)


class DeviceState(WireValue):
    """手机权限事实只允许固定字段，不把任意诊断 JSON 注入主脑。"""

    control_allowed: bool
    accessibility: bool
