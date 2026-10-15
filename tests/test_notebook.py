"""以真实文件、同连接 Store 和 Windows Worker 验证 Notebook 的版本及阅读路径。

所有内容由本次临时目录生成，不使用用户笔记或真实模型。外部编辑样例同时
验证可检测冲突与最终核验后的已知竞争窗口，不把文件系统替换声称为严格 CAS。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import stat
import subprocess
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from purrcept_engine import EngineConfig, EngineHost, HostStateError
from purrcept_engine.app.evidence import EvidenceAccess
from purrcept_engine.execution.catalog import Catalog, ObjectDeclaration
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugins.notebook import Notebook, NotebookError, factory
from purrcept_engine.plugins.skills import parse_note
from purrcept_engine.values import JsonObject, decode_object


@dataclass(frozen=True)
class _Fixture:
    notebook: Notebook
    evidence: EvidenceAccess
    root: Path


@pytest.fixture
async def fixture(tmp_path: Path) -> AsyncIterator[_Fixture]:
    store = await Store.open(tmp_path / "evidence.sqlite3")
    evidence = EvidenceAccess(store.knowledge, 8)
    root = tmp_path / "notebook"
    notebook = Notebook(root, evidence, instance_id="notes", seed_examples=False)
    await notebook.start()
    try:
        yield _Fixture(notebook, evidence, root)
    finally:
        await notebook.close()
        await store.close()


def _text(value: JsonObject, name: str) -> str:
    result = value[name]
    assert isinstance(result, str)
    return result


def test_frontmatter_preserves_later_delimiters_and_skill_default_name() -> None:
    content = "---\nname: '饮茶'\ndescription: 想喝茶时\nignored: untrusted\n---\n正文\n---\n尾页"
    parsed = parse_note("tea/SKILL.md", content, version="hash", byte_count=90, occurred_at=1.0)
    assert parsed.name == "饮茶" and parsed.description == "想喝茶时"
    assert parsed.content[parsed.body_start :] == "正文\n---\n尾页"
    assert "content" not in parsed.catalog_entry("me.notebook.read")
    folder = parse_note("tea/SKILL.md", "正文", version="hash", byte_count=6, occurred_at=1.0)
    assert folder.name == "tea" and not folder.skill


def test_factory_is_pure_and_preserves_actual_six_model_paths(tmp_path: Path) -> None:
    blueprint = factory.prepare(
        "notes", {"notebook_dir": "notes", "seed_examples": False}, tmp_path
    )
    assert not (tmp_path / "notes").exists()
    catalog = Catalog(
        (ObjectDeclaration("subject", "me", "主体"), *blueprint.objects), blueprint.capabilities
    )
    assert set(catalog.bindings) == {
        "me.notebook.tree",
        "me.notebook.read",
        "me.notebook.write",
        "me.notebook.mkdir",
        "me.notebook.move",
        "me.notebook.delete",
    }
    assert '"expected_version"' in catalog.bindings["me.notebook.write"].schema_json
    with pytest.raises(ValidationError):
        factory.prepare("notes", {"notebook_dir": "notes", "unknown": True}, tmp_path)


async def test_write_uses_real_hash_and_rejects_existing_and_external_edit(
    fixture: _Fixture,
) -> None:
    notes = fixture.notebook
    first = await notes.write("note.md", "最初正文")
    version = _text(first, "version")
    assert version == hashlib.sha256("最初正文".encode()).hexdigest()
    with pytest.raises(NotebookError, match="不能覆盖"):
        await notes.write("note.md", "覆盖")
    second = await notes.write("note.md", "第二版", expected_version=version)
    assert second["created"] is False
    (fixture.root / "note.md").write_text("外部编辑", encoding="utf-8")
    with pytest.raises(NotebookError) as rejected:
        await notes.write("note.md", "过期覆盖", expected_version=_text(second, "version"))
    assert rejected.value.code == "version_conflict"
    assert (fixture.root / "note.md").read_text(encoding="utf-8") == "外部编辑"
    assert list(fixture.root.glob(".purrcept-*.tmp")) == []


async def test_move_delete_version_and_directory_constraints(fixture: _Fixture) -> None:
    notes = fixture.notebook
    first = await notes.write("note.md", "正文")
    version = _text(first, "version")
    assert (await notes.mkdir("folder/nested"))["created"] is True
    assert (await notes.mkdir("folder/nested"))["changed"] is False
    with pytest.raises(NotebookError):
        await notes.move("note.md", "folder/note.md")
    moved = await notes.move("note.md", "folder/note.md", expected_version=version)
    assert moved["version"] == version and not (fixture.root / "note.md").exists()
    await notes.write("other.md", "另篇")
    with pytest.raises(FileExistsError):
        await notes.move("folder/note.md", "other.md", expected_version=version)
    with pytest.raises(NotebookError):
        await notes.delete("folder/note.md", expected_version="wrong")
    with pytest.raises(OSError):
        await notes.delete("folder")
    with pytest.raises(NotebookError):
        await notes.delete(".")
    with pytest.raises(NotebookError):
        await notes.move("folder", "renamed", expected_version=version)
    await notes.delete("folder/note.md", expected_version=version)
    await notes.delete("folder/nested")
    await notes.move("folder", "renamed")
    await notes.delete("renamed")
    assert not (fixture.root / "renamed").exists()


@pytest.mark.parametrize(
    "path",
    [
        "../outside.md",
        "/absolute.md",
        "C:/outside.md",
        "CON.md",
        "a:stream.md",
        "trailing./note.md",
    ],
)
async def test_invalid_path_never_writes(fixture: _Fixture, path: str) -> None:
    with pytest.raises(NotebookError) as rejected:
        await fixture.notebook.write(path, "正文")
    assert rejected.value.code == "invalid_path"
    assert list(fixture.root.iterdir()) == []


async def test_snapshot_continuation_keeps_old_content_and_path_ownership(
    fixture: _Fixture,
) -> None:
    notes = fixture.notebook
    content = "原始正文0123456789abcdefghij"
    first = await notes.write("note.md", content)
    page = await notes.read("note.md")
    assert len(_text(page, "content")) == fixture.evidence.page_chars
    cursor = _text(page, "next_cursor")
    (fixture.root / "note.md").unlink()
    next_page = await notes.read("note.md", cursor=cursor)
    assert next_page["content"] == content[8:16] and next_page["version"] == first["version"]
    assert next_page["evidence_ref"] == page["evidence_ref"]
    assert (await notes.read("note.md", cursor=cursor))["slice_ref"] == next_page["slice_ref"]
    with pytest.raises(NotebookError) as rejected:
        await notes.read("other.md", cursor=cursor)
    assert rejected.value.code == "cursor_binding"
    foreign = Notebook(fixture.root, fixture.evidence, instance_id="other", seed_examples=False)
    with pytest.raises(NotebookError):
        await foreign.read("note.md", cursor=cursor)
    await foreign.close()
    longer = await notes.read("note.md", cursor=cursor, max_chars=9)
    assert longer["content"] == content[8:17] and longer["version"] == page["version"]


async def test_name_alias_and_frontmatter_page_only_return_current_fragment(
    fixture: _Fixture,
) -> None:
    notes = fixture.notebook
    await notes.mkdir("tea")
    content = "---\nname: tea\ndescription: 适用时\n---\n正文0123456789"
    await notes.write("tea/SKILL.md", content)
    first = await notes.read("tea", max_chars=40)
    assert first["path"] == "tea/SKILL.md" and first["name"] == "tea"
    assert first["body"] == content[content.index("正文") : 40]
    continuation = await notes.read("tea", cursor=_text(first, "next_cursor"), max_chars=40)
    assert continuation["body"] == content[40:]
    await notes.write("other.md", "---\nname: tea\n---\n另一篇")
    with pytest.raises(NotebookError) as rejected:
        await notes.read("tea")
    assert rejected.value.code == "file_missing"


async def test_each_request_refreshes_skills_and_reports_bad_files(fixture: _Fixture) -> None:
    notes = fixture.notebook
    await notes.write("skill.md", "---\nname: 喝茶\ndescription: 想喝茶时\n---\n秘密正文")
    await notes.write("ordinary.md", "普通笔记正文")
    first = await notes.prepare_request()
    assert first["incomplete"] is False
    skills = first["skills"]
    assert isinstance(skills, list) and len(skills) == 1
    assert "秘密正文" not in str(first) and "普通笔记正文" not in str(first)
    (fixture.root / "skill.md").write_text(
        "---\nname: 新名\ndescription: 新说明\n---\n新正文", encoding="utf-8"
    )
    (fixture.root / "bad.md").write_bytes(b"\xff")
    (fixture.root / "huge.md").write_bytes(b"x" * 65537)
    second = await notes.prepare_request()
    assert second["skills"] != first["skills"]
    assert "新名" in str(second["skills"]) and "新说明" in str(second["skills"])
    assert "skills_version" not in second
    assert second["incomplete"] is True
    issues = second["issues"]
    assert isinstance(issues, list)
    assert {str(item["code"]) for item in issues if isinstance(item, dict)} == {
        "invalid_encoding",
        "file_too_large",
    }
    with pytest.raises(NotebookError) as rejected:
        await notes.read("skill")
    assert rejected.value.code == "catalog_incomplete"


async def test_scan_budget_and_designated_skills_directory_are_explicit(fixture: _Fixture) -> None:
    await fixture.notebook.mkdir("skills")
    await fixture.notebook.write("skills/a.md", "---\ndescription: a时\n---\na")
    await fixture.notebook.write("outside.md", "---\ndescription: outside时\n---\nout")
    notes = Notebook(
        fixture.root,
        fixture.evidence,
        instance_id="limited",
        max_entries=1,
        skills_dir="skills",
        seed_examples=False,
    )
    await notes.start()
    try:
        prepared = await notes.prepare_request()
        skills = prepared["skills"]
        assert isinstance(skills, list) and len(skills) == 1
        assert isinstance(skills[0], dict) and skills[0]["path"] == "skills/a.md"
        tree = await notes.tree()
        assert tree["truncated"] is True and tree["issues"]
    finally:
        await notes.close()


async def test_seed_examples_are_original_and_never_replace_existing(fixture: _Fixture) -> None:
    original = "已有用户笔记"
    (fixture.root / "journal.md").write_text(original, encoding="utf-8")
    notes = Notebook(fixture.root, fixture.evidence, instance_id="seed")
    await notes.start()
    try:
        assert (fixture.root / "journal.md").read_text(encoding="utf-8") == original
        rest = (fixture.root / "how-to-rest.md").read_text(encoding="utf-8")
        assert "到点不会自动醒来。" in rest and "me.bed.rest(expected_duration_minutes=...)" in rest
        assert (await notes.prepare_request())["incomplete"] is False
    finally:
        await notes.close()


async def test_stable_read_detects_actual_edit_on_same_opened_file(
    fixture: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    await fixture.notebook.write("note.md", "old")
    original = os.fstat
    calls = 0

    def edit_before_second_stat(fd: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        if calls == 2:
            (fixture.root / "note.md").write_text("actual external edit", encoding="utf-8")
        return original(fd)

    monkeypatch.setattr(os, "fstat", edit_before_second_stat)
    with pytest.raises(NotebookError) as rejected:
        await fixture.notebook.read("note.md")
    assert rejected.value.code == "version_conflict"


async def test_final_replace_window_is_an_explicit_platform_limit(
    fixture: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """外部编辑恰在最终核验后替换，首版明确不能保证跨编辑器 CAS。"""
    written = await fixture.notebook.write("note.md", "old")
    original = os.path.samestat
    observed: list[str] = []

    def editor_after_last_stat(before: os.stat_result, after: os.stat_result) -> bool:
        same = original(before, after)
        target = fixture.root / "note.md"
        target.write_text("unobserved external edit", encoding="utf-8")
        observed.append(target.read_text(encoding="utf-8"))
        return same

    monkeypatch.setattr(os.path, "samestat", editor_after_last_stat)
    await fixture.notebook.write("note.md", "ours", expected_version=_text(written, "version"))
    assert observed == ["unobserved external edit"]
    assert (fixture.root / "note.md").read_text(encoding="utf-8") == "ours"


async def test_external_junction_is_rejected_without_reading_body(fixture: _Fixture) -> None:
    outside = fixture.root.parent / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("outside secret", encoding="utf-8")
    linked = fixture.root / "link"
    result = await asyncio.to_thread(
        subprocess.run,
        ["cmd.exe", "/c", "mklink", "/J", str(linked), str(outside)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    try:
        with pytest.raises(NotebookError) as rejected:
            await fixture.notebook.read("link/secret.md")
        assert rejected.value.code == "path_escape"
        assert (await fixture.notebook.prepare_request())["incomplete"] is True
    finally:
        linked.rmdir()


async def test_cancelled_file_waiter_keeps_thread_until_close(
    fixture: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()
    original = os.fsync

    def delayed_sync(fd: int) -> None:
        entered.set()
        assert release.wait(5)
        original(fd)

    monkeypatch.setattr(os, "fsync", delayed_sync)
    writing = asyncio.create_task(fixture.notebook.write("late.md", "实际线程正文"))
    assert await asyncio.to_thread(entered.wait, 5)
    writing.cancel()
    await asyncio.sleep(0)
    writing.cancel()
    closing = asyncio.create_task(fixture.notebook.close())
    try:
        await asyncio.sleep(0.01)
        assert not closing.done() and not writing.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await writing
    await closing
    assert (fixture.root / "late.md").read_text(encoding="utf-8") == "实际线程正文"
    with pytest.raises(HostStateError):
        await fixture.notebook.tree()


def _config(directory: Path) -> EngineConfig:
    return EngineConfig.validate(
        {
            "host": {"data_dir": directory},
            "console": False,
            "models": {
                "local": {
                    "kind": "generation",
                    "model": "openai/test",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "context_window": 10000,
                    "max_output_tokens": 1000,
                    "capacity_rule": "test-only",
                }
            },
            "subject": {"foreground": "local", "reflection": "local", "summary": "local"},
            "plugins": [
                {
                    "instance_id": "notes",
                    "entry": "fixture-notebook",
                    "config": {"notebook_dir": "notebook", "seed_examples": False},
                }
            ],
        },
        base_dir=directory,
    )


async def test_real_host_worker_notebook_call_and_persistent_evidence(tmp_path: Path) -> None:
    host = EngineHost(_config(tmp_path), plugin_factories={"fixture-notebook": factory})
    await host.start()
    try:
        action = await host.execute(
            "from world import me\n"
            "written = await me.notebook.write(path='note.md', content='实际原文')\n"
            "read = await me.notebook.read(path='note.md')\n"
            "return {'version': written.version, 'content': read.content, "
            "'evidence_ref': read.evidence_ref}"
        )
        assert action.state == "completed" and action.result_json is not None
        returned = decode_object(action.result_json.encode())
        assert returned["content"] == "实际原文"
        evidence_id = _text(returned, "evidence_ref")
        assert [call.path for call in action.calls] == ["me.notebook.write", "me.notebook.read"]
        assert all(call.state == "confirmed" for call in action.calls)
        conflict = await host.execute(
            "from world import me\n"
            "return await me.notebook.write(path='note.md', content='不准覆盖')"
        )
        assert conflict.calls[0].state == "failed"
        assert (tmp_path / "notebook/note.md").read_text(encoding="utf-8") == "实际原文"
        target = tmp_path / "notebook/note.md"
        target.chmod(stat.S_IREAD)
        try:
            uncertain = await host.execute(
                "from world import me\n"
                "return await me.notebook.write(path='note.md', content='待恢复新文', "
                f"expected_version='{returned['version']}')"
            )
            receipt = uncertain.calls[0]
            assert receipt.state == "unknown" and receipt.error_type == "PermissionError"
            assert receipt.result_json is not None
            recovery = decode_object(receipt.result_json.encode())
            assert recovery["code"] == "publication_unknown" and recovery["native_code"] == 5
            observed, staged = recovery["target"], recovery["staged"]
            assert isinstance(observed, dict) and observed["version"] == returned["version"]
            assert isinstance(staged, dict) and staged["state"] == "stable"
            staged_path = _text(staged, "path")
            assert Path(staged_path).is_relative_to(tmp_path / "notebook")
            assert (
                await asyncio.to_thread(Path(staged_path).read_text, encoding="utf-8")
                == "待恢复新文"
            )
        finally:
            target.chmod(stat.S_IWRITE)
    finally:
        await host.close()
    with sqlite3.connect(tmp_path / "engine.sqlite3") as connection:
        row = connection.execute(
            "SELECT payload_json FROM knowledge_evidence WHERE evidence_id=?", (evidence_id,)
        ).fetchone()
        assert row is not None and "实际原文" in str(row[0])
