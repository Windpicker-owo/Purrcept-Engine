"""只替换SDK对象取得接缝，验证真实PatchrightDriver注册与执行保护流程。

不启动浏览器、不发HTTP。窄替身实现本批实际使用的SDK方法，类型cast仅在
SDK回调要求具体Route的位置确认替身边界，不借助Any或跳过生产参数校验。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal, cast

import pytest
from patchright import async_api
from patchright.async_api import Error, Route

from purrcept_engine.execution.protocol import JsonObject
from purrcept_engine.plugins.browser.driver import (
    BrowserActionError,
    BrowserError,
    BrowserInputError,
)
from purrcept_engine.plugins.browser.patchright import PatchrightDriver
from purrcept_engine.plugins.browser.settings import BrowserSettings


class DocumentSession:
    """公开CDP JSON回调替身，事件ACK留日志，能冻结实际发送以验证有界任务。"""

    def __init__(self) -> None:
        self.callback: Callable[[dict[str, object]], None] | None = None
        self.sent: list[tuple[str, JsonObject | None]] = []
        self.changed = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.detached = False

    def on(self, event: str, callback: Callable[[dict[str, object]], None]) -> None:
        assert event == "Fetch.requestPaused"
        self.callback = callback

    async def send(self, method: str, params: JsonObject | None = None) -> object:
        if self.block:
            await self.release.wait()
        self.sent.append((method, params))
        self.changed.set()
        return {}

    async def detach(self) -> None:
        self.detached = True

    async def event(self, request_id: str, url: str) -> None:
        assert self.callback is not None
        self.changed.clear()
        self.callback({"requestId": request_id, "request": {"url": url}})
        await asyncio.wait_for(self.changed.wait(), 1)


class Locator:
    """确认次序/默认参数替身，Enter失败仍可检验前面已写文本。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.enter_error = False

    async def count(self) -> int:
        return 1

    def nth(self, offset: int) -> Locator:
        assert offset == 0
        return self

    async def is_visible(self) -> bool:
        return True

    async def get_attribute(self, name: str) -> str | None:
        return "示例" if name == "aria-label" else None

    async def inner_text(self) -> str:
        return "示例"

    async def evaluate(self, expression: str) -> object:
        self.calls.append(("evaluate", expression))
        return "button"

    async def click(self, **options: float) -> None:
        self.calls.append(("click", options["timeout"]))

    async def fill(self, text: str, **options: float) -> None:
        self.calls.append(("fill", (text, options["timeout"])))

    async def press_sequentially(self, text: str, *, delay: float, **options: float) -> None:
        self.calls.append(("type", (text, delay, options["timeout"])))

    async def press(self, text: str, **options: float) -> None:
        self.calls.append(("press", (text, options["timeout"])))
        if self.enter_error:
            raise Error("offline Enter failure")

    async def screenshot(self, *, type: Literal["jpeg"], quality: int) -> bytes:
        self.calls.append(("screenshot", (type, quality)))
        return b"offline image"


class Page:
    """只有当前URL和题名，没有网络或真实页面对象。"""

    def __init__(self, *, opener: Page | None = None) -> None:
        self.url = "https://allowed.example/start"
        self.locator = Locator()
        self.parent = opener
        self.title_error = False
        self.closed = False
        self.navigated: Callable[[object], None] | None = None

    def on(self, event: str, callback: Callable[[object], None]) -> None:
        assert event == "framenavigated"
        self.navigated = callback

    def is_closed(self) -> bool:
        return self.closed

    async def title(self) -> str:
        if self.title_error:
            raise Error("offline title failure")
        return "离线网页"

    async def content(self) -> str:
        return "<p>离线内容</p>"

    async def opener(self) -> Page | None:
        return self.parent

    def get_by_role(self, role: str) -> Locator:
        return self.locator


class Frame:
    def __init__(self, page: Page) -> None:
        self.page = page


class Request:
    def __init__(self, page: Page | None, *, document: bool = True) -> None:
        self.page = page
        self.document = document

    def is_navigation_request(self) -> bool:
        return self.document

    @property
    def frame(self) -> Frame:
        if self.page is None:
            raise Error("Frame for this navigation request is not available")
        return Frame(self.page)


class RouteSpy:
    def __init__(self, page: Page | None, *, document: bool = True) -> None:
        self.request = Request(page, document=document)
        self.calls: list[str] = []

    async def abort(self, reason: str) -> None:
        self.calls.append("abort:" + reason)

    async def continue_(self) -> None:
        self.calls.append("continue")


class Context:
    """记下生产注册的真实route回调，不另写相同策略算法。"""

    def __init__(self) -> None:
        self.pages = [Page()]
        self.session = DocumentSession()
        self.guard: Callable[[Route], Awaitable[None]] | None = None
        self.unrouted = False
        self.closed = False

    async def new_cdp_session(self, page: Page) -> DocumentSession:
        assert page is self.pages[0]
        return self.session

    async def route(self, pattern: str, callback: Callable[[Route], Awaitable[None]]) -> None:
        assert pattern == "**/*"
        self.guard = callback

    async def unroute(self, pattern: str, callback: Callable[[Route], Awaitable[None]]) -> None:
        assert pattern == "**/*" and callback == self.guard
        self.unrouted = True

    async def close(self) -> None:
        self.closed = True
        for page in self.pages:
            page.closed = True


