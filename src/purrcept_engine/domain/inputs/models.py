"""定义外部输入的来源、原文、媒体与正式接管凭证。

输入插件构造提交值，宿主同事务保存原文与待处理关系后返回 Admission。
本模块只约束不可变数据，不调度注意、不回复或控制播放。
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from purrcept_core.models import AudioBlock, AudioBytes, ImageBlock, ImageBytes, TextBlock

from purrcept_engine.domain.inputs.content import (
    PRESENTATION_KEY,
    InputMediaIndex,
    InputPresentation,
)
from purrcept_engine.values import decode_object, encode_json

type InputBlock = TextBlock | ImageBlock | AudioBlock


def _nonempty(value: object, field: str) -> None:
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

    Attributes:
        adapter_id: 外部输入适配器的稳定身份。
        account_id: 当前接入账号的原生身份。
        conversation_id: 原生会话身份，跨通道不据此猜测同一用户。
        event_kind: 原生来源事件的类别，参与可靠去重。
        native_event_id: 平台给出的原生事件身份；None 表示缺少可靠去重依据。
        sender_id: 已知时保存的原生发送方身份。
        display_name: 来源的展示名称，不用于权限或稳定身份判断。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

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

    Attributes:
        content: 本次明确写入的完整正文。
        media_type: 完整媒体字节实际使用的 MIME 类型。
        name: 此次素材携带的可选文件名称，不参与内容摘要身份。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

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

    Attributes:
        details_json: 原生来源附加字段的严格 JSON 编码。
        minimum_wake: 直接唤起所建立的最低 WAKE 事实或对应升级判断资格。
        interrupt_generation: 此可信接入信号是否要求撤销观察到的旧生成代次。
        speech_started: 真实捕获的停音及下一轮通知信号，不授予生成抢占资格。
        missing_materials: 本次原文或媒体尚缺的具体材料说明。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        details_json 保存平台原生的 @、回复与会话关系，不建立人员权限。最低
        唤起只要求前台处理；interrupt_generation 请求核对当前代次后抢占，
        speech_started 表示已达到配置阈值的 VAD 信号，只要求音频停播和保底
        唤醒下一轮；它优先于同时携带的 interrupt_generation，不直接抢占，
        也不单凭起音建立紧急升级判断。语音转写接续沿用来源关联，不能再次
        伪造 speech_started；最终内容仍按普通高价值输入规则判断。

        InputSubmission.with_content 将提供方的来源与有序内容布局保存在现有 JSON
        扩展的保留键中；媒体字节仍进入同次提交的 media，不重复编码到 JSON。
        布局随原文一起接管、计量和重放，不改变存储信封格式。
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
        if PRESENTATION_KEY in details:
            InputPresentation.from_json(details[PRESENTATION_KEY])
        object.__setattr__(self, "details_json", encode_json(details).decode("utf-8"))

    @property
    def presentation(self) -> InputPresentation | None:
        """取得提供方已冻结的来源和顺序布局；未声明时返回 None。

        Note:
            只读已校验的保留键，不根据正文形状或插件身份猜测展示格式。
        """
        details = decode_object(self.details_json.encode("utf-8"))
        if PRESENTATION_KEY not in details:
            return None
        return InputPresentation.from_json(details[PRESENTATION_KEY])


@dataclass(frozen=True, slots=True)
class InputSubmission:
    """一次接管的发生事实；接收时间不改变原生重放的内容身份。

    Attributes:
        source: 含适配器、账号、会话和原生事件身份的可信来源。
        text: 本次明确传输、输入或呈现的完整文本。
        occurred_at: 原始事实实际发生的 Unix 时间戳（秒）。
        received_at: 此事实实际接收的时刻。
        media: 已接管媒体的引用集合，展开时必须取得原始 BLOB。
        context: 宿主或 Core 注入的可信调用上下文，不由模型自行填写。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

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
        presentation = self.context.presentation
        if presentation is not None:
            indices = {
                block.index for block in presentation.blocks if isinstance(block, InputMediaIndex)
            }
            if indices != set(range(len(self.media))):
                raise ValueError("输入展示必须准确引用本次提交的全部附件。")

    def with_content(self, source: str, blocks: Sequence[InputBlock]) -> "InputSubmission":
        """冻结提供方已渲染的有序 Core 内容块，保留文字与媒体的交错位置。

        Args:
            source: 用于分组和追踪的非空单行来源，例如 me.phone.qq 或 microphone。
            blocks: 按原始语义排列的 TextBlock、ImageBlock、AudioBlock 序列；图片/音频
                必须携带不可变原生字节，不能用 URL 代替已取得媒体。

        Returns:
            原文、来源和可信信号保持原值的新提交；媒体字节进入 media，有序布局只保存
            文字、附件索引和原始 alt_text。已有相同媒体复用，名称不会因布局而丢失。

        Raises:
            ValueError: 来源、块种类或媒体源无效，或有已接管附件未出现在新布局中。

        Note:
            在正式接管前调用；序列立即冻结，之后修改调用方的列表不影响提交。
            宿主只增加来源、输入身份和缺口 TextBlock，不重写这些块的正文或顺序。
            原生字节不进入 JSON，重复引用同一附件的位置仍保留；重放沿用第一次布局。
        """
        if not isinstance(blocks, (tuple, list)) or not blocks:
            raise ValueError("输入内容必须是非空的块序列。")
        assets = list(self.media)
        layout: list[TextBlock | InputMediaIndex] = []
        used: set[int] = set()
        for block in blocks:
            if isinstance(block, TextBlock):
                layout.append(block)
                continue
            if not isinstance(block, (ImageBlock, AudioBlock)) or not isinstance(
                block.source, (ImageBytes, AudioBytes)
            ):
                raise ValueError("输入只接受文字与已取得原生字节的图片/音频块。")
            native = block.source
            if isinstance(block, ImageBlock) and native.media_type not in (
                "image/png",
                "image/jpeg",
                "image/webp",
                "image/gif",
            ):
                raise ValueError("图片输入必须使用可原生交付的图片媒体类型。")
            matches = [
                index
                for index, asset in enumerate(assets)
                if asset.content == native.data and asset.media_type == native.media_type
            ]
            unused = [index for index in matches if index not in used]
            if unused:
                index = unused[0]
            elif matches:
                index = matches[0]
            else:
                index = len(assets)
                assets.append(MediaAsset(native.data, native.media_type))
            used.add(index)
            layout.append(InputMediaIndex(index, block.alt_text))
        presentation = InputPresentation(source, tuple(layout))
        details = decode_object(self.context.details_json.encode("utf-8"))
        details[PRESENTATION_KEY] = presentation.to_json()
        context = replace(self.context, details_json=encode_json(details).decode("utf-8"))
        return replace(self, media=tuple(assets), context=context)


