"""官方互动值与严格解析；金额保留平台单位，不将用户内容提升为指令。"""

from __future__ import annotations

from dataclasses import dataclass

from purrcept_engine.values import JsonObject, JsonValue, decode_object

from .bilibili import Danmaku, ProtocolError


@dataclass(frozen=True, slots=True)
class Gift:
    """一次原生送礼事件；price为单件标价，r_price原值不猜测为订单总支出。"""

    sender: str
    uid: str
    timestamp: float
    native_id: str
    name: str
    quantity: int
    paid: bool
    price: int
    r_price: int | None


@dataclass(frozen=True, slots=True)
class SuperChat:
    """有平台留言ID及UTC有效期的醒目留言，金额单位为人民币元。"""

    sender: str
    uid: str
    timestamp: float
    native_id: str
    message_id: int
    text: str
    rmb: int
    start_time: int
    end_time: int


@dataclass(frozen=True, slots=True)
class SuperChatDelete:
    """平台按留言ID撤回；原始事件不含用户、文本或发生时间。"""

    native_id: str
    message_ids: tuple[int, ...]


LiveEvent = Danmaku | Gift | SuperChat | SuperChatDelete


def _integer(value: JsonValue, *, minimum: int = 0, maximum: int = 2**53 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError("官方互动整数无效。")
    return value


def _text(value: JsonValue, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProtocolError("官方互动文本无效。")
    return value


def parse_interaction(body: bytes, room_id: int) -> Gift | SuperChat | SuperChatDelete | None:
    """校验三类官方互动，将原生事件变为明确业务值。

    Args:
        body: 已完成有限解压的JSON事件。
        room_id: 显式授权的固定房间。

    Returns:
        礼物、SC或撤回值；其他事件返回None。

    Raises:
        ProtocolError: 已知事件字段或房间不合法，拒绝猜测金额/身份。
    """
    value = decode_object(body)
    command = value.get("cmd")
    if command not in (
        "LIVE_OPEN_PLATFORM_SEND_GIFT",
        "LIVE_OPEN_PLATFORM_SUPER_CHAT",
        "LIVE_OPEN_PLATFORM_SUPER_CHAT_DEL",
    ):
        return None
    data = value.get("data")
    if not isinstance(data, dict):
        raise ProtocolError("官方互动对象无效。")
    if _integer(data.get("room_id"), minimum=1) != room_id:
        raise ProtocolError("官方互动房间不匹配。")
    msg_id = _text(data.get("msg_id"), 192)
    if command == "LIVE_OPEN_PLATFORM_SUPER_CHAT_DEL":
        ids = data.get("message_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 1000:
            raise ProtocolError("SC撤回列表无效。")
        return SuperChatDelete(
            "open_live:sc_delete:" + msg_id,
            tuple(dict.fromkeys(_integer(item, minimum=1) for item in ids)),
        )
    sender = _text(data.get("uname"), 128)
    uid = "open_id:" + _text(data.get("open_id"), 256)
    timestamp = _integer(data.get("timestamp"), minimum=1, maximum=32_503_680_000)
    if command == "LIVE_OPEN_PLATFORM_SEND_GIFT":
        paid = data.get("paid")
        if type(paid) is not bool:
            raise ProtocolError("礼物付费标记无效。")
        actual = data.get("r_price")
        return Gift(
            sender,
            uid,
            float(timestamp),
            "open_live:gift:" + msg_id,
            _text(data.get("gift_name"), 256),
            _integer(data.get("gift_num"), minimum=1, maximum=1_000_000),
            paid,
            _integer(data.get("price"), maximum=10**12),
            None if actual is None else _integer(actual, maximum=10**12),
        )
    start = _integer(data.get("start_time"), minimum=1, maximum=32_503_680_000)
    end = _integer(data.get("end_time"), minimum=1, maximum=32_503_680_000)
    if end <= start:
        raise ProtocolError("SC有效期无效。")
    identity = _integer(data.get("message_id"), minimum=1)
    return SuperChat(
        sender,
        uid,
        float(timestamp),
        f"open_live:sc:{identity}",
        identity,
        _text(data.get("message"), 4000),
        _integer(data.get("rmb"), minimum=1, maximum=10**9),
        start,
        end,
    )


def gift_label(gift: Gift) -> str:
    """返回明确区分免费、标价与实际支付的简短展示文本。

    Args:
        gift: 已校验的礼物值。

    Returns:
        名称、数量和付费类别；付费标价以1000平台单位=1元精确转换。
    """
    category = "免费礼物"
    if gift.paid:
        yuan, fraction = divmod(gift.price, 1000)
        price = f"{yuan}.{fraction:03d}".rstrip("0").rstrip(".")
        category = f"付费礼物 · 标价¥{price}/件"
    return f"{gift.name} ×{gift.quantity} · {category}"


def interaction_data(event: Gift | SuperChat) -> JsonObject:
    """将互动投影为有类型的外部输入材料，不插入系统提示。

    Args:
        event: 已校验的礼物或SC。

    Returns:
        保留金额原始单位、外部文字及稳定身份的JSON字段。
    """
    if isinstance(event, Gift):
        return {
            "type": "gift",
            "sender": event.sender,
            "gift_name": event.name,
            "quantity": event.quantity,
            "paid": event.paid,
            "unit_list_price": event.price,
            "platform_r_price": event.r_price,
            "price_unit": "1000=1元（金瓜子）" if event.paid else "银瓜子，非人民币",
            "amount_note": "标价及平台价值字段，不推断实际支付总额。",
        }
    return {
        "type": "super_chat",
        "sender": event.sender,
        "text": event.text,
        "message_id": event.message_id,
        "rmb": event.rmb,
        "start_time": event.start_time,
        "end_time": event.end_time,
    }
