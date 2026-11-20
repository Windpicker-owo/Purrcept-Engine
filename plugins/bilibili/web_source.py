"""通过哔哩哔哩网页接口只读访问推荐、搜索、评论与分 P 素材。

WebSource 把远端分页和 B 站字段翻译成插件的只读契约。请求不自动重试，
站点限制和登录要求按原状态报告。Cookie 只发送给 API，视频和字幕从独立的
受限媒体请求读取，始终用请求的 BV/CID 定位，分镜制作交给 storyboard。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from secrets import token_hex
from typing import BinaryIO
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from purrcept_engine.values import JsonObject, JsonValue, decode_object

from .source import (
    BVID_PATTERN,
    Comment,
    CommentOrder,
    FeedKind,
    Page,
    SourceError,
    Transcript,
    TranscriptLine,
    Video,
    VideoPart,
    VideoSummary,
)
from .transcribe import transcribe

_API = "https://api.bilibili.com"
_HOME = "https://www.bilibili.com/"
_MEDIA_DOMAINS = ("hdslb.com", "bilivideo.com", "bilivideo.cn", "bilivideo.net")
_WEB_HEADERS = {"Referer": _HOME, "User-Agent": "Mozilla/5.0 (compatible; Purrcept-Engine)"}
_JSON_LIMIT = 8 * 1024 * 1024
_VIDEO_LIMIT = 64 * 1024 * 1024
_AUDIO_LIMIT = 24 * 1024 * 1024

# WBI 的参数签名使用 nav 接口给出的两个键和固定的重排表。
_WBI_ORDER = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
)
_WBI_FILTER = str.maketrans("", "", "!'()*")


class WebSettings(BaseModel):
    """实时数据源的可选 HTTP 代理和操作者提供的 B 站登录 Cookie。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    proxy: str | None = Field(default=None, pattern=r"^(https?://[^\s]+)?$")
    cookie: SecretStr = SecretStr("")
    asr_model: str | None = Field(default=None, pattern=r"\S")

    @field_validator("cookie")
    @classmethod
    def _header_value(cls, value: SecretStr) -> SecretStr:
        """Cookie 是 ASCII 请求头的一行，不允许注入其他请求头。"""
        if any(not 32 <= ord(character) < 127 for character in value.get_secret_value()):
            raise ValueError("B 站 Cookie 必须是一行可打印 ASCII。")
        return value


def _object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        raise SourceError("invalid_response", "B 站返回的对象结构与预期不符。")
    return value


def _items(value: JsonValue) -> list[JsonValue]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SourceError("invalid_response", "B 站返回的列表结构与预期不符。")
    return value


def _required(record: JsonObject, field: str) -> JsonValue:
    """远端明确返回空列表时允许空页，漏掉必需字段则报告接口异常。"""
    if field not in record:
        raise SourceError("invalid_response", f"B 站响应缺少字段 {field}。")
    return record[field]


def _text(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""


def _int(value: JsonValue) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value):
        return int(value)
    return 0


