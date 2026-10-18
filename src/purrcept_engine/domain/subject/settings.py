"""定义主体模型声明和注意、前台协调的不可变设置。

应用在装配完成后显式注入后端、容量和预算，领域流程只读取声明。
这里不解析应用配置、不猜测模型能力或创建客户端。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from purrcept_core.models import (
    ModelBackend,
)

from purrcept_engine.domain.model_requests.contents import ContentKind, validate_supported_contents
from purrcept_engine.domain.model_requests.ports import MaterialCompiler, RetryPolicy
from purrcept_engine.domain.subject.session import (
    CapacityCheck,
)


@dataclass(frozen=True, slots=True)
class SubjectModel:
    """app 选择的会话模型资源、内容声明和预算依据，模型内容不能修改这些参数。

    Attributes:
        backend: 显式注入的模型后端，资源取得和关闭归装配层。
        model: 本次调用使用的显式模型名，不在领域层推断替代模型。
        output_tokens: 为本次生成预留的最大输出 token 数。
        capacity: 对完整实际请求执行容量核验的函数。
        retry: 唯一传输重试策略，不额外叠加 SDK 隐式重试。
        compiler: 按实际 SDK 布局冻结完整请求材料的纯编译器。
        supports_contents: 刺激允许交付的内容类型；默认仅 text，其他媒体原位转为文字引用。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    backend: ModelBackend
    model: str
    output_tokens: int
    capacity: CapacityCheck
    retry: RetryPolicy
    compiler: MaterialCompiler
    supports_contents: tuple[ContentKind, ...] = ("text",)

    def __post_init__(self) -> None:
        """程序式装配与配置加载共用相同内容声明约束。"""
        validate_supported_contents(self.supports_contents)


@dataclass(frozen=True, slots=True)
class CoordinatorSettings:
    """调度的明确初值，独立于 app 配置类型与模型调用方。

    Attributes:
        diagnostic_bytes: 单次尝试允许保留的流诊断字节数，不裁剪原参数或完整响应。
        coalesce_seconds: 普通输入合批等待的秒数。
        attention_batch: 一次注意判断允许取得的输入数量上限。
        max_requests: 本轮逻辑模型请求总上限，传输重试不重复占名额。
        turn_seconds: 本轮连续处理的总秒数预算，包含工具等待和重试退避。
        proactive_thinking: 主动思考的启动默认值，页面切换仅影响本次运行。
        proactive_idle_seconds: 空闲刺激之间至少等待的无新刺激秒数。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        diagnostic_bytes 只控制详细诊断，权威原文和响应由 Store 的真实接管额度
        限制。整轮预算在 ContinuousSession 开始主动处理时一次创建，不因重试
        或工具请求刷新；输入排队和注意判断分别拥有自己的期限。
    """

    diagnostic_bytes: int
    coalesce_seconds: float = 0.1
    attention_batch: int = 8
    max_requests: int = 8
    turn_seconds: float = 600.0
    proactive_thinking: bool = False
    proactive_idle_seconds: float = 300.0

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
        for value in (self.coalesce_seconds, self.turn_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError("调度时间必须是有限非负数。")
        if self.turn_seconds == 0:
            raise ValueError("整轮期限必须为正数。")
        if not isinstance(self.proactive_thinking, bool):
            raise ValueError("主动思考开关必须是布尔值。")
        if (
            isinstance(self.proactive_idle_seconds, bool)
            or not isinstance(self.proactive_idle_seconds, (int, float))
            or not math.isfinite(self.proactive_idle_seconds)
            or self.proactive_idle_seconds <= 0
        ):
            raise ValueError("主动思考空闲间隔必须是有限正数。")
