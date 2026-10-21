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
from purrcept_engine.plugin_system.captions import SpeechCaption
from purrcept_engine.plugin_system.panels import DevicePanel
from purrcept_engine.values import JsonObject


class InputPort(Protocol):
    """宿主借给输入插件的专用接管端口，不授予模型或存储访问权限。

    外部适配器提交不可变原文后保存 input_id；取消等待不会撤销已接管事务。
    重连按原生事件身份重投或查询，不能以通知丢失推断此前未接管。
    适配器了解来源语义，应在接管前用 InputSubmission.with_content 冻结来源和
    已渲染的有序 TextBlock/ImageBlock/AudioBlock，原生帧另存 details_json。
    框架按来源分组并增加标题、证据和缺口文字，不能改写文字或移动媒体位置；
    按目标模型 supports_contents 将不支持的媒体在原位置投影为明确文字引用。
    不得借展示省略发言正文或宣称已经读取平台媒体。
    """

    async def submit(self, submission: InputSubmission) -> Admission:
        """正式接管外部来源的原文与媒体，返回输入身份。

        Args:
            submission: 待正式接管的来源、原文、媒体与可信信号。

        Returns:
            正式接管凭证，包含 input_id 与重复来源标记；不表示模型已处理或对外回复。

        Raises:
            HostStateError: 宿主当前阶段不允许接管新输入。
            StorageError: 输入不合法、来源冲突、接管容量不足或权威存储失败。

        Note:
            原子接管或明确拒收，接管不等同于主脑已经处理。
        """
        ...

    async def query_input(self, input_id: str) -> InputRecord | None:
        """读取实际接管及处理状态，不执行模型或再次发送输入。

        Args:
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。

        Returns:
            首次接管的完整输入原文、来源与媒体引用。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
        ...

    async def query_processing(self, input_id: str) -> InputProcessing | None:
        """读取真实阶段和失败原因。

        Args:
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。

        Returns:
            输入实际处理阶段、版本和关联轮次。 未取得对应记录或素材时返回 None；读取故障仍抛出。

        Note:
            延期或处理中不构成采集画面完成确认。
        """
        ...


class NativeInputPort(Protocol):
    """宿主借出的原生事实只读查询，内置实现为 EngineHost。

    接入插件在重新计算回复关系或本地派生说明前，用原生来源身份查询
    首次接管记录。没有可靠事件身份时返回 None，不能猜测文字去重。
    返回值不授予再次派发资格；真实内容冲突仍由接入边界核验，权威
    读取故障原样传播。端口不暴露数据库或可变输入状态。
    """

    async def query_native_input(self, source: InputSource) -> InputRecord | None:
        """按适配器、账号、会话、事件类别和原生身份读首次事实。

        Args:
            source: 含适配器、账号、会话和原生事件身份的可信来源。

        Returns:
            首次接管的完整输入原文、来源与媒体引用。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
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
        """原子保存显式工具取得的原生媒体和原文，不另投注意或前台输入。

        Args:
            snapshot: 完整不可变原文快照，含来源、确切版本及媒体引用。
            assets: 与快照关联的完整媒体字节，随原文在同一权威边界保存。

        Returns:
            完整保存的原文快照，包含确切来源、版本与媒体关系。
        """
        ...

    async def put_evidence(
        self, snapshot: EvidenceSnapshot, *, needs_reflection: bool = False
    ) -> EvidenceSnapshot:
        """完整保存新快照及媒体关系，不能覆盖同身份的不同原文。

        Args:
            snapshot: 完整不可变原文快照，含来源、确切版本及媒体引用。
            needs_reflection: 是否为新原文登记可靠的待反思关系。 默认 False。

        Returns:
            完整保存的原文快照，包含确切来源、版本与媒体关系。
        """
        ...

    async def read_evidence(
        self,
        evidence_id: str,
        *,
        level: str = "evidence",
        cursor: str | None = None,
        max_chars: int = 8000,
    ) -> EvidencePage:
        """读取指定快照范围，页面带确切来源、版本与下一游标。

        Args:
            evidence_id: 已保存原文快照的稳定身份。
            level: evidence 展开原文，summary 或 overview 读取概要；与目标材料种类一并核验。
                默认 'evidence'。
            cursor: 宿主签发并保存的不透明续读游标，续读同一快照版本。 None 表示首次读取。
            max_chars: 本页正文的字符上限；分页游标继续绑定原快照。 默认 8000。

        Returns:
            当前版本的一页正文、确切范围和下一游标；取得页面不等于已呈现给模型。
        """
        ...

    async def read_evidence_cursor(self, cursor: str, *, max_chars: int = 8000) -> EvidencePage:
        """用已保存游标续读原快照，不能从外界文字自行解析游标权限。

        Args:
            cursor: 宿主签发并保存的不透明续读游标，续读同一快照版本。
            max_chars: 本页正文的字符上限；分页游标继续绑定原快照。 默认 8000。

        Returns:
            当前版本的一页正文、确切范围和下一游标；取得页面不等于已呈现给模型。
        """
        ...