def _duration(value: JsonValue) -> float | None:
    """搜索接口用 MM:SS 或 HH:MM:SS，首页和热门用秒。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,8}(?::[0-9]{1,2}){0,2}", value):
        total = 0
        for component in value.split(":"):
            total = total * 60 + int(component)
        return float(total)
    return None


def _summary(value: JsonObject) -> VideoSummary | None:
    """跳过直播或广告，清除搜索高亮标记并保留真正的稿件标识。"""
    bvid = _text(value.get("bvid"))
    if not re.fullmatch(BVID_PATTERN, bvid):
        return None
    title = unescape(re.sub(r"</?em(?:\s[^<>]*)?>", "", _text(value.get("title")))).strip()
    if not title:
        raise SourceError("invalid_response", "B 站稿件卡片缺少标题。")
    owner = value.get("owner")
    uploader = _text(owner.get("name")) if isinstance(owner, dict) else _text(value.get("author"))
    return VideoSummary(
        bvid=bvid,
        title=title[:200],
        uploader=uploader[:100],
        duration=_duration(value.get("duration")),
    )


def _cdn_url(value: str) -> str:
    """仅请求播放器给出的 HTTPS CDN，拒绝向其他主机发送媒体请求。"""
    try:
        url = httpx.URL("https:" + value if value.startswith("//") else value)
    except httpx.InvalidURL as error:
        raise SourceError("invalid_media_url", "B 站返回了无效的媒体地址。") from error
    if (
        url.scheme != "https"
        or url.port not in (None, 443)
        or url.username
        or url.password
        or not any(
            url.host == domain or url.host.endswith("." + domain) for domain in _MEDIA_DOMAINS
        )
    ):
        raise SourceError("invalid_media_url", "B 站返回的媒体地址不属于允许的 CDN。")
    return str(url)


def _media_url(tracks: list[JsonObject]) -> str:
    """按轨道优先级选择播放器提供的首个符合白名单的 CDN 地址。"""
    for track in tracks:
        candidates = [track.get(key) for key in ("baseUrl", "base_url", "url")]
        for key in ("backupUrl", "backup_url"):
            backups = track.get(key)
            if isinstance(backups, list):
                candidates.extend(backups)
        # 部分稿件的主地址是第三方 PCDN。先在本地筛选同一轨道的备用地址，
        # 避免访问不可信主机，也避免因主地址被拒绝而丢掉整个轨道。
        for candidate in candidates:
            if isinstance(candidate, str) and candidate:
                try:
                    return _cdn_url(candidate)
                except SourceError:
                    continue
    raise SourceError("invalid_media_url", "B 站主地址和备用地址中没有允许的 HTTPS CDN。")


def _comment(raw: JsonObject, root: str | None, *, pinned: bool = False) -> Comment:
    """保留大整数评论 ID，楼中楼通过服务端 root 核对其所属评论。"""
    identifier = _text(raw.get("rpid_str")) or str(_int(raw.get("rpid")))
    if root is not None and (received := _int(raw.get("root"))) and str(received) != root:
        raise SourceError("wrong_comment_thread", "B 站返回了另一条根评论的回复。")
    try:
        return Comment(
            rpid=identifier,
            root_rpid=root,
            pinned=pinned,
            author=_text(_object(raw.get("member") or {}).get("uname"))[:100],
            text=_text(_object(raw.get("content") or {}).get("message"))[:4000],
            likes=_int(raw.get("like")),
            published_at=_int(raw.get("ctime")),
            reply_count=_int(raw.get("rcount")) if root is None else None,
        )
    except ValidationError as error:
        raise SourceError("invalid_response", "B 站评论包含无效的字段。") from error


@dataclass(frozen=True, slots=True)
class _Continuation[Item]:
    scope: tuple[str, ...]
    upstream: str | None
    pending: tuple[Item, ...]


class _Pagination[Item]:
    """暂存上游多返回的项目，保证较小 limit 不会吞掉同一页的剩余内容。"""

    def __init__(self) -> None:
        self._states: OrderedDict[str, _Continuation[Item]] = OrderedDict()

    async def read(
        self,
        scope: tuple[str, ...],
        cursor: str | None,
        limit: int,
        fetch: Callable[[str | None], Awaitable[Page[Item]]],
    ) -> Page[Item]:
        """将本地游标与来源、关键词或评论线程绑定。"""
        if cursor is None:
            page = await fetch(None)
        else:
            state = self._states.get(cursor)
            if state is None or state.scope != scope:
                raise SourceError("invalid_cursor", "游标无效或已过期，请从第一页重新读取。")
            self._states.move_to_end(cursor)
            page = (
                Page(state.pending, state.upstream)
                if state.pending
                else await fetch(state.upstream)
            )
        pending = page.items[limit:]
        following = None
        if pending or page.next_cursor is not None:
            following = token_hex(16)
            self._states[following] = _Continuation(scope, page.next_cursor, pending)
            if len(self._states) > 64:
                self._states.popitem(last=False)
        return Page(page.items[:limit], following)


class WebSource:
    """由插件在宿主启动时创建的 B 站网页只读适配器。"""

    def __init__(self, settings: WebSettings, *, base_dir: Path | None = None) -> None:
        """配置单个 HTTP 客户端，API Cookie 在每个同源调用处显式附加。"""
        self._cookie = settings.cookie.get_secret_value()
        self._asr_model = (
            ((base_dir or Path.cwd()) / settings.asr_model).resolve()
            if settings.asr_model is not None
            else None
        )
        self._client = httpx.AsyncClient(
            headers=_WEB_HEADERS,
            trust_env=False,
            follow_redirects=False,
            proxy=settings.proxy or None,
            timeout=10.0,
        )
        self._wbi: tuple[str, float] | None = None
        self._videos: OrderedDict[str, tuple[Video, int]] = OrderedDict()
        self._video_pages = _Pagination[VideoSummary]()
        self._comment_pages = _Pagination[Comment]()

    async def close(self) -> None:
        """归还客户端连接。"""
        await self._client.aclose()

    async def _download(
        self,
        url: str,
        limit: int,
        *,
        params: dict[str, str] | None = None,
        api: bool = False,
        referer: str = _HOME,
        seconds: float = 12.0,
    ) -> io.BytesIO:
        """有界流读取，平台拒绝时直接返回错误且不发重试请求。"""
        headers = {"Referer": referer}
        if api and self._cookie:
            headers["Cookie"] = self._cookie
        # 手动构造 CDN 请求，避免把 API Set-Cookie 临时存入的会话带到媒体主机。
        request = (
            self._client.build_request("GET", url, params=params, headers=headers)
            if api
            else httpx.Request("GET", url, headers={**_WEB_HEADERS, **headers})
        )
        content = io.BytesIO()
        try:
            async with asyncio.timeout(seconds):
                reply = await self._client.send(request, stream=True)
                try:
                    if not reply.is_success:
                        auth = reply.status_code in (401, 403)
                        blocked = reply.status_code in (412, 429)
                        code = "blocked" if blocked else "access_denied" if auth else "http_error"
                        raise SourceError(code, f"B 站请求返回 HTTP {reply.status_code}。")
                    async for chunk in reply.aiter_bytes(chunk_size=128 * 1024):
                        if content.tell() + len(chunk) > limit:
                            raise SourceError("response_too_large", "B 站响应超过单次读取上限。")
                        content.write(chunk)
                finally:
                    await reply.aclose()
        except (httpx.HTTPError, TimeoutError) as error:
            content.close()
            # 媒体地址的查询串可能是临时凭据，报告类型而非远端全地址。
            raise SourceError(
                "network_error", f"B 站网络请求失败（{type(error).__name__}）。"
            ) from error
        except BaseException:
            content.close()
            raise
        content.seek(0)
        return content

    async def _json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        api: bool = False,
        referer: str = _HOME,
    ) -> JsonObject:
        """校验 JSON 外壳，避免把验证页或静默拒绝解释为空数据。"""
        with await self._download(
            url, _JSON_LIMIT, params=params, api=api, referer=referer
        ) as body:
            try:
                return decode_object(body.getvalue())
            except ValueError as error:
                raise SourceError("invalid_response", "B 站未返回可读取的 JSON。") from error

    async def _api(
        self,
        endpoint: str,
        params: dict[str, str] | None = None,
        *,
        signed: bool = False,
    ) -> JsonValue:
        """使用 B 站网页请求的 WBI 签名并展开响应 data。"""
        if signed:
            key = await self._wbi_key()
            query = {**(params or {}), "wts": str(int(time.time()))}
            params = {name: value.translate(_WBI_FILTER) for name, value in sorted(query.items())}
            params["w_rid"] = hashlib.md5(
                (urlencode(params) + key).encode(), usedforsecurity=False
            ).hexdigest()
        body = await self._json(_API + endpoint, api=True, params=params)
        code = body.get("code")
        if type(code) is not int:
            raise SourceError("invalid_response", "B 站未提供响应状态。")
        if code != 0:
            message = _text(body.get("message"))[:160]
            reason = {
                -101: "login_required",
                -403: "access_denied",
                -404: "not_found",
                -352: "blocked",
                -412: "blocked",
                -509: "blocked",
            }.get(code, "api_error")
            raise SourceError(reason, f"B 站返回 {code}：{message}")
        if body.get("data") is None:
            raise SourceError("unavailable", "B 站请求成功但没有提供数据。")
        return body["data"]

    async def _wbi_key(self) -> str:
        """短期复用 nav 的公开签名键，发送限制或失效由接口原样报告。"""
        now = time.monotonic()
        if self._wbi is not None and now < self._wbi[1]:
            return self._wbi[0]
        # 匿名 nav 用 -101 报告账号状态，仍可随 data 附带公开签名键。
        nav = await self._json(_API + "/x/web-interface/nav", api=True)
        if nav.get("code") not in (0, -101):
            code = nav.get("code")
            reason = "blocked" if code in (-352, -412, -509) else "api_error"
            raise SourceError(reason, f"B 站导航接口返回 {code}。")
        data = _object(nav.get("data"))
        keys = _object(data.get("wbi_img"))
        joined = "".join(
            _text(keys.get(field)).rsplit("/", 1)[-1].partition(".")[0]
            for field in ("img_url", "sub_url")
        )
        if re.fullmatch(r"[0-9a-fA-F]{64}", joined) is None:
            raise SourceError("invalid_response", "B 站未提供完整的 WBI 签名键。")
        mixed = "".join(joined[index] for index in _WBI_ORDER)
        self._wbi = (mixed, now + 600)
        return mixed

    async def feed(
        self,
        kind: FeedKind,
        bvid: str | None,
        cursor: str | None,
        limit: int,
    ) -> Page[VideoSummary]:
        """按显式来源读取推荐，首页翻页保持 B 站返回的顺序。"""

        async def fetch(upstream: str | None) -> Page[VideoSummary]:
            page = int(upstream or "1")
            if kind == "related":
                raw = await self._api("/x/web-interface/archive/related", {"bvid": bvid or ""})
                rows, more = _items(raw), False
            elif kind == "hot":
                data = _object(
                    await self._api("/x/web-interface/popular", {"pn": str(page), "ps": str(limit)})
                )
                rows, more = _items(_required(data, "list")), data.get("no_more") is not True
            else:
                data = _object(
                    await self._api(
                        "/x/web-interface/wbi/index/top/feed/rcmd",
                        {
                            "fresh_idx": str(page),
                            "ps": str(limit),
                            "fresh_type": "4",
                            "feed_version": "V8",
                        },
                        signed=True,
                    )
                )
                rows, more = _items(_required(data, "item")), True
            videos: dict[str, VideoSummary] = {}
            for raw in rows:
                row = _object(raw)
                if kind == "recommend" and row.get("goto") not in (None, "av"):
                    continue
                if item := _summary(row):
                    videos.setdefault(item.bvid, item)
            return Page(tuple(videos.values()), str(page + 1) if more and rows else None)

        return await self._video_pages.read(("feed", kind, bvid or ""), cursor, limit, fetch)

    async def search(self, query: str, cursor: str | None, limit: int) -> Page[VideoSummary]:
        """从 B 站视频搜索分页读取实际匹配项。"""

        async def fetch(upstream: str | None) -> Page[VideoSummary]:
            page = int(upstream or "1")
            data = _object(
                await self._api(
                    "/x/web-interface/wbi/search/type",
                    {"search_type": "video", "keyword": query, "page": str(page)},
                    signed=True,
                )
            )
            rows = _items(_required(data, "result"))
            results = tuple(item for raw in rows if (item := _summary(_object(raw))) is not None)
            return Page(results, str(page + 1) if page < _int(data.get("numPages")) else None)

        return await self._video_pages.read(("search", query), cursor, limit, fetch)

    async def video(self, bvid: str) -> Video:
        """读取稿件详情和真实分 P，同时缓存媒体和评论请求需要的 AID。"""
        data = _object(await self._api("/x/web-interface/view", {"bvid": bvid}))
        if data.get("bvid") != bvid:
            raise SourceError("wrong_video", "B 站返回的 BV 号与请求不一致。")
        card = _summary(data)
        aid = _int(data.get("aid"))
        if card is None or not aid:
            raise SourceError("invalid_response", "B 站稿件缺少稿件或分 P 身份。")
        try:
            parts: list[VideoPart] = []
            for item in _items(data.get("pages")):
                row = _object(item)
                parts.append(
                    VideoPart.model_validate(
                        {
                            "page": row.get("page"),
                            "cid": row.get("cid"),
                            "title": _text(row.get("part"))[:200],
                            "duration": row.get("duration"),
                        }
                    )
                )
            video = Video(
                bvid=bvid,
                title=card.title,
                uploader=card.uploader,
                parts=tuple(parts),
                description=_text(data.get("desc"))[:2000],
            )
        except ValidationError as error:
            raise SourceError("invalid_response", "B 站稿件的分 P 信息无效。") from error
        self._videos[bvid] = (video, aid)
        self._videos.move_to_end(bvid)
        if len(self._videos) > 64:
            self._videos.popitem(last=False)
        return video

    async def _aid(self, bvid: str, part: VideoPart | None = None) -> int:
        """复用刚打开的稿件身份，并确认后续请求仍使用该分 P。"""
        if bvid not in self._videos:
            await self.video(bvid)
        self._videos.move_to_end(bvid)
        video, aid = self._videos[bvid]
        if part is not None and (
            part.page > len(video.parts) or video.parts[part.page - 1].cid != part.cid
        ):
            raise SourceError("wrong_video_part", "请求的 CID 与选中的分 P 不一致。")
        return aid

    async def comments(
        self,
        bvid: str,
        root_rpid: str | None,
        order: CommentOrder,
        cursor: str | None,
        limit: int,
    ) -> Page[Comment]:
        """读取 B 站顶层评论或独立请求指定楼中楼。"""

        async def fetch(upstream: str | None) -> Page[Comment]:
            aid = str(await self._aid(bvid))
            params = {"oid": aid, "type": "1"}
            if root_rpid is not None:
                page = int(upstream or "1")
                response = _object(
                    await self._api(
                        "/x/v2/reply/reply",
                        {
                            **params,
                            "root": root_rpid,
                            "pn": str(page),
                            "ps": "20",
                        },
                    )
                )
                rows = _items(_required(response, "replies"))
                info = _object(response.get("page") or {})
                following = (
                    str(page + 1)
                    if rows and page * (_int(info.get("size")) or 20) < _int(info.get("count"))
                    else None
                )
                return Page(tuple(_comment(_object(row), root_rpid) for row in rows), following)

            # 网页的 mode=2 为最新、mode=3 为最热。楼中楼接口使用站点原始顺序。
            response = _object(
                await self._api(
                    "/x/v2/reply/wbi/main",
                    {
                        **params,
                        "mode": "3" if order == "hot" else "2",
                        "pagination_str": json.dumps(
                            {"offset": upstream or ""}, separators=(",", ":")
                        ),
                    },
                    signed=True,
                )
            )
            page_data = _object(response.get("cursor") or {})
            pagination = _object(page_data.get("pagination_reply") or {})
            following = _text(pagination.get("next_offset"))
            ranked: dict[str, Comment] = {}
            if upstream is None:
                for raw in _items(response.get("top_replies")):
                    item = _comment(_object(raw), None, pinned=True)
                    ranked[item.rpid] = item
            for raw in _items(_required(response, "replies")):
                item = _comment(_object(raw), None)
                ranked.setdefault(item.rpid, item)
            if page_data.get("is_end") is True or following in (upstream, "no-next-offset"):
                following = ""
            return Page(tuple(ranked.values()), following or None)

        return await self._comment_pages.read(
            ("comments", bvid, root_rpid or "", order),
            cursor,
            limit,
            fetch,
        )

    async def transcript(
        self,
        bvid: str,
        part: VideoPart,
        start: float,
        end: float,
    ) -> Transcript | None:
        """读取指定 CID 的字幕轨道，优先选择中文并保留原始时间。"""
        aid = await self._aid(bvid, part)
        try:
            data = _object(
                await self._api(
                    "/x/player/wbi/v2",
                    {"aid": str(aid), "cid": str(part.cid), "bvid": bvid},
                    signed=True,
                )
            )
        except SourceError as error:
            if error.code != "login_required" or self._asr_model is None:
                raise
            return await self._transcript_from_audio(bvid, part, start, end)
        if data.get("cid", part.cid) != part.cid or data.get("bvid", bvid) != bvid:
            raise SourceError("wrong_transcript", "B 站返回的字幕稿件或 CID 与请求不一致。")
        subtitle = _object(data.get("subtitle") or {})
        tracks = [_object(track) for track in _items(subtitle.get("subtitles"))]
        tracks = [track for track in tracks if _text(track.get("subtitle_url"))]
        if not tracks:
            if self._asr_model is not None:
                return await self._transcript_from_audio(bvid, part, start, end)
            if data.get("need_login_subtitle") is True:
                raise SourceError("login_required", "这个分 P 的字幕需要 B 站登录。")
            return None
        preferred = min(
            tracks,
            key=lambda track: (
                "zh" not in _text(track.get("lan")).lower(),
                _text(track.get("lan")).startswith("ai-"),
            ),
        )
        url = _cdn_url(_text(preferred.get("subtitle_url")))
        payload = await self._json(url, referer=f"{_HOME}video/{bvid}/")
        lines: list[TranscriptLine] = []
        try:
            for raw in _items(_required(payload, "body")):
                row = _object(raw)
                text = _text(row.get("content")).strip()
                if text:
                    lines.append(
                        TranscriptLine.model_validate(
                            {"start": row.get("from"), "end": row.get("to"), "text": text[:4000]}
                        )
                    )
        except ValidationError as error:
            raise SourceError("invalid_response", "B 站字幕包含无效的时间范围。") from error
        return Transcript(cid=part.cid, source="subtitle", lines=tuple(lines))

    async def _playback(self, bvid: str, part: VideoPart) -> JsonObject:
        """查询同一分 P 的完整播放轨，拒绝平台仅授权的试看片段。"""
        aid = await self._aid(bvid, part)
        data = _object(
            await self._api(
                "/x/player/playurl",
                {
                    "avid": str(aid),
                    "bvid": bvid,
                    "cid": str(part.cid),
                    "qn": "16",
                    "fnval": "16",
                    "fnver": "0",
                    "platform": "pc",
                },
            )
        )
        if data.get("cid", part.cid) != part.cid or data.get("bvid", bvid) != bvid:
            raise SourceError("wrong_video_part", "B 站返回的媒体与请求分 P 不一致。")
        if data.get("is_preview") in (1, True):
            raise SourceError("access_denied", "B 站仅提供试看片段。")
        return data

    async def _transcript_from_audio(
        self,
        bvid: str,
        part: VideoPart,
        start: float,
        end: float,
    ) -> Transcript:
        """字幕轨缺失时只转写指定窗口，结果标记为本地 ASR。"""
        model = self._asr_model
        if model is None or not model.is_file():
            raise SourceError("asr_unavailable", "本地 Whisper 模型文件不可用。")
        data = await self._playback(bvid, part)
        dash = _object(data.get("dash") or {})
        tracks = [_object(raw) for raw in _items(dash.get("audio"))]
        if not tracks:
            raise SourceError("asr_unavailable", "这个分 P 未提供可单独解码的音轨。")
        tracks.sort(key=lambda track: _int(track.get("bandwidth")))
        audio = await self._download(
            _media_url(tracks),
            _AUDIO_LIMIT,
            referer=f"{_HOME}video/{bvid}/?p={part.page}",
            seconds=15.0,
        )
        lines = await transcribe(audio, model, start, end)
        return Transcript(cid=part.cid, source="asr", lines=lines)

    async def open_video(self, bvid: str, part: VideoPart) -> BinaryIO | None:
        """获取同一 CID 的视频轨，限定媒体读取上限并移交流所有权。"""
        data = await self._playback(bvid, part)
        dash = _object(data.get("dash") or {})
        tracks = [_object(raw) for raw in _items(dash.get("video"))]
        # 优先低码率 AVC，仍保持完整 PTS 时间轴供 PyAV 定位。
        tracks.sort(
            key=lambda track: (
                not _text(track.get("codecs")).startswith("avc"),
                _int(track.get("bandwidth")),
            )
        )
        segments = _items(data.get("durl"))
        if len(segments) == 1:
            tracks.append(_object(segments[0]))
        if not tracks:
            raise SourceError("media_unavailable", "B 站未提供可单独定位的完整分 P 视频轨。")
        return await self._download(
            _media_url(tracks),
            _VIDEO_LIMIT,
            referer=f"{_HOME}video/{bvid}/?p={part.page}",
            seconds=25.0,
        )
