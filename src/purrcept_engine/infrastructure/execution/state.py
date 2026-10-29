"""执行器独占的行动和调用状态；真实效果仍以领域账本为权威。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from tempfile import TemporaryDirectory
from typing import Literal

from purrcept_engine.domain.execution.catalog import Binding
from purrcept_engine.domain.execution.models import CallRecord
from purrcept_engine.domain.execution.providers import (
    CallContext,
)
from purrcept_engine.infrastructure.execution.protocol import FrameChannel
from purrcept_engine.infrastructure.processes.windows import WindowsProcess
from purrcept_engine.values import JsonObject


@dataclass(slots=True)
class WorkerHandle:
    """宿主持有的短期资源；reader 结束后才允许关闭 FrameChannel。

    Attributes:
        process: 当前所有者持有的 Windows 进程句柄。
        channel: 当前 Worker 唯一借用的有界 IPC 帧通道。
        directory: 本次操作使用的明确目录。
        reader: CLI 创建并持有的逐行输入资源。
    """

    process: WindowsProcess
    channel: FrameChannel
    directory: TemporaryDirectory[str]
    reader: threading.Thread | None = None


@dataclass(slots=True)
class ScriptAction:
    """一次接管的内存资格；持久状态与实际效果始终以 Store 为权威。

    Attributes:
        action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
        source: 待执行的受限 Python 源码。
        input_id: 正式接管返回的输入身份，用于关联原文与后续处理。
        deadline: 所属事件循环的绝对单调截止时间，不是 Unix 时间戳。
        can_start: 实际派发前重新检查的资格回调，等待后仍须复核。
        messages: 按实际先后排列的完整 Core 消息，不从正文猜测交付身份。
        presented_call_ids: 主体交付门槛确认已完整呈现的调用身份；空集合不授予恢复资格。
        worker: 当前会话实际拥有的隔离进程资源。
        eligible: 当前行动是否仍允许启动下一次能力调用。
        sequence: 行动内从 1 开始的调用顺序号。
        stop_error: 停止当前 Worker 时实际取得的失败说明。
        failure: 当前所有者锁存的原始故障，不能转换为空成功。
        terminal: Worker 实际报告的终态；None 表示尚未取得。
    """

    action_id: str
    source: str
    input_id: str | None
    deadline: float
    can_start: Callable[[], bool] | None
    messages: asyncio.Queue[JsonObject | BaseException]
    presented_call_ids: frozenset[str] = frozenset()
    worker: WorkerHandle | None = None
    eligible: bool = True
    sequence: int = 0
    stop_error: str | None = None
    failure: BaseException | None = None
    terminal: WorkerTerminal | None = None


@dataclass(frozen=True, slots=True)
class WorkerTerminal:
    """已校验的脚本结果，资源监督结束后才保存整体终态。

    Attributes:
        state: 当前所有者持有的状态或待提交状态值。
        result_json: 已编码的实际结果；无可用结果时为 None。
        output: 此次 Worker 实际产生的有界标准输出。
        error: 当前流程已经取得的真实故障或失败说明。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    state: Literal["completed", "failed"]
    result_json: str | None
    output: str
    error: str | None


@dataclass(slots=True)
class StructuredAction:
    """单能力观察所有者，不借用 Worker；首版回执结束观察，真实调用独立收尾。

    Attributes:
        action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
        source: 含 kind、path 与 arguments 的冻结结构化调用 JSON，不作为源码执行。
        input_id: 正式接管返回的输入身份，用于关联原文与后续处理。
        deadline: 所属事件循环的绝对单调截止时间，不是 Unix 时间戳。
        can_start: 实际派发前重新检查的资格回调，等待后仍须复核。
        receipt: 只在权威提交完成后交付实际调用回执的 Future。
        before_start: Provider 启动前的异步核验；返回 None 拒绝，正有限秒数只收紧期限。
        start_ready: 等待独立资格核验完成的 Future；None 表示没有此门槛。
        eligible: 当前是否仍允许继续派发这次结构化调用。
        stop_error: 阻止本次结构化调用继续派发的真实原因。
        timed_out: 当前调用是否已超过原固定截止。
    """

    action_id: str
    source: str
    input_id: str | None
    deadline: float
    can_start: Callable[[], bool] | None
    receipt: asyncio.Future[CallRecord]
    before_start: Callable[[], Awaitable[float | None]] | None = None
    start_ready: asyncio.Future[None] | None = None
    eligible: bool = True
    stop_error: str | None = None
    timed_out: bool = False


@dataclass(slots=True)
class CapabilityCall:
    """已准备且尚未释放资源的调用，deadline 在接收意图时固定。

    Attributes:
        action: 本次执行服务接管的冻结脚本或结构化调用。
        sequence: 行动内从 1 开始的调用顺序号。
        binding: 冻结目录中的精确路径与参数契约。
        arguments: 按已装配参数模型校验的能力关键字参数。
        context: 宿主或 Core 注入的可信调用上下文，不由模型自行填写。
        ordinary: 是否占用普通调用名额，而非独立控制名额。
        target_reserved: 当前调用是否已经取得目标串行位置。
        provider_started: 是否已经跨过真实 Provider 启动门槛。
    """

    action: ScriptAction | StructuredAction
    sequence: int
    binding: Binding
    arguments: JsonObject
    context: CallContext
    ordinary: bool
    target_reserved: bool = False
    provider_started: bool = False
    # 原异常只借到对应回执日志提交，随后清空，避免 traceback 长期持有 Provider 资源。
    diagnostic_error: BaseException | None = None

    @property
    def source_id(self) -> str:
        """活动输入关联共用来源轮转，脚本内调用仍按该行动保留明确次序。"""
        if isinstance(self.action, StructuredAction) and self.action.input_id is not None:
            return f"structured:{self.action.input_id}"
        return self.action.action_id
