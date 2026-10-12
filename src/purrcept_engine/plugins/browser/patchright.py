"""真实Patchright单页资源、逐跳文档导航保护与最近快照句柄。

SDK只在start加载，所有取得的资源立即保存；close逆序收回监听、上下文和
SDK。目标程序解析见locate，不安装软件。普通_blank链接仍保留click handler
并改成当前页；脚本popup被阻止。内部错误不伪造空树、旧标题或成功请求。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol, cast

from purrcept_engine.execution.protocol import JsonObject

from .driver import (
    BrowserActionError,
    BrowserError,
    BrowserInputError,
    ImageCapture,
    PageView,
    SnapshotResult,
)
from .navigation import NavigationPolicy
from .settings import BrowserSettings

if TYPE_CHECKING:
    from patchright.async_api import Browser, BrowserContext, Locator, Page, Playwright, Route


class _DocumentSession(Protocol):
    """SDK的CDP标注不完整，只声明实际使用的JSON/ACK接缝，不另造传输层。"""

    def on(self, event: str, callback: Callable[[dict[str, object]], None]) -> None: ...

    async def send(self, method: str, params: JsonObject | None = None) -> object: ...

    async def detach(self) -> None: ...


_LOG = logging.getLogger(__name__)
_ROLES = (
    "button",
    "link",
    "textbox",
    "searchbox",
    "checkbox",
    "radio",
    "combobox",
    "tab",
    "menuitem",
    "slider",
    "switch",
)


class PatchrightDriver:
    """由BrowserSession创建的单页驱动，配置/句柄都不进入行动Worker。"""

    def __init__(self, settings: BrowserSettings) -> None:
        """构造不加载SDK、不找程序或建立网络连接。"""
        self.settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._cdp: _DocumentSession | None = None
        self._policy: NavigationPolicy | None = None
        self._handles: dict[str, Locator] = {}
        self._navigation: set[asyncio.Task[None]] = set()
        self._navigation_error: BaseException | None = None
        self._popup_blocked = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def alive(self) -> bool:
        """只查实际页面资源状态，不重新读取页面。"""
        return self._page is not None and not self._page.is_closed()

    async def start(self) -> None:
        """原有CDP或持久上下文选择，失败不自动重发另一会话。"""
        from patchright.async_api import async_playwright

        from .locate import resolve_launch

        if self._close_task is not None:
            raise BrowserError("浏览器资源已开始关闭。")
        if self._playwright is not None:
            return
        self._playwright = await async_playwright().start()
        settings = self.settings
        if settings.cdp_url:
            self._browser = await self._playwright.chromium.connect_over_cdp(settings.cdp_url)
            if not self._browser.contexts:
                raise BrowserError("CDP浏览器没有可借用的上下文。")
            self._context = self._browser.contexts[0]
        else:
            launch = await asyncio.to_thread(resolve_launch, settings)
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(settings.user_data_dir),
                executable_path=launch.executable_path,
                channel=launch.channel,
                headless=settings.headless,
                no_viewport=not settings.headless,
            )
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        self._page.on("framenavigated", self._invalidate_handles)

    def _invalidate_handles(self, _frame: object) -> None:
        """实际文档跳转也失效快照，不仅依赖模型调用open。"""
        self._handles.clear()

    async def set_navigation_policy(self, policy: NavigationPolicy) -> None:
        """CDP Fetch逐跳暂停Document；上下文路由拒绝衍生或归属未知的新页面。"""
        context = self._context
        if context is None:
            raise BrowserError("浏览器尚未取得上下文。")
        self._policy = policy
        self._cdp = cast(_DocumentSession, await context.new_cdp_session(self._require_page()))
        self._cdp.on("Fetch.requestPaused", self._schedule_navigation)
        await self._cdp.send("Network.setBypassServiceWorker", {"bypass": True})
        await self._cdp.send(
            "Fetch.enable", {"patterns": [{"resourceType": "Document", "requestStage": "Request"}]}
        )
        await context.route("**/*", self._guard_other_pages)

    def _schedule_navigation(self, event: dict[str, object]) -> None:
        """SDK回调任务拥有收尾和失败状态；未知错误不能继续放行导航。"""
        if self._close_task is not None:
            return
        # CDP已暂停这些文档。过载时保持拒绝资格，不能无限建任务或放行导航。
        if len(self._navigation) >= 64:
            self._navigation_error = BrowserError("并发文档请求保护超过64项，须关闭当前会话。")
            return
        task = asyncio.create_task(self._check_navigation(event), name="browser-document-guard")
        self._navigation.add(task)
        task.add_done_callback(self._navigation_done)

    def _navigation_done(self, task: asyncio.Task[None]) -> None:
        self._navigation.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self._navigation_error = error
            _LOG.error(
                "浏览器文档请求保护失败。", exc_info=(type(error), error, error.__traceback__)
            )

    async def _check_navigation(self, event: dict[str, object]) -> None:
        """真实SDK事件边界仅取需要的URL和requestId，不记录Cookie/授权头。"""
        session, policy = self._cdp, self._policy
        request_id, request = event.get("requestId"), event.get("request")
        if session is None or policy is None:
            raise BrowserError("文档保护尚未安装。")
        if not isinstance(request_id, str) or not isinstance(request, dict):
            raise BrowserError("CDP文档请求事件形状无效。")
        url: object = cast(dict[str, object], request).get("url")
        if not isinstance(url, str):
            raise BrowserError("CDP文档请求缺少URL。")
        try:
            policy(url)
        except ValueError:
            await session.send(
                "Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}
            )
            return
        await session.send("Fetch.continueRequest", {"requestId": request_id})

    async def _guard_other_pages(self, route: Route) -> None:
        """SDK拥有的路由对象，popup不能绕过当前页的CDP保护。"""
        from patchright.async_api import Error

        request = route.request
        if request.is_navigation_request():
            try:
                page = request.frame.page
            except Error as error:
                if "Frame for this navigation request is not available" not in str(error):
                    raise
                self._popup_blocked = True
                await route.abort("blockedbyclient")
                return
            if page is not self._page:
                opener = await page.opener()
                while opener is not None:
                    if opener is self._page:
                        self._popup_blocked = True
                        await route.abort("blockedbyclient")
                        return
                    opener = await opener.opener()
        await route.continue_()

    def _require_page(self) -> Page:
        if self._navigation_error is not None:
            raise BrowserError("实际文档导航保护已失败。") from self._navigation_error
        if self._page is None or self._close_task is not None:
            raise BrowserError("浏览器页面尚未就绪或已关闭。")
        return self._page

    def _handle(self, handle: str) -> Locator:
        result = self._handles.get(handle)
        if result is None:
            raise BrowserInputError("snapshot句柄已失效，请重新取得snapshot。")
        return result

    async def open(self, url: str, *, timeout_seconds: float) -> PageView:
        """导航前的session预检之外，每一次请求仍由已安装策略核验。"""
        from patchright.async_api import Error

        self._handles.clear()
        self._popup_blocked = False
        response = await self._require_page().goto(
            url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000
        )
        try:
            return await self._view(None if response is None else response.status)
        except Error as error:
            raise BrowserActionError(
                "导航已完成，但页面状态取得失败。",
                {"operation": "open", "navigation": "confirmed", "url": self._require_page().url},
            ) from error

    async def _view(self, status: int | None = None) -> PageView:
        page = self._require_page()
        title = await page.title()
        return PageView(page.url, title, status, "popup" if self._popup_blocked else None)

    async def view(self) -> PageView:
        """当前身份，不把上次HTTP状态猜成此次读取的状态。"""
        return await self._view()

    async def html(self) -> str:
        """完整渲染HTML；明确SDK读取故障供Session保留已完成交互。"""
        from patchright.async_api import Error

        try:
            return await self._require_page().content()
        except Error as error:
            raise BrowserError("实际页面HTML读取失败。") from error

    async def inner_text(self) -> str:
        """HTML没有可抽取正文时取得实际body文本。"""
        return await self._require_page().inner_text("body")

    async def snapshot(self, *, interactive: bool) -> SnapshotResult:
        """保留原角色/每种最多40个节点，不吞掉实际SDK读取失败。"""
        page = self._require_page()
        self._handles.clear()
        lines = [f'- document "{await page.title()}"']
        refs: dict[str, str] = {}
        roles = _ROLES if interactive else (*_ROLES, "heading")
        for role in roles:
            locator = page.get_by_role(role)
            for offset in range(min(await locator.count(), 40)):
                target = locator.nth(offset)
                if not await target.is_visible():
                    continue
                name = await _accessible_name(target)
                index = len(refs) + 1
                ref, handle = f"e{index}", f"pr:{index}"
                lines.append(f'- {role} "{name.replace(chr(34), chr(39))[:80]}" [ref={ref}]')
                refs[ref], self._handles[handle] = handle, target
        return SnapshotResult("\n".join(lines), refs)

    async def _after_action(self, operation: str) -> PageView:
        """SDK已完成交互后的读取失败必须保留前一阶段真实完成事实。"""
        from patchright.async_api import Error

        try:
            return await self._view()
        except Error as error:
            facts: JsonObject = {
                "operation": operation,
                "interaction": "confirmed",
                "page_state": "failed",
                "error_type": type(error).__name__,
            }
            raise BrowserActionError("实际交互已完成，但后续页面状态取得失败。", facts) from error

    async def click(self, handle: str) -> PageView:
        """只修改_blank目标，不用goto代替真实click handler。"""
        locator = self._handle(handle)
        self._handles.clear()
        self._popup_blocked = False
        await locator.evaluate("""el => {
            const link = el.closest('a[href], area[href]');
            if (!link) return;
            const target = link.getAttribute('target')
                ?? document.querySelector('base[target]')?.getAttribute('target');
            if (target?.toLowerCase() === '_blank') link.setAttribute('target', '_self');
        }""")
        await locator.click(timeout=self.settings.action_timeout_seconds * 1000)
        return await self._after_action("click")

    async def type_text(self, handle: str, text: str, *, delay_ms: float, submit: bool) -> PageView:
        """实际清空/输入/Enter分阶段；不因后续状态失败重做输入。"""
        from patchright.async_api import Error

        locator = self._handle(handle)
        self._handles.clear()
        self._popup_blocked = False
        timeout = self.settings.action_timeout_seconds * 1000
        facts: JsonObject = {
            "operation": "type",
            "focus": "unknown",
            "cleared": "not_started",
            "input": "not_started",
            "submission": "not_started",
        }
        try:
            await locator.click(timeout=timeout)
            facts["focus"], facts["cleared"] = "confirmed", "unknown"
            await locator.fill("", timeout=timeout)
            facts["cleared"], facts["input"] = "confirmed", "unknown"
            if delay_ms > 0:
                await locator.press_sequentially(text, delay=delay_ms, timeout=timeout)
            else:
                await locator.fill(text, timeout=timeout)
            facts["input"] = "confirmed"
            if submit:
                facts["submission"] = "unknown"
                await locator.press("Enter", timeout=timeout)
                facts["submission"] = "confirmed"
        except Error as error:
            # SDK未确认的那一步仍可能已产生效果；前一步确认不能被后来异常抹掉。
            raise BrowserActionError("浏览器输入部分步骤失败，实际效果见回执。", facts) from error
        return await self._after_action("type")

    async def scroll(self, *, direction: str, handle: str | None, amount: int) -> PageView:
        """保留元素滚入/视口滚动，不做未知方向回退。"""
        page = self._require_page()
        if handle is not None:
            await self._handle(handle).scroll_into_view_if_needed(
                timeout=self.settings.action_timeout_seconds * 1000
            )
        elif direction == "top":
            await page.evaluate("window.scrollTo(0,0)")
        elif direction == "bottom":
            await page.evaluate("window.scrollTo(0,document.body.scrollHeight)")
        elif direction in ("up", "down"):
            await page.mouse.wheel(0, -amount if direction == "up" else amount)
        else:
            raise BrowserInputError("未知滚动方向。")
        return await self._after_action("scroll")

    async def screenshot(self, *, full_page: bool) -> ImageCapture:
        """完整JPEG，不先按缓存图片边长裁剪。"""
        data = await self._require_page().screenshot(
            type="jpeg", quality=self.settings.screenshot_quality, full_page=full_page
        )
        return ImageCapture(data, "image/jpeg", "page-screenshot")

    async def save(self, handle: str) -> ImageCapture:
        """原图不可取得时沿用元素截图，但明确保留原图失败原因。"""
        from patchright.async_api import Error

        locator, page = self._handle(handle), self._require_page()
        tag: object = await locator.evaluate("el => el.tagName.toLowerCase()")
        original_error: str | None = None
        if tag == "img":
            src = await locator.get_attribute("src")
            if src:
                try:
                    response = await page.request.get(src)
                    body = await response.body()
                    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
                    if response.ok and body and media_type.startswith("image/"):
                        return ImageCapture(body, media_type, "original-image")
                    original_error = (
                        f"原图响应不可用：status={response.status}，type={media_type}。"
                    )
                except Error as error:
                    original_error = f"原图取得失败：{type(error).__name__}。"
            else:
                original_error = "img没有可取得的src。"
        data = await locator.screenshot(type="jpeg", quality=self.settings.screenshot_quality)
        return ImageCapture(data, "image/jpeg", "element-screenshot", original_error)

    async def close(self) -> None:
        """重复加入同一关闭任务，取消观察不撤销实际资源回收。"""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="browser-driver-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        """失败保留对应引用，仍继续后续清理，避免半关闭被当作可重建资源。"""
        failures: list[BaseException] = []
        callbacks: list[tuple[Callable[[], Awaitable[None]], Callable[[], None]]] = []
        context, session = self._context, self._cdp
        if context is not None and session is not None:
            callbacks.append(
                (lambda: context.unroute("**/*", self._guard_other_pages), lambda: None)
            )
        if session is not None:
            callbacks.append((session.detach, lambda: setattr(self, "_cdp", None)))
        for operation, confirmed in callbacks:
            try:
                await operation()
                confirmed()
            except BaseException as error:
                failures.append(error)
        tasks = tuple(self._navigation)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._browser is not None:
            try:
                await self._browser.close()
                self._browser, self._context, self._page = None, None, None
            except BaseException as error:
                failures.append(error)
        elif self._context is not None:
            try:
                await self._context.close()
                self._context, self._page = None, None
            except BaseException as error:
                failures.append(error)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
                self._playwright = None
            except BaseException as error:
                failures.append(error)
        self._handles.clear()
        if failures:
            raise BaseExceptionGroup("浏览器资源收尾失败。", failures)


async def _accessible_name(locator: Locator) -> str:
    """沿用aria-label、可见文本、placeholder次序，不吞掉真实读取异常。"""
    label = await locator.get_attribute("aria-label")
    if label and label.strip():
        return label.strip()
    text = await locator.inner_text()
    if text.strip():
        return " ".join(text.split())
    placeholder = await locator.get_attribute("placeholder")
    return "" if placeholder is None else placeholder.strip()
