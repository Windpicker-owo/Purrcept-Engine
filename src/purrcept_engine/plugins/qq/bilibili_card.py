"""为 QQ 分享准备 B 站视频链接、公开元数据和已签名的 OneBot 卡片段。

链接校验约束短链跳转，视频资料只从固定公开 API 读取。这里不持有 QQ 连接、
凭据或发送权限，调用方负责签名请求、截止时间及发送回执。
"""

from __future__ import annotations

import re

import httpx

from purrcept_engine.domain.bilibili import BVID_PATTERN, VIDEO_HOSTS
from purrcept_engine.plugin_system import CapabilityInputError
from purrcept_engine.plugins.qq.messages import object_value
from purrcept_engine.values import (
    ActionProtocolError,
    JsonObject,
    JsonValue,
    decode_object,
    encode_json,
)

_SHORT_HOSTS = frozenset({"b23.tv", "bili2233.cn"})
_VIDEO_PATH = re.compile(rf"/video/({BVID_PATTERN}|av[1-9][0-9]{{0,18}})/?")
_VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
_VIEW_BYTES = 512 * 1024


class BilibiliCardError(ValueError):
    """当前视频无法组装原生分享卡片，仍可使用已校验链接。"""


def video_link(value: str) -> tuple[httpx.URL, str | None]:
    """接受 BV 号、视频长链接或官方短链，保留分 P 和播放时点。"""
    value = value.strip()
    if re.fullmatch(BVID_PATTERN, value):
        return httpx.URL(f"https://www.bilibili.com/video/{value}/"), value
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as error:
        raise CapabilityInputError("B 站视频链接无效。") from error
    if url.scheme in {"https", "http"} and not url.userinfo and url.port is None:
        if url.host in VIDEO_HOSTS and (match := _VIDEO_PATH.fullmatch(url.path)):
            params = [(key, url.params[key]) for key in ("p", "t") if key in url.params]
            return httpx.URL(f"https://www.bilibili.com/video/{match[1]}/", params=params), match[1]
        if url.host in _SHORT_HOSTS and re.fullmatch(r"/[A-Za-z0-9]{1,32}/?", url.path):
            return url.copy_with(scheme="https", query=None, fragment=None), None
    raise CapabilityInputError("请提供 BV 号、B 站视频长链接或官方短链。")


async def resolve_short_link(client: httpx.AsyncClient, url: httpx.URL) -> tuple[httpx.URL, str]:
    """只请求官方短链，每跳先校验；遇到视频长链接后停止发起跳转请求。"""
    for _ in range(3):
        async with client.stream("GET", url) as response:
            location = response.headers.get("location")
            if not response.is_redirect or not location:
                raise BilibiliCardError("B 站短链未返回视频跳转。")
        try:
            destination = url.join(location)
        except httpx.InvalidURL as error:
            raise CapabilityInputError("B 站短链返回了无效跳转。") from error
        url, video_id = video_link(str(destination))
        if video_id is not None:
            return url, video_id
    raise BilibiliCardError("B 站短链跳转超过三次。")


async def card_parameters(client: httpx.AsyncClient, url: httpx.URL, video_id: str) -> JsonObject:
    """读取同一稿件的公开标题和封面，构造 NapCat 的 B 站签名模板参数。"""
    params = {"bvid": video_id} if video_id.startswith("BV") else {"aid": video_id[2:]}
    body = bytearray()
    async with client.stream("GET", _VIEW_URL, params=params) as response:
        if response.status_code != 200:
            raise BilibiliCardError(f"B 站视频资料请求返回 HTTP {response.status_code}。")
        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
            if len(body) + len(chunk) > _VIEW_BYTES:
                raise BilibiliCardError("B 站视频资料超过读取上限。")
            body.extend(chunk)
    try:
        payload = decode_object(bytes(body))
    except ActionProtocolError as error:
        raise BilibiliCardError("B 站视频资料格式无效。") from error
    if type(payload.get("code")) is not int or payload["code"] != 0:
        raise BilibiliCardError("B 站未提供可用的视频资料。")
    data = object_value(payload.get("data"))
    if ("bvid" in params and data.get("bvid") != video_id) or (
        "aid" in params and (type(data.get("aid")) is not int or str(data["aid"]) != params["aid"])
    ):
        raise BilibiliCardError("B 站返回的视频身份与请求不一致。")
    title, picture = data.get("title"), data.get("pic")
    if not isinstance(title, str) or not title.strip() or not isinstance(picture, str):
        raise BilibiliCardError("B 站视频资料缺少标题或封面。")
    try:
        cover = httpx.URL("https:" + picture if picture.startswith("//") else picture)
    except httpx.InvalidURL as error:
        raise BilibiliCardError("B 站视频封面地址无效。") from error
    if (
        cover.scheme not in {"https", "http"}
        or cover.userinfo
        or cover.port is not None
        or not (cover.host == "hdslb.com" or cover.host.endswith(".hdslb.com"))
    ):
        raise BilibiliCardError("B 站视频封面地址不在图片 CDN 范围内。")
    description = data.get("desc")
    return {
        "type": "bili",
        "title": " ".join(title.split())[:120],
        "desc": " ".join(description.split())[:160] if isinstance(description, str) else "",
        "picUrl": str(cover.copy_with(scheme="https")),
        "jumpUrl": str(url),
        "webUrl": str(url),
    }


def signed_message(data: JsonValue) -> list[JsonValue]:
    """封装 NapCat 返回的签名内容，兼容结构化 Ark 和文档中的 Ark 字符串。"""
    mapping = object_value(data)
    nested = mapping.get("data", data)
    ark = object_value(nested).get("ark", nested)
    try:
        if isinstance(ark, str):
            content = decode_object(ark.encode("utf-8"))
        else:
            content = object_value(ark)
            ark = encode_json(content).decode("utf-8")
    except (ActionProtocolError, UnicodeError) as error:
        raise BilibiliCardError("NapCat 返回了无法读取的签名卡片。") from error
    if not isinstance(content.get("app"), str) or not content["app"]:
        raise BilibiliCardError("NapCat 未返回可发送的签名卡片。")
    return [{"type": "json", "data": {"data": ark}}]
