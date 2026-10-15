"""执行接管与资源额度的稳定拒绝类型；不包含平台或客户端状态。"""

from __future__ import annotations

from purrcept_engine.errors import EngineError


class ExecutionBusy(EngineError):
    """所需资源名额已满；本次意图未接管，也没有隐式等待队列。"""


class ExecutionClosed(EngineError):
    """服务已停止接管新行动。"""


class ExecutionLimitError(EngineError):
    """行动源码或结构化参数不能完整放入配置授予的额度。"""
