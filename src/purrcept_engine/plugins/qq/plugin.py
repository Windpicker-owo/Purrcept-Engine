"""完整 QQ 冻结目录与实际 Provider 绑定，连接与事实交接由 QQService 负责。

prepare 只校验配置和声明 me.phone.qq 的 25 个原入口及 reconnect；build 才加载
可选 websockets 实现。所有能力走 FunctionCapability 的一次 Pydantic 校验，
写入不会通过动作别名轮试，前置查询超时也不会迟到继续执行后续写入。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from pydantic import BaseModel

from purrcept_engine.plugin_system import (
    CallContext,
    CallOutcome,
    CapabilityDeclaration,
    CapabilityInputError,
    DevicePanel,
    FunctionCapability,
    ObjectDeclaration,
    PanelControl,
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
    typed_factory,
)
from purrcept_engine.plugins.qq import parameters as p
from purrcept_engine.plugins.qq.media import QQMediaError
from purrcept_engine.plugins.qq.messages import (
    directory,
    id_text,
    image_references,
    object_value,
    project_message,
)
from purrcept_engine.plugins.qq.ports import ConnectionRejected, Connector, QQCallError
from purrcept_engine.plugins.qq.qzone import cards, write_result
from purrcept_engine.plugins.qq.service import QQService
from purrcept_engine.plugins.qq.settings import QQSettings
from purrcept_engine.plugins.qq.stickers import StickerPublicationError
from purrcept_engine.values import JsonObject, JsonValue, encode_json

_SCHEMAS: tuple[tuple[str, type[BaseModel], str, str, str], ...] = (
    (
        "send",
        p.Send,
        ("在指定 QQ 私聊或群聊中发送消息，支持文字、图片、文件、语音，以及群聊 @ 和回复。"),
        (
            "需要向特定好友或群聊发送消息时使用。可多次调用发送多条，"
            "系统会自动加入打字间隔。群聊可用 at 填入一名成员的 QQ 号进行 "
            "@；reply 填入要回复的 message_id。voice "
            "填标准 base64 音频或沙箱内音频路径，作为语音消息发出。"
            "voice 与 text 可同时给；部分实现端可能只播语音，"
            "推荐语音单独一条。两者 at/reply 均可选，常一起用。私聊禁止 "
            "at。回复和at仅在必要时使用，如需要明确提示对方或者原消息已被覆盖时"
            "，通常情况不要使用。"
        ),
        (
            "await me.phone.qq."
            'send(chat="group", '
            'target_id="987654", text="看这个", '
            'at="123456", reply="11111")'
        ),
    ),
    (
        "read",
        p.Read,
        "读取指定 QQ 私聊或群聊会话中最近的聊天记录。",
        (
            "用于查阅特定好友或群聊近期交流记录。本轮刚感知到的新消息已在上下文中，"
            "无需重复读取。 首次要求 chat/target_id，count "
            "省略仍为20；续读仅指定 cursor，不重新查询平台。"
        ),
        ('await me.phone.qq.read(chat="group", target_id="987654", count=20)'),
    ),
    (
        "friends",
        p.Empty,
        "获取当前账号的 QQ 好友列表及备注信息。",
        "用于查找好友、确认备注名称或获取好友 QQ 号。",
        "await me.phone.qq.friends()",
    ),
    (
        "groups",
        p.Empty,
        "获取当前账号加入的 QQ 群列表及群基本信息。",
        "用于确认已加入的群聊列表及对应群号。",
        "await me.phone.qq.groups()",
    ),
    (
        "members",
        p.Members,
        "获取指定 QQ 群的成员列表及群名片、角色信息。",
        ("用于查询指定群内的成员名单或群名片信息。成员较多时数据量较大，建议按需调用。"),
        ('await me.phone.qq.members(group_id="987654")'),
    ),
    (
        "login",
        p.Empty,
        "获取当前登录的 QQ 账号信息（QQ 号及昵称）。",
        "用于确认当前账号自身的登录身份与昵称。",
        "await me.phone.qq.login()",
    ),
    (
        "recall",
        p.Recall,
        "撤回指定的 QQ 消息（群聊撤回他人消息通常需要管理员权限）。",
        ("发送错误或需要清理群内不良消息时使用。message_id 须从先前感知或读取的消息记录中获取。"),
        ('await me.phone.qq.recall(message_id="12345")'),
    ),
    (
        "mute",
        p.Mute,
        "对指定群成员执行禁言操作（单位为秒，0 表示解除禁言）。",
        "用于群管理维护秩序。当前账号必须拥有群管理员或群主权限。",
        ('await me.phone.qq.mute(group_id="987654", user_id="123456", duration_seconds=600)'),
    ),
    (
        "unmute",
        p.Unmute,
        "解除指定群成员的禁言状态。",
        ("用于群管理中恢复被禁言成员的发言权限。当前账号必须拥有群管理员或群主权限。"),
        ('await me.phone.qq.unmute(group_id="987654", user_id="123456")'),
    ),
    (
        "poke",
        p.Poke,
        "在私聊或群聊中对指定好友或群成员发送「戳一戳」。",
        "用于轻量提醒或互动。",
        ('await me.phone.qq.poke(chat="group", target_id="987654", user_id="123456")'),
    ),
    (
        "react",
        p.React,
        ("给一条消息贴表情或取消（SnowLuma set_msg_emoji_like）。emoji_id 是表情数字编号字符串。"),
        ("想用表情回应某条消息时用。add=false 表示取消。不是发一条新消息。"),
        ('await me.phone.qq.react(message_id="12345", emoji_id="76")'),
    ),
    (
        "forward",
        p.Forward,
        "把已有消息或合并转发直接转到私聊或群。",
        "想原样转发某条消息时用。伪造内容请用 compose_forward。",
        ('await me.phone.qq.forward(chat="group", target_id="987654", message_id="12345")'),
    ),
    (
        "compose_forward",
        p.ComposeForward,
        ("构造自定义合并转发节点并发出（伪造转发内容：调用方提供 name/uin/content）。"),
        "需要拼一段看起来像聊天记录的转发时用。普通转发请用 forward。",
        (
            "await me.phone.qq."
            'compose_forward(chat="group", '
            'target_id="987654", '
            'nodes=[{"name": "小明", "uin": '
            '"10001", "content": "你好"}])'
        ),
    ),
    (
        "save_sticker",
        p.SaveSticker,
        "把一张图存进贴纸库并打标签。可用 message_id 或 url。",
        "以后想反复发的表情先存下来。默认不会把图像内容塞进感知。",
        ('await me.phone.qq.save_sticker(tag="猫猫", url="https://example.com/cat.png")'),
    ),
    (
        "send_sticker",
        p.SendSticker,
        "按 id 或标签发出一张已保存贴纸。",
        "贴纸库里已有标签时用。不要把原始像素写进行动代码。",
        ('await me.phone.qq.send_sticker(chat="group", target_id="987654", tag="猫猫")'),
    ),
    (
        "stickers",
        p.Empty,
        ("浏览已保存贴纸：路径 → 标签和 sticker_id。默认不展示图像内容。"),
        "想确认库里有哪些标签时用。看画面请再调用 view。",
        "await me.phone.qq.stickers()",
    ),
    (
        "qzone_post",
        p.QZonePost,
        "发表一条 QQ 空间说说，可带图并设置查看权限。",
        (
            "想发说说时用。ugc_right：1所有人 4好友 16部分好友可见 "
            "64仅自己 128部分好友不可见；16/128 需要 "
            "target_uins。图片走 id/url/本地白名单路径，"
            "不要塞像素。"
        ),
        ('await me.phone.qq.qzone_post(content="今天天气好", ugc_right=1)'),
    ),
    (
        "qzone_list",
        p.QZoneList,
        "打开某人的 QQ 空间说说列表。省略 target_uin 则看自己。",
        ("想看说说、拿 tid 去点赞评论时用。结果只有文字预览和图片 id/url，没有像素。"),
        ('await me.phone.qq.qzone_list(target_uin="123456", num=20)'),
    ),
    (
        "qzone_feeds",
        p.QZoneFeeds,
        ("打开 QQ 空间好友动态（feed）。协议侧深翻页未实现，只有第一页可靠。"),
        (
            "想刷好友动态时用。page_num 默认 1；不要依赖更深页。"
            "图片只有 id/url。 page_num>1 明确拒绝，"
            "has_more 不承诺本适配器支持深页。"
        ),
        ("await me.phone.qq.qzone_feeds(count=10)"),
    ),
    (
        "qzone_comment",
        p.QZoneComment,
        "评论一条 QQ 空间说说，可带图。",
        "已经有 tid 时用。target_uin 省略则评论自己的说说。",
        ('await me.phone.qq.qzone_comment(tid="abc", content="好看")'),
    ),
    (
        "qzone_like",
        p.QZoneLike,
        "给一条 QQ 空间说说点赞。",
        ("已经有 tid 时用。知道发表时间就传 abstime（unix 秒），会更可靠。"),
        ('await me.phone.qq.qzone_like(tid="abc", target_uin="123456", abstime=1700000000)'),
    ),
    (
        "qzone_unlike",
        p.QZoneLike,
        ("取消一条 QQ 空间说说的赞。SnowLuma 注明取消赞端点待真机核实。"),
        "已经点过赞、想收回时用。参数与 qzone_like 相同。",
        ('await me.phone.qq.qzone_unlike(tid="abc")'),
    ),
    (
        "qzone_delete",
        p.Tid,
        "按 tid 删除自己空间里的一条说说。",
        ("发错了、想删掉说说时用。tid 来自 qzone_list 或 qzone_post。"),
        ('await me.phone.qq.qzone_delete(tid="abc")'),
    ),
    (
        "qzone_set_right",
        p.QZoneSetRight,
        "修改一条已发说说的查看权限。",
        ("16/128 需要 target_uins。权限枚举与 qzone_post 相同。"),
        ('await me.phone.qq.qzone_set_right(tid="abc", ugc_right=64)'),
    ),
    (
        "qzone_ban",
        p.QZoneBan,
        ("拉黑或解除拉黑某人（改自己 QQ 空间黑名单）。enable 默认 true。"),
        "不想再看到某人空间动态时用。enable=false 解除。",
        ('await me.phone.qq.qzone_ban(user_id="123456", enable=True)'),
    ),
    (
        "reconnect",
        p.Empty,
        "发起一次有限连接恢复；reverse 等待外部接入，不重发旧 API。",
        "reverse 查询真实监听；direct 仅启动配置的一次有限恢复。",
        "await me.phone.qq.reconnect()",
    ),
)

_READS = frozenset(
    {"read", "friends", "groups", "members", "login", "stickers", "qzone_list", "qzone_feeds"}
)


class QQCapabilities:
    """按冻结声明进入实际业务，唯一 Service 持有连接、文件与事实端口。"""

    def __init__(self, service: QQService) -> None:
        """由 build 绑定实际资源，不在模型端构造远端对象。"""
        self.service = service

    async def invoke(self, name: str, parameters: BaseModel, context: CallContext) -> CallOutcome:
        """只捕获明确媒体参数拒绝与原生平台拒绝，内部和权威故障保持原型。"""
        try:
            return await self._invoke(name, parameters, context)
        except QQMediaError as error:
            raise CapabilityInputError(str(error)) from error
        except StickerPublicationError as error:
            return CallOutcome("unknown", error.recovery, type(error).__name__, str(error))
        except QQCallError as error:
            # 此处只接多阶段前置 lookup 的失败；后续副作用没有派发，不能报已发送。
            return CallOutcome(
                "failed",
                {
                    "phase": "write_not_started",
                    "prerequisite": self.service.error_outcome(error).value,
                },
                type(error).__name__,
                str(error),
            )

    async def _invoke(self, name: str, parameters: BaseModel, context: CallContext) -> CallOutcome:
        """从原入口的明确参数走原生动作，不通过通用参数字典猜业务分支。"""
        service = self.service
        if name == "reconnect" and isinstance(parameters, p.Empty):
            return CallOutcome("confirmed", await service.client.reconnect(context.call_id))
        if isinstance(parameters, p.Read):
            return await service.read(parameters, context)
        if isinstance(parameters, p.Send):
            message, temporary = await service.media.io(lambda: service.media.prepare(parameters))
            pause = service.settings.typing_pause_seconds(
                parameters.text if parameters.text.strip() else "·"
            )
            if pause:
                await asyncio.sleep(pause)
            return await self.send(
                parameters.chat,
                parameters.target_id,
                message,
                context,
                temporary=temporary,
                extra={
                    "text": parameters.text,
                    "image_count": len(parameters.images or []),
                    "file_count": len(parameters.files or []),
                    "voice_count": int(bool(parameters.voice)),
                    "has_voice": bool(parameters.voice),
                },
            )
        if isinstance(parameters, p.Empty):
            if name == "stickers":
                records = await service.media.io(service.stickers.list)
                return CallOutcome(
                    "confirmed", {"stickers": [record.value() for record in records]}
                )
            actions = {
                "friends": "get_friend_list",
                "groups": "get_group_list",
                "login": "get_login_info",
            }
            action = actions[name]
            generation = service.client.generation

            def info(data: JsonValue) -> JsonObject:
                if name == "login":
                    account = id_text(object_value(data).get("user_id"))
                    if account:
                        service.confirm_account(account, generation)
                    return {
                        "user_id": account or None,
                        "nickname": object_value(data).get("nickname"),
                    }
                return directory(data, kind="friends" if name == "friends" else "groups")

            return await service.rpc(action, {}, context, info)
        if isinstance(parameters, p.Members):
            return await service.rpc(
                "get_group_member_list",
                {"group_id": int(parameters.group_id)},
                context,
                lambda data: {"group_id": parameters.group_id, **directory(data, kind="members")},
            )
        if isinstance(parameters, p.React):
            return await service.rpc(
                "set_msg_emoji_like",
                {
                    "message_id": int(parameters.message_id),
                    "emoji_id": parameters.emoji_id,
                    "set": parameters.add,
                },
                context,
                lambda data: {
                    "message_id": parameters.message_id,
                    "emoji_id": parameters.emoji_id,
                    "added": parameters.add,
                    "platform_reported_success": True,
                    "actual": data,
                },
            )
        if isinstance(parameters, p.Recall):
            return await service.rpc(
                "delete_msg",
                {"message_id": int(parameters.message_id)},
                context,
                lambda data: {
                    "message_id": parameters.message_id,
                    "recalled": True,
                    "confirmation_source": "platform_acknowledgement",
                    "actual": data,
                },
            )
        if isinstance(parameters, p.Unmute):
            duration = (
                min(2592000, max(0, parameters.duration_seconds))
                if isinstance(parameters, p.Mute)
                else 0
            )
            return await service.rpc(
                "set_group_ban",
                {
                    "group_id": int(parameters.group_id),
                    "user_id": int(parameters.user_id),
                    "duration": duration,
                },
                context,
                lambda data: {
                    "group_id": parameters.group_id,
                    "user_id": parameters.user_id,
                    "duration_seconds": duration,
                    "muted": duration > 0,
                    "actual": data,
                    "confirmation_source": "platform_acknowledgement",
                },
            )
        if isinstance(parameters, p.Poke):
            params: JsonObject = {"user_id": int(parameters.user_id or parameters.target_id)}
            if parameters.chat == "group":
                params["group_id"] = int(parameters.target_id)
            return await service.rpc(
                "send_poke",
                params,
                context,
                lambda data: {
                    "chat": parameters.chat,
                    "target_id": parameters.target_id,
                    "user_id": str(params["user_id"]),
                    "poked": True,
                    "actual": data,
                    "confirmation_source": "platform_acknowledgement",
                },
            )
        if isinstance(parameters, p.Forward):
            if parameters.message_id:
                action = (
                    "forward_group_single_msg"
                    if parameters.chat == "group"
                    else "forward_friend_single_msg"
                )
                params = {
                    "group_id" if parameters.chat == "group" else "user_id": int(
                        parameters.target_id
                    ),
                    "message_id": int(parameters.message_id),
                }
                return await service.rpc(
                    action,
                    params,
                    context,
                    lambda data: {
                        **self.forward_result(data, parameters.chat, parameters.target_id),
                        "source_message_id": parameters.message_id,
                        "mode": "direct",
                    },
                )
            data, reference = await service.lookup(
                "get_forward_msg", {"id": parameters.forward_id}, context
            )
            messages = object_value(data).get("messages")
            if not isinstance(messages, list):
                raise ConnectionRejected("get_forward_msg 没有实际 messages 数组。")
            nodes: list[JsonValue] = []
            for message in messages:
                if not isinstance(message, dict):
                    raise ConnectionRejected("get_forward_msg 含非对象节点。")
                if message.get("type") == "node":
                    nodes.append(message)
                    continue
                sender = object_value(message.get("sender"))
                display_name = sender.get("nickname", message.get("nickname", message.get("name")))
                uin = id_text(sender.get("user_id", message.get("user_id", message.get("uin"))))
                content = message.get("content", message.get("message"))
                if not isinstance(display_name, str) or not uin or content is None:
                    raise ConnectionRejected("get_forward_msg 节点缺少真实 name/uin/content。")
                nodes.append(
                    {"type": "node", "data": {"name": display_name, "uin": uin, "content": content}}
                )
            return await self.forward(
                parameters.chat,
                parameters.target_id,
                nodes,
                context,
                extra={
                    "source_forward_id": parameters.forward_id,
                    "source_response": reference,
                    "mode": "direct",
                },
            )
        if isinstance(parameters, p.ComposeForward):
            nodes: list[JsonValue] = [
                {
                    "type": "node",
                    "data": {"name": node.name, "uin": node.uin, "content": node.content},
                }
                for node in parameters.nodes
            ]
            extras: JsonObject = {
                key: value.strip()
                for key, value in (
                    ("source", parameters.source),
                    ("summary", parameters.summary),
                    ("prompt", parameters.prompt),
                )
                if value.strip()
            }
            return await self.forward(
                parameters.chat,
                parameters.target_id,
                nodes,
                context,
                extra={**extras, "mode": "custom"},
                request_extra=extras,
            )
        if isinstance(parameters, p.SaveSticker):
            raw = parameters.url
            if parameters.message_id and not raw.strip():
                data, _ = await service.lookup(
                    "get_msg", {"message_id": int(parameters.message_id)}, context
                )
                images = image_references(object_value(data).get("message"))
                if images:
                    reference = object_value(images[0])
                    raw = id_text(reference.get("url"))
                    if not raw:
                        file = id_text(reference.get("file"))
                        file_id = id_text(reference.get("file_id", reference.get("fileId")))
                        if not file and not file_id:
                            raise QQMediaError("消息图片没有实际 URL 或协议文件身份。")
                        image_data, _ = await service.lookup(
                            "get_image",
                            {"file": file or file_id, "file_id": file_id or file},
                            context,
                        )
                        raw = self.image_data_source(image_data)
                else:
                    raw = self.image_data_source(data)
            asset = await service.media.image(raw)

            def save_file() -> CallOutcome:
                # 文件串行工作可能等待旧线程；真正写入前再核对原调用期限。
                if time.monotonic() >= context.deadline:
                    return CallOutcome(
                        "failed",
                        {"phase": "write_not_started"},
                        "QQCallDeadline",
                        "贴纸写入在实际开始前期限耗尽。",
                    )
                try:
                    record = service.stickers.save(asset, tag=parameters.tag)
                except StickerPublicationError as error:
                    return CallOutcome("unknown", error.recovery, type(error).__name__, str(error))
                return CallOutcome("confirmed", record.value())

            async def save() -> CallOutcome:
                return await service.media.io(save_file)

            return await service.file_result(context, save)
        if isinstance(parameters, p.SendSticker):
            record = await service.media.io(
                lambda: service.stickers.get(sticker_id=parameters.sticker_id, tag=parameters.tag)
            )
            path = await service.media.io(lambda: service.stickers.path(record))
            pause = service.settings.typing_pause_seconds("·")
            if pause:
                await asyncio.sleep(pause)
            message: JsonValue = [
                {
                    "type": "image",
                    "data": {
                        "file": path.as_uri(),
                        # SnowLuma 的 OneBot 字段是 sub_type；省略或拼成 subtype
                        # 会按普通图片发送，summary 只控制摘要文字，不决定显示样式。
                        "sub_type": 1,
                        "summary": "[" + record.tag + "]",
                    },
                }
            ]
            return await self.send(
                parameters.chat,
                parameters.target_id,
                message,
                context,
                extra={"sticker_id": record.sticker_id, "tag": record.tag},
            )
        return await self.qzone(name, parameters, context)

    def image_data_source(self, data: JsonValue) -> str:
        """只取旧 get_image/get_msg 实际编码或 URL 字段，不把本地缓存名猜成路径。"""
        mapping = object_value(data)
        for key in ("base64", "file", "url"):
            value = mapping.get(key)
            if not isinstance(value, str):
                continue
            if value.startswith(("base64://", "http://", "https://")):
                return value
            if value.startswith("data:") and ";base64," in value:
                return "base64://" + value.split(";base64,", 1)[1]
        nested = mapping.get("data")
        if isinstance(nested, dict):
            return self.image_data_source(nested)
        raise QQMediaError("平台未返回实际图片编码或可取得 URL。")

    async def send(
        self,
        chat: str,
        target_id: str,
        message: JsonValue,
        context: CallContext,
        *,
        temporary: tuple[Path, ...] = (),
        extra: JsonObject | None = None,
    ) -> CallOutcome:
        """已准备消息只派发一次；确认缺 message_id 时保留字段未知。"""
        action = "send_group_msg" if chat == "group" else "send_private_msg"
        params: JsonObject = {
            "group_id" if chat == "group" else "user_id": int(target_id),
            "message": message,
        }
        generation = self.service.client.generation

        def result(data: JsonValue) -> JsonObject:
            message_id = id_text(object_value(data).get("message_id"))
            if message_id:
                cached = project_message(
                    {
                        "message_id": message_id,
                        "user_id": self.service.scope(generation),
                        "self_id": self.service.scope(generation),
                        "message": message,
                    },
                    chat=chat,
                    target_id=target_id,
                )
                self.service.cache.remember(self.service.scope(generation), cached)
            return {
                "chat": chat,
                "target_id": target_id,
                "message_id": message_id or None,
                "unknown_fields": [] if message_id else ["message_id"],
                "platform_reported_success": True,
                **(extra or {}),
            }

        return await self.service.rpc(action, params, context, result, temporary=temporary)

    def forward_result(self, data: JsonValue, chat: str, target: str) -> JsonObject:
        """保留实际转发身份，空 data 不以原输入消息身份补造新消息。"""
        mapping = object_value(data)
        return {
            "chat": chat,
            "target_id": target,
            "message_id": id_text(mapping.get("message_id")) or None,
            "forward_id": id_text(mapping.get("forward_id", mapping.get("res_id"))) or None,
            "platform_reported_success": True,
        }

    async def forward(
        self,
        chat: str,
        target: str,
        nodes: list[JsonValue],
        context: CallContext,
        *,
        extra: JsonObject | None = None,
        request_extra: JsonObject | None = None,
    ) -> CallOutcome:
        """前置读取完整成功后才执行合并转发；不安排迟到续写。"""
        action = "send_group_forward_msg" if chat == "group" else "send_private_forward_msg"
        params: JsonObject = {
            "group_id" if chat == "group" else "user_id": int(target),
            "messages": nodes,
            **(request_extra or {}),
        }
        return await self.service.rpc(
            action,
            params,
            context,
            lambda data: {
                **self.forward_result(data, chat, target),
                "node_count": len(nodes),
                **(extra or {}),
            },
        )

    async def qzone(self, name: str, parameters: BaseModel, context: CallContext) -> CallOutcome:
        """九个实际空间动作共用信封/原文机制，各自保持原参数和响应字段。"""
        params: JsonObject = {}
        if isinstance(parameters, p.QZonePost):
            action = "send_qzone_msg"
            params = {"content": parameters.content, "ugc_right": parameters.ugc_right}
            if parameters.target_uins:
                params["target_uins"] = [int(value) for value in parameters.target_uins]
            if parameters.images:
                params["images"] = await self.service.media.io(
                    lambda: self.service.media.qzone_sources(parameters.images or [])
                )
        elif isinstance(parameters, p.QZoneList):
            action = "get_qzone_msg_list"
            params = {"pos": parameters.pos, "num": min(100, max(1, parameters.num))}
            if parameters.target_uin:
                params["target_uin"] = int(parameters.target_uin)
        elif isinstance(parameters, p.QZoneFeeds):
            action = "get_qzone_feeds"
            params = {"page_num": 1, "count": min(50, max(1, parameters.count))}
        elif isinstance(parameters, p.QZoneComment):
            action = "comment_qzone"
            params = {"tid": parameters.tid, "content": parameters.content}
            if parameters.target_uin:
                params["target_uin"] = int(parameters.target_uin)
            if parameters.images:
                params["images"] = await self.service.media.io(
                    lambda: self.service.media.qzone_sources(parameters.images or [])
                )
        elif isinstance(parameters, p.QZoneLike):
            action = "like_qzone" if name == "qzone_like" else "unlike_qzone"
            params = {"tid": parameters.tid}
            if parameters.target_uin:
                params["target_uin"] = int(parameters.target_uin)
            if parameters.abstime:
                params["abstime"] = parameters.abstime
        elif isinstance(parameters, p.QZoneSetRight):
            action = "set_qzone_msg_right"
            params = {"tid": parameters.tid, "ugc_right": parameters.ugc_right}
            if parameters.target_uins:
                params["target_uins"] = [int(value) for value in parameters.target_uins]
        elif isinstance(parameters, p.QZoneBan):
            action = "set_qzone_ban"
            params = {"user_id": int(parameters.user_id), "enable": parameters.enable}
        elif isinstance(parameters, p.Tid) and name == "qzone_delete":
            action = "delete_qzone_msg"
            params = {"tid": parameters.tid}
        else:
            raise TypeError("QQ 冻结声明与实际参数模型不一致。")

        def project(data: JsonValue) -> JsonObject:
            if name not in {"qzone_list", "qzone_feeds"}:
                return write_result(name, data, params)
            value = cards(data, feeds=name == "qzone_feeds")
            value.update(params)
            value["target_uin"] = id_text(params.get("target_uin"))
            # 短卡片仍逐项完整；本页超出呈现容量时留下实际位置和原响应读取入口。
            field = "feeds" if name == "qzone_feeds" else "messages"
            projected = value[field]
            if not isinstance(projected, list):
                raise RuntimeError("QZone 投影缺少声明列表。")
            kept: list[JsonValue] = []
            size = 2
            for card in projected:
                cost = len(encode_json(card).decode()) + int(bool(kept))
                if size + cost > self.service.evidence.page_chars:
                    break
                kept.append(card)
                size += cost
            value[field] = kept
            value["overview_omitted_positions"] = list(range(len(kept), len(projected)))
            value["level"] = "overview"
            return value

        return await self.service.rpc(action, params, context, project)


def _declare(
    instance_id: str, settings: QQSettings, base_dir: Path, connector: Connector | None = None
) -> PluginBlueprint:
    """纯目录保持在线/离线完全相同；phone 父分组由 app 共享装配。"""
    object_id = instance_id + ":qq"
    declarations = tuple(
        CapabilityDeclaration(
            instance_id + ":qq." + name,
            object_id,
            name,
            description,
            model,
            settings.call_timeout_seconds + settings.typing_delay_max_seconds,
            example.replace("me.phone.qq.", "me.phone." + settings.segment + "."),
            usage,
            concurrency="parallel" if name in _READS else "target_serial",
            control=name == "reconnect",
        )
        for name, model, description, usage, example in _SCHEMAS
    )

    async def build(ports: PluginPorts) -> PluginRuntime:
        actual = connector
        if actual is None:
            from purrcept_engine.plugins.qq.websocket import WebSocketConnector

            actual = WebSocketConnector()
        service = QQService(instance_id, settings, base_dir, ports, actual)
        capabilities = QQCapabilities(service)

        def bind(declaration: CapabilityDeclaration[BaseModel]) -> FunctionCapability[BaseModel]:
            async def run(parameters: BaseModel, context: CallContext) -> CallOutcome:
                return await capabilities.invoke(declaration.name, parameters, context)

            return FunctionCapability(declaration, run)

        return PluginRuntime(
            tuple(bind(declaration) for declaration in declarations),
            start=service.client.start,
            stop_inputs=service.client.stop_inputs,
            close=service.close,
            snapshot=service.snapshot,
            prepare_request=service.prepare_request,
            panel=DevicePanel(
                "qq",
                (PanelControl("reconnect", "恢复连接", instance_id + ":qq.reconnect"),),
                service.client.panel_revision,
            ),
        )

    return PluginBlueprint(
        (ObjectDeclaration(object_id, settings.segment, settings.label, settings.phone_id),),
        declarations,
        build,
        required_modules=("websockets",) if connector is None else (),
    )


factory: PluginFactory = typed_factory(QQSettings, _declare)


def factory_with_connector(connector: Connector) -> PluginFactory:
    """程序式装配显式传输端口，供离线契约验证；不会自动替换生产传输。"""
    return typed_factory(
        QQSettings,
        lambda instance_id, settings, base_dir: _declare(
            instance_id, settings, base_dir, connector
        ),
    )
