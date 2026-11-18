"""为后台列举、读取和编辑 Markdown 手账，文件身份限制在配置的笔记目录内。

编辑使用读取时的内容哈希，新建要求目标不存在。列表只生成预览，检索与索引归笔记插件。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from purrcept_engine.app.admin_config import is_obj_list, is_str_dict
from purrcept_engine.plugins.skills import note_preview, parse_note
from purrcept_engine.values import JsonObject, JsonValue

_NOTE_SUFFIXES = frozenset({".md", ".markdown"})
_MAX_NOTE_BYTES = 256 * 1024


class NoteSave(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    instance: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=256)
    content: str = Field(max_length=200_000)
    revision: str | None = Field(default=None, max_length=128)


def discover_notebooks(config_path: Path | None) -> list[tuple[str, Path]]:
    """从 purrcept.toml 解析已配置的 Notebook 目录；未启用时检查默认 notes/。"""
    if config_path is None or not config_path.is_file():
        return []
    base = config_path.resolve().parent
    found: list[tuple[str, Path]] = []
    try:
        loaded: object = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError):
        loaded = {}
    raw: Mapping[str, object] = loaded if is_str_dict(loaded) else {}
    plugins = raw.get("plugins")
    if is_obj_list(plugins):
        for p in plugins:
            if not is_str_dict(p) or p.get("entry") != "notebook":
                continue
            iid = str(p.get("instance_id", "notebook"))
            cfg = p.get("config")
            nb_dir = str(cfg.get("notebook_dir", "notes")) if is_str_dict(cfg) else "notes"
            resolved = (base / nb_dir).resolve()
            found.append((iid, resolved))
    if not found:
        default_notes = (base / "notes").resolve()
        found.append(("notes", default_notes))
    return found


def _safe_note_path(root: Path, rel_path: str) -> Path:
    cleaned = rel_path.strip().replace("\\", "/").lstrip("/")
    if not cleaned or ".." in cleaned.split("/"):
        raise ValueError("笔记路径不合法。")
    candidate = (root / cleaned).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as err:
        raise ValueError("笔记路径不能越出笔记本根目录。") from err
    if candidate.suffix.lower() not in _NOTE_SUFFIXES:
        raise ValueError("仅支持读写 .md 或 .markdown 笔记文件。")
    return candidate


def list_notebooks_sync(config_path: Path | None) -> JsonObject:
    """列出所有已发现 Notebook 实例下的笔记与技能摘要。"""
    roots = discover_notebooks(config_path)
    if not roots:
        return {"available": False, "instances": []}
    instances: list[JsonValue] = []
    for iid, root in roots:
        files_out: list[tuple[float, JsonObject]] = []
        if root.is_dir():
            for child in root.rglob("*"):
                if (
                    not child.is_file()
                    or child.suffix.lower() not in _NOTE_SUFFIXES
                    or any(part.startswith(".") for part in child.relative_to(root).parts)
                ):
                    continue
                try:
                    rel = child.relative_to(root).as_posix()
                    target = _safe_note_path(root, rel)
                    stat = target.stat()
                    if stat.st_size > _MAX_NOTE_BYTES:
                        continue
                    raw_text = target.read_text(encoding="utf-8")
                except (OSError, ValueError):
                    # 枚举期间文件可能消失；越界链接与不可读文件都不产生预览。
                    continue
                top_folder = rel.split("/", 1)[0] if "/" in rel else ""
                parsed = parse_note(
                    rel,
                    raw_text,
                    version="workspace",
                    byte_count=stat.st_size,
                    occurred_at=stat.st_mtime,
                )
                category = (
                    "skills"
                    if parsed.description is not None
                    else "journal"
                    if top_folder == "journal"
                    else "memory"
                    if top_folder == "memory"
                    else "notes"
                )
                title = parsed.name or child.stem
                preview = note_preview(parsed, max_chars=100)
                files_out.append(
                    (
                        stat.st_mtime,
                        {
                            "path": rel,
                            "category": category,
                            "title": title,
                            "description": parsed.description or preview,
                            "size_bytes": stat.st_size,
                            "updated_at": stat.st_mtime,
                        },
                    )
                )
        files_out.sort(key=lambda pair: pair[0], reverse=True)
        instances.append(
            {
                "instance_id": iid,
                "root_dir": root.name,
                "exists": root.is_dir(),
                "files": [entry for _, entry in files_out[:200]],
            }
        )
    return {"available": True, "instances": instances}


def read_note_sync(config_path: Path | None, instance: str, rel_path: str) -> JsonObject:
    """读取单篇笔记全文与乐观锁哈希版本。"""
    roots = dict(discover_notebooks(config_path))
    root = roots.get(instance)
    if root is None:
        raise FileNotFoundError("未找到对应的笔记本实例。")
    target = _safe_note_path(root, rel_path)
    raw = target.read_bytes()
    if len(raw) > _MAX_NOTE_BYTES:
        raise ValueError("笔记超过 256 KiB 编辑上限。")
    stat = target.stat()
    return {
        "instance": instance,
        "path": target.relative_to(root.resolve()).as_posix(),
        "content": raw.decode("utf-8"),
        "revision": hashlib.sha256(raw).hexdigest(),
        "updated_at": stat.st_mtime,
    }


def save_note_sync(config_path: Path | None, payload: NoteSave) -> JsonObject:
    """校验乐观锁版本后原子保存单篇笔记或技能文件。"""
    roots = dict(discover_notebooks(config_path))
    root = roots.get(payload.instance)
    if root is None:
        raise FileNotFoundError("未找到对应的笔记本实例。")
    root.mkdir(parents=True, exist_ok=True)
    target = _safe_note_path(root, payload.path)
    creating = payload.revision in (None, "", "missing")
    if not creating:
        if not target.is_file():
            raise ValueError("原笔记文件已不存在，请刷新列表。")
        existing = target.read_bytes()
        existing_rev = hashlib.sha256(existing).hexdigest()
        if payload.revision != existing_rev:
            raise ValueError("笔记已在后台被主体或外部编辑器更新，请重新读取后再保存。")

    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = payload.content.encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=".note-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if creating:
            # 同目录硬链接原子发布完整文件，同时拒绝覆盖并发创建的同名笔记。
            try:
                os.link(temp_name, target)
            except FileExistsError as error:
                raise ValueError("同名笔记已存在，请更换新笔记的路径或打开原文编辑。") from error
        else:
            os.replace(temp_name, target)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    stat = target.stat()
    return {
        "saved": True,
        "instance": payload.instance,
        "path": target.relative_to(root.resolve()).as_posix(),
        "revision": hashlib.sha256(encoded).hexdigest(),
        "updated_at": stat.st_mtime,
    }
