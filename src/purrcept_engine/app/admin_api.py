"""真后台模式（True Admin Mode）本机管理 HTTP 路由装配。

所有路由挂在主 WebServer 的 LocalHttpBoundary 之内（仅限 127.0.0.1 / localhost，
非 GET 请求强制校验 X-Purrcept-Token），将请求分发至：
1. /api/chat：网页对话与刺激测试台
2. /api/config：purrcept.toml 可视化与源码双模编辑（admin_config.py）
3. /api/notebooks*：Notebook 记忆与技能 Markdown 管理（admin_notebooks.py）
4. /api/studio/* & /api/live2d/preferences：内嵌声线调音室与 Live2D 热更新
"""

from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from tomlkit.exceptions import ParseError

from purrcept_engine.app.admin_config import (
    RawSave,
    VisualSave,
    read_config_view,
    resolve_live2d_target,
    resolve_studio_target,
    save_config_file,
)
from purrcept_engine.app.admin_notebooks import (
    NoteSave,
    list_notebooks_sync,
    read_note_sync,
    save_note_sync,
)
from purrcept_engine.app.studio.server import Studio
from purrcept_engine.errors import ConfigError, StorageError
from purrcept_engine.plugins.live2d.preferences import (
    PreferenceConflict,
    Preferences,
    load_preferences,
    save_preferences,
)
from purrcept_engine.values import ActionProtocolError, JsonObject, decode_object

_MAX_JSON_BODY = 256 * 1024


@runtime_checkable
class ChatAdminHost(Protocol):
    """支持网页对话收发的宿主扩展协议。"""

    async def query_chat(self, *, limit: int = 40) -> JsonObject: ...

    async def submit_chat(self, text: str, *, sender_name: str = "Web 管理员") -> JsonObject: ...


