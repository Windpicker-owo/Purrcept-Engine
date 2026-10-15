"""定义领域协作契约与不可变设置；调用与所有权由应用显式连接。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from purrcept_core.models import (
    ModelBackend,
)

from purrcept_engine.domain.model_requests.ports import MaterialCompiler, RetryPolicy
from purrcept_engine.domain.subject.session import (
    CapacityCheck,
)


@dataclass(frozen=True, slots=True)
class SubjectModel:
    """app 选择的前台资源和预算依据，模型内容不能修改这些参数。"""

    backend: ModelBackend
    model: str
    output_tokens: int
    capacity: CapacityCheck
    retry: RetryPolicy
    compiler: MaterialCompiler


@dataclass(frozen=True, slots=True)
class CoordinatorSettings:
    """调度的明确初值，独立于 app 配置类型与模型调用方。

    diagnostic_bytes 只控制详细诊断，权威原文和响应由 Store 的真实接管额度
    限制。整轮预算在 ContinuousSession 开始主动处理时一次创建，不因重试
    或工具请求刷新；输入排队和注意判断分别拥有自己的期限。
    """

    diagnostic_bytes: int
    coalesce_seconds: float = 0.1
    attention_batch: int = 8
    defer_seconds: float = 30.0
    max_requests: int = 8
    turn_seconds: float = 600.0

    def __post_init__(self) -> None:
        """校验公开构造边界，禁止布尔值或非有限时间被当作额度。"""
        for value in (self.attention_batch, self.max_requests):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("主体批量和请求次数必须是正整数。")
        if (
            isinstance(self.diagnostic_bytes, bool)
            or not isinstance(self.diagnostic_bytes, int)
            or self.diagnostic_bytes < 0
        ):
            raise ValueError("诊断字节额度必须是非负整数。")
        for value in (self.coalesce_seconds, self.defer_seconds, self.turn_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError("调度时间必须是有限非负数。")
        if self.defer_seconds == 0 or self.turn_seconds == 0:
            raise ValueError("延期和整轮期限必须为正数。")
