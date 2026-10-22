"""插件与替换执行器的统一公开契约入口。

工厂只 prepare 纯声明，Host 通过 build/start/stop_inputs/close 取得和收回资源。
这里只导出领域类型与有限端口，不加载具体插件、SDK、数据库或应用装配，
也不扫描或注册实现。具体协议的文档拥有调用顺序、取消与所有权契约。
"""

from purrcept_engine.domain.activities.contracts import (
    ActivityDefinition,
    ActivityDriver,
    ActivityError,
    ActivityRecovery,
    DriverAcceptance,
    RecoveryObservation,
)
from purrcept_engine.domain.activities.models import (
    ActivityCommand,
    ActivityFeedback,
    ActivityRecord,
    ActivityState,
    CommandKind,
    FeedbackKind,
)
from purrcept_engine.domain.activities.port import ActivityPort, ActivityStep
from purrcept_engine.domain.audio.errors import AudioError
from purrcept_engine.domain.audio.facts import make_fact
from purrcept_engine.domain.audio.models import AudioFact
from purrcept_engine.domain.audio.ports import AudioPort
from purrcept_engine.domain.execution.catalog import (
    Binding,
    CapabilityDeclaration,
    Catalog,
    ObjectDeclaration,
)
from purrcept_engine.domain.execution.models import ActionRecord, CallRecord, CallResultRecord
from purrcept_engine.domain.execution.ports import ActionExecutor, ExecutionJournal
from purrcept_engine.domain.execution.providers import (
    CallContext,
    CallOutcome,
    CapabilityImplementation,
    CapabilityInputError,
    FunctionCapability,
)
from purrcept_engine.domain.execution.settings import ExecutionSettings
from purrcept_engine.domain.inputs.content import InputMediaIndex, InputPresentation
from purrcept_engine.domain.inputs.models import (
    Admission,
    InputBlock,
    InputContext,
    InputRecord,
    InputSource,
    InputSubmission,
    MediaAsset,
    MediaReference,
)
from purrcept_engine.domain.knowledge.models import (
    EvidencePage,
    EvidenceSnapshot,
    KnowledgeInputError,
)
from purrcept_engine.domain.subject.models import InputProcessing
from purrcept_engine.domain.time.models import (
    Alarm,
    AlarmFiring,
    Minutes,
    RestChange,
    RestState,
    TimeError,
)
from purrcept_engine.domain.time.ports import TimePort
from purrcept_engine.domain.time.scheduling import parse_alarm_at, resolve_timezone
from purrcept_engine.plugin_system.audio_port import AudioOutputPort
from purrcept_engine.plugin_system.body import BodyIntent
from purrcept_engine.plugin_system.body_port import BodyPort
from purrcept_engine.plugin_system.contracts import (
    EvidencePort,
    InputPort,
    LateResultPort,
    NativeInputPort,
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
    typed_factory,
)
from purrcept_engine.plugin_system.panels import DevicePanel, PanelControl, PreviewFrame
from purrcept_engine.values import JsonObject, JsonValue

__all__ = [
    "ActionExecutor",
    "ActionRecord",
    "ActivityCommand",
    "ActivityDefinition",
    "ActivityDriver",
    "ActivityError",
    "ActivityFeedback",
    "ActivityPort",
    "ActivityRecord",
    "ActivityRecovery",
    "ActivityState",
    "ActivityStep",
    "Admission",
    "Alarm",
    "AlarmFiring",
    "AudioError",
    "AudioFact",
    "AudioOutputPort",
    "AudioPort",
    "Binding",
    "BodyIntent",
    "BodyPort",
    "CallContext",
    "CallOutcome",
    "CallRecord",
    "CallResultRecord",
    "CapabilityDeclaration",
    "CapabilityImplementation",
    "CapabilityInputError",
    "Catalog",
    "CommandKind",
    "DevicePanel",
    "DriverAcceptance",
    "EvidencePage",
    "EvidencePort",
    "EvidenceSnapshot",
    "ExecutionJournal",
    "ExecutionSettings",
    "FeedbackKind",
    "FunctionCapability",
    "InputBlock",
    "InputContext",
    "InputMediaIndex",
    "InputPort",
    "InputPresentation",
    "InputProcessing",
    "InputRecord",
    "InputSource",
    "InputSubmission",
    "JsonObject",
    "JsonValue",
    "KnowledgeInputError",
    "LateResultPort",
    "MediaAsset",
    "MediaReference",
    "Minutes",
    "NativeInputPort",
    "ObjectDeclaration",
    "PanelControl",
    "PluginBlueprint",
    "PluginFactory",
    "PluginPorts",
    "PluginRuntime",
    "PreviewFrame",
    "RecoveryObservation",
    "RestChange",
    "RestState",
    "TimeError",
    "TimePort",
    "make_fact",
    "parse_alarm_at",
    "resolve_timezone",
    "typed_factory",
]
