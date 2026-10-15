"""宿主侧 QQ 附件、语音临时文件与原生图片读取，不向 Worker 暴露文件句柄。

相对路径固定以主配置目录解释，发送路径须在明确允许根中；解析后的外部
symlink 不因为原字符串位于根内而获准。文件工作由本所有者持有的 to_thread
任务完成，取消调用等待不能把实际读写留成无人收尾的线程。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from purrcept_engine.domain.inputs.models import MediaAsset
from purrcept_engine.plugins.qq.parameters import Send
from purrcept_engine.plugins.qq.settings import QQSettings, resolve_path
from purrcept_engine.values import JsonValue, encode_json


class QQMediaError(ValueError):
    """路径、类型、容量或预期媒体获取失败；不接管为完整原生材料。"""


class MediaBusy(QQMediaError):
    """实际文件工作名额已满；既有线程继续归资源所有者持有。"""


def is_url(value: str) -> bool:
    """仅接受有实际主机且无用户凭据的 http(s) 媒体来源。"""
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and parsed.username is None


def sniff_type(data: bytes) -> str:
    """按已验证字节签名辨识常用图片，不把 HTTP 声明当成像素证明。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise QQMediaError("媒体字节不是支持的 PNG/GIF/JPEG/WebP 图片。")


class MediaOwner:
    """build 创建的文件/HTTP 所有者，close 只回收本次登记的临时文件。

    未确认发送的临时语音保留到真实 echo 或传输关闭；不删除用户提供的文件。
    临时文件最多 pending_calls 个，未知请求不能无界扩张磁盘暂存。
    """

    def __init__(self, settings: QQSettings, base_dir: Path) -> None:
        """固定根目录和有限 HTTP 客户端，不探测媒体或创建目录。"""
        self.settings = settings
        self.base_dir = base_dir
        self.media_dir = resolve_path(settings.media_dir, base_dir)
        self.sticker_dir = resolve_path(settings.sticker_dir, base_dir)
        self.roots = (
            base_dir / ".purrcept",
            self.media_dir,
            self.sticker_dir,
            *(resolve_path(value, base_dir) for value in settings.extra_media_roots),
        )
        self._http = httpx.AsyncClient(timeout=10.0, follow_redirects=False, trust_env=False)
        self._threads: set[asyncio.Task[object]] = set()
        self._detached: set[asyncio.Task[object]] = set()
        self._failures: list[BaseException] = []
        self._file_lock = asyncio.Lock()
        self._temporary: set[Path] = set()
        self._closed = False

    async def io[Result](self, operation: Callable[[], Result]) -> Result:
        """实际文件操作由独立任务拥有；取消等待不取消已运行的线程。"""
        if self._closed:
            raise RuntimeError("QQ 媒体资源已关闭。")
        if len(self._threads) >= self.settings.pending_calls:
            raise MediaBusy("QQ 实际文件工作名额已满。")

        async def run() -> Result:
            async with self._file_lock:
                return await asyncio.to_thread(operation)

        task = asyncio.create_task(run(), name="qq-media-file")
        self._threads.add(task)

        def finished(owner: asyncio.Task[Result]) -> None:
            self._threads.discard(owner)
            if not owner.cancelled():
                error = owner.exception()
                if owner in self._detached and error is not None:
                    self._failures.append(error)
            self._detached.discard(owner)

        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done() and not task.cancelled():
                if (error := task.exception()) is not None:
                    self._failures.append(error)
            else:
                self._detached.add(task)
            raise

    def path(self, raw: str) -> Path:
        """验证完整实际路径和大小；HTTP URI 不能充当本地文件。"""
        if raw.startswith("file:"):
            parsed = urlparse(raw)
            if parsed.netloc:
                raise QQMediaError("不允许远程 file URI。")
            raw = unquote(parsed.path).lstrip("/") if os.name == "nt" else unquote(parsed.path)
        path = resolve_path(raw, self.base_dir).resolve(strict=True)
        if not any(path.is_relative_to(root.resolve()) for root in self.roots):
            raise QQMediaError("QQ 文件超出允许目录或经 symlink 指向外部。")
        size = path.stat().st_size
        if not path.is_file() or size <= 0 or size > self.settings.file_max_bytes:
            raise QQMediaError("QQ 文件必须为非空普通文件且满足 file_max_bytes。")
        return path

    def read(self, raw: str) -> bytes:
        """按实际流再次约束大小，避免 stat 后文件增长绕过容量。"""
        path = self.path(raw)
        with path.open("rb") as stream:
            content = stream.read(self.settings.file_max_bytes + 1)
        if not content or len(content) > self.settings.file_max_bytes:
            raise QQMediaError("QQ 文件读取时为空或已超过容量。")
        return content

    async def image(self, raw: str) -> MediaAsset:
        """取得完整有限图片后才构造原生媒体；获取故障不伪造像素。"""
        if is_url(raw):
            try:
                async with self._http.stream("GET", raw) as response:
                    response.raise_for_status()
                    parts: list[bytes] = []
                    size = 0
                    async for part in response.aiter_bytes():
                        size += len(part)
                        if size > self.settings.file_max_bytes:
                            raise QQMediaError("QQ 图片下载超过 file_max_bytes。")
                        parts.append(part)
                    content = b"".join(parts)
            except (httpx.HTTPError, TimeoutError) as error:
                raise QQMediaError("QQ 图片未取得：" + type(error).__name__) from error
        elif raw.startswith("base64://"):
            content = self.decode_base64(raw.removeprefix("base64://"))
        else:
            content = await self.io(lambda: self.read(raw))
        return MediaAsset(content, sniff_type(content))

    def decode_base64(self, raw: str) -> bytes:
        """标准 base64 在解码前后均有界，不把任意路径探测失败当成编码。"""
        if len(raw) > (self.settings.file_max_bytes + 2) // 3 * 4 + 4096:
            raise QQMediaError("QQ 内联媒体编码超过容量。")
        try:
            data = base64.b64decode("".join(raw.split()), validate=True)
        except (ValueError, binascii.Error) as error:
            raise QQMediaError("QQ 内联媒体必须为标准 base64。") from error
        if not data or len(data) > self.settings.file_max_bytes:
            raise QQMediaError("QQ 内联媒体为空或超过容量。")
        return data

    def prepare(self, parameters: Send) -> tuple[JsonValue, tuple[Path, ...]]:
        """在文件线程构造原 OneBot 消息段，纯文字仍为字符串。"""
        previous = set(self._temporary)
        try:
            return self._prepare(parameters)
        except BaseException as original:
            try:
                self.release(tuple(self._temporary - previous))
            except BaseException as cleanup:
                raise BaseExceptionGroup(
                    "QQ 附件准备与临时文件收回失败。", [original, cleanup]
                ) from None
            raise

    def _prepare(self, parameters: Send) -> tuple[JsonValue, tuple[Path, ...]]:
        """同一个文件任务持有全部准备阶段，避免取消释放串行锁后遗留线程写入。"""
        if not any(
            (parameters.images, parameters.files, parameters.voice, parameters.at, parameters.reply)
        ):
            return parameters.text, ()
        segments: list[JsonValue] = []
        prepared_bytes = 2

        def add(segment: JsonValue) -> None:
            nonlocal prepared_bytes
            proposed = prepared_bytes + len(encode_json(segment)) + int(bool(segments))
            if proposed > self.settings.frame_bytes:
                raise QQMediaError("QQ 附件组合超过请求帧容量，尚未发送。")
            segments.append(segment)
            prepared_bytes = proposed

        temporary: list[Path] = []
        if parameters.reply:
            add({"type": "reply", "data": {"id": parameters.reply}})
        if parameters.at:
            add({"type": "at", "data": {"qq": parameters.at}})
        if parameters.at and (parameters.voice or not parameters.text.strip()):
            add({"type": "text", "data": {"text": " "}})
        if parameters.voice:
            raw = parameters.voice.strip()
            if is_url(raw):
                source = raw
            elif raw.startswith(("file:", ".", "/", "\\")) or (len(raw) >= 2 and raw[1] == ":"):
                source = self.path(raw).as_uri()
            else:
                # 明确文件存在时仍保留普通相对文件名的原习惯；长编码不调用 stat。
                candidate = resolve_path(raw, self.base_dir) if len(raw) < 240 else None
                if candidate is not None and candidate.is_file():
                    source = self.path(raw).as_uri()
                else:
                    data = self.decode_base64(raw)
                    if len(self._temporary) >= self.settings.pending_calls:
                        raise QQMediaError("未确认语音临时文件名额已满。")
                    self.media_dir.mkdir(parents=True, exist_ok=True)
                    suffix = ".wav" if data.startswith(b"RIFF") else ".mp3"
                    descriptor, name = tempfile.mkstemp(
                        prefix="voice-", suffix=suffix, dir=self.media_dir
                    )
                    path = Path(name)
                    self._temporary.add(path)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(data)
                    temporary.append(path)
                    source = path.as_uri()
            add({"type": "record", "data": {"file": source}})
        if parameters.text.strip():
            text = parameters.text
            if parameters.at and not parameters.voice and not text[0].isspace():
                text = " " + text
            add({"type": "text", "data": {"text": text}})
        for raw in parameters.images or []:
            add({"type": "image", "data": {"file": self.image_source(raw)}})
        for raw in parameters.files or []:
            if is_url(raw):
                raise QQMediaError("files 必须为允许目录内的本地文件。")
            path = self.path(raw)
            add({"type": "file", "data": {"file": path.as_uri(), "name": path.name}})
        return segments, tuple(temporary)

    def image_source(self, raw: str) -> str:
        """沿旧规则小图片内联，较大图片发送 file URI；URL 仅交给平台。"""
        if is_url(raw):
            return raw
        if raw.startswith("base64://"):
            self.decode_base64(raw.removeprefix("base64://"))
            return raw
        path = self.path(raw)
        if path.stat().st_size > self.settings.image_max_bytes:
            return path.as_uri()
        return "base64://" + base64.b64encode(self.read(raw)).decode("ascii")

    def qzone_sources(self, values: list[str]) -> list[JsonValue]:
        """逐项核验空间图片组合容量，不先把全部本地图片展开到内存。"""
        sources: list[JsonValue] = []
        size = 2
        for raw in values:
            source = self.image_source(raw)
            size += len(encode_json(source)) + int(bool(sources))
            if size > self.settings.frame_bytes:
                raise QQMediaError("QZone 图片组合超过请求帧容量，尚未发送。")
            sources.append(source)
        return sources

    def release(self, paths: tuple[Path, ...]) -> None:
        """只删除本所有者实际登记的语音文件，不接收任意路径清理。"""
        for path in paths:
            if path in self._temporary:
                path.unlink(missing_ok=True)
                self._temporary.remove(path)

    async def close(self) -> None:
        """等文件工作归还后释放 HTTP 与本次临时文件，用户媒体和贴纸保留。"""
        if self._closed:
            return
        if self._threads:
            await asyncio.gather(*tuple(self._threads), return_exceptions=True)
        try:
            await self.io(lambda: self.release(tuple(self._temporary)))
        finally:
            await self._http.aclose()
        self._closed = True
        if len(self._failures) == 1:
            raise self._failures[0]
        if self._failures:
            raise BaseExceptionGroup("取消等待后的 QQ 文件工作失败。", self._failures)
