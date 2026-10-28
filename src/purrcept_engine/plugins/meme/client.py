"""读取梗百科公开 v1 API，并在实例内串行限流、缓存有限响应。

MemeClient 由插件 build 创建、close 释放；不读取网站账号或携带 Cookie。
网络和协议错误保留为 MemeAPIError，交互调用不自动重试，日更任务决定退避。
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlencode

import httpx
from pydantic import ValidationError

from purrcept_engine.errors import EngineError
from purrcept_engine.plugins.meme.models import BASE_URL, Meme, MemeDetail, MemePage

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class MemeAPIError(EngineError):
    """公开 API 的网络、HTTP 或正文契约失败，retry_after 为服务器建议等待秒数。"""

    def __init__(self, message: str, *, retry_after: float = 0) -> None:
        """保存可供日更任务退避的明确错误。

        Args:
            message: 不含原生 HTML 错误页的失败说明。
            retry_after: 下次请求前至少等待的秒数。
        """
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after(value: str | None) -> float:
    """Retry-After 支持秒数或 HTTP 日期；坏头不掩盖原 HTTP 失败。"""
    if value is None:
        return 0
    if value.isdecimal():
        return float(value)
    try:
        target = parsedate_to_datetime(value)
    except (ValueError, TypeError, OverflowError):
        return 0
    if target.utcoffset() is None:
        return 0
    return max(0, (target - datetime.now(UTC)).total_seconds())


class MemeClient:
    """每个插件独占一个 HTTP 客户端和最多 64 个缓存条目。

    Note:
        调用在一个事件循环内串行，缓存使用完整编码路径作为键。
        服务端缓存期内仍可能看到已撤下的资料；收藏只是本地快照。
    """

    def __init__(self, timeout: float) -> None:
        """创建尚未联网的客户端。

        Args:
            timeout: 每次 HTTP 操作的超时秒数。
        """
        self._http = httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            headers={"User-Agent": "Purrcept-Meme/1.0", "Accept": "application/json"},
        )
        self._timeout = timeout
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._not_before = 0.0

    async def _get(self, path: str, ttl: float) -> bytes:
        """只缓存成功响应，并限制解压后的正文；限流期间禁止交互绕过退避。"""
        async with self._lock:
            now = time.monotonic()
            cached = self._cache.get(path)
            if cached is not None and cached[0] > now:
                self._cache.move_to_end(path)
                return cached[1]
            if now < self._not_before:
                raise MemeAPIError("梗百科请求正在退避。", retry_after=self._not_before - now)
            try:
                # 公开 API 不使用账号。丢弃此前响应的 Cookie，后续请求也不携带身份。
                self._http.cookies.clear()
                async with (
                    asyncio.timeout(self._timeout),
                    self._http.stream("GET", BASE_URL + path) as response,
                ):
                    if response.status_code != 200:
                        delay = _retry_after(response.headers.get("Retry-After"))
                        if response.status_code == 429 or response.status_code >= 500:
                            delay = max(60, delay)
                        self._not_before = time.monotonic() + delay
                        raise MemeAPIError(
                            f"梗百科 API 返回 HTTP {response.status_code}。", retry_after=delay
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_RESPONSE_BYTES:
                            raise MemeAPIError("梗百科响应超过 2 MiB 上限。")
            except httpx.HTTPError as error:
                raise MemeAPIError(f"梗百科网络请求失败：{type(error).__name__}。") from error
            except TimeoutError as error:
                raise MemeAPIError("梗百科请求超过完整响应期限。") from error

            data = bytes(body)
            self._cache[path] = (time.monotonic() + ttl, data)
            self._cache.move_to_end(path)
            while len(self._cache) > 64:
                self._cache.popitem(last=False)
            return data

    async def search(
        self, query: str = "", tag: str = "", limit: int = 10, offset: int = 0
    ) -> MemePage:
        """按公开 API 子串匹配检索，空 query 用于浏览最近更新。

        Args:
            query: 标题、标签和正文关键词，最多 80 字符。
            tag: 单个完整标签，最多 40 字符。
            limit: 每页 1 到 30 条。
            offset: 非负分页偏移，下一页使用 offset + limit。

        Returns:
            含 has_more 的最新优先列表；没有总数承诺。

        Raises:
            MemeAPIError: 网络、HTTP 状态或公开响应结构无效。
        """
        path = "/api/v1/public/memes?" + urlencode(
            {"q": query.strip(), "tag": tag.strip(), "limit": limit, "offset": offset}
        )
        data = await self._get(path, 60)
        try:
            page = MemePage.model_validate_json(data)
        except ValidationError as error:
            self._cache.pop(path, None)
            raise MemeAPIError("梗百科列表响应不符合公开 v1 契约。") from error
        if page.offset != offset or page.limit != limit or len(page.items) > limit:
            raise MemeAPIError("梗百科分页响应与请求范围不一致。")
        return page

    async def detail(self, slug: str) -> Meme:
        """读取原生 slug 的详情，路径分隔符会编码而不会改变请求路由。

        Args:
            slug: 检索或收藏返回的词条身份。

        Returns:
            含 content_html 的远端资料快照。

        Raises:
            MemeAPIError: 词条不可见、请求失败或返回身份不匹配。
        """
        path = "/api/v1/public/memes/" + quote(slug, safe="")
        data = await self._get(path, 120)
        try:
            item = MemeDetail.model_validate_json(data).item
        except ValidationError as error:
            self._cache.pop(path, None)
            raise MemeAPIError("梗百科详情响应不符合公开 v1 契约。") from error
        if item.slug != slug or item.content_html is None:
            raise MemeAPIError("梗百科详情缺少正文或返回了其他词条。")
        return item

    async def close(self) -> None:
        """归还当前实例的 HTTP 资源；由宿主结束能力借用后调用。"""
        await self._http.aclose()
