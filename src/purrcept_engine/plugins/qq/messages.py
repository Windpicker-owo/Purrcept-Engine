"""完整消息原文的有限投影与会话缓存，不取得宿主权威存储权限。

过滤在媒体读取和正式接管前执行，只检查推送本身的来源；引用和转发保持原文。
缓存限制会话数量及总字节，只为预期历史读取故障提供明确标记的已有范围。
消息正文与原始 OneBot 段均保留，图片链接不代表已经取得像素。
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field

from purrcept_engine.execution.protocol import (
    ActionProtocolError,
    JsonObject,
    JsonValue,
    decode_object,
    encode_json,
)
from purrcept_engine.storage.models import InputSubmission

from .settings import QQSettings


def id_text(value: JsonValue) -> str:
    """原生整数/字符串身份保留符号；bool 不能充当 QQ 号或消息身份。"""
    if isinstance(value, str):
        return value
    if type(value) is int:
        return str(value)
    return ""


def object_value(value: JsonValue) -> JsonObject:
    """仅把实际 JSON 对象视为字段集合，不从字符串或序列猜对象。"""
    return value if isinstance(value, dict) else {}


def message_text(value: JsonValue) -> str:
    """保留完整文字和非文字段的可辨识位置，不裁剪正文或声明已看媒体。"""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for segment in value:
        node = object_value(segment)
        data = object_value(node.get("data"))
        kind = node.get("type")
        if kind == "text":
            text = data.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif kind == "at":
            parts.append("@" + id_text(data.get("qq")))
        elif kind in ("forward", "node"):
            content = data.get("content", data.get("message"))
            if isinstance(content, (str, list)):
                parts.append("[转发节点]" + message_text(content))
            else:
                parts.append("[" + kind + "]")
        elif isinstance(kind, str):
            parts.append("[" + kind + "]")
        else:
            parts.append("[无法投影的消息段]")
    return "".join(parts)


def image_references(value: JsonValue) -> list[JsonValue]:
    """返回实际图片段原字段，既不下载也不补造 URL/id。"""
    result: list[JsonValue] = []
    if isinstance(value, list):
        for segment in value:
            node = object_value(segment)
            if node.get("type") in ("image", "mface"):
                result.append(
                    {
                        **object_value(node.get("data")),
                        "kind": node["type"],
                        "pixels_acquired": False,
                    }
                )
    return result


def filter_reason(payload: JsonObject, settings: QQSettings) -> str | None:
    """只过滤自动推送的会话/直接发送者，不能递归屏蔽引用或限制能力调用。"""
    group = id_text(payload.get("group_id"))
    sender = id_text(payload.get("user_id"))
    if sender in settings.ban_user_id:
        return "ignored_sender"
    if group:
        excluded = group in settings.group_list
        allowed = excluded if settings.group_list_type == "whitelist" else not excluded
        return None if allowed else "group_out_of_scope"
    excluded = sender in settings.private_list
    allowed = excluded if settings.private_list_type == "whitelist" else not excluded
    return None if allowed else "private_out_of_scope"


def project_message(payload: JsonObject, *, chat: str, target_id: str) -> JsonObject:
    """保留旧公开字段及实际原段；欠缺平台字段为空值并列出覆盖未知。"""
    sender = object_value(payload.get("sender"))
    raw = payload.get("message", payload.get("raw_message"))
    self_id = id_text(payload.get("self_id"))
    mentioned: bool | None = None if isinstance(raw, str) or not self_id else False
    reply_id = ""
    if isinstance(raw, list):
        for segment in raw:
            node = object_value(segment)
            data = object_value(node.get("data"))
            if node.get("type") == "at" and self_id and id_text(data.get("qq")) == self_id:
                mentioned = True
            if node.get("type") == "reply":
                reply_id = id_text(data.get("id"))
    sender_name = sender.get("card") or sender.get("nickname")
    return {
        "chat": chat,
        "target_id": target_id,
        "target_name": payload.get("group_name") if chat == "group" else sender.get("nickname"),
        "sender_id": id_text(payload.get("user_id", sender.get("user_id"))),
        "sender_name": sender_name if isinstance(sender_name, str) else "",
        "message_id": id_text(payload.get("message_id")),
        "text": message_text(raw),
        "self_id": self_id,
        "time": payload.get("time"),
        "images": image_references(raw),
        "mentioned_self": mentioned,
        "reply_message_id": reply_id,
        "reply_to_self": None if reply_id or mentioned is None else False,
        "authored_text": "".join(
            str(object_value(object_value(part).get("data")).get("text", ""))
            for part in raw
            if object_value(part).get("type") == "text"
        )
        if isinstance(raw, list)
        else raw
        if isinstance(raw, str)
        else "",
        "raw_message": raw,
    }


def forward_ids(value: JsonValue) -> tuple[str, ...]:
    """保留 Runtime 的 forward/JSON/XML resid 识别，不把普通链接当作转发。"""
    if not isinstance(value, list):
        return ()
    found: list[str] = []
    for segment in value:
        node = object_value(segment)
        data = object_value(node.get("data"))
        kind = node.get("type")
        if kind == "forward":
            identity = id_text(data.get("id", data.get("resid")))
            if identity:
                found.append(identity)
        elif kind in ("json", "lightapp", "card"):
            raw = data.get("data")
            if not isinstance(raw, str):
                continue
            try:
                card = decode_object(raw.encode())
            except ActionProtocolError:
                continue  # 原卡片仍保存；无法识别并不授权猜测转发身份。
            marker = id_text(card.get("app")) + " " + id_text(card.get("view"))
            if not any(
                name in marker.lower()
                for name in ("multimsg", "multi_forward", "forwardmsg", "longmsg")
            ):
                continue
            pending: list[JsonValue] = [card]
            while pending:
                item = pending.pop()
                if isinstance(item, dict):
                    for key, child in item.items():
                        if key in {"resid", "resId", "resid_", "res_id"} and id_text(child):
                            found.append(id_text(child))
                        elif isinstance(child, (dict, list)):
                            pending.append(child)
                elif isinstance(item, list):
                    pending.extend(item)
        elif kind == "xml":
            raw = data.get("data")
            if isinstance(raw, str) and re.search(r'service(?:ID|id)=["\'](?:33|35)["\']', raw):
                match = re.search(r'resid=["\']([^"\']+)["\']', raw)
                if match:
                    found.append(match.group(1))
    return tuple(dict.fromkeys(found))


def directory(data: JsonValue, *, kind: str) -> JsonObject:
    """保留好友/群/成员公开字段及字符串身份，异常条目按实际位置报告。"""
    if not isinstance(data, list):
        return {kind: None, "issues": [{"position": None, "reason": "平台未提供实际列表"}]}
    rows: list[JsonValue] = []
    issues: list[JsonValue] = []
    identity = "group_id" if kind == "groups" else "user_id"
    fields = (
        ("group_name", "member_count")
        if kind == "groups"
        else ("nickname", "remark")
        if kind == "friends"
        else ("nickname", "card", "role")
    )
    for position, item in enumerate(data):
        if not isinstance(item, dict) or not id_text(item.get(identity)):
            issues.append({"position": position, "reason": "条目缺少实际对象或身份"})
            rows.append({"position": position, "unavailable": True})
            continue
        row: JsonObject = {identity: id_text(item.get(identity))}
        row.update({field: item.get(field) for field in fields})
        rows.append(row)
    return {kind: rows, "issues": issues}


@dataclass(slots=True)
class _Conversation:
    messages: list[JsonObject]
    size: int
    submissions: dict[str, InputSubmission] = field(
        default_factory=lambda: dict[str, InputSubmission]()
    )


class HistoryCache:
    """一个 QQ 实例拥有的有限缓存，按账号和会话区分，不伪造平台覆盖。

    只在已确认取得/接管消息后 remember；超界丢弃缓存并报告范围未知，不删除
    Store 已接管经历。新账号不会借用旧账号材料，未确认账号不跨连接代次使用。
    """

    def __init__(self, settings: QQSettings) -> None:
        """只配置内存额度，不读取磁盘或构造额外权威数据库。"""
        self._settings = settings
        self._conversations: OrderedDict[tuple[str, str, str], _Conversation] = OrderedDict()
        self._bytes = 0

    def remember(
        self, scope: str, message: JsonObject, *, submission: InputSubmission | None = None
    ) -> None:
        """同一消息身份替换投影；已接管信封也计入本缓存额度并随消息淘汰。

        信封只用于重放相同原事件，防止后来本地标签或补取响应变化改变该事实；
        它不是独立权威去重库，也不能凭缓存存在推断宿主新事务成功。
        """
        chat, target = message.get("chat"), message.get("target_id")
        if not isinstance(chat, str) or not isinstance(target, str):
            raise ValueError("QQ 缓存消息缺少实际会话。")
        key = (scope, chat, target)
        conversation = self._conversations.pop(key, _Conversation([], 0))
        self._bytes -= conversation.size
        message_id = message.get("message_id")
        if message_id:
            conversation.messages = [
                item for item in conversation.messages if item.get("message_id") != message_id
            ]
        conversation.messages.append(message)
        conversation.messages = conversation.messages[-100:]
        if submission is not None and submission.source.native_event_id is not None:
            conversation.submissions[submission.source.native_event_id] = submission
        retained = {id_text(item.get("message_id")) for item in conversation.messages}
        conversation.submissions = {
            identity: item
            for identity, item in conversation.submissions.items()
            if identity in retained
        }
        conversation.size = len(encode_json(conversation.messages)) + sum(
            len(item.text.encode())
            + len(item.context.details_json.encode())
            + sum(len(asset.content) for asset in item.media)
            for item in conversation.submissions.values()
        )
        if conversation.size <= self._settings.cache_bytes:
            self._conversations[key] = conversation
            self._bytes += conversation.size
        while self._conversations and (
            len(self._conversations) > self._settings.cache_conversations
            or self._bytes > self._settings.cache_bytes
        ):
            _, removed = self._conversations.popitem(last=False)
            self._bytes -= removed.size

    def read(self, scope: str, chat: str, target_id: str, count: int) -> list[JsonValue]:
        """只返回当前已知范围，缺缓存由调用者报告故障而不是空历史。"""
        value = self._conversations.get((scope, chat, target_id))
        return [] if value is None else list(value.messages[-count:])

    def find(self, scope: str, chat: str, target_id: str, message_id: str) -> JsonObject | None:
        """回复关系只在同账号同会话的已取得消息中核验，不跨会话猜发送者。"""
        value = self._conversations.get((scope, chat, target_id))
        if value is None:
            return None
        return next((item for item in value.messages if item.get("message_id") == message_id), None)

    def submission(
        self, scope: str, chat: str, target_id: str, message_id: str
    ) -> InputSubmission | None:
        """仅返回仍有缓存额度的实际已接管信封，重放仍需走宿主原子提交。"""
        conversation = self._conversations.get((scope, chat, target_id))
        return None if conversation is None else conversation.submissions.get(message_id)