# 维护者属性契约（此 dataclass 也被嵌入 Pydantic schema，保留类说明）。
# Attributes:
#     media_id: 完整媒体字节的内容摘要，供同库读取和完整性核验。
#     media_type: 这些完整字节实际使用的 MIME 类型。
#     byte_length: 原始内容的完整字节数。
#     name: 本次引用携带的可选文件名称，不参与内容身份。
@dataclass(frozen=True, slots=True)
class MediaReference:
    """权威媒体的内容摘要与本次输入中的引用信息。"""

    media_id: str
    media_type: str
    byte_length: int
    name: str | None = None


@dataclass(frozen=True, slots=True)
class Admission:
    """原文和待办已提交的确认；重复原生输入沿用第一次接管身份。

    Attributes:
        input_id: 正式接管返回的输入身份，用于关联原文与后续处理。
        duplicate: 是否复用了相同原生来源身份的首次接管记录。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    input_id: str
    duplicate: bool


@dataclass(frozen=True, slots=True)
class InputRecord:
    """原文、来源和当前处置状态的查询快照。

    Attributes:
        input_id: 正式接管返回的输入身份，用于关联原文与后续处理。
        source: 含适配器、账号、会话和原生事件身份的可信来源。
        text: 本次明确传输、输入或呈现的完整文本。
        occurred_at: 原始事实实际发生的 Unix 时间戳（秒）。
        received_at: 此事实实际接收的时刻。
        media: 已接管媒体的引用集合，展开时必须取得原始 BLOB。
        state: 当前所有者持有的状态或待提交状态值。
        logical_bytes: 本次操作占用的逻辑载荷字节数，用于接管前背压。
        context: 宿主或 Core 注入的可信调用上下文，不由模型自行填写。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

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
