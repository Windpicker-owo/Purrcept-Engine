"""声明插件的配置、冻结能力和资源寿命，不启动外部接入。

插件工厂先将配置解析成纯 Blueprint，app 汇总完整目录后才调用 build。
Runtime 的停止接入与资源释放分开：已开始能力仍可借用资源直到 execution
确认收尾。插件不取得主脑会话或通用 SQLite 连接，也没有运行期热替换。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from purrcept_engine.domain.activities.contracts import ActivityDefinition
from purrcept_engine.domain.audio.ports import AudioPort
from purrcept_engine.domain.execution.catalog import CapabilityDeclaration, ObjectDeclaration
from purrcept_engine.domain.execution.models import CallRecord
from purrcept_engine.domain.execution.providers import (
    CallContext,
    CallOutcome,
    CapabilityImplementation,
)
from purrcept_engine.domain.inputs.models import (
    Admission,
    InputRecord,
    InputSource,
    InputSubmission,
    MediaAsset,
)
from purrcept_engine.domain.knowledge.models import EvidencePage, EvidenceSnapshot
from purrcept_engine.domain.subject.models import InputProcessing
from purrcept_engine.domain.time.ports import TimePort
from purrcept_engine.plugin_system.audio_port import AudioOutputPort
from purrcept_engine.plugin_system.body_port import BodyPort
from purrcept_engine.plugin_system.panels import DevicePanel
from purrcept_engine.values import JsonObject


class InputPort(Protocol):
    """宿主借给输入插件的专用接管端口，不授予模型或存储访问权限。

    外部适配器提交不可变原文后保存 input_id；取消等待不会撤销已接管事务。
    重连按原生事件身份重投或查询，不能以通知丢失推断此前未接管。
    """

    async def submit(self, submission: InputSubmission) -> Admission:
        """原子接管或明确拒收，接管不等同于主脑已经处理。"""
        ...

    async def query_input(self, input_id: str) -> InputRecord | None:
        """读取实际接管及处理状态，不执行模型或再次发送输入。"""
        ...

    async def query_processing(self, input_id: str) -> InputProcessing | None:
        """读取真实阶段和失败原因；延期或处理中不构成采集画面完成确认。"""
        ...


class NativeInputPort(Protocol):
    """宿主借出的原生事实只读查询，内置实现为 EngineHost。

    接入插件在重新计算回复关系或本地派生说明前，用原生来源身份查询
    首次接管记录。没有可靠事件身份时返回 None，不能猜测文字去重。
    返回值不授予再次派发资格；真实内容冲突仍由接入边界核验，权威
    读取故障原样传播。端口不暴露数据库或可变输入状态。
    """

    async def query_native_input(self, source: InputSource) -> InputRecord | None:
        """按适配器、账号、会话、事件类别和原生身份读首次事实。"""
        ...


class EvidencePort(Protocol):
    """插件借用的原文快照/分页端口，不授予认识修订或任意数据库操作。

    首读提交完整原文，续读由宿主保存的游标查回同一快照；取得页面不构成
    模型已读证明。插件按自己的业务来源检查快照归属。取消等待不撤销已
    接管快照，缺失媒体及权威故障原样失败。
    """

    @property
    def page_chars(self) -> int:
        """返回本实例统一页长，插件不另设会静默裁剪原文的上限。"""
        ...

    async def capture(
        self, snapshot: EvidenceSnapshot, assets: tuple[MediaAsset, ...]
    ) -> EvidenceSnapshot:
        """原子保存显式工具取得的原生媒体和原文，不另投注意或前台输入。"""
        ...

    async def put_evidence(
        self, snapshot: EvidenceSnapshot, *, needs_reflection: bool = False
    ) -> EvidenceSnapshot:
        """完整保存新快照及媒体关系，不能覆盖同身份的不同原文。"""
        ...

    async def read_evidence(
        self,
        evidence_id: str,
        *,
        level: str = "evidence",
        cursor: str | None = None,
        max_chars: int = 8000,
    ) -> EvidencePage:
        """读取指定快照范围，页面带确切来源、版本与下一游标。"""
        ...

    async def read_evidence_cursor(self, cursor: str, *, max_chars: int = 8000) -> EvidencePage:
        """用已保存游标续读原快照，不能从外界文字自行解析游标权限。"""
        ...


class LateResultPort(Protocol):
    """插件提交实际迟到协议确认的限定端口，不授予重新派发权限。

    app 按当前 Blueprint 的真实能力路径借出，只有保存了 started 门槛且
    action/input 与 CallContext 对应的调用可用。方法等待首版回执提交后追加
    不可变版本；插件持有提交任务到真实结束。确认不改写原行动结束结果，
    不根据请求参数或断线推断效果，也不能使用另一个插件的调用身份。
    """

    async def record(self, context: CallContext, outcome: CallOutcome) -> CallRecord:
        """保存真实迟到结果或返回相同已存事实；存储故障保持原型。"""
        ...


@dataclass(frozen=True, slots=True)
class PluginPorts:
    """app 显式注入的有限依赖；字段增加须对应真实业务交接。

    inputs 用于接管与查询；snapshot 读取宿主提交后的只读投影。设备客户端
    和通用数据库不在端口中。evidence 为共享原文/分页服务，仅实际需要的
    组件借用；认识写入不在此门面内。
    audio 是同库阶段事实端口，不是播放器；native_audio_enabled只表示前台
    模型显式声明音频能力且已登记非文本专用的媒体计量规则。它不证明真实
    网关可用或本次材料在预算内，真实材料仍须通过完整请求的容量预检。
    """

    inputs: InputPort
    snapshot: Callable[[], JsonObject]
    evidence: EvidencePort | None = None
    time: TimePort | None = None
    late_results: LateResultPort | None = None
    audio: AudioPort | None = None
    native_audio_enabled: bool = False
    native_inputs: NativeInputPort | None = None


@dataclass(frozen=True, slots=True)
class PluginRuntime:
    """build 交回的资源所有者及实际能力绑定，由宿主一次取得和收回。

    start 在完整目录及输入端口就绪后调用；stop_inputs 只停止新接入及重连，
    不关闭仍被已开始调用使用的连接。close 在 execution 收尾后调用一次。
    无对应资源时不必提供空生命周期函数。内部异常原样失败，外围离线应由
    具体插件在其状态投影中表达。所有后台任务必须归这些函数的所有者管理。
    snapshot 供宿主诊断和面板查询，不能自动进入模型上下文。prepare_request
    在轮次预检和每个逻辑模型请求前返回判断和行动所需的状态、技能等材料；插件须
    显式选择业务字段，保留不可用原因、材料缺口及必要行动身份，不复制诊断
    队列、句柄、版本散列或原生单调时钟。返回只读 JSON 加入动态 user 尾部，
    不提供此入口就不注入状态；不能修改固定目录、执行技能或发外部消息。
    panel 只列出已批准的面板快捷控制，不把全部模型能力变成页面入口。
    audio_output和body是具体所有者交回的有限运行借用，app在全部build之后
    一次绑定音频到可选身体，再执行start；关闭仍归各自Runtime。
    resources_released是可选的明确释放明证：只有全部真实借用、客户端与设备
    已归还才为True。close报告旧业务故障时app可据此继续释放依赖，仍保留
    原异常；没有此明证的关闭故障不能靠快照或任务数猜成资源已经释放。
    """

    capabilities: tuple[CapabilityImplementation, ...]
    start: Callable[[], Awaitable[None]] | None = None
    stop_inputs: Callable[[], Awaitable[None]] | None = None
    close: Callable[[], Awaitable[None]] | None = None
    snapshot: Callable[[], JsonObject] | None = None
    prepare_request: Callable[[], Awaitable[JsonObject]] | None = None
    panel: DevicePanel | None = None
    audio_output: AudioOutputPort | None = None
    body: BodyPort | None = None
    resources_released: Callable[[], bool] | None = None


@dataclass(frozen=True, slots=True)
class PluginBlueprint:
    """由工厂纯解析得到的声明和构造函数，配置校验不执行 build。

    required_modules 是明确启用时必须可导入的依赖，不用于自动安装。对象父链
    与能力声明经统一 Catalog 核验；build 返回的实现须精确对应这些声明。
    工厂可把已校验配置保存在 build 闭包，不能在声明阶段打开设备或网络。
    build 返回前取得的资源由具体插件所有者登记；部分构造失败必须在该
    所有者中清理并保留原错误，不能要求尚未取得 Runtime 的宿主猜测资源。
    """

    objects: tuple[ObjectDeclaration, ...]
    capabilities: tuple[CapabilityDeclaration[BaseModel], ...]
    build: Callable[[PluginPorts], Awaitable[PluginRuntime]]
    required_modules: tuple[str, ...] = ()
    activities: tuple[ActivityDefinition, ...] = ()


@dataclass(frozen=True, slots=True)
class PluginFactory:
    """内置登记或外部 module:attribute 导出的纯插件配置工厂。

    app 传入唯一实例身份、原始配置和主配置绝对基准目录。prepare 负责自身
    字段与声明路径解析，并拒绝未知字段；它不能探测网络或打开设备。
    使用 typed_factory 可让具体插件保持精确参数类型而无需 cast。
    """

    prepare: Callable[[str, object, Path], PluginBlueprint]


def typed_factory[Settings: BaseModel](
    settings_type: type[Settings],
    declare: Callable[[str, Settings, Path], PluginBlueprint],
) -> PluginFactory:
    """连接具体 Pydantic 配置与纯声明；字段脱敏由应用配置边界统一完成。"""

    def prepare(instance_id: str, raw: object, base_dir: Path) -> PluginBlueprint:
        settings = settings_type.model_validate(raw)
        return declare(instance_id, settings, base_dir)

    return PluginFactory(prepare)
