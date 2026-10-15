"""宿主的公开快照和构造接缝；查询值不持有实际资源。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from purrcept_engine.app.config import ExecutionSettings
from purrcept_engine.domain.execution.catalog import Catalog
from purrcept_engine.domain.execution.ports import ActionExecutor, ExecutionJournal
from purrcept_engine.domain.execution.providers import (
    CapabilityImplementation,
)

type HostPhase = Literal["new", "starting", "running", "closing", "closed", "failed"]


type ExecutorFactory = Callable[
    [Catalog, tuple[CapabilityImplementation, ...], ExecutionJournal, ExecutionSettings, Path],
    ActionExecutor,
]


@dataclass(frozen=True, slots=True)
class HostSnapshot:
    """一次宿主事实投影；版本只在生命周期变化后增加，查询不启动资源。

    Attributes:
        instance_id: 本次装配中唯一的插件或宿主实例身份。
        version: 当前宿主生命周期状态实际变更的修订号，读取不会递增。
        phase: 宿主实际生命周期阶段，与查询版本同时更新。
        pending_resources: 尚未确认归还的实际资源名称，关闭超时后仍由原所有者持有。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    instance_id: str
    version: int
    phase: HostPhase
    pending_resources: tuple[str, ...]
