"""保留旧 QQ 模型入口的参数名，在 FunctionCapability 边界一次严格校验。

来源过滤不是能力权限。这里仅约束 QQ 原生身份、首读/续读组合和已批准的
QZone 范围；不加入身份角色、幂等假设或端点别名探测。
"""

from typing import Annotated, Literal, Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from purrcept_engine.values import JsonValue

QQId = Annotated[str, Field(pattern=r"^[1-9][0-9]*$")]
MessageId = Annotated[str, Field(pattern=r"^-?[1-9][0-9]*$")]
Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
Chat = Literal["private", "group"]
Right = Literal[1, 4, 16, 64, 128]


class Parameters(BaseModel):
    """公开调用的严格模型；JSON 数组保持原有 list 形状。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Empty(Parameters):
    """无模型参数的入口不接受运行期预算或伪造权限字段。"""


class Target(Parameters):
    """明确会话路由，不从发送者角色推导目标。"""

    chat: Chat
    target_id: QQId


class Send(Target):
    """按原顺序组合回复、@、语音、文字、图片和文件。"""

    text: str = ""
    images: list[Text] | None = None
    files: list[Text] | None = None
    voice: str = ""
    at: QQId | Literal[""] = ""
    reply: MessageId | Literal[""] = ""

    @model_validator(mode="after")
    def valid_message(self) -> Self:
        """私聊 @ 明确拒绝；空消息不发送给平台试错。"""
        if self.chat == "private" and self.at:
            raise ValueError("私聊不支持 at。")
        if not any((self.text.strip(), self.images, self.files, self.voice.strip())):
            raise ValueError("不能发送空消息。")
        return self


class Read(Parameters):
    """首读的 count 省略为 20；续读只允许原宿主 cursor。"""

    chat: Chat | None = None
    target_id: QQId | None = None
    count: int | None = None
    cursor: Text | None = None

    @model_validator(mode="after")
    def source_or_cursor(self) -> Self:
        """续读不混入新的会话或取数条件，防止把旧快照当成新查询。"""
        if self.cursor is not None:
            if any(value is not None for value in (self.chat, self.target_id, self.count)):
                raise ValueError("cursor 续读不能同时指定 chat、target_id 或 count。")
        elif self.chat is None or self.target_id is None:
            raise ValueError("首次读取必须指定 chat 和 target_id。")
        return self


class Members(Parameters):
    """查询指定群的实际成员信息。"""

    group_id: QQId


class Recall(Parameters):
    """保留平台带符号的消息身份。"""

    message_id: MessageId


class Unmute(Parameters):
    """群和成员身份共同限定禁言目标。"""

    group_id: QQId
    user_id: QQId


class Mute(Unmute):
    """与旧实现相同，真实派发时把时长限制在 0–2592000 秒。"""

    duration_seconds: int = 1800


class Poke(Target):
    """user_id 省略时沿用 target_id，不增加人员权限判断。"""

    user_id: QQId | Literal[""] = ""


class React(Recall):
    """平台表情身份和增删标记按原参数传递。"""

    emoji_id: Text
    add: bool = True


class Forward(Target):
    """单条消息身份优先于合并转发身份，沿用旧入口的明确优先级。"""

    message_id: MessageId | Literal[""] = ""
    forward_id: str = ""

    @model_validator(mode="after")
    def one_source(self) -> Self:
        """至少提供一个真实来源，不生成转发身份。"""
        if not self.message_id and not self.forward_id.strip():
            raise ValueError("必须提供 message_id 或 forward_id。")
        return self


class ForwardNode(Parameters):
    """合并转发保留展示名、QQ 号和完整 OneBot 消息内容。"""

    name: Text
    uin: QQId
    content: JsonValue


class ComposeForward(Target):
    """显式节点列表，不从上下文猜消息正文。"""

    nodes: list[ForwardNode] = Field(min_length=1)
    source: str = ""
    summary: str = ""
    prompt: str = ""


class SaveSticker(Parameters):
    """标签保存到磁盘，沿用有效 URL 优先于消息身份的旧入口约定。"""

    tag: Annotated[str, Field(pattern=r"^\S{1,64}$")]
    message_id: MessageId | Literal[""] = ""
    url: str = ""

    @model_validator(mode="after")
    def one_source(self) -> Self:
        """不能把无来源的保存伪装成完成。"""
        if not self.message_id and not self.url.strip():
            raise ValueError("必须提供 message_id 或 url。")
        if self.url:
            parsed = urlparse(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("url 必须为实际 http(s) 图片 URL。")
        return self


class SendSticker(Target):
    """沿用 sticker_id 优先于 tag 的真实库查询约定。"""

    sticker_id: str = ""
    tag: str = ""

    @model_validator(mode="after")
    def actual_source(self) -> Self:
        """至少提供一种贴纸检索身份。"""
        if not self.sticker_id.strip() and not self.tag.strip():
            raise ValueError("必须指定 sticker_id 或 tag。")
        return self


class QZoneRight(Parameters):
    """公开可见性枚举；部分好友范围必须显式给出。"""

    ugc_right: Right
    target_uins: list[QQId] | None = None

    @model_validator(mode="after")
    def explicit_partial_range(self) -> Self:
        """16/128 不允许空名单，不自动推导账号关系。"""
        if self.ugc_right in (16, 128) and not self.target_uins:
            raise ValueError("ugc_right=16/128 必须提供 target_uins。")
        return self


class QZonePost(QZoneRight):
    """发布原文与可选图片，输出不把此请求权利当成实际应用值。"""

    content: Text
    ugc_right: Right = 1
    images: list[Text] | None = None


class QZoneList(Parameters):
    """远端 pos 是实际查询参数，与宿主快照 cursor 分开。"""

    target_uin: QQId | Literal[""] = ""
    pos: Annotated[int, Field(ge=0)] = 0
    num: int = 20


class QZoneFeeds(Parameters):
    """SnowLuma 首批只取得第 1 页，深页明确拒绝而不试探。"""

    page_num: Literal[1] = 1
    count: int = 10


class Tid(Parameters):
    """完整 tid 沿用已取得的协议身份，不臆造别名。"""

    tid: Text


class QZoneComment(Tid):
    """评论目标与原文独立于入站来源过滤。"""

    content: Text
    target_uin: QQId | Literal[""] = ""
    images: list[Text] | None = None


class QZoneLike(Tid):
    """未知 abstime 传 0；已知时沿用 Unix 秒值。"""

    target_uin: QQId | Literal[""] = ""
    abstime: Annotated[int, Field(ge=0)] = 0


class QZoneSetRight(QZoneRight):
    """修改指定说说的权利，仍须读取实际平台返回。"""

    tid: Text


class QZoneBan(Parameters):
    """空间黑名单业务动作，不建立 Engine 人员权限。"""

    user_id: QQId
    enable: bool = True