class LateResultPort(Protocol):
    """插件提交实际迟到协议确认的限定端口，不授予重新派发权限。

    app 按当前 Blueprint 的真实能力路径借出，只有保存了 started 门槛且
    action/input 与 CallContext 对应的调用可用。方法等待首版回执提交后追加
    不可变版本；插件持有提交任务到真实结束。确认不改写原行动结束结果，
    不根据请求参数或断线推断效果，也不能使用另一个插件的调用身份。
    """

    async def record(self, context: CallContext, outcome: CallOutcome) -> CallRecord:
        """保存真实迟到结果或返回相同已存事实。

        Args:
            context: 宿主或 Core 注入的可信调用上下文，不由模型自行填写。
            outcome: 此次操作的真实结论，不由期望效果推断成功。

        Returns:
            原调用当前的权威结果及不可变版本历史。

        Note:
            存储故障保持原型。
        """
        ...


@dataclass(frozen=True, slots=True)
class PluginPorts:
    """app 显式注入的有限依赖；字段增加须对应真实业务交接。

    Attributes:
        inputs: 正式接管与查询输入的有限端口，不持有主脑或通用存储。
        snapshot: 读取当前宿主已提交状态的同步函数，不授予状态修改权限。
        evidence: 可选完整原文与分页端口，仅需要捕获证据的插件借用。
        time: 可选的同库闹铃与休息业务端口。
        late_results: 将实际迟到确认追加到原调用的有限端口。
        audio: 同库音频阶段事实端口，不是播放器。
        native_audio_enabled: 宿主确认模型音频声明与媒体计量规则均可用于原声准入。
        native_inputs: 按原生事件身份查询首次接管事实的有限端口。
        captions: 查询已明确发布的当前播放台词；不查询主脑文本或工具结果。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

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
    captions: Callable[[], tuple[SpeechCaption, ...]] | None = None


@dataclass(frozen=True, slots=True)
class PluginRuntime:
    """build 交回的资源所有者及实际能力绑定，由宿主一次取得和收回。

    Attributes:
        capabilities: 与 Blueprint 逐项对应的当前宿主能力实现。
        start: 完整装配后开放本插件接入的异步入口。
        stop_inputs: 关闭时停止新接入的入口，在途回包仍须保留。
        close: 借用归还后释放真实资源的入口，由宿主按依赖调用。
        snapshot: 只读已提交状态的同步投影，用于诊断与面板。
        prepare_request: 每次请求前提供有限业务状态的异步函数。
        panel: 本机有限控制与预览的投影服务。
        audio_output: 可选的实际声音所有者借用，不转移播放器资源所有权。
        body: 可选身体端口；缺省时声音资源仍独立工作。
        resources_released: 仅在全部真实资源确认归还时返回 True 的显式证明入口。
        caption: 可选的当前真实播放台词投影，声音结束或被撤销时返回 None。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

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
    caption: Callable[[], SpeechCaption | None] | None = None


@dataclass(frozen=True, slots=True)
class PluginBlueprint:
    """由工厂纯解析得到的声明和构造函数，配置校验不执行 build。

    Attributes:
        objects: 用于构建目录的稳定对象声明及父子关系。
        capabilities: 与对象声明绑定的完整能力契约集合。
        build: 在完整目录就绪后借用有限端口构造本实例资源的函数。
        required_modules: 显式启用插件时必须安装的可选模块名，不自动安装。
        activities: 此宿主唯一的活动状态提交服务。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

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

    Attributes:
        prepare: 仅解析原始配置与生成声明的工厂，不连接服务或设备。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        app 传入唯一实例身份、原始配置和主配置绝对基准目录。prepare 负责自身
        字段与声明路径解析，并拒绝未知字段；它不能探测网络或打开设备。
        使用 typed_factory 可让具体插件保持精确参数类型而无需 cast。
    """

    prepare: Callable[[str, object, Path], PluginBlueprint]


def typed_factory[Settings: BaseModel](
    settings_type: type[Settings],
    declare: Callable[[str, Settings, Path], PluginBlueprint],
) -> PluginFactory:
    """连接具体 Pydantic 配置与纯声明。

    Args:
        settings_type: 解析此插件原始配置的 Pydantic 模型。
        declare: 接收实例身份、已校验配置与基准目录的纯声明函数。

    Returns:
        接收实例身份、原始配置及基准目录的纯配置工厂。

    Note:
        字段脱敏由应用配置边界统一完成。
    """

    def prepare(instance_id: str, raw: object, base_dir: Path) -> PluginBlueprint:
        """校验原始插件配置，再交给纯声明函数构建 Blueprint。"""
        settings = settings_type.model_validate(raw)
        return declare(instance_id, settings, base_dir)

    return PluginFactory(prepare)
