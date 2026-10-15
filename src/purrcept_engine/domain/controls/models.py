"""保存公开值及其语义校验，不打开数据库或调用设备。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from purrcept_engine.errors import EngineError
from purrcept_engine.values import JsonObject, decode_object

type ControlState = Literal["admitted", "running", "finished", "rejected", "interrupted"]


class ControlError(EngineError):
    """页面参数、旧版本或容量拒绝；不表示权威连接损坏。"""

    def __init__(self, message: str, *, code: str = "control_arguments") -> None:
        """保留接入层可定位的类别；消息不得携带凭据或完整模型正文。"""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ControlRecord:
    """控制观察的持久记录；finished 只表示已取得对应业务返回。

    action_id 和 command_id 指向真实业务记录，后续确认必须查询该原身份。
    即使页面等待已结束，未知回执仍可被执行器的迟到事实澄清。
    """

    request_id: str
    instance_id: str
    payload_json: str
    state: ControlState
    admitted_at: float
    action_id: str | None
    command_id: str | None
    finished_at: float | None
    error_code: str | None
    error: str | None

    def projection(self) -> JsonObject:
        """输出有限接管事实，参数只含面板本次允许填写的控制值。"""
        return {
            "request_id": self.request_id,
            "instance_id": self.instance_id,
            "request": decode_object(self.payload_json.encode()),
            "state": self.state,
            "admitted_at": self.admitted_at,
            "action_id": self.action_id,
            "command_id": self.command_id,
            "finished_at": self.finished_at,
            "error_code": self.error_code,
            "error": self.error,
        }
