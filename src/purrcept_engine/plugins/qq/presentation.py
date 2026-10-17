"""把 QQ 入站事实投影成必要字段，再渲染为稳定的 YAML 刺激。

QQService 在正式接管前调用 render_qq_input，并冻结结果到 InputContext。
渲染只使用本次事件、已完成的消息投影和同账号同会话的有限缓存，不查询网络、
不读取设备、不按名字合并身份。昵称缺失时使用账号，原始平台帧仍由 Service 保存。
"""

from collections.abc import Sequence
from datetime import datetime

from purrcept_engine.plugins.qq.messages import id_text, object_value
from purrcept_engine.serialization import render_yaml
from purrcept_engine.values import JsonObject, JsonValue, encode_json


def _label(value: str) -> str:
    """名称和动作只占一行；原始换行仍保存在平台记录中，不让它伪造新的发言。"""
    return value.replace("\r", "\\r").replace("\n", "\\n")


def _name(identity: str, known: Sequence[JsonValue]) -> str:
    """只从当前账号和会话的已知消息取最后一个实际昵称，不向平台补查。"""
    for value in reversed(known):
        message = object_value(value)
        if id_text(message.get("sender_id")) == identity:
            name = id_text(message.get("sender_name"))
            if name:
                return name
    return ""


def _person(identity: str, name: str, *, account: str) -> str:
    """稳定账号决定是否为主体；同名不获得“你”的身份。"""
    if not identity:
        return "未知对象"
    suffix = "，你" if identity == account else ""
    if name:
        return f"{_label(name)}({identity}{suffix})"
    if suffix:
        return f"{identity}（你）"
    return identity


def _message_details(message: JsonObject) -> list[JsonValue]:
    """补齐正文投影之外的回复、媒体引用和未能展开的材料，不声明读过像素。"""
    lines: list[JsonValue] = []
    images = message.get("images")
    if isinstance(images, list):
        for value in images:
            image = object_value(value)
            # 引用不是原生媒体交付；保留可定位字段和贴纸标签，不注入原始事件信封。
            references = [
                f"{label}={_label(id_text(image[key]))}"
                for key, label in (
                    ("url", "地址"),
                    ("file", "文件"),
                    ("file_id", "文件标识"),
                    ("sticker_id", "贴纸标识"),
                    ("tag", "标签"),
                )
                if key in image and id_text(image[key])
            ]
            location = "；".join(references) if references else "平台未提供可定位地址"
            lines.append(f"[图片/贴纸引用，未读取像素：{location}]")
    raw = message.get("raw_message")
    if isinstance(raw, list):
        for value in raw:
            segment = object_value(value)
            if segment.get("type") not in (
                "text",
                "at",
                "reply",
                "image",
                "mface",
                "forward",
                "node",
            ):
                # JSON/XML 卡片、文件和未知消息段不能只剩一个 [类型] 占位符。
                lines.append("[消息段原文：" + encode_json(value).decode("utf-8") + "]")
    forwarded = message.get("forward")
    if isinstance(forwarded, dict) and forwarded.get("issues"):
        lines.append("[转发材料缺口：" + encode_json(forwarded["issues"]).decode("utf-8") + "]")
    return lines


def _reactions(value: JsonValue) -> str:
    """常见回应呈现表情与数量；不认识的回应保持原值，避免猜测表情含义。"""
    if value is None:
        return "平台未提供具体回应"
    if not isinstance(value, list):
        return encode_json(value).decode("utf-8")
    descriptions: list[str] = []
    for item in value:
        reaction = object_value(item)
        identity = id_text(reaction.get("emoji_id"))
        count = reaction.get("count")
        if identity and type(count) is int and set(reaction) == {"emoji_id", "count"}:
            descriptions.append(f"表情{identity} × {count}")
        else:
            descriptions.append(encode_json(item).decode("utf-8"))
    return "；".join(descriptions) if descriptions else "平台回应列表为空"


