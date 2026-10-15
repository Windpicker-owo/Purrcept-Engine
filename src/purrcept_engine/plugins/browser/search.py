"""从搜索结果页 HTML 抽出标题、链接和摘要。

默认引擎是 Bing。没有识别出格式时返回空列表；Session同时保存完整HTML，
并明确有限解析不代表远端全部结果，实际解析异常不以空列表代替。
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from purrcept_engine.plugins.browser.settings import BrowserSettings, SearchEngine

_BING = "https://www.bing.com/search?q={query}"
_DUCKDUCKGO = "https://html.duckduckgo.com/html/?q={query}"


def build_search_url(settings: BrowserSettings, query: str) -> str:
    """把查询填进配置的模板。

    Args:
        settings: 当前组件已校验的配置或纯声明。
        query: 用于关键词或语义检索的意图文本。

    Returns:
        按明确引擎和完整查询构造的搜索 URL。
    """

    encoded = quote_plus(query.strip())
    template = settings.search_url.strip() if settings.search_url else ""
    if not template:
        template = _BING if settings.search_engine is SearchEngine.BING else _DUCKDUCKGO
    if "{query}" in template:
        return template.replace("{query}", encoded)
    separator = "&" if "?" in template else "?"
    return f"{template}{separator}q={encoded}"


def parse_search_results(html: str, *, engine: SearchEngine, limit: int) -> list[dict[str, str]]:
    """解析 Bing 或 DuckDuckGo HTML 结果。

    Args:
        html: 当次实际取得的完整 HTML，抽取前不按模型页长裁剪。
        engine: 显式选择的搜索引擎。
        limit: 本次读取或更新的最大条目数。

    Returns:
        包含标题、链接和摘要的顺序结果，不宣称已读取目标网页。
    """

    if engine is SearchEngine.DUCKDUCKGO:
        hits = _DuckDuckGoParser.collect(html)
    else:
        hits = _BingParser.collect(html)
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for hit in hits:
        url = hit["url"]
        if not url or url in seen:
            continue
        seen.add(url)
        unique.append(hit)
        if len(unique) >= limit:
            break
    return unique


class _BingParser(HTMLParser):
    """抽取 ``li.b_algo`` 里的标题链接和相邻段落。"""

    def __init__(self) -> None:
        """创建单次 Bing 结果解析的标题、链接和摘要缓冲。"""
        super().__init__(convert_charrefs=True)
        self.hits: list[dict[str, str]] = []
        self._in_algo = 0
        self._in_title = False
        self._in_snippet = False
        self._href = ""
        self._title: list[str] = []
        self._snippet: list[str] = []

    @classmethod
    def collect(cls, html: str) -> list[dict[str, str]]:
        """用一次独立解析收集搜索结果的链接、标题与摘要。

        Args:
            html: 当次实际取得的完整 HTML，抽取前不按模型页长裁剪。

        Returns:
            本次解析得到的结果条目，含标题、链接及摘要。
        """
        parser = cls()
        parser.feed(html)
        parser.close()
        return parser.hits

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """按文档顺序采用当前开始标签，更新抽取器的嵌套状态。

        Args:
            tag: HTMLParser 按文档顺序交付的标签名。
            attrs: 当前 HTML 开始标签的属性列表。
        """
        mapping = {key: value or "" for key, value in attrs}
        classes = mapping.get("class", "")
        if tag == "li" and "b_algo" in classes.split():
            self._in_algo += 1
            self._href = ""
            self._title = []
            self._snippet = []
            return
        if not self._in_algo:
            return
        if tag in {"h2", "h3"}:
            self._in_title = True
            return
        if tag == "a" and self._in_title and not self._href:
            self._href = mapping.get("href", "")
            return
        if tag == "p" and "b_algoSlug" not in classes:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        """按文档顺序闭合当前标签，结束对应文本收集范围。

        Args:
            tag: HTMLParser 按文档顺序交付的标签名。
        """
        if tag in {"h2", "h3"} and self._in_title:
            self._in_title = False
            return
        if tag == "p" and self._in_snippet:
            self._in_snippet = False
            return
        if tag == "li" and self._in_algo:
            self._flush()
            self._in_algo -= 1

    def handle_data(self, data: str) -> None:
        """收集当前允许范围内的正文，不采用被跳过标签的内容。

        Args:
            data: 当前搜索结果标题或摘要范围内的正文片段。
        """
        if self._in_title:
            self._title.append(data)
        elif self._in_snippet:
            self._snippet.append(data)

    def _flush(self) -> None:
        """只在当前搜索结果边界汇总已收集字段，避免跨结果拼接标题或链接。"""
        title = "".join(self._title).strip()
        url = _clean_bing_url(self._href)
        snippet = "".join(self._snippet).strip()
        if title and url:
            self.hits.append({"title": title, "url": url, "snippet": snippet})


class _DuckDuckGoParser(HTMLParser):
    """抽取 ``result__a`` / ``result__snippet``。"""

    def __init__(self) -> None:
        """创建单次 DuckDuckGo 结果解析的标题、链接和摘要缓冲。"""
        super().__init__(convert_charrefs=True)
        self.hits: list[dict[str, str]] = []
        self._in_title = False
        self._in_snippet = False
        self._href = ""
        self._title: list[str] = []
        self._snippet: list[str] = []

    @classmethod
    def collect(cls, html: str) -> list[dict[str, str]]:
        """用一次独立解析收集搜索结果的链接、标题与摘要。

        Args:
            html: 当次实际取得的完整 HTML，抽取前不按模型页长裁剪。

        Returns:
            本次解析得到的结果条目，含标题、链接及摘要。
        """
        parser = cls()
        parser.feed(html)
        parser.close()
        parser._flush()
        return parser.hits

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """按文档顺序采用当前开始标签，更新抽取器的嵌套状态。

        Args:
            tag: HTMLParser 按文档顺序交付的标签名。
            attrs: 当前 HTML 开始标签的属性列表。
        """
        mapping = {key: value or "" for key, value in attrs}
        classes = mapping.get("class", "")
        tokens = classes.split()
        if tag == "a" and "result__a" in tokens:
            self._flush()
            self._in_title = True
            self._href = mapping.get("href", "")
            self._title = []
            return
        if tag in {"a", "td"} and "result__snippet" in tokens:
            self._in_snippet = True
            self._snippet = []

    def handle_endtag(self, tag: str) -> None:
        """按文档顺序闭合当前标签，结束对应文本收集范围。

        Args:
            tag: HTMLParser 按文档顺序交付的标签名。
        """
        if tag == "a" and self._in_title:
            self._in_title = False
            return
        if tag in {"a", "td"} and self._in_snippet:
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        """收集当前允许范围内的正文，不采用被跳过标签的内容。

        Args:
            data: 当前搜索结果标题或摘要范围内的正文片段。
        """
        if self._in_title:
            self._title.append(data)
        elif self._in_snippet:
            self._snippet.append(data)

    def _flush(self) -> None:
        """在下一结果边界前采用当前完整标题与链接，随后清空局部收集状态。"""
        title = "".join(self._title).strip()
        url = _clean_ddg_url(self._href)
        snippet = "".join(self._snippet).strip()
        if title and url:
            self.hits.append({"title": title, "url": url, "snippet": snippet})
        self._href = ""
        self._title = []
        self._snippet = []


def _clean_bing_url(url: str) -> str:
    """只展开原格式已知的跳转参数，不发送请求或猜未知编码。"""
    stripped = url.strip()
    if not stripped:
        return ""
    parsed = urlparse(stripped)
    if parsed.path == "/ck/a" or "bing.com/ck/" in stripped:
        query = parse_qs(parsed.query)
        for key in ("u", "r"):
            raw = query.get(key, [""])[0]
            if raw:
                return unquote(raw)
    return stripped


def _clean_ddg_url(url: str) -> str:
    """保留原DuckDuckGo uddg解码，其他地址按取得值交给调用者。"""
    stripped = url.strip()
    if not stripped:
        return ""
    parsed = urlparse(stripped)
    if "duckduckgo.com/l/" in stripped:
        query = parse_qs(parsed.query)
        target = query.get("uddg", [""])[0]
        if target:
            return unquote(target)
    return stripped


__all__ = ["build_search_url", "parse_search_results"]
