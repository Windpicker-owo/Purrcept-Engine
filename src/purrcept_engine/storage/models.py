"""定义输入接管的不可变事实与存储查询值。

接入适配器创建 ``InputSubmission``，存储服务确认原文和待办已提交后返回
``Admission``。查询返回媒体引用而非复制 BLOB；来源字段用于路由、去重和追溯，
不建立跨通道身份关联，也不代表能力权限。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from purrcept_engine.execution.protocol import decode_object, encode_json


def _nonempty(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串。")


def _optional_text(value: str | None, field: str) -> None:
    if value is not None:
        _nonempty(value, field)


def _aware(value: datetime, field: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field} 必须是带时区的时间。")


@dataclass(frozen=True, slots=True)
class InputSource:
    """由接入方提供来源范围，不把不同通道的发送者合并。

    原生事件身份的去重范围由适配器实例、账号、会话和事件类别共同确定。
    没有可靠原生 ID 时应传 ``None``；存储为每次接收创建独立身份，无法承诺
    跨连接重放去重。显示名与发送者 ID 保存发生事实，不参与权限判断。
    """

    adapter_id: str
    account_id: str
    conversation_id: str
    event_kind: str
    native_event_id: str | None = None
    sender_id: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        """在接入边界核验来源身份；显示名不提供身份认证。"""
        _nonempty(self.adapter_id, "adapter_id")
        _nonempty(self.account_id, "account_id")
        _nonempty(self.conversation_id, "conversation_id")
        _nonempty(self.event_kind, "event_kind")
        _optional_text(self.native_event_id, "native_event_id")
        _optional_text(self.sender_id, "sender_id")
        _optional_text(self.display_name, "display_name")


@dataclass(frozen=True, slots=True)
class MediaAsset:
    """提交方交接的完整媒体字节，不接受仍可修改的缓冲区。

    内容摘要用于同库 BLOB 去重；媒体类型和名称属于这次引用，而不是摘要的
    全局属性。相同字节出现在不同输入中仍分别计入逻辑积压额度。
    """

    content: bytes
    media_type: str
    name: str | None = None

    def __post_init__(self) -> None:
        """确认完整字节的不可变所有权及引用信息。"""
        if not isinstance(self.content, bytes):
            raise ValueError("content 必须是不可变 bytes。")
        _nonempty(self.media_type, "media_type")
        _optional_text(self.name, "name")


@dataclass(frozen=True, slots=True)
class InputContext:
    """接入方确认的关系与程序信号，正文不能自行授予这些标记。

    details_json 保存平台原生的 @、回复与会话关系，不建立人员权限。最低
    唤起只要求前台处理；interrupt_generation 请求核对当前代次后抢占，
    speech_started 表示已达到配置阈值的 VAD 信号，并另外要求音频停播。
    语音转写接续沿用来源关联，不能再次伪造 speech_started。
    """

    details_json: str = "{}"
    minimum_wake: bool = False
    interrupt_generation: bool = False
    speech_started: bool = False
    missing_materials: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """在可信接入边界冻结有限 JSON 关系，拒绝非对象及伪布尔值。"""
        if not isinstance(self.details_json, str):
            raise ValueError("details_json 必须是 JSON 对象文本。")
        details = decode_object(self.details_json.encode("utf-8"))
        if not isinstance(details, dict):
            raise ValueError("details_json 必须是 JSON 对象文本。")
        for value in (self.minimum_wake, self.interrupt_generation, self.speech_started):
            if type(value) is not bool:
                raise ValueError("程序输入信号必须是 bool。")
        if not isinstance(self.missing_materials, tuple):
            raise ValueError("missing_materials 必须是不可变缺口说明元组。")
        for reason in self.missing_materials:
            _nonempty(reason, "missing_materials")
        object.__setattr__(self, "details_json", encode_json(details).decode("utf-8"))


@dataclass(frozen=True, slots=True)
class InputSubmission:
    """一次接管的发生事实；接收时间不改变原生重放的内容身份。

    两个时间均须带时区，存储规范化为 UTC。正文允许为空以表示纯媒体输入；
    媒体元组保留接入方提供的顺序。对象创建完成后可安全交给数据库线程。
    """

    source: InputSource
    text: str
    occurred_at: datetime
    received_at: datetime
    media: tuple[MediaAsset, ...] = ()
    context: InputContext = InputContext()

    def __post_init__(self) -> None:
        """校验接管信封，确保传给数据库线程的事实不可变且带时区。"""
        if not isinstance(self.source, InputSource):
            raise ValueError("source 必须是 InputSource。")
        if not isinstance(self.context, InputContext):
            raise ValueError("context 必须是 InputContext。")
        if not isinstance(self.text, str):
            raise ValueError("text 必须是字符串。")
        _aware(self.occurred_at, "occurred_at")
        _aware(self.received_at, "received_at")
        if not isinstance(self.media, tuple) or any(
            not isinstance(asset, MediaAsset) for asset in self.media
        ):
            raise ValueError("media 必须是 MediaAsset 元组。")


@dataclass(frozen=True, slots=True)
class MediaReference:
    """权威媒体的内容摘要与本次输入中的引用信息。"""

    media_id: str
    media_type: str
    byte_length: int
    name: str | None = None


@dataclass(frozen=True, slots=True)
class Admission:
    """原文和待办已提交的确认；重复原生输入沿用第一次接管身份。"""

    input_id: str
    duplicate: bool


@dataclass(frozen=True, slots=True)
class InputRecord:
    """原文、来源和当前处置状态的查询快照。

    ``processed`` 只说明存储收到明确处置提交，不证明模型理解、实际回复或
    外部效果。媒体正文按引用另行读取；查询和读取待办不会改变处置状态。
    """

    input_id: str
    source: InputSource
    text: str
    occurred_at: datetime
    received_at: datetime
    media: tuple[MediaReference, ...]
    state: Literal["pending", "processed"]
    logical_bytes: int
    context: InputContext = InputContext()
