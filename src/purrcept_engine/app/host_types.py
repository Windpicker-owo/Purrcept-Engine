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
    """一次宿主事实投影；版本只在生命周期变化后增加，查询不启动资源。"""

    instance_id: str
    version: int
    phase: HostPhase
    pending_resources: tuple[str, ...]
