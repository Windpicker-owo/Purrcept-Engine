"""Notebook 的中文查询词和有界原文片段规则，不访问索引、文件或模型。

索引保留单字以支持明确的单字查询；自然句只用相邻双字和拉丁词召回，避免
常用单字的 OR 匹配淹没专题页。片段在文件版本复核后从原文截取，字符位置
始终对应完整文件，不能当作宿主签发的原文阅读凭据。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass


def index_terms(text: str) -> tuple[str, ...]:
    """为 FTS5 保留中文单字、相邻双字与小写拉丁词，重复词只登记一次。

    Args:
        text: 已取得的完整笔记或查询文字。

    Returns:
        按首次出现顺序去重的词；规则与已有 Notebook 索引一致，无须重建文件版本。
    """
    terms: list[str] = []
    for word in re.findall(r"[\u3400-\u9fff]+|[a-z0-9_]+", text.casefold()):
        if "\u3400" <= word[0] <= "\u9fff":
            terms.extend(word)
            terms.extend(word[index : index + 2] for index in range(len(word) - 1))
        else:
            terms.append(word)
    return tuple(dict.fromkeys(terms))


def query_terms(query: str) -> tuple[str, ...]:
    """优先用双字和拉丁词查询；只有没有此类词时才允许中文单字召回。

    Args:
        query: 用户的关键词或自然句，不经过模型改写或同义词扩展。

    Returns:
        可安全逐项引用到 FTS5 表达式的查询词，片段匹配使用相同规则。
    """
    terms = index_terms(query)
    specific = tuple(term for term in terms if len(term) > 1 or term.isascii())
    return specific or terms


@dataclass(frozen=True, slots=True)
class SearchSnippet:
    """某个已复核文件版本的连续原文片段，范围使用 Python 字符半开区间。

    matched 只说明有词面对应。纯向量命中没有词面位置时返回正文开头，并以
    False 明确标记；模型不能从片段缺少关键词推断该页没有语义相关内容。
    """

    text: str
    start: int
    end: int
    matched: bool


def search_snippet(
    text: str, query: str, *, body_start: int = 0, max_chars: int = 240
) -> SearchSnippet:
    """截取完整查询或最多不同查询词出现的窗口，保持原始空白与字符位置。

    Args:
        text: 通过当前文件版本复核的完整原文。
        query: 与检索使用同一查询，大小写不改变原文位置。
        body_start: 没有词面命中时的正文起点，跳过笔记 frontmatter。
        max_chars: 返回片段的字符上限，默认 240；上下文也计入该上限。

    Returns:
        连续原文及对应字符位置。不会拼接远处句子或插入省略号，不生成 slice_ref。

    Raises:
        ValueError: 正文起点越界或片段上限不是正数。
    """
    if not 0 <= body_start <= len(text) or max_chars < 1:
        raise ValueError("Notebook 片段范围或字符上限无效。")
    terms = query_terms(query)
    literal = query.strip()
    exact = re.search(re.escape(literal), text, re.IGNORECASE) if literal else None
    anchor: int | None = None
    if exact is not None:
        anchor = exact.start()
        # 完整查询不能因前文额度挤掉末尾；短词则保留少量左侧上下文。
        context = max(0, min(40, max_chars - len(exact.group())))
    elif terms:
        pattern = "(?=(" + "|".join(re.escape(term) for term in terms) + "))"
        matches = tuple(re.finditer(pattern, text, re.IGNORECASE))
        if matches:
            anchor = matches[0].start()
        # 单个拉丁词也可能长过整个片段。保留其起点用于截取，但不能让它阻断
        # 后续窗口的双指针推进，或把未进入窗口的词记成负次数。
        matches = tuple(match for match in matches if len(match.group(1)) <= max_chars)
        counts: Counter[str] = Counter()
        right, best = 0, 0
        for left, match in enumerate(matches):
            # 只比较不同查询词的覆盖，重复常用词不能挤走人物和事件的组合。
            while right < len(matches) and matches[right].end(1) - match.start() <= max_chars:
                counts[matches[right].group(1).casefold()] += 1
                right += 1
            if len(counts) > best:
                best, anchor = len(counts), match.start()
            term = matches[left].group(1).casefold()
            counts[term] -= 1
            if not counts[term]:
                del counts[term]
        # 不在命中窗口左侧额外挤入前文，避免截掉已选择窗口末端的命中词。
        context = 0
    else:
        context = 0

    matched = anchor is not None
    start = max(0, anchor - context) if anchor is not None else body_start
    if not matched:
        while start < len(text) and text[start].isspace():
            start += 1
    end = min(len(text), start + max_chars)
    return SearchSnippet(text[start:end], start, end, matched)