def parse_qq_input(
    payload: JsonObject,
    message: JsonObject,
    occurred_at: datetime,
    *,
    known_messages: Sequence[JsonValue] = (),
) -> JsonObject:
    """解析 QQ 入站语义，只选择会影响注意判断和行动定位的必要字段。

    Args:
        payload: 已通过入站过滤的本次原始 QQ 事件。
        message: 完成引用关系核验和转发展开的完整消息投影。
        occurred_at: 已校验的带时区发生时间，按宿主本地时区显示 HH:MM。
        known_messages: 同账号同会话的已知消息快照，只用于补齐实际取得的群名和昵称。

    Returns:
        含 group/private 和 messages 的独立对象。事件统一保留 type、time、sender、
        text；消息原生身份只在确实存在时保留。poke 的 target 是用户，reply/reaction
        的 target 是 message:<原生消息标识>；不把回应目标伪装成事件自身的 message_id。

    Raises:
        ValueError: 发生时间没有时区，或消息投影缺少完整字符串正文。

    Note:
        只把实际 target_id 等于 self_id 的目标称为“你”。本次消息的名称优先于
        同会话缓存；没有已知名称时显示账号，不伪造用户举例中的昵称，也不发起
        额外 QQ 查询。展示结果由调用方接管后冻结，重放不受后来的缓存变化影响。
    """
    if occurred_at.utcoffset() is None:
        raise ValueError("QQ 展示时间必须带时区。")

    # 先从已取得的同会话事实确认展示名称，渲染阶段不引入平台 I/O 或新的身份映射。
    account = id_text(payload.get("self_id"))
    sender = id_text(payload.get("user_id"))
    group = id_text(payload.get("group_id"))
    name = id_text(message.get("sender_name")) or _name(sender, known_messages)
    actor = _person(sender, name, account=account)
    if group:
        group_name = id_text(payload.get("group_name"))
        if not group_name:
            for value in reversed(known_messages):
                group_name = id_text(object_value(value).get("target_name"))
                if group_name:
                    break
        label = f"{_label(group_name)}({group})" if group_name else group
        scope: JsonObject = {"group": label}
    else:
        scope = {"private": _person(sender, name, account=account)}

    stamp = occurred_at.astimezone().strftime("%H:%M")
    notice = payload.get("notice_type")
    entry: JsonObject = {}
    message_id = id_text(payload.get("message_id"))
    if payload.get("post_type") != "notice" and message_id:
        entry["message_id"] = message_id
    entry.update({"type": "message", "time": stamp, "sender": actor})
    if notice == "notify" and payload.get("sub_type") == "poke":
        target = id_text(payload.get("target_id"))
        target_name = id_text(payload.get("target_name")) or _name(target, known_messages)
        recipient = _person(target, target_name, account=account)
        entry.update(
            {
                "type": "poke",
                "target": recipient,
                "text": id_text(payload.get("action")) or "戳了戳",
            }
        )
        suffix = id_text(payload.get("suffix"))
        if suffix:
            entry["suffix"] = suffix
    elif notice == "group_msg_emoji_like":
        entry["type"] = "reaction"
        if message_id:
            entry["target"] = "message:" + message_id
        entry["text"] = _reactions(payload.get("likes"))
    else:
        body = message.get("text")
        if not isinstance(body, str):
            raise ValueError("QQ 消息投影必须保留完整字符串正文。")
        reply = id_text(message.get("reply_message_id"))
        if reply:
            entry.update({"type": "reply", "target": "message:" + reply})
            relation = message.get("reply_to_self")
            if type(relation) is bool:
                entry["reply_to_self"] = relation
        entry["text"] = body
        if message.get("mentioned_self") is True:
            entry["mentioned_self"] = True
        details = _message_details(message)
        if details:
            entry["notes"] = details
    scope["messages"] = [entry]
    return scope


def render_qq_input(
    payload: JsonObject,
    message: JsonObject,
    occurred_at: datetime,
    *,
    known_messages: Sequence[JsonValue] = (),
) -> str:
    """把提供方选定的 QQ 必要字段渲染为 YAML，供接管时冻结。

    Args:
        payload: 已通过入站过滤的本次原始 QQ 事件。
        message: 完成关系核验和转发展开的完整消息投影。
        occurred_at: 已校验的带时区发生时间。
        known_messages: 同账号同会话的已知消息快照，仅用于实际名称。

    Returns:
        含明确字段语义的 YAML 文本；所有外部字符串安全引用，不注入原始平台帧。

    Raises:
        ValueError: 时间或完整消息正文不符合 parse_qq_input 契约。
        ActionProtocolError: 选择后的材料不能表示为严格 JSON 值。

    Note:
        语义与身份约束集中在 parse_qq_input；此函数只决定展示格式，不发起 I/O。
    """
    fields = parse_qq_input(payload, message, occurred_at, known_messages=known_messages)
    return render_yaml(fields)
