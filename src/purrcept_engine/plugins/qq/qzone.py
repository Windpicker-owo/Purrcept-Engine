"""SnowLuma 空间响应的用途明确短卡片，原响应保存由 QQService 负责。

动作与字段依据旧可用路径及 https://snowluma.github.io/zh/docs/api/qzone/。
这里不兼容探测 NapCat 别名；缺失、非预期条目保留位置和原因。短卡片不是
完整原文，图片 URL 也不代表像素已取得；实际部署版本和取消赞效果仍须实机验收。
"""

import re
from html import unescape

from purrcept_engine.plugins.qq.messages import id_text, object_value
from purrcept_engine.values import JsonObject, JsonValue


def cards(data: JsonValue, *, feeds: bool) -> JsonObject:
    """只投影文档实际 msglist/feeds 字段，不因旧别名猜测全页已解析。"""
    mapping = object_value(data)
    field = "feeds" if feeds else "msglist"
    items = mapping.get(field)
    result: list[JsonValue] = []
    issues: list[JsonValue] = []
    if not isinstance(items, list):
        issues.append({"position": None, "reason": "响应缺少实际 " + field + " 数组"})
        items = []
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            issues.append({"position": position, "reason": "条目不是 JSON 对象"})
            result.append({"position": position, "unavailable": True})
            continue
        tid = id_text(item.get("key" if feeds else "tid"))
        if not tid:
            issues.append({"position": position, "reason": "条目缺少 key/tid"})
        raw_text = item.get("html" if feeds else "content")
        text = raw_text if isinstance(raw_text, str) else ""
        if feeds:
            text = unescape(re.sub(r"<[^>]*>", " ", text))
        preview = " ".join(text.split())
        shortened = len(preview) > 240
        preview = preview[:239] + "…" if shortened else preview
        card: JsonObject = {
            "position": position,
            "tid": tid or None,
            "text": preview,
            "level": "overview",
            "text_complete": not shortened and not feeds,
            "time": item.get("time"),
            "abstime": item.get("time"),
            "author": {"uin": id_text(item.get("uin")) or None, "name": item.get("nickname")},
            "images": item.get("images", []),
            "pixels_acquired": False,
        }
        if feeds:
            card["key"] = item.get("key")
            card["appid"] = item.get("appid")
            links = re.findall(
                r'<img[^>]+(?:src|data-src)=["\']([^"\']+)["\']',
                text if not isinstance(raw_text, str) else raw_text,
                re.IGNORECASE,
            )
            card["images"] = list(links)
        else:
            card["comment_count"] = item.get("comment_num")
            card["is_private"] = item.get("is_private")
        result.append(card)
    return {
        "feeds" if feeds else "messages": result,
        "total": mapping.get("total") if type(mapping.get("total")) is int else None,
        "has_more": mapping.get("has_more") if type(mapping.get("has_more")) is bool else None,
        "issues": issues,
        "coverage": {
            "acquired_page_only": True,
            "remote_complete": None,
            "projected_complete": not issues,
            "deep_feed_pages_supported": False,
        },
    }


def write_result(name: str, data: JsonValue, request: JsonObject) -> JsonObject:
    """分离 requested 与 actual；成功信封不补成平台未返回的 tid/right/id。"""
    mapping = object_value(data)
    result: JsonObject = {
        "requested": request,
        "platform_reported_success": True,
        "actual": mapping,
        "unknown_fields": [],
    }
    unknown: list[JsonValue] = []
    if name == "qzone_post":
        result["tid"] = id_text(mapping.get("tid")) or None
        result["ugc_right"] = mapping.get("ugc_right")
        result["text"] = request.get("content")
        images = request.get("images")
        result["image_count"] = len(images) if isinstance(images, list) else 0
        unknown.extend(key for key in ("tid", "ugc_right") if mapping.get(key) is None)
    elif name == "qzone_comment":
        result["tid"] = request.get("tid")
        result["text"] = request.get("content")
        result["target_uin"] = id_text(request.get("target_uin"))
        result["comment_id"] = id_text(mapping.get("comment_id", mapping.get("cid"))) or None
        images = request.get("images")
        result["image_count"] = len(images) if isinstance(images, list) else 0
        if result["comment_id"] is None:
            unknown.append("comment_id")
    elif name == "qzone_set_right":
        result["tid"] = request.get("tid")
        result["ugc_right"] = mapping.get("ugc_right")
        result["target_uins"] = mapping.get("target_uins")
        unknown.extend(key for key in ("ugc_right", "target_uins") if mapping.get(key) is None)
    elif name in {"qzone_like", "qzone_unlike"}:
        result.update(
            {
                "tid": request.get("tid"),
                "target_uin": id_text(request.get("target_uin")),
                "abstime": request.get("abstime", 0),
                "liked": name == "qzone_like",
                "confirmation_source": "platform_acknowledgement",
            }
        )
    elif name == "qzone_delete":
        result.update(
            {
                "tid": request.get("tid"),
                "deleted": True,
                "confirmation_source": "platform_acknowledgement",
            }
        )
    elif name == "qzone_ban":
        result.update(
            {
                "user_id": id_text(request.get("user_id")),
                "enable": request.get("enable"),
                "banned": request.get("enable"),
                "confirmation_source": "platform_acknowledgement",
            }
        )
    result["unknown_fields"] = unknown
    return result
