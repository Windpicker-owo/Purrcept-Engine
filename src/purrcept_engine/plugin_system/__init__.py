"""插件作者的统一契约入口；不加载具体设备、数据库或应用装配。"""

from purrcept_engine.domain.execution.catalog import CapabilityDeclaration, ObjectDeclaration
from purrcept_engine.domain.execution.providers import CallContext, CallOutcome, FunctionCapability

from .contracts import PluginBlueprint, PluginFactory, PluginPorts, PluginRuntime, typed_factory

__all__ = [
    "CallContext",
    "CallOutcome",
    "CapabilityDeclaration",
    "FunctionCapability",
    "ObjectDeclaration",
    "PluginBlueprint",
    "PluginFactory",
    "PluginPorts",
    "PluginRuntime",
    "typed_factory",
]
