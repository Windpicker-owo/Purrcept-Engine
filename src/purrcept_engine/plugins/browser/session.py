"""一页浏览器的串行调用、不可变网页分页与同库原生媒体交接。

读页先保存完整HTML，再保存实际呈现方式；续读只从EvidencePort旧快照
取得，不接触网页。截图/保存先接管BLOB，磁盘路径是可供显式发送的镜像，
缓存淘汰只清理由本实例新建的截图文件，不删除权威媒体或用户文件。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import time
from collections import deque
from collections.abc import Awaitable
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from purrcept_engine.plugin_system import (
    CallOutcome,
    EvidencePage,
    EvidencePort,
    EvidenceSnapshot,
    MediaAsset,
    MediaReference,
)
from purrcept_engine.plugins.browser.driver import (
    BrowserActionError,
    BrowserDriver,
    BrowserError,
    BrowserInputError,
    PageView,
)
from purrcept_engine.plugins.browser.extract import html_to_markdown, html_to_text
from purrcept_engine.plugins.browser.navigation import normalize_and_authorize_url
from purrcept_engine.plugins.browser.search import build_search_url, parse_search_results
from purrcept_engine.plugins.browser.settings import BrowserSettings
from purrcept_engine.values import JsonObject, decode_object, encode_json

_CAPTCHA = ("captcha", "recaptcha", "hcaptcha", "verify you are human", "unusual traffic")
_CHALLENGE = (
    "just a moment",
    "attention required",
    "cf-challenge",
    "cf-browser-verification",
    "checking your browser",
)


def detect_block(view: PageView, html: str) -> str | None:
    """沿用标题/URL/HTML前8000字符的验证码线索，线索不等于验证成功。

    Args:
        view: 当前驱动实际取得的页面身份与状态。
        html: 当次实际取得的完整 HTML，抽取前不按模型页长裁剪。

    Returns:
        实际已有拦截原因，或 captcha/challenge 线索；未发现时返回 None。
    """
    if view.blocked is not None:
        return view.blocked
    haystack = (view.title + "\n" + view.url + "\n" + html[:8000]).lower()
    if any(token in haystack for token in _CAPTCHA):
        return "captcha"
    return "challenge" if any(token in haystack for token in _CHALLENGE) else None


class BrowserSession:
    """app借出的浏览器资源，执行期间串行，关闭在所有真实调用借用结束后。

    evidence是共享原文/媒体端口，不拥有数据库。纯声明不给Driver；build
    创建本实例，首次能力调用才 start；失败也由宿主 close 实际已取得资源。已接管媒体和快照不随
    后续模型/Worker失败撤销，关闭不删除profile、下载或同库媒体。
    """

    def __init__(
        self,
        instance_id: str,
        driver: BrowserDriver,
        settings: BrowserSettings,
        evidence: EvidencePort,
    ) -> None:
        """登记资源和配置，不在构造读取浏览器或创建目录。

        Args:
            instance_id: 本次装配中唯一的插件或宿主实例身份。
            driver: 当前会话唯一借用的浏览器驱动。
            settings: 当前组件已校验的配置或纯声明。
            evidence: 完整原文、媒体及不可变分页服务。
        """
        self.instance_id, self.driver, self.settings = instance_id, driver, settings
        self.evidence = evidence
        self._lock = asyncio.Lock()
        self._refs: dict[str, str] = {}
        self._started = False
        self._closed = False
        self._start_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._owned: set[asyncio.Future[object]] = set()
        self._latest: PageView | None = None
        self._observed_at: float | None = None
        self._screenshots: deque[Path] = deque()

    async def _own[Result](self, operation: Awaitable[Result]) -> Result:
        """资源写入/原文接管归实例，不随取消观察消失。"""
        task = asyncio.ensure_future(operation)
        owned = cast(asyncio.Future[object], task)
        self._owned.add(owned)
        owned.add_done_callback(self._done)
        return await asyncio.shield(task)

    def _done(self, task: asyncio.Future[object]) -> None:
        self._owned.discard(task)
        if not task.cancelled():
            task.exception()

    async def start(self) -> None:
        """启动当前浏览器会话并安装逐跳导航策略。

        Raises:
            BrowserError: 浏览器已关闭。

        Note:
            取消等待不丢失取得中的资源；重复加入同一次真实启动。
        """
        if self._close_task is not None:
            raise BrowserError("浏览器已关闭。")
        if self._start_task is None:
            self._start_task = asyncio.create_task(self._start(), name="browser-session-start")
        await asyncio.shield(self._start_task)

    async def _start(self) -> None:
        """沿用持久profile，策略安装完整成功后才允许能力调用。"""
        # 同一初始化任务由所有首次调用共享；实际资源取得仍与操作、关闭共用锁。
        async with self._lock:
            await self._own(
                asyncio.to_thread(self.settings.user_data_dir.mkdir, parents=True, exist_ok=True)
            )
            await self.driver.start()
            await self.driver.set_navigation_policy(self.check_url)
            self._started = True

    def check_url(self, url: str) -> str:
        """声明主机约束同一函数由入口与真实逐跳请求使用。

        Args:
            url: 本次明确访问的资源 URL，实际请求仍须通过对应策略。

        Returns:
            经协议与主机策略校验的规范 URL，实际文档请求仍逐跳核验。

        Raises:
            BrowserInputError: 参数、材料或当前状态不满足此入口的约束。
        """
        try:
            return normalize_and_authorize_url(
                url,
                allowed_hosts=self.settings.allowed_hosts,
                blocked_hosts=self.settings.blocked_hosts,
            )
        except ValueError as error:
            raise BrowserInputError(str(error)) from error

    def _check(self) -> None:
        """入口检查不能跨等待保留资格；每次取得操作锁后必须再核验关闭门槛。"""
        if not self._started or self._close_task is not None:
            raise BrowserError("浏览器尚未就绪或已关闭。")

    async def _decorate(self, view: PageView, operation: str) -> JsonObject:
        """交互确认后的读取失败保留事实，不以空HTML猜状态或重新执行。"""
        try:
            html = await self.driver.html()
        except BrowserError as error:
            raise BrowserActionError(
                "实际交互已完成，但后续页面正文取得失败。",
                {**view.payload(), "operation": operation, "interaction": "confirmed"},
            ) from error
        result = view.payload()
        blocked = detect_block(view, html)
        if blocked is not None:
            result["blocked"] = blocked
        self._latest, self._observed_at = view, time.time()
        return result

    async def status(self) -> JsonObject:
        """查询当前实际页面身份与可用状态。

        Note:
            实际读页面身份，不额外读正文或取图。
        """
        self._check()
        async with self._lock:
            self._check()
            view = await self.driver.view()
            self._latest, self._observed_at = view, time.time()
            return view.payload()

    async def open(self, url: str) -> JsonObject:
        """预检后真实导航，保留加载/验证码状态，不自动再请求。

        Args:
            url: 本次明确访问的资源 URL，实际请求仍须通过对应策略。
        """
        self._check()
        normalized = self.check_url(url)
        async with self._lock:
            self._check()
            self._refs.clear()
            view = await self.driver.open(
                normalized, timeout_seconds=self.settings.navigation_timeout_seconds
            )
            return await self._decorate(view, "open")

    async def _save_text(self, text: str, kind: str, source: JsonObject) -> EvidenceSnapshot:
        """实际完整材料先存权威库，返回字段不再次复制全文元数据。"""
        snapshot = EvidenceSnapshot(
            evidence_id="browser-" + uuid4().hex,
            version=1,
            kind=kind,
            text=text,
            source_json=encode_json(source).decode(),
            occurred_at=time.time(),
        )
        return await self._own(self.evidence.put_evidence(snapshot))

    async def read(
        self,
        mode: Literal["markdown", "text"] | None = None,
        max_chars: int | None = None,
        cursor: str | None = None,
    ) -> JsonObject:
        """取得完整页面快照，按指定正文格式提供不可变分页。

        Args:
            mode: markdown 或 text；None 使用配置默认模式。
            max_chars: 本页正文的字符上限；分页游标继续绑定原快照。 None
                使用本实例配置的统一页长。
            cursor: 宿主签发并保存的不透明续读游标，续读同一快照版本。 None 表示首次读取。

        Raises:
            BrowserInputError: cursor续读不能同时指定新的mode；max_chars必须为正整数。
            BrowserActionError: 读取期间页面URL改变，已保存取得原文。

        Note:
            首读保存完整HTML/正文，游标只续同一快照，不刷新平台或猜cursor。
        """
        if cursor is not None and mode is not None:
            raise BrowserInputError("cursor续读不能同时指定新的mode。")
        limit = (
            min(self.evidence.page_chars, self.settings.max_chars)
            if max_chars is None
            else max_chars
        )
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise BrowserInputError("max_chars必须为正整数。")
        if cursor is not None:
            return self._page(await self.evidence.read_evidence_cursor(cursor, max_chars=limit))
        self._check()
        async with self._lock:
            self._check()
            before = await self.driver.view()
            html = await self.driver.html()
            after = await self.driver.view()
            raw = await self._save_text(
                html,
                "browser.html",
                {
                    "plugin": self.instance_id,
                    "url_before": before.url,
                    "url_after": after.url,
                    "title": after.title,
                    "identity_consistent": before.url == after.url,
                },
            )
            if before.url != after.url:
                raise BrowserActionError(
                    "读取期间页面URL改变，已保存取得原文。",
                    {"raw_ref": raw.evidence_id, "acquisition": "failed"},
                )
            actual_mode = "markdown" if mode is None else mode
            if actual_mode == "markdown":
                body, renderer = await self._own(asyncio.to_thread(html_to_markdown, html))
            else:
                body, renderer = await self._own(asyncio.to_thread(html_to_text, html)), "html-text"
            if not body:
                body, renderer = await self.driver.inner_text(), "visible-text"
            source: JsonObject = {
                "plugin": self.instance_id,
                "url": after.url,
                "title": after.title,
                "mode": actual_mode,
                "renderer": renderer,
                "raw_ref": raw.evidence_id,
            }
            blocked = detect_block(after, html)
            if after.status is not None:
                source["status"] = after.status
            if blocked is not None:
                source["blocked"] = blocked
            snapshot = await self._save_text(body, "browser.read", source)
            page = await self.evidence.read_evidence(snapshot.evidence_id, max_chars=limit)
            self._latest, self._observed_at = after, time.time()
            return self._page(page)

    def _page(self, page: EvidencePage) -> JsonObject:
        """只交真实片段，原始HTML引用不变；跨插件游标不当作当前浏览器快照。"""
        snapshot, fragment = page.snapshot, page.slice
        if snapshot is None or fragment is None or snapshot.kind != "browser.read":
            raise BrowserInputError("cursor不是浏览器正文快照。")
        source = decode_object(snapshot.source_json.encode())
        if source.get("plugin") != self.instance_id:
            raise BrowserInputError("cursor属于另一浏览器实例。")
        return {
            **source,
            "content": page.text,
            "evidence_ref": snapshot.evidence_id,
            "slice_ref": fragment.slice_ref,
            "range": {"unit": fragment.unit, "start": fragment.start, "end": fragment.end},
            "next_cursor": page.next_cursor,
            "complete": page.next_cursor is None,
        }

    async def search(self, query: str) -> JsonObject:
        """保留原搜索引擎/条数，同时保存完整结果页HTML，不把空解析冒充全部无结果。

        Args:
            query: 用于关键词或语义检索的意图文本。

        Raises:
            BrowserInputError: query不能为空。
        """
        self._check()
        stripped = query.strip()
        if not stripped:
            raise BrowserInputError("query不能为空。")
        url = self.check_url(build_search_url(self.settings, stripped))
        async with self._lock:
            self._check()
            self._refs.clear()
            view = await self.driver.open(
                url, timeout_seconds=self.settings.navigation_timeout_seconds
            )
            html = await self.driver.html()
            raw = await self._save_text(
                html,
                "browser.search",
                {
                    "plugin": self.instance_id,
                    "url": view.url,
                    "title": view.title,
                    "query": stripped,
                },
            )
            payload = view.payload()
            payload.update({"query": stripped, "raw_ref": raw.evidence_id})
            blocked = detect_block(view, html)
            if blocked is not None:
                payload.update({"blocked": blocked, "results": []})
            else:
                results = await self._own(
                    asyncio.to_thread(
                        parse_search_results,
                        html,
                        engine=self.settings.search_engine,
                        limit=self.settings.max_search_results,
                    )
                )
                payload["results"] = [
                    {key: value for key, value in item.items()} for item in results
                ]
                payload["coverage"] = {
                    "max_results": self.settings.max_search_results,
                    "external_complete": False,
                }
            self._latest, self._observed_at = view, time.time()
            return payload

    async def snapshot(self, *, interactive: bool) -> JsonObject:
        """刷新最近ref映射。

        Args:
            interactive: 是否仅投影可交互节点。

        Returns:
            业务对象，按实际结果提供 snapshot、interactive、ref_count 字段。

        Note:
            目录稳定，页面树不进入固定目录前缀。
        """
        self._check()
        async with self._lock:
            self._check()
            view = await self.driver.view()
            result = await self.driver.snapshot(interactive=interactive)
            self._refs = dict(result.refs)
            return {
                **view.payload(),
                "snapshot": result.text,
                "interactive": interactive,
                "ref_count": len(self._refs),
            }

    def _ref(self, ref: str) -> str:
        handle = self._refs.get(ref.strip().removeprefix("@"))
        if handle is None:
            raise BrowserInputError("ref未知或已失效，请重新snapshot，不要猜编号。")
        return handle

    async def click(self, ref: str) -> JsonObject:
        """真实点击完成后清旧ref，后续读取失败不触发重新点击。

        Args:
            ref: 当前页面快照签发的节点引用，页面变化后不可复用旧引用。

        Returns:
            业务对象，按实际结果提供 ref 字段。
        """
        self._check()
        async with self._lock:
            self._check()
            handle = self._ref(ref)
            self._refs.clear()
            view = await self.driver.click(handle)
            return {**await self._decorate(view, "click"), "ref": ref}

    async def type_text(self, ref: str, text: str, *, submit: bool) -> JsonObject:
        """保留每字/封顶停顿。

        Args:
            ref: 当前页面快照签发的节点引用，页面变化后不可复用旧引用。
            text: 本次明确传输、输入或呈现的完整文本。
            submit: 是否在完成文字输入后明确发送 Enter。

        Returns:
            业务对象，按实际结果提供 ref、submit 字段。

        Raises:
            BrowserInputError: text不能为空。

        Note:
            空文本拒绝，不清空已填框作为降级。
        """
        self._check()
        if not text:
            raise BrowserInputError("text不能为空。")
        delay_ms = self.settings.typing_pause_seconds(text) / len(text) * 1000
        async with self._lock:
            self._check()
            handle = self._ref(ref)
            self._refs.clear()
            view = await self.driver.type_text(handle, text, delay_ms=delay_ms, submit=submit)
            return {**await self._decorate(view, "type"), "ref": ref, "submit": submit}

    async def scroll(self, direction: str, ref: str | None, amount: int) -> JsonObject:
        """保留可选元素滚动与默认600像素，不自行调整当前页面来源。

        Args:
            direction: 明确的滚动方向，由浏览器边界校验。
            ref: 当前页面快照签发的节点引用，页面变化后不可复用旧引用。
            amount: 本次滚动的像素数量。

        Returns:
            业务对象，按实际结果提供 direction 字段。
        """
        self._check()
        async with self._lock:
            self._check()
            handle = None if ref is None else self._ref(ref)
            view = await self.driver.scroll(direction=direction, handle=handle, amount=amount)
            return {**await self._decorate(view, "scroll"), "direction": direction}

    async def image(self, *, ref: str | None, full_page: bool = False) -> CallOutcome:
        """原生BLOB确认后才写可发送镜像，镜像失败仍保留已取得媒体。

        Args:
            ref: 当前页面快照签发的节点引用，页面变化后不可复用旧引用。
            full_page: 是否截取完整页面，而非仅当前视口。 默认 False。

        Returns:
            实际确认、失败或未知结论，以及可 JSON 化的业务值和原生媒体引用。
        """
        self._check()
        async with self._lock:
            self._check()
            view = await self.driver.view()
            capture = (
                await self.driver.screenshot(full_page=full_page)
                if ref is None
                else (await self.driver.save(self._ref(ref)))
            )
        digest = hashlib.sha256(capture.data).hexdigest()
        media = MediaReference(digest, capture.media_type, len(capture.data))
        source: JsonObject = {
            "plugin": self.instance_id,
            "url": view.url,
            "title": view.title,
            "page_identity": "before_acquisition",
            "acquired_via": capture.acquired_via,
            "original_error": capture.original_error,
        }
        snapshot = EvidenceSnapshot(
            evidence_id="browser-image-" + uuid4().hex,
            version=1,
            kind="browser.image",
            text="浏览器实际取得图像。",
            source_json=encode_json(source).decode(),
            occurred_at=time.time(),
            media=(media,),
        )
        committed = await self._own(
            self.evidence.capture(snapshot, (MediaAsset(capture.data, capture.media_type),))
        )
        try:
            width, height = await self._own(asyncio.to_thread(_dimensions, capture.data))
            directory = (
                (self.settings.screenshot_dir or self.settings.user_data_dir / "screenshots")
                if ref is None
                else self.settings.user_data_dir / "downloads"
            )
            suffix = {
                "image/jpeg": ".jpg",
                "image/jpg": ".jpg",
                "image/png": ".png",
                "image/gif": ".gif",
                "image/webp": ".webp",
            }.get(capture.media_type, ".bin")
            path = directory / (digest + suffix)
            created = await self._own(asyncio.to_thread(_mirror, path, capture.data))
        except (OSError, ValueError) as error:
            return CallOutcome(
                "failed",
                {"evidence_ref": committed.evidence_id, "sha256": digest, "mirror_state": "failed"},
                type(error).__name__,
                str(error),
                media=committed.media,
            )
        value: JsonObject = {
            "path": str(path),
            "media_type": capture.media_type,
            "width": width,
            "height": height,
            "sha256": digest,
            "source": source,
            "evidence_ref": committed.evidence_id,
        }
        if ref is None and created:
            self._screenshots.append(path)
            try:
                while len(self._screenshots) > self.settings.screenshot_max_files:
                    expired = self._screenshots[0]
                    await self._own(asyncio.to_thread(expired.unlink))
                    self._screenshots.popleft()
            except OSError as error:
                # 新图已取得并保存，旧缓存删除失败不能抹掉本次媒体/路径事实。
                value["cache_cleanup"] = "failed"
                return CallOutcome(
                    "failed", value, type(error).__name__, str(error), media=committed.media
                )
        return CallOutcome("confirmed", value, media=committed.media)

    def state(self) -> JsonObject:
        """只投影已观察的页面，不在查询时导航、取图或重新读正文。

        Returns:
            业务对象，按实际结果提供 phase、page、observed_at 字段。
        """
        if self._closed:
            phase = "closed"
        elif self._close_task is not None:
            phase = "closing"
        elif self._started:
            phase = "ready"
        elif (
            self._start_task is not None
            and self._start_task.done()
            and self._start_task.exception() is not None
        ):
            phase = "failed"
        else:
            phase = "declared" if self._start_task is None else "starting"
        return {
            "phase": phase,
            "page": None if self._latest is None else self._latest.payload(),
            "observed_at": self._observed_at,
        }

    async def close(self) -> None:
        """取消等待只取消观察，共享实际资源关闭任务。"""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="browser-session-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        """等实际启动与提交结束，再释放驱动；清理失败保留原异常。"""
        # 启动观察者可能取消，启动本身仍持有后续取得资源的资格。
        # 必须先加入它，避免close完成后start又交回新页面。
        if self._start_task is not None:
            await asyncio.gather(self._start_task, return_exceptions=True)
        # 先等待已进入驱动的调用和持久提交，再实际释放浏览器。
        async with self._lock:
            results = await asyncio.gather(*tuple(self._owned), return_exceptions=True)
            failures = [result for result in results if isinstance(result, BaseException)]
            try:
                await self.driver.close()
            except BaseException as error:
                failures.append(error)
            else:
                self._closed = True
            self._started = False
            self._refs.clear()
            if failures:
                raise BaseExceptionGroup("浏览器资源或已接管提交收尾失败。", failures)


def _dimensions(data: bytes) -> tuple[int, int]:
    """读取真实格式尺寸，未知编码明确失败，不以猜测0尺寸代替。"""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return image.size


def _mirror(path: Path, data: bytes) -> bool:
    """仅新建或确认相同内容镜像，哈希同名被外界占用时不覆盖用户文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as output:
            output.write(data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise FileExistsError("浏览器缓存路径被不同内容占用。") from None
        return False
    return True
