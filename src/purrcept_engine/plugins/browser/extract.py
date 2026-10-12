"""保留网页正文抽取语义，完整结果交宿主分页，不在抽取层截断。

Markdown沿用trafilatura及无正文识别时的基础HTML转换；plain text保留可见
标签文本。基础转换是原有呈现方式，返回明确renderer，缺依赖或实际抽取
异常不降级。HTML原始材料由BrowserSession另外保存并关联，不从本文反造。
"""

from __future__ import annotations

from html.parser import HTMLParser
from re import compile as compile_re

_WHITESPACE = compile_re(r"[ \t]+\n")
_MULTI_BLANK = compile_re(r"\n{3,}")
_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "template"})
_BLOCK_TAGS = frozenset(
    {"p", "div", "section", "article", "header", "footer", "main", "blockquote", "br", "tr"}
)
_HEADINGS = {f"h{level}": level for level in range(1, 7)}


def html_to_markdown(html: str) -> tuple[str, str]:
    """完整Markdown及实际renderer；未识别主体时使用原有基础转换。"""
    from trafilatura import extract

    extracted = extract(
        html,
        output_format="markdown",
        include_links=True,
        include_tables=True,
        include_comments=False,
    )
    if extracted is not None and extracted.strip():
        return extracted.strip(), "trafilatura"
    parser = _MarkdownParser()
    parser.feed(html)
    parser.close()
    return parser.text(), "basic-html"


def html_to_text(html: str) -> str:
    """完整可见文本，保留换行，不提前取前8000字符。"""
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    return parser.text()


class _MarkdownParser(HTMLParser):
    """保留基础块级/链接呈现；跳过区域用深度计数，防止内部文本混入正文。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _HEADINGS:
            self._chunks.append("\n" + "#" * _HEADINGS[tag] + " ")
            return
        if tag in {"li"}:
            self._chunks.append("\n- ")
            return
        if tag in {"ul", "ol"}:
            self._chunks.append("\n")
            return
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")
            return
        if tag == "a":
            href = dict(attrs).get("href")
            self._href = href
            self._link_text = []
            return
        if tag in {"code", "pre"}:
            self._chunks.append("`")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "a":
            label = "".join(self._link_text).strip()
            href = self._href
            self._href = None
            self._link_text = []
            if label and href:
                self._chunks.append(f"[{label}]({href})")
            elif label:
                self._chunks.append(label)
            return
        if tag in {"code", "pre"}:
            self._chunks.append("`")
            return
        if tag in _HEADINGS or tag in _BLOCK_TAGS or tag in {"ul", "ol", "li"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._href is not None:
            self._link_text.append(data)
            return
        self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks)
        joined = _WHITESPACE.sub("\n", joined)
        joined = _MULTI_BLANK.sub("\n\n", joined)
        return joined.strip()


class _TextParser(HTMLParser):
    """保留可见标签文本和块换行，嵌套跳过区域必须完整结束才恢复收集。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS or tag in _HEADINGS or tag == "li" or tag == "br":
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks)
        joined = _WHITESPACE.sub("\n", joined)
        joined = _MULTI_BLANK.sub("\n\n", joined)
        return joined.strip()
