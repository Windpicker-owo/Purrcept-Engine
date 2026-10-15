"""独立调音室的有界上传、原声、串行试听与预设版本路由。

不构造 Engine、模型或扬声器；浏览器播放 WAV，服务仅持有一个内存样本。
格式签名限定为 WAV/MP3/FLAC，共用现场 FFmpeg DSP，按实际展开 PCM
限制 90 秒。HTTP 来源保护复用 LocalHttpBoundary，不维护第二套安全规则。
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Callable
from importlib.resources import files
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from purrcept_engine.app.local_http import LocalHttpBoundary
from purrcept_engine.app.studio.profiles import ProfileConflict, load_profile, save_profile
from purrcept_engine.plugins.audio.dsp import VoiceProfile, decode_pcm, encode_wav
from purrcept_engine.plugins.audio.models import AudioError
from purrcept_engine.values import ActionProtocolError, JsonObject, decode_object

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_SECONDS = 90


class _Render(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    clip_id: str = Field(min_length=1, max_length=64)
    profile: VoiceProfile


class _Save(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    revision: str = Field(min_length=1, max_length=64)
    profile: VoiceProfile


class StudioInputError(ValueError):
    """上传格式、媒体限额或草稿参数无效，现有有效样本不被替换。"""


async def _work[T](call: Callable[[], T]) -> T:
    """取消仍等文件线程结束，不提前释放版本写入的串行锁。"""
    task = asyncio.create_task(asyncio.to_thread(call), name="studio-file-operation")
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def _body(request: Request, limit: int) -> bytes:
    """逐块限制真实字节数，不依赖可伪造的 Content-Length。"""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise StudioInputError("上传或请求超过限额。")
        body.extend(chunk)
    return bytes(body)


def _format(raw: bytes) -> Literal["wav", "mp3", "flac"]:
    """只选明确音频解码器，禁止播放列表或任意媒体探测读取外部地址。"""
    if raw.startswith(b"RIFF") and raw[8:12] == b"WAVE":
        return "wav"
    if raw.startswith(b"fLaC"):
        return "flac"
    if raw.startswith(b"ID3") or (len(raw) > 1 and raw[0] == 255 and raw[1] & 224 == 224):
        return "mp3"
    raise StudioInputError("请上传 WAV、MP3 或 FLAC 音频。")


def _json(value: JsonObject, status: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})


class Studio:
    """ASGI 工厂创建的单样本所有者，串行操作不创建无界等待/渲染任务。

    正忙时直接拒绝新的上传/试听。取消当前试听会关闭同一 FFmpeg 子进程
    后再释放锁；旧 clip_id 始终拒绝，不把旧页面操作施加在新样本上。
    """

    def __init__(self, path: Path, sample_rate: int, executable: str, token: str) -> None:
        """只保存独立 CLI 指定的路径与程序，不读取主体密钥或配置。"""
        self.path, self.rate, self.executable, self.token = path, sample_rate, executable, token
        self.lock = asyncio.Lock()
        self.pcm = b""
        self.clip = ""

    async def session(self, request: Request) -> Response:
        """同源页面取得会话令牌和样本事实，刷新不改变样本或设备。"""
        del request
        return _json(
            {
                "token": self.token,
                "sample_rate": self.rate,
                "clip_id": self.clip or None,
                "duration": len(self.pcm) / 2 / self.rate,
            }
        )

    async def profile(self, request: Request) -> Response:
        """版本冲突明确拒绝，已保存预设只在后续输出或试听采用。"""
        try:
            if request.method == "GET":
                profile, revision = await _work(lambda: load_profile(self.path))
            else:
                value = _Save.model_validate(decode_object(await _body(request, 16384)))
                if self.lock.locked():
                    return _json(
                        {"error": "studio_busy", "message": "当前音频操作尚未完成，请稍后保存。"},
                        409,
                    )
                async with self.lock:
                    profile = value.profile
                    revision = await _work(lambda: save_profile(self.path, profile, value.revision))
            return _json(
                {
                    "profile": decode_object(profile.model_dump_json().encode("utf-8")),
                    "revision": revision,
                }
            )
        except ProfileConflict as error:
            return _json({"error": "profile_conflict", "message": str(error)}, 409)
        except (ActionProtocolError, ValidationError, StudioInputError, ValueError):
            return _json(
                {
                    "error": "profile_invalid",
                    "message": "预设无效，已保存设置未改动，请重新载入并检查参数。",
                },
                400,
            )
        except OSError:
            return _json(
                {
                    "error": "profile_io",
                    "message": "预设文件无法读写，设置未确认，请检查配置路径权限。",
                },
                500,
            )

    async def _decode(
        self, raw: bytes, format: Literal["wav", "mp3", "flac"], profile: VoiceProfile
    ) -> bytes:
        """共享现场 DSP，展开过程中限制实际 PCM，取消归还同一解码进程。"""

        async def chunks() -> AsyncIterator[bytes]:
            for offset in range(0, len(raw), 16384):
                yield raw[offset : offset + 16384]

        pcm = bytearray()
        decoder = decode_pcm(
            chunks(),
            executable=self.executable,
            sample_rate=self.rate,
            profile=profile,
            input_format=format,
            max_seconds=MAX_SECONDS,
        )
        try:
            async with asyncio.timeout(45):
                async for chunk in decoder:
                    pcm.extend(chunk)
        finally:
            await decoder.aclose()
        if not pcm or len(pcm) % 2 or len(pcm) > self.rate * 2 * MAX_SECONDS:
            raise StudioInputError("解码样本为空、不完整或超过 90 秒。")
        return bytes(pcm)

    async def upload(self, request: Request) -> Response:
        """成功解码才替换当前样本，文件名从不参与磁盘路径。"""
        if self.lock.locked():
            return _json({"error": "studio_busy", "message": "当前试听尚未完成，请稍后上传。"}, 409)
        async with self.lock:
            try:
                raw = await _body(request, MAX_UPLOAD_BYTES)
                pcm = await self._decode(raw, _format(raw), VoiceProfile())
            except (StudioInputError, AudioError, TimeoutError):
                return _json(
                    {
                        "error": "audio_invalid",
                        "message": "音频读取失败，原样本保留。请上传 90 秒以内、"
                        "20 MiB 以内的 WAV、MP3 或 FLAC。",
                    },
                    400,
                )
            self.pcm, self.clip = pcm, uuid4().hex
            return _json({"clip_id": self.clip, "duration": len(pcm) / 2 / self.rate})

    async def original(self, request: Request) -> Response:
        """返回同采样率无损原声；旧样本不能读取或混用新样本。"""
        if request.path_params["clip"] != self.clip or not self.pcm:
            return _json(
                {"error": "clip_stale", "message": "样本已经替换，请重新载入当前样本。"}, 404
            )
        return Response(
            encode_wav(self.pcm, self.rate),
            media_type="audio/wav",
            headers={"Cache-Control": "no-store"},
        )

    async def render(self, request: Request) -> Response:
        """冻结草稿只处理当前 clip，失败不修改有效原声或磁盘预设。"""
        if self.lock.locked():
            return _json(
                {"error": "studio_busy", "message": "当前试听尚未完成，请稍后重新试听。"}, 409
            )
        async with self.lock:
            try:
                value = _Render.model_validate(decode_object(await _body(request, 16384)))
                if value.clip_id != self.clip or not self.pcm:
                    return _json(
                        {"error": "clip_stale", "message": "样本已经替换，请重新载入当前样本。"},
                        409,
                    )
                pcm = await self._decode(encode_wav(self.pcm, self.rate), "wav", value.profile)
            except (
                ActionProtocolError,
                ValidationError,
                StudioInputError,
                AudioError,
                TimeoutError,
            ):
                return _json(
                    {
                        "error": "render_invalid",
                        "message": "试听处理失败，原声和已保存预设保留。请检查参数并重新试听。",
                    },
                    400,
                )
            return Response(
                encode_wav(pcm, self.rate),
                media_type="audio/wav",
                headers={"Cache-Control": "no-store"},
            )


def create_studio(
    path: Path,
    *,
    port: int,
    sample_rate: int = 32000,
    executable: str = "ffmpeg",
    token: str | None = None,
) -> ASGIApp:
    """构造独立有限路由，不启动主体、设备或监听器；调用方拥有 ASGI 生命周期。"""
    if sample_rate not in {16000, 24000, 32000, 44100, 48000} or not 1 <= port <= 65535:
        raise ValueError("调音室采样率或监听端口无效。")
    session_token = token or secrets.token_urlsafe(32)
    studio = Studio(path.resolve(), sample_rate, executable, session_token)
    static = files("purrcept_engine.app.studio").joinpath("static")

    async def index(request: Request) -> Response:
        del request
        return Response(
            static.joinpath("index.html").read_bytes(),
            media_type="text/html",
            headers={
                "Content-Security-Policy": "default-src 'self'; media-src 'self' blob:; "
                "style-src 'self'; script-src 'self'; frame-ancestors 'none'",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def asset(request: Request) -> Response:
        name = request.path_params["name"]
        media = {
            "studio.css": "text/css",
            "studio.js": "text/javascript",
            "purrcept.css": "text/css",
        }
        if name not in media:
            return Response(status_code=404)
        return Response(
            static.joinpath(name).read_bytes(),
            media_type=media[name],
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/assets/{name}", asset),
            Route("/api/session", studio.session),
            Route("/api/profile", studio.profile, methods=["GET", "POST"]),
            Route("/api/source", studio.upload, methods=["POST"]),
            Route("/api/source/{clip}", studio.original),
            Route("/api/render", studio.render, methods=["POST"]),
        ]
    )
    return LocalHttpBoundary(app, port=port, token=session_token)
