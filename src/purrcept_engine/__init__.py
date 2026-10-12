"""Purrcept Engine 的嵌入入口与输入值类型。

EngineHost 拥有显式生命周期；接管输入不等于已经产生对外输出。插件声明
和执行器扩展在其职责模块公开，内部数据库连接不向宿主调用者开放。
"""

from .app.config import EngineConfig, load_config
from .app.host import EngineHost, HostSnapshot
from .errors import ConfigError, EngineError, HostStateError, InstanceLocked, StorageError
from .storage import (
    ActionRecord,
    Admission,
    CallRecord,
    InputContext,
    InputRecord,
    InputSource,
    InputSubmission,
    MediaAsset,
)

__all__ = [
    "ActionRecord",
    "Admission",
    "CallRecord",
    "ConfigError",
    "EngineConfig",
    "EngineError",
    "EngineHost",
    "HostSnapshot",
    "HostStateError",
    "InputContext",
    "InputRecord",
    "InputSource",
    "InputSubmission",
    "InstanceLocked",
    "MediaAsset",
    "StorageError",
    "load_config",
]
