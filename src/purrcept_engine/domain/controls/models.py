"""定义本机面板控制的接管记录与稳定业务错误。

请求身份和参数摘要用于同一宿主内去重，实际行动与活动命令另有回执。
这里不执行快捷控制，也不根据 HTTP 受理推断设备完成。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from purrcept_engine.errors import EngineError
from purrcept_engine.values import JsonObject, decode_object

type ControlState = Literal["admitted", "running", "finished", "rejected", "interrupted"]


class ControlError(EngineError):
    """页面参数、旧版本或容量拒绝；不表示权威连接损坏。"""

    def __init__(self, message: str, *, code: str = "control_arguments") -> None:
        """保留接入层可定位的类别。

        Args:
            message: 对调用方公开的原因或错误说明。
            code: 供调用方分类处理的稳定错误码。 默认 'control_arguments'。

        Note:
            消息不得携带凭据或完整模型正文。
        """
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ControlRecord:
    """控制观察的持久记录；finished 只表示已取得对应业务返回。

    Attributes:
        request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。
        instance_id: 本次装配中唯一的插件或宿主实例身份。
        payload_json: 已编码的业务事实，不包含任意 Python 对象。
        state: 当前所有者持有的状态或待提交状态值。
        admitted_at: 权威接管此工作的 Unix 时间戳（秒）。
        action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
        command_id: 控制命令的稳定身份；重复提交须沿用原身份和内容。
        finished_at: 此记录实际结束的 Unix 时间戳；None 表示尚未保存结束事实。
        error_code: 供查询和调用方分类处理的稳定错误码。
        error: 当前流程已经取得的真实故障或失败说明。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

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
        """输出有限接管事实，参数只含面板本次允许填写的控制值。

        Returns:
            业务对象，按实际结果提供 request_id、instance_id、request、state、admitted_at、
            action_id、command_id、finished_at、error_code、error 字段。
        """
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
