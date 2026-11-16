"""用真实 Notebook 与临时 SQLite 验证自然语言检索和当前版本片段。

样本覆盖专名、长句、单字噪声以及词面完全不同的语义命中，不访问用户笔记。
向量由显式替身提供；片段必须逐字来自返回版本，不能借搜索生成原文阅读资格。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from purrcept_engine.app.evidence import EvidenceAccess
from purrcept_engine.app.notebook_resources import NotebookAccess
from purrcept_engine.domain.notebook.search import search_snippet
from purrcept_engine.domain.notebook.settings import EmbeddingSettings
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugins.notebook import Notebook
from purrcept_engine.values import JsonObject


@pytest.fixture
async def notebook(tmp_path: Path) -> AsyncIterator[Notebook]:
    """向测试借出真实文件与 SQLite 端口，并等待所有文件和数据库资源归还。"""
    store = await Store.open(tmp_path / "search.sqlite3")
    access = NotebookAccess(store.notebook("notes"), store.evidence, None)
    notes = Notebook(
        tmp_path / "notes",
        EvidenceAccess(store.evidence, 8000),
        instance_id="notes",
        store=access,
        seed_examples=False,
    )
    await notes.start()
    try:
        yield notes
    finally:
        await notes.close()
        await access.close()
        await store.close()


def items(result: JsonObject) -> list[JsonObject]:
    """取得实际搜索回执中的条目，让字段断言仍通过严格 JSON 类型核验。"""
    value = result["items"]
    assert isinstance(value, list)
    records: list[JsonObject] = []
    for item in value:
        assert isinstance(item, dict)
        records.append(item)
    return records


def paths(records: list[JsonObject]) -> set[str]:
    """核验路径字段后比较结果集合，不把严格 JSON 值当作可哈希字符串。"""
    result: set[str] = set()
    for record in records:
        path = record["path"]
        assert isinstance(path, str)
        result.add(path)
    return result


async def test_long_chinese_query_rejects_isolated_character_noise(notebook: Notebook) -> None:
    """词面专名和自然句均能定位专题，不能让大量仅沾单字的日记挤占候选。"""
    await notebook.mkdir("memory/games")
    await notebook.mkdir("journal")
    await notebook.write(
        "memory/games/河畔木屋.md",
        "---\nsummary: 河畔木屋的共同经历\n---\n"
        "# 河畔木屋\n拾风邀请我联机，沿河盖房子，最后修好了木质露台。",
    )
    for number in range(18):
        await notebook.write(
            f"journal/noise-{number:02}.md",
            "今天和朋友聊天，起身走到窗前，风吹进了房间，屋里有一台新机器。",
        )
    assert items(await notebook.search("河畔木屋"))[0]["path"] == "memory/games/河畔木屋.md"
    matches = items(await notebook.search("和拾风一起联机盖房子"))
    assert [item["path"] for item in matches] == ["memory/games/河畔木屋.md"]
    # 单字本身是明确查询时仍可检索，避免通过删除索引单字破坏旧文件与此能力。
    assert items(await notebook.search("风"))
    await notebook.write("journal/common.md", "今天一起吃饭，晚上一起散步。")
    matches = items(await notebook.search("和拾风一起联机盖房子"))
    assert matches[0]["path"] == "memory/games/河畔木屋.md"
    assert paths(matches) == {"memory/games/河畔木屋.md", "journal/common.md"}


async def test_search_returns_exact_bounded_context_and_current_version(notebook: Notebook) -> None:
    """摘要不能代替命中正文；片段携带原文件字符范围，外部编辑后不能返回旧片段。"""
    content = (
        "---\nsummary: 周末共同经历\n---\n"
        + "没有相关内容的前文。\n" * 80
        + "拾风邀请我联机，在河畔一起盖房子，屋顶是深色木板。\n"
        + "随后去散步。\n" * 80
    )
    written = await notebook.write("experience.md", content)
    version = written["version"]
    assert isinstance(version, str)
    result = items(await notebook.search("和拾风一起联机盖房子"))[0]
    assert result["version"] == version
    snippet, bounds = result["snippet"], result["snippet_range"]
    assert isinstance(snippet, str) and isinstance(bounds, dict)
    start, end = bounds["start"], bounds["end"]
    assert isinstance(start, int) and isinstance(end, int)
    assert bounds["unit"] == "character" and content[start:end] == snippet
    assert len(snippet) <= 240 and "拾风邀请我联机" in snippet and "盖房子" in snippet
    assert result["snippet_matched"] is True and "slice_ref" not in result
    await notebook.write("experience.md", "已改为周末看书。", expected_version=version)
    assert items(await notebook.search("河畔木屋")) == []


async def test_embedding_hybrid_keeps_paraphrase_and_keyword_routes(tmp_path: Path) -> None:
    """真实索引融合两条候选路线；语义独有命中仍返回真实摘录并明确无词面命中。"""

    class Embedding:
        """为已知语义样本及其问法提供固定方向，其他资料保持正交。"""

        model_id = "offline-search-v1"

        async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            """离线计算已声明的有限向量，不创建传输或生成模型请求。"""
            return tuple(
                (1.0, 0.0)
                if text in ("共同建造", "双人营造住所") or "方块世界" in text
                else (0.0, 1.0)
                for text in texts
            )

    store = await Store.open(tmp_path / "hybrid.sqlite3")
    settings = EmbeddingSettings(
        model="openai/offline", model_id="offline-search-v1", api_base="https://offline.invalid/v1"
    )
    access = NotebookAccess(
        store.notebook("hybrid"), store.evidence, settings, embedding=Embedding()
    )
    notes = Notebook(
        tmp_path / "notes",
        EvidenceAccess(store.evidence, 8000),
        instance_id="hybrid",
        store=access,
        seed_examples=False,
    )
    await notes.start()
    try:
        text = "在方块世界里两个人修好了一栋小楼。"
        await notes.write("semantic.md", text)
        await notes.write("keyword.md", "共同参加活动，留下建造记录。")
        await access.maintain_index()
        matches = items(await notes.search("共同建造"))
        assert paths(matches) == {"semantic.md", "keyword.md"}
        assert (
            next(item for item in matches if item["path"] == "semantic.md")["snippet_matched"]
            is False
        )
        # 另一种问法没有任何词面对应，依然能走已有向量路线。
        paraphrases = items(await notes.search("双人营造住所"))
        assert paths(paraphrases) == {"semantic.md", "keyword.md"}
        assert paraphrases[0]["path"] == "semantic.md"
        assert all(item["snippet_matched"] is False for item in paraphrases)
        assert all(isinstance(item["snippet"], str) and item["snippet"] for item in paraphrases)
        assert (await access.status())["semantic_enabled"] is True
    finally:
        await notes.close()
        await access.close()
        await store.close()


def test_snippet_prefers_distinct_context_over_repeated_generic_matches() -> None:
    """常用词在文首重复出现时，摘录应选择后面能对应人物及事件的段落。"""
    text = "一起散步。" * 100 + "🙂\r\n拾风邀我联机，我们一起在河畔盖房子。\r\n" + "晚安。" * 100
    snippet = search_snippet(text, "和拾风一起联机盖房子")
    assert "拾风" in snippet.text and "联机" in snippet.text and "盖房子" in snippet.text
    assert text[snippet.start : snippet.end] == snippet.text and len(snippet.text) <= 240


@pytest.mark.parametrize(
    ("text", "query", "max_chars"),
    [
        ("前文。" * 100 + "河畔木屋" + "后文。" * 100, "河畔木屋", 4),
        ("记录 MixedCase Context 的正文。", "mixedcase", 12),
        ("起点 " + "a" * 300, "a" * 300, 240),
        ("起点 " + "a" * 300, "a" * 300 + " other", 240),
        ("起点拾风", "拾风邀请我", 1),
    ],
)
def test_snippet_keeps_original_offsets_under_strict_limits(
    text: str, query: str, max_chars: int
) -> None:
    """字母大小写、超长词和极小窗口均保持有限连续片段，不制造负字符范围。"""
    snippet = search_snippet(text, query, max_chars=max_chars)
    assert snippet.matched and 0 <= snippet.start < snippet.end <= len(text)
    assert len(snippet.text) <= max_chars and text[snippet.start : snippet.end] == snippet.text


async def test_query_boundaries_and_literal_fts_syntax(notebook: Notebook) -> None:
    """无词查询不扩大成全库扫描，SQL/FTS 运算符不能越过字面查询边界。"""
    from purrcept_engine.plugins.notebook import NotebookError

    await notebook.write("daily.md", "正常笔记。")
    assert items(await notebook.search('" OR *')) == []
    assert items(await notebook.search("？！")) == []
    for query in (" ", "字" * 513):
        with pytest.raises(NotebookError, match="512"):
            await notebook.search(query)


async def test_standalone_files_use_same_query_and_snippet_rules(
    notebook: Notebook, tmp_path: Path
) -> None:
    """没有派生账本的直接文件实例也不能退回到自然句全文包含或单字 OR。"""
    standalone = Notebook(
        tmp_path / "standalone", notebook.evidence, instance_id="standalone", seed_examples=False
    )
    await standalone.start()
    try:
        await standalone.write("target.md", "拾风邀我联机盖房子。")
        await standalone.write("noise.md", "风吹进房间，大家起身去吃饭。")
        matches = items(await standalone.search("和拾风一起联机盖房子"))
        assert paths(matches) == {"target.md"}
        assert matches[0]["snippet"] == "拾风邀我联机盖房子。"
    finally:
        await standalone.close()
