"""宿主侧 QQ 附件、入站语音和发送临时文件，不向 Worker 暴露文件句柄。

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

from purrcept_engine.infrastructure.files import DownloadError, download_file
from purrcept_engine.plugin_system import MediaAsset
from purrcept_engine.plugins.qq.parameters import Send
from purrcept_engine.plugins.qq.ports import encode_request, message_request
from purrcept_engine.plugins.qq.settings import QQSettings, resolve_path
from purrcept_engine.plugins.qq.voice import MAX_VOICE_BYTES, VoiceError, decode_voice
from purrcept_engine.values import JsonObject, JsonValue, encode_json


class QQMediaError(ValueError):
    """路径、类型、容量或预期媒体获取失败；不接管为完整原生材料。"""


class MediaBusy(QQMediaError):
    """实际文件工作名额已满；既有线程继续归资源所有者持有。"""


def is_url(value: str) -> bool:
    """仅接受有实际主机且无用户凭据的 http(s) 媒体来源。

    Args:
        value: 待判断的原始媒体来源，只有含真实主机且无用户凭据的 HTTP(S) URL 才成立。

    Returns:
        仅当来源为含真实主机且无用户凭据的 HTTP(S) URL 时为 True。
    """
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and parsed.username is None


def sniff_type(data: bytes) -> str:
    """按已验证字节签名辨识常用图片，不把 HTTP 声明当成像素证明。

    Args:
        data: 已经读取并受大小限制的媒体原始字节。

    Returns:
        按已知字节签名辨识的图片 MIME 类型。

    Raises:
        QQMediaError: 媒体字节不是支持的 PNG/GIF/JPEG/WebP 图片。
    """
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

    未确认发送的临时附件保留到真实 echo 或传输关闭；不删除用户提供的文件。
    临时文件最多 pending_calls 个，未知请求不能无界扩张磁盘暂存。
    """

    def __init__(self, settings: QQSettings, base_dir: Path) -> None:
        """固定根目录和有限 HTTP 客户端，不探测媒体或创建目录。

        Args:
            settings: 当前组件已校验的配置或纯声明。
            base_dir: 解析相对配置路径的基准目录。
        """
        self.settings = settings
        self.base_dir = base_dir
        self.media_dir = resolve_path(settings.media_dir, base_dir)
        self.roots = (
            base_dir / ".purrcept",
            self.media_dir,
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
        """实际文件操作由独立任务拥有。

        Args:
            operation: 当前所有者负责推进并回收结果的操作。

        Returns:
            同步操作完成后的真实返回值，异常保持原类型。

        Raises:
            RuntimeError: QQ 媒体资源已关闭。
            MediaBusy: QQ 实际文件工作名额已满。

        Note:
            取消等待不取消已运行的线程。
        """
        if self._closed:
            raise RuntimeError("QQ 媒体资源已关闭。")
        if len(self._threads) >= self.settings.pending_calls:
            raise MediaBusy("QQ 实际文件工作名额已满。")

        async def run() -> Result:
            """将已校验参数交给当前冻结能力绑定的真实实现。"""
            async with self._file_lock:
                return await asyncio.to_thread(operation)

        task = asyncio.create_task(run(), name="qq-media-file")
        self._threads.add(task)

        def finished(owner: asyncio.Task[Result]) -> None:
            """收回已结束的工作所有者，并保留原任务中的真实故障。"""
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

    async def voice(self, url: str) -> bytes:
        """有界下载并解码平台语音；线程由此媒体所有者持有到实际归还。

        Args:
            url: 平台提供的 HTTP(S) 语音地址。

        Returns:
            解码后的完整 WAV 字节。

        Raises:
            QQMediaError: 下载、格式或容量不满足语音读取要求。
        """
        try:
            data = await download_file(
                self._http, url, max_bytes=MAX_VOICE_BYTES, timeout_seconds=15
            )
            return await self.io(lambda: decode_voice(data))
        except (DownloadError, VoiceError) as error:
            raise QQMediaError(str(error)) from error

    def path(self, raw: str) -> Path:
        """验证完整实际路径和大小。

        Args:
            raw: 需要完整读取的本地文件路径；HTTP URI 不是本地路径。

        Returns:
            沿显式基准目录解析的实际路径。

        Raises:
            QQMediaError: 媒体路径、真实格式、大小或贴纸身份不满足当前读取与保存要求。

        Note:
            HTTP URI 不能充当本地文件。
        """
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
        """按实际流再次约束大小，避免 stat 后文件增长绕过容量。

        Args:
            raw: 需要完整读取的本地文件路径；HTTP URI 不是本地路径。

        Raises:
            QQMediaError: QQ 文件读取时为空或已超过容量。
        """
        path = self.path(raw)
        with path.open("rb") as stream:
            content = stream.read(self.settings.file_max_bytes + 1)
        if not content or len(content) > self.settings.file_max_bytes:
            raise QQMediaError("QQ 文件读取时为空或已超过容量。")
        return content

    async def image(self, raw: str) -> MediaAsset:
        """取得完整有限图片后才构造原生媒体。

        Args:
            raw: 明确的 HTTP(S) 图片 URL 或允许路径内的本地图片来源。

        Returns:
            包含完整原始字节和内容引用的媒体素材。

        Raises:
            QQMediaError: QQ 图片下载超过 file_max_bytes；参数、
                材料或当前状态不满足此入口的约束。

        Note:
            获取故障不伪造像素。
        """
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
        """标准 base64 在解码前后均有界，不把任意路径探测失败当成编码。

        Args:
            raw: 待解码的标准 base64 字符串，编码与解码字节均受预算限制。

        Raises:
            QQMediaError: 媒体路径、真实格式、大小或贴纸身份不满足当前读取与保存要求。
        """
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
        """在文件线程构造原 OneBot 消息段，纯文字仍为字符串。

        Args:
            parameters: 已通过能力参数模型校验的调用参数。

        Returns:
            可发送的原生消息分段及其需要保留到真实调用结束的临时文件。

        Raises:
            BaseExceptionGroup: QQ 附件准备与临时文件收回失败。
        """
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
        action, params = message_request(parameters.chat, parameters.target_id, segments)
        prepared_bytes = len(encode_request(action, params))

        def add(segment: JsonValue) -> None:
            """记录段的实际 UTF-8 字节，最终以完整请求预算决定图片来源。"""
            nonlocal prepared_bytes
            prepared_bytes += len(encode_json(segment)) + int(bool(segments))
            segments.append(segment)

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
                    suffix = ".wav" if data.startswith(b"RIFF") else ".mp3"
                    path = self._stage_media(data, prefix="voice-", suffix=suffix)
                    temporary.append(path)
                    source = path.as_uri()
            add({"type": "record", "data": {"file": source}})
        if parameters.text.strip():
            text = parameters.text
            if parameters.at and not parameters.voice and not text[0].isspace():
                text = " " + text
            add({"type": "text", "data": {"text": text}})

        # 先为全部附件和信封预留空间，避免前面的图片内联后挤掉后续段。
        local_images: list[tuple[JsonObject, Path, int]] = []
        encoded_images: list[tuple[JsonObject, str, bytes]] = []
        for raw in parameters.images or []:
            image_data: JsonObject = {"file": raw}
            if raw.startswith("base64://"):
                content = self.decode_base64(raw.removeprefix("base64://"))
                encoded_images.append((image_data, raw, content))
            elif not is_url(raw):
                path = self.path(raw)
                uri = path.as_uri()
                reference_bytes = len(encode_json(uri))
                # 极小图片的内联编码可能比长路径更短，把它也计入最小可发送帧。
                inline = self._inline_image(path, capacity=reference_bytes)
                image_data["file"] = inline or uri
                if inline is None:
                    local_images.append((image_data, path, reference_bytes))
            add({"type": "image", "data": image_data})
        for raw in parameters.files or []:
            if is_url(raw):
                raise QQMediaError("files 必须为允许目录内的本地文件。")
            path = self.path(raw)
            add({"type": "file", "data": {"file": path.as_uri(), "name": path.name}})

        # 直接给出的 Base64 原样保留，只有帧装不下时才暂存为文件引用。
        limit = self.settings.frame_bytes
        for image_data, raw, content in sorted(
            encoded_images, key=lambda image: len(image[1]), reverse=True
        ):
            if prepared_bytes <= limit:
                break
            try:
                suffix = "." + sniff_type(content).split("/")[1]
            except QQMediaError:
                # 保留可由平台自行解释的内联格式，继续寻找能换成文件的图片。
                continue
            path = self._stage_media(content, prefix="image-", suffix=suffix)
            uri = path.as_uri()
            saved = len(encode_json(raw)) - len(encode_json(uri))
            if saved > 0:
                image_data["file"] = uri
                prepared_bytes -= saved
                temporary.append(path)
            else:
                self.release((path,))
        if prepared_bytes > limit:
            raise QQMediaError(
                "QQ 附件组合超过请求帧容量，尚未发送。"
                f"需要 {prepared_bytes} 字节，上限 {limit} 字节。"
            )

        # 对能放进余量的小图片保留原先的内联行为。先按文件大小估算，读完再核验。
        for image_data, path, reference_bytes in local_images:
            capacity = limit - prepared_bytes + reference_bytes
            source = self._inline_image(path, capacity=capacity)
            if source is not None:
                image_data["file"] = source
                prepared_bytes += len(encode_json(source)) - reference_bytes
        return segments, tuple(temporary)

    def _stage_media(self, content: bytes, *, prefix: str, suffix: str) -> Path:
        """登记临时文件后再写入，准备失败或实际发送结束时可统一回收。"""
        if len(self._temporary) >= self.settings.pending_calls:
            raise QQMediaError("QQ 未确认附件的临时文件名额已满。")
        self.media_dir.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=self.media_dir)
        path = Path(name)
        self._temporary.add(path)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        return path

    def image_source(self, raw: str) -> str:
        """为小图片构造内联来源，较大图片使用受限本地 file URI。

        Args:
            raw: 明确的 HTTP(S) 图片 URL 或允许路径内的本地图片来源。

        Note:
            URL 仅交给平台。
        """
        if is_url(raw):
            return raw
        if raw.startswith("base64://"):
            self.decode_base64(raw.removeprefix("base64://"))
            return raw
        path = self.path(raw)
        return self._inline_image(path) or path.as_uri()

    def _inline_image(self, path: Path, *, capacity: int | None = None) -> str | None:
        """先估算 Base64 编码尺寸，文件读取后按实际编码复核剩余帧预算。"""
        size = path.stat().st_size
        if size > self.settings.image_max_bytes:
            return None
        if capacity is not None and len('"base64://"') + 4 * ((size + 2) // 3) > capacity:
            return None
        source = "base64://" + base64.b64encode(self.read(str(path))).decode("ascii")
        return source if capacity is None or len(encode_json(source)) <= capacity else None

    def qzone_sources(self, values: list[str]) -> list[JsonValue]:
        """逐项核验空间图片组合容量，不先把全部本地图片展开到内存。

        Args:
            values: 待校验、编码或构造数组的原始值集合。

        Raises:
            QQMediaError: QZone 图片组合超过请求帧容量，尚未发送。
        """
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
        """只删除本所有者实际登记的临时附件，不接收任意路径清理。

        Args:
            paths: 当前端口允许关联的冻结能力路径集合。
        """
        for path in paths:
            if path in self._temporary:
                path.unlink(missing_ok=True)
                self._temporary.remove(path)

    def resources_released(self) -> bool:
        """确认文件工作、临时文件和 HTTP 池均已实际归还。

        Returns:
            完成清理后为 True；取消观察留下的历史文件错误不撤销归还事实。
        """
        return self._closed

    async def close(self) -> None:
        """等文件工作归还后释放 HTTP 与本次临时文件，用户媒体和贴纸保留。

        Raises:
            BaseExceptionGroup: 取消等待后的 QQ 文件工作失败。
        """
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