class Browser:
    def __init__(self, context: Context) -> None:
        self.contexts = [context]
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        await self.contexts[0].close()


class Chromium:
    def __init__(self, browser: Browser) -> None:
        self.browser = browser
        self.calls: list[str] = []

    async def connect_over_cdp(self, url: str) -> Browser:
        self.calls.append(url)
        return self.browser


class Playwright:
    def __init__(self, browser: Browser) -> None:
        self.chromium = Chromium(browser)
        self.stopped = False

    async def start(self) -> Playwright:
        return self

    async def stop(self) -> None:
        self.stopped = True


def setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[PatchrightDriver, Context, Playwright]:
    context = Context()
    sdk = Playwright(Browser(context))
    monkeypatch.setattr(async_api, "async_playwright", lambda: sdk)
    settings = BrowserSettings(user_data_dir=tmp_path, cdp_url="http://offline.invalid:9222")
    return PatchrightDriver(settings), context, sdk


def policy(url: str) -> str:
    if not url.startswith("https://allowed.example/"):
        raise BrowserInputError("offline blocked URL")
    return url


@pytest.mark.asyncio
async def test_registered_document_guard_checks_each_redirect_without_subresource_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    driver, context, sdk = setup(monkeypatch, tmp_path)
    await driver.start()
    try:
        await driver.set_navigation_policy(policy)
        session = context.session
        assert session.sent[:2] == [
            ("Network.setBypassServiceWorker", {"bypass": True}),
            (
                "Fetch.enable",
                {"patterns": [{"resourceType": "Document", "requestStage": "Request"}]},
            ),
        ]
        await session.event("first", "https://allowed.example/start")
        await session.event("redirect", "https://blocked.example/after")
        assert session.sent[-2:] == [
            ("Fetch.continueRequest", {"requestId": "first"}),
            ("Fetch.failRequest", {"requestId": "redirect", "errorReason": "BlockedByClient"}),
        ]
        assert context.guard is not None
        subresource = RouteSpy(None, document=False)
        await context.guard(cast(Route, subresource))
        assert subresource.calls == ["continue"]
    finally:
        await driver.close()
    assert context.unrouted and context.closed and context.session.detached and sdk.stopped
    assert sdk.chromium.calls == ["http://offline.invalid:9222"]


@pytest.mark.asyncio
async def test_registered_route_blocks_popup_unknown_frame_and_descendants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    driver, context, _ = setup(monkeypatch, tmp_path)
    await driver.start()
    try:
        await driver.set_navigation_policy(policy)
        assert context.guard is not None
        for page in (
            None,
            Page(opener=context.pages[0]),
            Page(opener=Page(opener=context.pages[0])),
        ):
            route = RouteSpy(page)
            await context.guard(cast(Route, route))
            assert route.calls == ["abort:blockedbyclient"]
        unrelated = RouteSpy(Page())
        await context.guard(cast(Route, unrelated))
        assert unrelated.calls == ["continue"]
        assert (await driver.view()).blocked == "popup"
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_real_click_keeps_handler_and_actual_navigation_invalidates_handles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    driver, context, _ = setup(monkeypatch, tmp_path)
    await driver.start()
    try:
        await driver.set_navigation_policy(policy)
        snapshot = await driver.snapshot(interactive=True)
        handle = snapshot.refs["e1"]
        await driver.click(handle)
        calls = context.pages[0].locator.calls
        assert calls[0][0] == "evaluate" and "'_blank'" in str(calls[0][1])
        assert "'_self'" in str(calls[0][1]) and calls[1] == ("click", 30000.0)
        snapshot = await driver.snapshot(interactive=False)
        callback = context.pages[0].navigated
        assert callback is not None
        callback(object())
        with pytest.raises(BrowserInputError):
            await driver.click(snapshot.refs["e1"])
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_enter_failure_preserves_confirmed_text_and_unknown_submission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    driver, context, _ = setup(monkeypatch, tmp_path)
    await driver.start()
    try:
        await driver.set_navigation_policy(policy)
        snapshot = await driver.snapshot(interactive=True)
        context.pages[0].locator.enter_error = True
        with pytest.raises(BrowserActionError) as raised:
            await driver.type_text(snapshot.refs["e1"], "测试", delay_ms=40, submit=True)
        assert raised.value.facts == {
            "operation": "type",
            "focus": "confirmed",
            "cleared": "confirmed",
            "input": "confirmed",
            "submission": "unknown",
        }
        assert context.pages[0].locator.calls == [
            ("click", 30000.0),
            ("fill", ("", 30000.0)),
            ("type", ("测试", 40, 30000.0)),
            ("press", ("Enter", 30000.0)),
        ]
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_bounded_document_guard_overload_fails_closed_and_releases_tasks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    driver, context, _ = setup(monkeypatch, tmp_path)
    await driver.start()
    try:
        await driver.set_navigation_policy(policy)
        context.session.block = True
        callback = context.session.callback
        assert callback is not None
        for offset in range(65):
            callback({"requestId": str(offset), "request": {"url": "https://allowed.example/doc"}})
        with pytest.raises(BrowserError, match="保护已失败"):
            await driver.view()
    finally:
        await driver.close()
    assert context.session.detached and context.closed
