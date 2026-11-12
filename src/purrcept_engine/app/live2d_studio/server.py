"""本机表演调节页：临时预览、版本保存、VTS 连接和固定本地语音试听。

调用方构造唯一视觉运行时；页面读取不连接设备。ASGI 生命周期关闭播放器
和视觉资源。预览草稿只进入该运行时，保存才写模型专用偏好，正式插件每秒
读取。连接/试听串行，滑块可在试听期间更新；不启动主体、消息或在线模型。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing, asynccontextmanager
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from purrcept_engine.app.local_http import LocalHttpBoundary
from purrcept_engine.plugin_system import AudioError, BodyIntent
from purrcept_engine.plugins.audio.dsp import VoiceProfile, decode_pcm
from purrcept_engine.plugins.audio.player import FFplayPlayer
from purrcept_engine.plugins.live2d.preferences import (
    PreferenceConflict,
    Preferences,
    load_preferences,
    save_preferences,
)
from purrcept_engine.plugins.live2d.runtime import Live2DRuntime
from purrcept_engine.values import ActionProtocolError, JsonObject, decode_object


class _Edit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    preferences: Preferences
    revision: str


async def _edit(request: Request) -> _Edit:
    """限制真实请求字节，再严格解析；浏览器不能指定文件或设备路径。"""
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 32768:
            raise ValueError("偏好请求超过 32 KiB。")
    return _Edit.model_validate(decode_object(bytes(raw)))


def create_studio(
    path: Path,
    runtime: Live2DRuntime,
    model_id: str,
    *,
    port: int = 8767,
    token: str | None = None,
    sample: Path | None = None,
    sample_rate: int = 32000,
    ffmpeg: str = "ffmpeg",
    ffplay: str = "ffplay",
    voice_profile: VoiceProfile | None = None,
) -> ASGIApp:
    """装配一个模型的调节页，不启动监听、设备或付费合成。

    Args:
        path: 正式插件与页面共用的固定偏好路径。
        runtime: 本服务独占的视觉所有者，不能借用正在运行的 Host 实例。
        model_id: 校准模型身份，所有草稿与文件必须匹配。
        port: 仅回环监听的端口。
        token: 可注入的本机会话凭据，缺省随机生成。
        sample: 可选本地音频，由启动命令指定，浏览器不能改路径。
        sample_rate: 与视觉运行时一致的 PCM 采样率。
        ffmpeg: 明确解码程序。
        ffplay: 明确本地播放器。
        voice_profile: 与正式音频输出相同的调音预设。

    Returns:
        带本机来源和写入令牌校验的 ASGI 应用。

    Note:
        lifespan 归还资源；草稿不会因浏览器退出而写入磁盘。
    """
    session_token = token or secrets.token_urlsafe(32)
    player = FFplayPlayer(ffplay, sample_rate=sample_rate)
    operation, saving = asyncio.Lock(), asyncio.Lock()
    static = files(__package__).joinpath("static")

    async def state(request: Request) -> Response:
        """读取磁盘版本和实际连接状态，不让刷新覆盖正在预览的草稿。"""
        del request
        value, revision = await asyncio.to_thread(load_preferences, path, model_id)
        return JSONResponse(
            {
                "token": session_token,
                "preferences": value.model_dump(),
                "revision": revision,
                "defaults": Preferences(model_id=model_id).model_dump(),
                "status": runtime.snapshot(),
                "sample_available": sample is not None,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def edit(request: Request) -> Response:
        """预览只更新内存；保存持锁直到文件线程真正结束，再确认新版本。"""
        command = await _edit(request)
        if command.preferences.model_id != model_id:
            raise ValueError("偏好与当前模型不符。")
        revision = command.revision
        if request.url.path.endswith("save"):
            async with saving:
                task = asyncio.create_task(
                    asyncio.to_thread(save_preferences, path, command.preferences, revision)
                )
                try:
                    revision = await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
        runtime.configure(command.preferences)
        return JSONResponse({"revision": revision, "status": runtime.snapshot()})

    async def device(request: Request) -> Response:
        """明确连接或断开；停止可打断试听，其余设备操作只允许一个在途。"""
        action = request.path_params["action"]
        if action == "stop":
            await player.interrupt()
            return JSONResponse({"status": "stopped"})
        if operation.locked():
            return JSONResponse({"message": "当前操作尚未完成，请先停止试听。"}, status_code=409)
        async with operation:
            if action == "connect":
                await runtime.connect()
            elif action == "disconnect":
                await runtime.disconnect()
            elif action == "audio":
                if sample is None or runtime.snapshot()["connected"] is not True:
                    raise ValueError("请先连接 VTS，并在启动时指定本地试听音频。")
                result = await play_sample()
                return JSONResponse(result)
            else:
                return Response(status_code=404)
        return JSONResponse(runtime.snapshot())

    async def play_sample() -> JsonObject:
        """同一音轨通过正式解码和媒体时钟驱动嘴部，PCM 分为固定 20ms 块。"""
        assert sample is not None
        sample_path = sample
        player.check_available()

        def read() -> bytes:
            with sample_path.open("rb") as stream:
                raw = stream.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("试听音频超过 8 MiB。")
            return raw

        raw = await asyncio.to_thread(read)
        identity = "studio-" + uuid4().hex
        deadline = time.monotonic() + 45

        async def encoded() -> AsyncGenerator[bytes]:
            yield raw

        async def pcm() -> AsyncGenerator[bytes]:
            pending = bytearray()
            offset, size = 0, sample_rate // 50 * 2
            async with aclosing(
                decode_pcm(
                    encoded(),
                    executable=ffmpeg,
                    sample_rate=sample_rate,
                    profile=voice_profile or VoiceProfile(),
                    max_seconds=30,
                )
            ) as decoded:
                async for chunk in decoded:
                    pending.extend(chunk)
                    while len(pending) >= size:
                        block = bytes(pending[:size])
                        del pending[:size]
                        runtime.playback_pcm(identity, sample_rate, offset, block)
                        offset += size // 2
                        yield block
                if pending:
                    yield bytes(pending)

        async def started() -> None:
            runtime.playback_started(
                identity, BodyIntent(delivery="explain", valence=0.35), deadline
            )

        try:
            async with aclosing(pcm()) as source:
                result = await player.play(
                    source,
                    playback_id=identity,
                    deadline=deadline,
                    started=started,
                    progress=lambda elapsed, energy: runtime.playback_progress(
                        identity, elapsed, energy
                    ),
                )
            return decode_object(json.dumps(asdict(result)).encode())
        finally:
            runtime.playback_finished(identity)

    async def index(request: Request) -> Response:
        del request
        return Response(
            static.joinpath("index.html").read_bytes(),
            media_type="text/html",
            headers={
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self'; frame-ancestors 'none'",
                "Cache-Control": "no-store",
            },
        )

    async def asset(request: Request) -> Response:
        name = request.path_params["name"]
        media = {"studio.js": "text/javascript", "studio.css": "text/css"}
        if name not in media:
            return Response(status_code=404)
        return Response(static.joinpath(name).read_bytes(), media_type=media[name])

    async def invalid(request: Request, error: Exception) -> Response:
        del request
        message = "参数或文件无效，请检查模型和设置。"
        if isinstance(error, PreferenceConflict):
            message = str(error)
        elif isinstance(error, AudioError):
            message = "试听失败，请检查本地音频与播放器。"
        return JSONResponse(
            {"message": message}, status_code=409 if isinstance(error, PreferenceConflict) else 400
        )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        del app
        try:
            yield
        finally:
            try:
                await player.close()
            finally:
                await runtime.close()

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/assets/{name}", asset),
            Route("/api/session", state),
            Route("/api/preview", edit, methods=["POST"]),
            Route("/api/save", edit, methods=["POST"]),
            Route("/api/device/{action}", device, methods=["POST"]),
        ],
        lifespan=lifespan,
        exception_handlers={
            ValueError: invalid,
            ValidationError: invalid,
            ActionProtocolError: invalid,
            OSError: invalid,
            AudioError: invalid,
        },
    )
    return LocalHttpBoundary(app, port=port, token=session_token)
