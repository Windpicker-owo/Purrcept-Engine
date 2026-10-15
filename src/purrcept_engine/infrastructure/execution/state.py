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
    """宿主持有的短期资源；reader 结束后才允许关闭 FrameChannel。"""

    process: WindowsProcess
    channel: FrameChannel
    directory: TemporaryDirectory[str]
    reader: threading.Thread | None = None


@dataclass(slots=True)
class ScriptAction:
    """一次接管的内存资格；持久状态与实际效果始终以 Store 为权威。"""

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
    """已校验的脚本结果，资源监督结束后才保存整体终态。"""

    state: Literal["completed", "failed"]
    result_json: str | None
    output: str
    error: str | None


@dataclass(slots=True)
class StructuredAction:
    """单能力观察所有者，不借用 Worker；首版回执结束观察，真实调用独立收尾。"""

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
    """已准备且尚未释放资源的调用，deadline 在接收意图时固定。"""

    action: ScriptAction | StructuredAction
    sequence: int
    binding: Binding
    arguments: JsonObject
    context: CallContext
    ordinary: bool
    target_reserved: bool = False
    provider_started: bool = False

    @property
    def source_id(self) -> str:
        """活动输入关联共用来源轮转，脚本内调用仍按该行动保留明确次序。"""
        if isinstance(self.action, StructuredAction) and self.action.input_id is not None:
            return f"structured:{self.action.input_id}"
        return self.action.action_id