class _ChatPost(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    text: str = Field(min_length=1, max_length=8000)
    sender_name: str = Field(default="Web 管理员", min_length=1, max_length=64)


class _Live2DSave(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    revision: str = Field(min_length=1, max_length=128)
    preferences: Preferences


def _json(value: JsonObject, status: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})


async def _read_json(request: Request, *, max_bytes: int = _MAX_JSON_BODY) -> JsonObject:
    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > max_bytes:
            raise ValueError("payload_too_large")
        payload.extend(chunk)
    return decode_object(bytes(payload))


def admin_routes(
    host: object,
    *,
    token: str,
    config_path: Path | None,
    owner_data_dir: Path | None,
) -> list[Route]:
    """构造挂载在统一本机来源边界下的真后台管理路由。"""
    config_lock = asyncio.Lock()
    note_lock = asyncio.Lock()
    live2d_lock = asyncio.Lock()

    voice_path, sample_rate, ffmpeg_exe = resolve_studio_target(config_path)
    studio = Studio(voice_path, sample_rate, ffmpeg_exe, token)

    async def chat_endpoint(request: Request) -> Response:
        if not isinstance(host, ChatAdminHost):
            return _json(
                {"error": "chat_unavailable", "message": "当前宿主实例未开放对话调试入口。"},
                503,
            )
        if request.method == "GET":
            limit_param = request.query_params.get("limit", "40")
            limit = int(limit_param) if limit_param.isdigit() else 40
            return _json(await host.query_chat(limit=limit))
        try:
            body = _ChatPost.model_validate(await _read_json(request, max_bytes=32 * 1024))
        except (ActionProtocolError, ValidationError):
            return _json({"error": "chat_invalid", "message": "请填写非空的聊天消息。"}, 400)
        except ValueError as err:
            status = 413 if str(err) == "payload_too_large" else 400
            return _json({"error": "chat_invalid", "message": "消息内容无效或超过长度上限。"}, status)
        if not body.text.strip():
            return _json({"error": "chat_empty", "message": "消息内容不能为空白。"}, 400)
        try:
            receipt = await host.submit_chat(body.text, sender_name=body.sender_name.strip())
        except StorageError as err:
            return _json({"error": err.code, "message": str(err)}, 409)
        return _json(receipt, 202)

    async def config_endpoint(request: Request) -> Response:
        if config_path is None:
            return _json(
                {
                    "available": False,
                    "message": "当前以嵌入式模式运行，未绑定 purrcept.toml 配置文件。",
                }
            )
        if request.method == "GET":
            try:
                view = await asyncio.to_thread(read_config_view, config_path)
                return _json(view)
            except (OSError, UnicodeError, tomllib.TOMLDecodeError) as err:
                return _json({"error": "config_read_failed", "message": str(err)}, 409)

        try:
            raw_payload = await _read_json(request, max_bytes=_MAX_JSON_BODY)
            mode = raw_payload.get("mode")
            parsed_save: VisualSave | RawSave = (
                RawSave.model_validate(raw_payload)
                if mode == "raw"
                else VisualSave.model_validate(raw_payload)
            )
        except (ActionProtocolError, ValidationError) as err:
            issues = (
                "; ".join(
                    ".".join(str(p) for p in item["loc"])
                    for item in err.errors(include_input=False, include_url=False)
                )
                if isinstance(err, ValidationError)
                else "json"
            )
            return _json(
                {"error": "config_invalid", "message": f"配置字段格式有误：{issues}"},
                400,
            )
        except ValueError as err:
            status = 413 if str(err) == "payload_too_large" else 400
            return _json({"error": "config_invalid", "message": "配置提交内容过大或格式无效。"}, status)

        async with config_lock:
            try:
                saved = await asyncio.to_thread(
                    save_config_file,
                    config_path,
                    parsed_save,
                    owner_data_dir=owner_data_dir,
                )
                new_voice, new_rate, new_exe = await asyncio.to_thread(
                    resolve_studio_target, config_path
                )
                studio.path, studio.rate, studio.executable = new_voice, new_rate, new_exe
                return _json(saved)
            except (ConfigError, ParseError, tomllib.TOMLDecodeError) as err:
                return _json({"error": "config_rejected", "message": str(err)}, 409)
            except OSError as err:
                return _json({"error": "config_io", "message": f"配置写入失败：{err}"}, 500)

    async def notebooks_list(request: Request) -> Response:
        del request
        return _json(await asyncio.to_thread(list_notebooks_sync, config_path))

    async def notebooks_file(request: Request) -> Response:
        if request.method == "GET":
            instance = request.query_params.get("instance", "").strip()
            rel_path = request.query_params.get("path", "").strip()
            if not instance or not rel_path:
                return _json({"error": "note_invalid", "message": "缺少笔记实例或路径参数。"}, 400)
            try:
                note = await asyncio.to_thread(read_note_sync, config_path, instance, rel_path)
                return _json(note)
            except FileNotFoundError as err:
                return _json({"error": "note_missing", "message": str(err)}, 404)
            except (ValueError, UnicodeError, OSError) as err:
                return _json({"error": "note_invalid", "message": str(err)}, 400)

        try:
            body = NoteSave.model_validate(await _read_json(request, max_bytes=_MAX_JSON_BODY))
        except ValueError:
            return _json({"error": "note_invalid", "message": "笔记保存参数无效或超过上限。"}, 400)
        async with note_lock:
            try:
                saved = await asyncio.to_thread(save_note_sync, config_path, body)
                return _json(saved)
            except FileNotFoundError as err:
                return _json({"error": "note_missing", "message": str(err)}, 404)
            except (ValueError, OSError) as err:
                return _json({"error": "note_conflict", "message": str(err)}, 409)

    async def studio_status(request: Request) -> Response:
        del request
        live2d_target = await asyncio.to_thread(resolve_live2d_target, config_path)
        return _json(
            {
                "voice": {
                    "profile_name": studio.path.name,
                    "sample_rate": studio.rate,
                    "clip_id": studio.clip or None,
                    "duration": len(studio.pcm) / 2 / studio.rate if studio.rate else 0.0,
                },
                "live2d_available": live2d_target is not None,
            }
        )

    async def live2d_endpoint(request: Request) -> Response:
        target = await asyncio.to_thread(resolve_live2d_target, config_path)
        if target is None:
            return _json(
                {
                    "available": False,
                    "message": "尚未在 purrcept.toml 配置 Live2D 插件或其模型校准文件不存在。",
                }
            )
        pref_path, model_id = target
        if request.method == "GET":
            try:
                prefs, rev = await asyncio.to_thread(load_preferences, pref_path, model_id)
                return _json(
                    {
                        "available": True,
                        "model_id": model_id,
                        "preferences": decode_object(prefs.model_dump_json().encode("utf-8")),
                        "revision": rev,
                    }
                )
            except (ValueError, OSError) as err:
                return _json({"error": "live2d_invalid", "message": str(err)}, 409)

        try:
            body = _Live2DSave.model_validate(await _read_json(request, max_bytes=32 * 1024))
        except ValueError:
            return _json({"error": "live2d_invalid", "message": "Live2D 表演参数格式无效。"}, 400)
        if body.preferences.model_id != model_id:
            return _json({"error": "live2d_model", "message": "模型身份与当前校准不一致。"}, 409)
        async with live2d_lock:
            try:
                rev = await asyncio.to_thread(
                    save_preferences, pref_path, body.preferences, body.revision
                )
                return _json(
                    {
                        "available": True,
                        "saved": True,
                        "model_id": model_id,
                        "preferences": decode_object(
                            body.preferences.model_dump_json().encode("utf-8")
                        ),
                        "revision": rev,
                    }
                )
            except PreferenceConflict as err:
                return _json({"error": "live2d_conflict", "message": str(err)}, 409)
            except (ValueError, OSError) as err:
                return _json({"error": "live2d_io", "message": str(err)}, 500)

    return [
        Route("/api/chat", chat_endpoint, methods=["GET", "POST"]),
        Route("/api/config", config_endpoint, methods=["GET", "POST"]),
        Route("/api/notebooks", notebooks_list, methods=["GET"]),
        Route("/api/notebooks/file", notebooks_file, methods=["GET", "POST"]),
        Route("/api/studio/status", studio_status, methods=["GET"]),
        Route("/api/studio/profile", studio.profile, methods=["GET", "POST"]),
        Route("/api/studio/source", studio.upload, methods=["POST"]),
        Route("/api/studio/source/{clip}", studio.original, methods=["GET"]),
        Route("/api/studio/render", studio.render, methods=["POST"]),
        Route("/api/live2d/preferences", live2d_endpoint, methods=["GET", "POST"]),
    ]
