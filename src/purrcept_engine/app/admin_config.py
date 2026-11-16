"""真后台模式（True Admin Mode）配置文件读写、脱敏与目标插件路径解析。"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, TypeGuard

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tomlkit.exceptions import ParseError
from tomlkit.items import AoT, Table

from purrcept_engine.app.config import EngineConfig
from purrcept_engine.app.onboarding import PLUGIN_GUIDES
from purrcept_engine.errors import ConfigError
from purrcept_engine.infrastructure.locking import InstanceLock
from purrcept_engine.plugins.live2d.models import ModelProfile
from purrcept_engine.values import JsonObject, JsonValue

_MASKED_SECRET = "********"
_ENV_PATTERN = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}")


def is_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


def is_str_dict(value: object) -> TypeGuard[Mapping[str, object]]:
    if not is_mapping(value):
        return False
    return all(isinstance(k, str) for k in value)


def is_obj_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


class CharacterPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    name: str = ""
    one_line: str = ""
    persona: str = ""
    relationship: str = ""
    speech_style: str = ""
    notes: str = ""


class GeneralPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    data_dir: str = Field(min_length=1, max_length=512)
    console: bool = True
    web_enabled: bool = True
    web_port: int = Field(ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class SubjectPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    foreground: str = Field(min_length=1, max_length=64)
    attention: str = ""
    reflection: str = ""
    proactive_thinking: bool = False
    proactive_idle_seconds: float = Field(default=300.0, gt=0, allow_inf_nan=False)
    behavior_review_enabled: bool = True
    character: CharacterPayload = Field(default_factory=CharacterPayload)


class ModelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    kind: Literal["generation", "decision"] = "generation"
    model: str = Field(min_length=1, max_length=256)
    api_base: str = Field(min_length=1, max_length=512)
    api_key: str = ""
    keep_existing_key: bool = True
    context_window: int = Field(default=64000, gt=0)
    max_output_tokens: int = Field(default=4096, gt=0)
    input_limit: int = Field(default=16000, gt=0)


class PluginPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    instance_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    entry: str = Field(min_length=1, max_length=256)
    enabled: bool = True
    config: JsonObject = Field(default_factory=lambda: dict[str, JsonValue]())


class VisualSave(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    mode: Literal["visual"]
    revision: str = Field(min_length=1, max_length=128)
    general: GeneralPayload
    subject: SubjectPayload
    models: list[ModelPayload] = Field(min_length=1, max_length=32)
    plugins: list[PluginPayload] = Field(
        default_factory=lambda: list[PluginPayload](), max_length=64
    )


class RawSave(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    mode: Literal["raw"]
    revision: str = Field(min_length=1, max_length=128)
    raw_toml: str = Field(min_length=1, max_length=200_000)


def _mask_key_hint(raw_key: str) -> tuple[str, str | None, bool]:
    stripped = raw_key.strip()
    if not stripped:
        return "免密 / 未填写", None, False
    if _ENV_PATTERN.search(stripped):
        return f"环境变量：{stripped}", stripped, True
    if len(stripped) <= 6:
        return "已设置（已隐藏）", None, True
    return f"{stripped[:3]}••••{stripped[-3:]}", None, True


def _table_child(container: tomlkit.TOMLDocument | Table, key: str) -> Table | None:
    if key not in container:
        return None
    item = container.item(key)
    return item if isinstance(item, Table) else None


def _ensure_table(container: tomlkit.TOMLDocument | Table, key: str) -> Table:
    existing = _table_child(container, key)
    if existing is not None:
        return existing
    created = tomlkit.table()
    container[key] = created
    return created


def _mask_document_secrets(text: str) -> str:
    """将源码视图中的明文 api_key 替换为掩码，保留注释与 ${ENV_VAR} 占位符不变。"""
    try:
        doc = tomlkit.parse(text)
        raw_parsed: object = tomllib.loads(text)
    except (ParseError, tomllib.TOMLDecodeError):
        return text
    raw_models = raw_parsed.get("models") if is_str_dict(raw_parsed) else None
    models = _table_child(doc, "models")
    if models is not None and is_str_dict(raw_models):
        for name in raw_models:
            entry = _table_child(models, name)
            if entry is not None and "api_key" in entry:
                raw_item = entry.item("api_key").unwrap()
                if (
                    isinstance(raw_item, str)
                    and raw_item.strip()
                    and not _ENV_PATTERN.search(raw_item)
                ):
                    entry["api_key"] = _MASKED_SECRET
    return tomlkit.dumps(doc)


def _restore_masked_secrets(proposed_text: str, original_raw: Mapping[str, object]) -> str:
    """在源码保存模式下，将未改动的掩码还原为磁盘上的原明文密钥。"""
    doc = tomlkit.parse(proposed_text)
    proposed_raw: object = tomllib.loads(proposed_text)
    proposed_models = proposed_raw.get("models") if is_str_dict(proposed_raw) else None
    orig_models = original_raw.get("models")
    models = _table_child(doc, "models")
    if models is not None and is_str_dict(proposed_models) and is_str_dict(orig_models):
        for name in proposed_models:
            entry = _table_child(models, name)
            orig_entry = orig_models.get(name)
            if (
                entry is not None
                and "api_key" in entry
                and is_str_dict(orig_entry)
                and entry.item("api_key").unwrap() == _MASKED_SECRET
            ):
                orig_key = orig_entry.get("api_key")
                if isinstance(orig_key, str):
                    entry["api_key"] = orig_key
    return tomlkit.dumps(doc)


def _to_json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if is_obj_list(value):
        return [_to_json_value(item) for item in value]
    if is_str_dict(value):
        return {k: _to_json_value(v) for k, v in value.items()}
    return str(value)


def read_config_view(config_path: Path | None) -> JsonObject:
    """读取并生成供 WebUI 双模配置编辑器使用的脱敏视图。"""
    if config_path is None or not config_path.is_file():
        return {
            "available": False,
            "message": "当前以嵌入式模式运行，未绑定 purrcept.toml 配置文件。",
        }
    raw_bytes = config_path.read_bytes()
    revision = hashlib.sha256(raw_bytes).hexdigest()
    text = raw_bytes.decode("utf-8-sig")
    raw_loaded: object = tomllib.loads(text)
    raw_data: Mapping[str, object] = raw_loaded if is_str_dict(raw_loaded) else {}

    validation_error: str | None = None
    try:
        EngineConfig.validate(raw_data, base_dir=config_path.parent)
    except ConfigError as error:
        validation_error = str(error)

    host_sec = raw_data.get("host")
    data_dir = (
        str(host_sec.get("data_dir", ".purrcept-engine"))
        if is_str_dict(host_sec)
        else ".purrcept-engine"
    )
    console_val = raw_data.get("console")
    console_enabled = console_val if isinstance(console_val, bool) else True

    web_sec = raw_data.get("web")
    web_enabled = bool(web_sec.get("enabled", False)) if is_str_dict(web_sec) else False
    web_port_raw = web_sec.get("port", 8765) if is_str_dict(web_sec) else 8765
    web_port = web_port_raw if isinstance(web_port_raw, int) else 8765

    log_sec = raw_data.get("logging")
    log_level_raw = log_sec.get("level", "INFO") if is_str_dict(log_sec) else "INFO"
    log_level = (
        log_level_raw if log_level_raw in ("DEBUG", "INFO", "WARNING", "ERROR") else "INFO"
    )

    subj_sec = raw_data.get("subject")
    subj_dict: Mapping[str, object] = subj_sec if is_str_dict(subj_sec) else {}
    char_sec = subj_dict.get("character")
    char_dict: Mapping[str, object] = char_sec if is_str_dict(char_sec) else {}
    idle_raw = subj_dict.get("proactive_idle_seconds", 300.0)
    idle_seconds = float(idle_raw) if isinstance(idle_raw, (int, float)) else 300.0
    review_en_raw = subj_dict.get("behavior_review_enabled", True)
    review_enabled = review_en_raw if isinstance(review_en_raw, bool) else True

    models_out: list[JsonValue] = []
    models_sec = raw_data.get("models")
    if is_str_dict(models_sec):
        for name, item in models_sec.items():
            if not is_str_dict(item):
                continue
            raw_key = str(item.get("api_key", ""))
            hint, env_expr, has_key = _mask_key_hint(raw_key)
            cw_raw = item.get("context_window", 64000)
            mot_raw = item.get("max_output_tokens", 4096)
            il_raw = item.get("input_limit", 16000)
            models_out.append(
                {
                    "name": name,
                    "kind": "decision" if item.get("kind") == "decision" else "generation",
                    "model": str(item.get("model", "")),
                    "api_base": str(item.get("api_base", "")),
                    "api_key_hint": hint,
                    "env_expr": env_expr,
                    "has_api_key": has_key,
                    "context_window": cw_raw if isinstance(cw_raw, int) else 64000,
                    "max_output_tokens": mot_raw if isinstance(mot_raw, int) else 4096,
                    "input_limit": il_raw if isinstance(il_raw, int) else 16000,
                }
            )

    plugins_out: list[JsonValue] = []
    plugins_sec = raw_data.get("plugins")
    if is_obj_list(plugins_sec):
        for p in plugins_sec:
            if not is_str_dict(p):
                continue
            cfg_raw = p.get("config")
            cfg_obj: JsonObject = (
                {k: _to_json_value(v) for k, v in cfg_raw.items()}
                if is_str_dict(cfg_raw)
                else {}
            )
            plugins_out.append(
                {
                    "instance_id": str(p.get("instance_id", "")),
                    "entry": str(p.get("entry", "")),
                    "enabled": bool(p.get("enabled", True)),
                    "config": cfg_obj,
                }
            )

    catalog_out: list[JsonValue] = []
    for guide in PLUGIN_GUIDES:
        snippet_cfg: JsonObject = {}
        try:
            parsed_snippet: object = tomllib.loads(guide.toml_snippet)
            arr = parsed_snippet.get("plugins") if is_str_dict(parsed_snippet) else None
            if is_obj_list(arr) and arr and is_str_dict(arr[0]):
                first = arr[0]
                raw_c = first.get("config")
                if is_str_dict(raw_c):
                    snippet_cfg = {k: _to_json_value(v) for k, v in raw_c.items()}
        except tomllib.TOMLDecodeError:
            snippet_cfg = {}
        catalog_out.append(
            {
                "name": guide.entry,
                "summary": guide.summary,
                "default_config": snippet_cfg,
                "snippet": guide.toml_snippet,
            }
        )

    fg_str = str(subj_dict.get("foreground", ""))
    refl_str = str(subj_dict.get("reflection", fg_str))
    return {
        "available": True,
        "config_name": config_path.name,
        "revision": revision,
        "validation_error": validation_error,
        "general": {
            "data_dir": data_dir,
            "console": console_enabled,
            "web_enabled": web_enabled,
            "web_port": web_port,
            "log_level": log_level,
        },
        "subject": {
            "foreground": fg_str,
            "attention": str(subj_dict.get("attention", "")),
            "reflection": refl_str,
            "proactive_thinking": bool(subj_dict.get("proactive_thinking", False)),
            "proactive_idle_seconds": idle_seconds,
            "behavior_review_enabled": review_enabled,
            "character": {
                "name": str(char_dict.get("name", "") or ""),
                "one_line": str(char_dict.get("one_line", "") or ""),
                "persona": str(char_dict.get("persona", "") or ""),
                "relationship": str(char_dict.get("relationship", "") or ""),
                "speech_style": str(char_dict.get("speech_style", "") or ""),
                "notes": str(char_dict.get("notes", "") or ""),
            },
        },
        "models": models_out,
        "plugins": plugins_out,
        "plugin_catalog": catalog_out,
        "raw_toml": _mask_document_secrets(text),
    }


def _populate_toml_dict(target: Table, source: JsonObject) -> None:
    for key, val in source.items():
        if val is None:
            continue
        if isinstance(val, dict):
            sub = tomlkit.table()
            _populate_toml_dict(sub, val)
            target[key] = sub
        else:
            target[key] = val


def _apply_visual_update(original_text: str, payload: VisualSave) -> str:
    """在保留原有 TOML 注释的前提下更新核心配置段、模型与插件表。"""
    doc = tomlkit.parse(original_text)
    loaded_orig: object = tomllib.loads(original_text)
    original_raw: Mapping[str, object] = loaded_orig if is_str_dict(loaded_orig) else {}
    orig_models_raw = original_raw.get("models")
    orig_models: Mapping[str, object] = (
        orig_models_raw if is_str_dict(orig_models_raw) else {}
    )

    doc["console"] = payload.general.console

    host_tbl = _ensure_table(doc, "host")
    host_tbl["data_dir"] = payload.general.data_dir

    web_tbl = _ensure_table(doc, "web")
    web_tbl["enabled"] = payload.general.web_enabled
    web_tbl["port"] = payload.general.web_port

    log_tbl = _ensure_table(doc, "logging")
    log_tbl["level"] = payload.general.log_level

    models_tbl = _ensure_table(doc, "models")
    new_names = {m.name for m in payload.models}
    for existing in orig_models:
        if existing not in new_names and existing in models_tbl:
            del models_tbl[existing]
    for m in payload.models:
        entry = _ensure_table(models_tbl, m.name)
        entry["kind"] = m.kind
        entry["model"] = m.model
        entry["api_base"] = m.api_base
        if not m.keep_existing_key or m.api_key.strip():
            entry["api_key"] = m.api_key
        else:
            prev = orig_models.get(m.name)
            prev_key = prev.get("api_key") if is_str_dict(prev) else None
            if isinstance(prev_key, str):
                entry["api_key"] = prev_key
            elif "api_key" not in entry:
                entry["api_key"] = ""
        if m.kind == "generation":
            entry["context_window"] = m.context_window
            entry["max_output_tokens"] = m.max_output_tokens
        else:
            for extra_key in ("context_window", "max_output_tokens"):
                if extra_key in entry:
                    del entry[extra_key]
            entry["input_limit"] = m.input_limit

    subj_tbl = _ensure_table(doc, "subject")
    subj_tbl["foreground"] = payload.subject.foreground
    subj_tbl["reflection"] = payload.subject.reflection or payload.subject.foreground
    att_val = payload.subject.attention.strip()
    if att_val:
        subj_tbl["attention"] = att_val
    elif "attention" in subj_tbl:
        del subj_tbl["attention"]
    subj_tbl["proactive_thinking"] = payload.subject.proactive_thinking
    subj_tbl["proactive_idle_seconds"] = payload.subject.proactive_idle_seconds
    subj_tbl["behavior_review_enabled"] = payload.subject.behavior_review_enabled

    char_tbl = _ensure_table(subj_tbl, "character")
    char_tbl["name"] = payload.subject.character.name
    char_tbl["one_line"] = payload.subject.character.one_line
    char_tbl["persona"] = payload.subject.character.persona
    char_tbl["relationship"] = payload.subject.character.relationship
    char_tbl["speech_style"] = payload.subject.character.speech_style
    char_tbl["notes"] = payload.subject.character.notes

    if payload.plugins:
        aot = tomlkit.aot()
        for p in payload.plugins:
            pt = tomlkit.table()
            pt["instance_id"] = p.instance_id
            pt["entry"] = p.entry
            pt["enabled"] = p.enabled
            if p.config:
                ct = tomlkit.table()
                _populate_toml_dict(ct, p.config)
                pt["config"] = ct
            aot.insert(len(aot), pt)
        if "plugins" in doc:
            del doc["plugins"]
        doc["plugins"] = aot
    elif "plugins" in doc:
        existing_plugins = doc.item("plugins")
        if isinstance(existing_plugins, AoT) or is_obj_list(existing_plugins.unwrap()):
            del doc["plugins"]

    return tomlkit.dumps(doc)


def save_config_file(
    config_path: Path | None,
    payload: VisualSave | RawSave,
    *,
    owner_data_dir: Path | None,
) -> JsonObject:
    """校验并原子写回 purrcept.toml，自动滚动保留最近 5 份配置备份。"""
    if config_path is None or not config_path.is_file():
        raise ConfigError("当前未绑定可写入的 purrcept.toml 配置文件。")
    resolved_path = config_path.resolve()
    original_bytes = resolved_path.read_bytes()
    current_rev = hashlib.sha256(original_bytes).hexdigest()
    if payload.revision != current_rev:
        raise ConfigError("配置文件已在磁盘上被其他操作修改，请点击重新读取后再保存。")

    original_text = original_bytes.decode("utf-8-sig")
    loaded_orig: object = tomllib.loads(original_text)
    original_raw: Mapping[str, object] = loaded_orig if is_str_dict(loaded_orig) else {}
    if isinstance(payload, VisualSave):
        proposed = _apply_visual_update(original_text, payload)
    else:
        proposed = _restore_masked_secrets(payload.raw_toml, original_raw)

    parsed_proposed: object = tomllib.loads(proposed)
    validated = EngineConfig.validate(parsed_proposed, base_dir=resolved_path.parent)

    if owner_data_dir is not None and owner_data_dir.resolve() != validated.host.data_dir.resolve():
        raise ConfigError("运行期间不允许更改正在占用的 host.data_dir 目录。")

    lock = InstanceLock(validated.host.data_dir) if owner_data_dir is None else None
    if lock is not None:
        lock.acquire()
    try:
        if resolved_path.read_bytes() != original_bytes:
            raise ConfigError("保存期间配置文件被外部修改，已放弃覆盖。")
        backup_dir = validated.host.data_dir / "config-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_file = backup_dir / f"{resolved_path.name}.{current_rev[:10]}.bak"
        if not backup_file.exists():
            backup_file.write_bytes(original_bytes)
        backups = sorted(
            backup_dir.glob(f"{resolved_path.name}.*.bak"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for stale in backups[5:]:
            stale.unlink(missing_ok=True)

        fd, temp_name = tempfile.mkstemp(prefix=".purrcept-cfg-", dir=resolved_path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(proposed.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, resolved_path)
        finally:
            Path(temp_name).unlink(missing_ok=True)
    finally:
        if lock is not None:
            lock.release()

    updated = read_config_view(resolved_path)
    return {
        **updated,
        "saved": True,
        "restart_required": True,
        "backup_name": backup_file.name,
    }


def resolve_studio_target(config_path: Path | None) -> tuple[Path, int, str]:
    """解析调音室预设路径、采样率与 ffmpeg 程序；未配置时回退默认路径。"""
    if config_path is None or not config_path.is_file():
        return Path(".purrcept-engine/voice-profile.json").resolve(), 32000, "ffmpeg"
    base = config_path.resolve().parent
    try:
        loaded: object = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError):
        loaded = {}
    raw: Mapping[str, object] = loaded if is_str_dict(loaded) else {}
    plugins = raw.get("plugins")
    if is_obj_list(plugins):
        for p in plugins:
            if not is_str_dict(p) or p.get("entry") != "microphone":
                continue
            cfg = p.get("config")
            if not is_str_dict(cfg):
                continue
            prof = str(cfg.get("postprocess_profile", "")).strip()
            rate_raw = cfg.get("sample_rate", 32000)
            rate = (
                rate_raw
                if isinstance(rate_raw, int)
                and rate_raw in {16000, 24000, 32000, 44100, 48000}
                else 32000
            )
            exe = str(cfg.get("processing_executable", "ffmpeg")).strip() or "ffmpeg"
            target = (base / (prof or ".purrcept-engine/voice-profile.json")).resolve()
            return target, rate, exe
    return (base / ".purrcept-engine/voice-profile.json").resolve(), 32000, "ffmpeg"


def resolve_live2d_target(config_path: Path | None) -> tuple[Path, str] | None:
    """解析已启用或声明的 Live2D 模型校准文件与偏好文件路径。"""
    if config_path is None or not config_path.is_file():
        return None
    base = config_path.resolve().parent
    try:
        loaded: object = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    raw: Mapping[str, object] = loaded if is_str_dict(loaded) else {}
    plugins = raw.get("plugins")
    if not is_obj_list(plugins):
        return None
    for p in plugins:
        if not is_str_dict(p) or p.get("entry") != "live2d":
            continue
        cfg = p.get("config")
        if not is_str_dict(cfg):
            continue
        prof_rel = str(cfg.get("profile_path", "")).strip()
        if not prof_rel:
            continue
        prof_path = (base / prof_rel).resolve()
        if not prof_path.is_file():
            continue
        try:
            profile = ModelProfile.model_validate_json(prof_path.read_bytes())
        except (OSError, ValidationError, ValueError):
            continue
        pref_rel = str(cfg.get("preferences_path", "")).strip()
        pref_path = (
            (base / pref_rel).resolve()
            if pref_rel
            else prof_path.with_suffix(".preferences.json")
        )
        return pref_path, profile.model_id
    return None
