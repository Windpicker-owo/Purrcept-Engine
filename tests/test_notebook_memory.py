"""以真实文件、SQLite 和确定性模型验证 Notebook 单一记忆闭环。

模型替身只生成提案；安全发布、版本冲突、可靠队列及索引使用生产实现。
所有路径和资料来自临时目录，不访问真实账号、模型或用户数据库。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from purrcept_core.models import (
    Message,
    ModelEventSink,
    ModelRequest,
    ModelResponse,
    SystemInstruction,
)

from purrcept_engine import EngineConfig, EngineHost, InputSource, InputSubmission
from purrcept_engine.app.evidence import EvidenceAccess
from purrcept_engine.app.notebook_resources import NotebookAccess
from purrcept_engine.domain.evidence.models import EvidenceSnapshot
from purrcept_engine.domain.model_requests.service import RequestBudget
from purrcept_engine.domain.notebook.contracts import Publication
from purrcept_engine.domain.notebook.settings import EmbeddingSettings
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugins.notebook import Notebook, NotebookError, NotebookRegistrationError
from purrcept_engine.plugins.notebook_memory import ConsolidationSettings, NotebookMemory


async def observe_progress() -> None:
    """给实际文件或数据库提交一次推进机会，外层测试持有绝对超时。"""
    await asyncio.sleep(0.01)


class Model:
    """记录完整交付的材料，按这些来源产生受控专题修订。"""

    def __init__(self) -> None:
        self.requests: list[tuple[Message, ...]] = []
        self.invalid_source = False
        self.target = "memory/people/小雨.md"
        self.content = "- 主体与小雨一起喝过茶，周末看书的约定尚未完成。"
        self.skip = False
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def generate(
        self,
        instructions: tuple[SystemInstruction, ...],
        messages: tuple[Message, ...],
        *,
        batch_id: str,
        sources: tuple[str, ...],
        budget: RequestBudget,
    ) -> ModelResponse:
        """只引用本次真实交付页，测试可暂停以制造前台并发写入。"""
        budget.claim()
        assert instructions and batch_id
        self.requests.append(messages)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.skip:
            return ModelResponse(Message.assistant('{"revisions": [], "defer": false}'))
        material = json.loads(messages[0].text)
        target = next((doc for doc in material["targets"] if doc["path"] == self.target), None)
        proposal = {
            "revisions": [
                {
                    "path": self.target,
                    "expected_version": None if target is None else target["file_version"],
                    "summary": "小雨喜欢茶，周末一起看书。",
                    "content": self.content,
                    "sources": ["slice:" + "0" * 64] if self.invalid_source else list(sources),
                }
            ],
            "defer": False,
        }
        return ModelResponse(Message.assistant(json.dumps(proposal, ensure_ascii=False)))


@pytest.fixture
async def resources(tmp_path: Path) -> AsyncIterator[tuple[Store, Notebook, NotebookAccess, Model]]:
    store = await Store.open(tmp_path / "engine.sqlite3")
    access = NotebookAccess(store.notebook("notes"), store.evidence, None)
    model = Model()
    notebook = Notebook(
        tmp_path / "notes",
        EvidenceAccess(store.evidence, 8000),
        instance_id="notes",
        store=access,
        model=model,
        seed_examples=False,
    )
    await notebook.start()
    try:
        yield store, notebook, access, model
    finally:
        await notebook.close()
        await access.close()
        await store.close()


async def test_append_serializes_and_reuses_document_snapshot(
    resources: tuple[Store, Notebook, NotebookAccess, Model], tmp_path: Path
) -> None:
    """并发追加不丢段落，反复读取同内容不制造快照或重复队列版本。"""
    _, notebook, access, _ = resources
    results = await asyncio.gather(
        *(notebook.append(f"随笔-{number}", timestamp=False) for number in range(12))
    )
    path = f"journal/{datetime.now().astimezone():%Y-%m-%d}.md"
    text = (tmp_path / "notes" / path).read_text(encoding="utf-8")
    assert all(f"随笔-{number}" in text for number in range(12))
    assert all(result["path"] == path for result in results)
    first = await notebook.read(path)
    second = await notebook.read(path)
    assert first["evidence_version"] == second["evidence_version"] == 12
    work = await access.pending()
    assert len(work) == 1 and work[0].changes == 12
    with sqlite3.connect(tmp_path / "engine.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM knowledge_evidence").fetchone() == (12,)
        assert connection.execute("SELECT DISTINCT purpose FROM analysis_work").fetchall() == []


async def test_overflow_empty_and_unsafe_append_leave_original(
    resources: tuple[Store, Notebook, NotebookAccess, Model], tmp_path: Path
) -> None:
    _, notebook, _, _ = resources
    await notebook.append("原始内容", path="daily.md")
    original = (tmp_path / "notes/daily.md").read_bytes()
    for content, path in [
        (" ", "daily.md"),
        ("内容", "../escape.md"),
        ("内容", "attachments/a.md"),
        ("内容", "CON.md"),
        ("文" * 30_000, "daily.md"),
    ]:
        with pytest.raises(NotebookError):
            await notebook.append(content, path)
    assert (tmp_path / "notes/daily.md").read_bytes() == original


async def test_layered_index_search_and_move_remove_ghosts(
    resources: tuple[Store, Notebook, NotebookAccess, Model], tmp_path: Path
) -> None:
    _, notebook, access, _ = resources
    await notebook.write("ordinary.md", "---\nsummary: 共同经历\n---\n主体记下喝茶。\n秘密细节。")
    await notebook.write("skill.md", "---\ndescription: 操作时使用\n---\n秘密技能。")
    material = await notebook.prepare_request()
    assert "秘密细节" not in str(material) and "秘密技能" not in str(material)
    assert "共同经历" in str(material["notes_index"])
    assert "skill.md" in str(material["skills"])
    items = (await notebook.search("喝茶"))["items"]
    assert isinstance(items, list) and len(items) == 1
    page = await notebook.read("ordinary.md")
    assert isinstance(page["version"], str)
    await notebook.move("ordinary.md", "moved.md", expected_version=page["version"])
    assert "ordinary.md" not in str((await notebook.search("喝茶"))["items"])
    moved = await notebook.read("moved.md")
    assert isinstance(moved["version"], str)
    await notebook.delete("moved.md", expected_version=moved["version"])
    assert (await notebook.search("喝茶"))["items"] == []
    assert {document.path for document in await access.documents()} == {"skill.md"}


async def test_worker_publishes_only_generated_pages_and_keeps_sources(
    resources: tuple[Store, Notebook, NotebookAccess, Model], tmp_path: Path
) -> None:
    _, notebook, access, model = resources
    await notebook.append("小雨喜欢喝茶，周末打算一起看书。")
    source = (await access.pending())[0]
    original = (tmp_path / "notes" / source.document.path).read_bytes()
    worker = NotebookMemory(
        notebook, access, model, ConsolidationSettings(enabled=True, model="test")
    )
    await worker.consolidate((source,))
    assert await access.pending() == ()
    assert (tmp_path / "notes" / source.document.path).read_bytes() == original
    generated = (tmp_path / "notes" / model.target).read_text(encoding="utf-8")
    assert "## 要点" in generated and "slice:" in generated
    assert "## 概览" not in generated and generated.count(model.content) == 1
    assert "小雨" in str((await notebook.search("小雨"))["items"])
    overview = await notebook.read(model.target, level="overview")
    assert overview["text"] == model.content
    assert await access.publications() == ()
    assert len(model.requests) == 1


@pytest.mark.parametrize("violation", ["source", "path", "date", "wrapped", "long"])
async def test_invalid_proposals_cool_down_without_writing(
    resources: tuple[Store, Notebook, NotebookAccess, Model], tmp_path: Path, violation: str
) -> None:
    _, notebook, access, model = resources
    await notebook.append("有效原始笔记。")
    model.invalid_source = violation == "source"
    if violation == "path":
        model.target = "journal/forged.md"
    elif violation == "date":
        model.target = "memory/people/2026-11-16-凌晨.md"
    elif violation == "wrapped":
        model.content = "## 概览\n\n## 概览\n\n打印原文。"
    elif violation == "long":
        model.content = "扩写" * 1000
    worker = NotebookMemory(
        notebook, access, model, ConsolidationSettings(enabled=True, model="test")
    )
    await worker.consolidate(await access.pending())
    work = await access.pending()
    assert len(work) == 1 and work[0].next_attempt_at > 0
    assert not (tmp_path / "notes" / model.target).exists()


async def test_source_changes_during_generation_reject_late_publication(
    resources: tuple[Store, Notebook, NotebookAccess, Model], tmp_path: Path
) -> None:
    _, notebook, access, model = resources
    await notebook.append("原本的计划。")
    old = (await access.pending())[0]
    model.release = asyncio.Event()
    worker = NotebookMemory(
        notebook, access, model, ConsolidationSettings(enabled=True, model="test")
    )
    task = asyncio.create_task(worker.consolidate((old,)))
    await model.entered.wait()
    await notebook.append("计划已经改期。")
    model.release.set()
    await task
    assert not (tmp_path / "notes" / model.target).exists()
    current = (await access.pending())[0]
    assert current.document.file_version != old.document.file_version
    assert current.next_attempt_at == 0


async def test_successive_consolidation_retains_historical_provenance(
    resources: tuple[Store, Notebook, NotebookAccess, Model],
    tmp_path: Path,
) -> None:
    """新批只获得新来源资格，但专题原来的可追溯来源不会被新修订丢掉。"""
    _, notebook, access, model = resources
    worker = NotebookMemory(
        notebook, access, model, ConsolidationSettings(enabled=True, model="test")
    )
    await notebook.append("初次一起喝茶。")
    await worker.consolidate(await access.pending())
    first = (tmp_path / "notes" / model.target).read_text(encoding="utf-8")
    old_source = first.rsplit("\n## 来源\n", 1)[-1].strip()
    await notebook.append("后来约定周末一起看书。")
    model.content = "- 小雨与主体一起喝过茶。\n- 周末约好看书，仍待完成。"
    await worker.consolidate(await access.pending())
    second = (tmp_path / "notes" / model.target).read_text(encoding="utf-8")
    assert old_source in second and second.count("- slice:") == 2
    assert len(model.requests) == 2 and await access.pending() == ()


async def test_blog_can_finish_without_writing_and_still_supply_durable_facts(
    resources: tuple[Store, Notebook, NotebookAccess, Model], tmp_path: Path
) -> None:
    """草稿经过同一筛选。空提案结案，有长期价值时可引用草稿原文。"""
    _, notebook, access, model = resources
    await notebook.mkdir("blog")
    await notebook.write("blog/surf-picks.md", "随手分享今天看到的三个链接。")
    worker = NotebookMemory(
        notebook, access, model, ConsolidationSettings(enabled=True, model="test")
    )
    model.skip = True
    await worker.consolidate(await access.pending())
    assert await access.pending() == () and await access.publications() == ()
    assert not (tmp_path / "notes/memory").exists()
    material = json.loads(model.requests[0][0].text)
    assert material["source_pages"][0]["source"]["path"] == "blog/surf-picks.md"

    await notebook.append("小雨说以后推荐她乌龙茶。", path="blog/surf-picks.md")
    model.skip = False
    model.content = "- 小雨希望以后推荐乌龙茶。"
    await worker.consolidate(await access.pending())
    assert (await notebook.read(model.target, level="overview"))["text"] == model.content
    assert await access.pending() == ()


async def test_unchanged_content_does_not_append_another_source(
    resources: tuple[Store, Notebook, NotebookAccess, Model],
) -> None:
    """模型提议原样重写时只结案，本次引用不会让活页变长。"""
    _, notebook, access, model = resources
    worker = NotebookMemory(
        notebook, access, model, ConsolidationSettings(enabled=True, model="test")
    )
    await notebook.append("初次一起喝茶。")
    await worker.consolidate(await access.pending())
    first = await notebook.read(model.target)
    await notebook.append("再次提及一起喝茶。")
    await worker.consolidate(await access.pending())
    assert (await notebook.read(model.target))["version"] == first["version"]
    assert await access.pending() == () and len(model.requests) == 2


async def test_existing_people_are_reused_beyond_the_first_four_targets(
    resources: tuple[Store, Notebook, NotebookAccess, Model],
) -> None:
    """模型取得确切人物卡全文，不因文件名排在后面就被迫另起一页。"""
    _, notebook, access, model = resources
    await notebook.mkdir("memory")
    await notebook.mkdir("memory/people")
    for name in (*(f"联系人{number}" for number in range(15)), "小雨"):
        await notebook.write(f"memory/people/{name}.md", f"- {name}喜欢喝茶。")
    await notebook.append("小雨明确说以后更喜欢乌龙茶。")
    worker = NotebookMemory(
        notebook, access, model, ConsolidationSettings(enabled=True, model="test")
    )
    model.content = "- 小雨更喜欢乌龙茶。"
    await worker.consolidate(await access.pending())
    assert (await notebook.read(model.target, level="overview"))["text"] == model.content
    material = json.loads(model.requests[0][0].text)
    assert {"path": model.target, "content": "- 小雨喜欢喝茶。"} in material["existing_pages"]
    assert material["person_pages_remaining"] == 0
    assert await access.pending() == ()

    await notebook.append("新认识的小林明确说不吃辣。")
    model.target = "memory/people/小林.md"
    model.content = "- 小林不吃辣。"
    await worker.consolidate(await access.pending())
    assert len(await access.pending()) == 1
    assert model.target not in {doc.path for doc in await access.documents()}

    await notebook.append("小林希望每次点菜都避开辣椒。")
    model.target = "memory/people/contacts.md"
    await worker.consolidate(await access.pending())
    assert (await notebook.read(model.target, level="overview"))["text"] == model.content
    assert await access.pending() == ()


async def test_legacy_fragments_remain_searchable_without_filling_notes_index(
    resources: tuple[Store, Notebook, NotebookAccess, Model], tmp_path: Path
) -> None:
    """旧日期页保留原文和检索入口，动态目录只采纳稳定分类。"""
    _, notebook, _, model = resources
    memory = tmp_path / "notes/memory"
    memory.mkdir()
    for number in range(65):
        (memory / f"2026-11-16-surf-{number}.md").write_text("旧冲浪记录", encoding="utf-8")
    await notebook.mkdir("blog")
    for number in range(6):
        await notebook.write(f"blog/surf-{number}.md", f"草稿正文 {number}")
    await notebook.mkdir("memory/people")
    await notebook.write(model.target, "- 小雨喜欢喝茶。")
    await notebook.write("ordinary.md", "共同经历。")

    material = await notebook.prepare_request()
    index = material["notes_index"]
    assert isinstance(index, list)
    paths = [str(note["path"]) for note in index if isinstance(note, dict)]
    assert paths[0] == model.target and "ordinary.md" in paths
    assert len(paths) == 6 and sum(path.startswith("blog/") for path in paths) == 4
    assert material["notes_truncated"] is True
    assert "2026-11-16-surf-" in str((await notebook.search("旧冲浪记录", path="memory"))["items"])


async def test_optional_vectors_never_return_a_file_changed_during_query(
    resources: tuple[Store, Notebook, NotebookAccess, Model],
    tmp_path: Path,
) -> None:
    """实际向量派生事务参与测试；网络等待后必须再次核对文件哈希。"""

    class Embedding:
        model_id = "isolated-space"

        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            if texts == ("unseen-query",):
                self.entered.set()
                await self.release.wait()
            return tuple((1.0, 0.0) for _ in texts)

    store, notebook, _, _ = resources
    embedding = Embedding()
    settings = EmbeddingSettings(
        model="openai/offline", model_id=embedding.model_id, api_base="https://embedding.invalid/v1"
    )
    access = NotebookAccess(store.notebook("notes"), store.evidence, settings, embedding=embedding)
    notebook_with_vectors = Notebook(
        tmp_path / "notes",
        EvidenceAccess(store.evidence, 8000),
        instance_id="notes",
        store=access,
        seed_examples=False,
    )
    await notebook.write("daily.md", "查询之前的正文。")
    await access.maintain_index()
    task = asyncio.create_task(notebook_with_vectors.search("unseen-query"))
    try:
        await embedding.entered.wait()
        (tmp_path / "notes/daily.md").write_text("查询等待期间外部已修改。", encoding="utf-8")
        embedding.release.set()
        result = await task
        assert result["items"] == [] and result["semantic_pending"] == 1
        assert (await access.status())["semantic_enabled"] is True
    finally:
        embedding.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await notebook_with_vectors.close()
        await access.close()


async def test_external_edit_reconciles_and_instances_are_isolated(
    resources: tuple[Store, Notebook, NotebookAccess, Model], tmp_path: Path
) -> None:
    store, notebook, access, _ = resources
    await notebook.write("daily.md", "最初的笔记。")
    (tmp_path / "notes/daily.md").write_text("外部修改后的喝茶约定。", encoding="utf-8")
    await notebook.synchronize()
    assert (await access.pending())[0].changes == 2
    assert (await notebook.search("喝茶"))["items"]
    other = NotebookAccess(store.notebook("other"), store.evidence, None)
    assert await other.search("喝茶") == ()
    (tmp_path / "notes/daily.md").unlink()
    await notebook.synchronize()
    assert await access.pending() == ()
    assert await access.search("喝茶") == ()


async def test_explicit_source_is_delivered_but_unreferenced_interaction_is_not(
    resources: tuple[Store, Notebook, NotebookAccess, Model],
) -> None:
    """整理入口是亲笔笔记及其引用，不能自动吸收全部交互。"""
    store, notebook, access, model = resources
    for identity in ("referenced", "unrelated"):
        await store.evidence.put_evidence(
            EvidenceSnapshot(
                evidence_id=identity,
                version=1,
                kind="input",
                text=identity,
                source_json="{}",
                occurred_at=1.0,
            )
        )
    quoted = await store.evidence.read_evidence("referenced")
    assert quoted.slice is not None
    await notebook.append("喝茶的原文依据：" + quoted.slice.slice_ref)
    worker = NotebookMemory(
        notebook, access, model, ConsolidationSettings(enabled=True, model="test")
    )
    await worker.consolidate(await access.pending())
    delivered = json.loads(model.requests[0][0].text)["source_pages"]
    assert {page["id"] for page in delivered} == {
        "referenced",
        f"notebook:notes:journal/{datetime.now().astimezone():%Y-%m-%d}.md",
    }


async def test_recovery_only_observes_publication_without_replaying(
    resources: tuple[Store, Notebook, NotebookAccess, Model],
    tmp_path: Path,
) -> None:
    """跨盘库中断通过结果哈希核验；未知意图不自动创建文件。"""
    _, notebook, access, model = resources
    written = await notebook.append("已发布的真实笔记。", path="daily.md")
    assert isinstance(written["version"], str)
    await access.intend(
        Publication(
            identity="confirmed",
            path="daily.md",
            expected_version=None,
            result_version=written["version"],
        )
    )
    await access.intend(
        Publication(
            identity="unknown",
            path="missing.md",
            expected_version=None,
            result_version="not-an-actual-hash",
        )
    )
    worker = NotebookMemory(notebook, access, model, ConsolidationSettings())
    await worker.start()
    await worker.close()
    assert await access.publications() == ()
    assert not (tmp_path / "notes/missing.md").exists() and not model.requests
    with sqlite3.connect(tmp_path / "engine.sqlite3") as connection:
        assert dict(connection.execute("SELECT identity,state FROM notebook_publications")) == {
            "confirmed": "confirmed",
            "unknown": "unknown",
        }


async def test_registration_failure_keeps_actual_file_and_requires_reconciliation(
    tmp_path: Path,
) -> None:
    """登记失败不能假装文件未写；恢复扫描只登记结果，不重复追加。"""

    class Broken(EvidenceAccess):
        async def record_document(self, snapshot: EvidenceSnapshot) -> EvidenceSnapshot:
            raise StorageError("合成接管背压。", code="store_busy")

    store = await Store.open(tmp_path / "engine.sqlite3")
    access = NotebookAccess(store.notebook("notes"), store.evidence, None)
    notebook = Notebook(
        tmp_path / "notes",
        Broken(store.evidence, 8000),
        instance_id="notes",
        store=access,
        seed_examples=False,
    )
    await notebook.start()
    try:
        with pytest.raises(NotebookRegistrationError) as failure:
            await notebook.append("只能出现一次。", path="daily.md")
        assert isinstance(failure.value.__cause__, StorageError)
        assert (tmp_path / "notes/daily.md").read_text(encoding="utf-8").count(
            "只能出现一次。"
        ) == 1
        notebook.evidence = EvidenceAccess(store.evidence, 8000)
        await notebook.synchronize()
        assert len(await access.pending()) == 1
    finally:
        await notebook.close()
        await access.close()
        await store.close()


async def test_host_worker_action_and_next_foreground_share_only_notebook_files(
    tmp_path: Path,
) -> None:
    """真实 Host/Worker 写笔记，独立命名模型整理，下一轮通过动态目录发现专题。"""

    class Backend:
        def __init__(self, *, organize: bool = False) -> None:
            self.organize = organize
            self.requests: list[ModelRequest] = []

        async def generate(
            self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
        ) -> ModelResponse:
            self.requests.append(request)
            if not self.organize:
                return ModelResponse(Message.assistant("内部思考结束。"))
            material = json.loads(request.messages[0].text)
            assert not request.tools
            source = material["source_pages"][0]["slice_ref"]
            return ModelResponse(
                Message.assistant(
                    json.dumps(
                        {
                            "revisions": [
                                {
                                    "path": "memory/people/小雨.md",
                                    "expected_version": None,
                                    "summary": "与小雨喝茶的共同经历。",
                                    "content": "记录明确的共同经历，不推断长期人格。",
                                    "sources": [source],
                                }
                            ]
                        }
                    )
                )
            )

    declaration = {
        "kind": "generation",
        "model": "openai/offline",
        "api_key": "synthetic",
        "api_base": "http://127.0.0.1:9999/v1",
        "context_window": 100_000,
        "max_output_tokens": 1000,
    }
    config = EngineConfig.validate(
        {
            "host": {"data_dir": tmp_path / "state"},
            "console": False,
            "models": {"main": declaration, "organize": declaration},
            "subject": {
                "foreground": "main",
                "reflection": "main",
                "behavior_review_enabled": False,
            },
            "plugins": [
                {
                    "instance_id": "notes",
                    "entry": "notebook",
                    "config": {
                        "notebook_dir": "notes",
                        "seed_examples": False,
                        "consolidation": {
                            "enabled": True,
                            "model": "organize",
                            "batch_changes": 1,
                            "scan_seconds": 0.01,
                        },
                    },
                }
            ],
        },
        base_dir=tmp_path,
    )
    foreground, organize = Backend(), Backend(organize=True)
    host = EngineHost(config, generation_backends={"main": foreground, "organize": organize})
    await host.start()
    try:
        action = await host.execute(
            'from world import me\nawait me.notebook.append(content="今天和小雨一起喝茶。")'
        )
        assert action.state == "completed"
        async with asyncio.timeout(10):
            while not (tmp_path / "notes/memory/people/小雨.md").exists():
                await observe_progress()
        now = datetime.now(UTC)
        admitted = await host.submit(
            InputSubmission(
                InputSource("test", "local", "chat", "text", "first"),
                "想起一起喝茶的事。",
                now,
                now,
            )
        )
        async with asyncio.timeout(10):
            while not foreground.requests:
                await observe_progress()
            while (
                record := await host.query_input(admitted.input_id)
            ) is not None and record.state != "processed":
                await observe_progress()
        assert "memory/people/小雨.md" in str(foreground.requests[0].reminders)
        assert "recall" not in {tool.name for tool in foreground.requests[0].tools}
        assert len(organize.requests) == 1
        panel = await host.query_panel()
        notebooks = panel["notebooks"]
        assert isinstance(notebooks, dict) and "notes" in notebooks
        assert "reflection" not in panel and "knowledge" not in panel
    finally:
        await host.close()
