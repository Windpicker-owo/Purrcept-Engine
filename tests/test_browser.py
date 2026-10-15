"""离线网页/合成图像与真实Store、Windows Worker验证浏览器实际入口。

替身只实现BrowserDriver契约，不启动真实浏览器或发送网络请求。SDK保护
另在test_browser_driver验证真实注册回调；此处核对分页、媒体与调用回执。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from PIL import Image
from pydantic import ValidationError

from purrcept_engine import EngineConfig, EngineHost
from purrcept_engine.app.evidence import EvidenceAccess
from purrcept_engine.domain.inputs.models import Admission, InputRecord, InputSubmission, MediaAsset
from purrcept_engine.domain.knowledge.models import EvidenceSnapshot, KnowledgeInputError
from purrcept_engine.domain.subject.models import InputProcessing
from purrcept_engine.errors import ConfigError
from purrcept_engine.execution.catalog import Catalog, ObjectDeclaration
from purrcept_engine.execution.providers import CallContext, CapabilityInputError
from purrcept_engine.plugin_system.contracts import PluginPorts
from purrcept_engine.plugins.browser import plugin
from purrcept_engine.plugins.browser.driver import (
    BrowserActionError,
    BrowserError,
    BrowserInputError,
    ImageCapture,
    PageView,
    SnapshotResult,
)
from purrcept_engine.plugins.browser.locate import resolve_launch
from purrcept_engine.plugins.browser.navigation import NavigationPolicy, normalize_and_authorize_url
from purrcept_engine.plugins.browser.search import build_search_url, parse_search_results
from purrcept_engine.plugins.browser.session import BrowserSession
from purrcept_engine.plugins.browser.settings import BrowserSettings, SearchEngine
from purrcept_engine.storage.store import Store
from purrcept_engine.values import JsonObject, decode_object


def image_bytes(color: int = 0) -> bytes:
    with Image.new("RGB", (17, 9), (color, color, color)) as image, io.BytesIO() as encoded:
        image.save(encoded, format="PNG")
        return encoded.getvalue()


class Inputs:
    """真实Store有限输入端口，本插件所有显式读取不得调用submit。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def submit(self, submission: InputSubmission) -> Admission:
        raise AssertionError("browser explicit reads must not emit foreground input")

    async def query_input(self, input_id: str) -> InputRecord | None:
        return await self.store.get_input(input_id)

    async def query_processing(self, input_id: str) -> InputProcessing | None:
        return await self.store.input_processing(input_id)


class OfflineDriver:
    """只有离线已给材料，调用日志用于证明旧cursor不再访问平台。"""

    def __init__(self) -> None:
        self.started = False
        self.closed = 0
        self.calls: list[tuple[str, object]] = []
        self.page = PageView("https://example.com/page", "离线网页")
        self.content = "<html><body><p>" + "完整网页段落。" * 2000 + "</p></body></html>"
        self.data = image_bytes()
        self.html_error: BrowserError | None = None
        self.start_error: BrowserError | None = None
        self.block_start = False
        self.start_entered, self.start_release = asyncio.Event(), asyncio.Event()
        self.block_close = False
        self.close_entered, self.close_release = asyncio.Event(), asyncio.Event()
        self.policy: NavigationPolicy | None = None

    async def start(self) -> None:
        self.start_entered.set()
        if self.block_start:
            await self.start_release.wait()
        self.started = True
        if self.start_error is not None:
            raise self.start_error

    async def close(self) -> None:
        self.close_entered.set()
        if self.block_close:
            await self.close_release.wait()
        self.closed += 1
        self.started = False

    async def set_navigation_policy(self, policy: NavigationPolicy) -> None:
        self.policy = policy

    async def open(self, url: str, *, timeout_seconds: float) -> PageView:
        self.calls.append(("open", (url, timeout_seconds)))
        assert self.policy is not None
        self.policy(url)
        self.page = PageView(url, "离线网页", 200)
        return self.page

    async def view(self) -> PageView:
        self.calls.append(("view", None))
        return self.page

    async def html(self) -> str:
        self.calls.append(("html", None))
        if self.html_error is not None:
            raise self.html_error
        return self.content

    async def inner_text(self) -> str:
        self.calls.append(("inner_text", None))
        return "可见正文"

    async def snapshot(self, *, interactive: bool) -> SnapshotResult:
        self.calls.append(("snapshot", interactive))
        return SnapshotResult('- textbox "离线输入" [ref=e1]', {"e1": "offline:1"})

    async def click(self, handle: str) -> PageView:
        self.calls.append(("click", handle))
        return self.page

    async def type_text(self, handle: str, text: str, *, delay_ms: float, submit: bool) -> PageView:
        self.calls.append(("type", (handle, text, delay_ms, submit)))
        return self.page

    async def scroll(self, *, direction: str, handle: str | None, amount: int) -> PageView:
        self.calls.append(("scroll", (direction, handle, amount)))
        return self.page

    async def screenshot(self, *, full_page: bool) -> ImageCapture:
        self.calls.append(("screenshot", full_page))
        return ImageCapture(self.data, "image/png", "page-screenshot")

    async def save(self, handle: str) -> ImageCapture:
        self.calls.append(("save", handle))
        return ImageCapture(self.data, "image/png", "original-image")


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    database = await Store.open(tmp_path / "engine.sqlite3")
    try:
        yield database
    finally:
        await database.close()


def session(store: Store, tmp_path: Path, driver: OfflineDriver) -> BrowserSession:
    return BrowserSession(
        "browser",
        driver,
        BrowserSettings(user_data_dir=tmp_path / "profile"),
        EvidenceAccess(store.knowledge, 8000),
    )


@pytest.mark.asyncio
async def test_read_full_original_pagination_and_repeated_cursor_remain_old_page(
    store: Store,
    tmp_path: Path,
) -> None:
    driver = OfflineDriver()
    browser = session(store, tmp_path, driver)
    await browser.start()
    try:
        page = await browser.read(mode="text", max_chars=97)
        assert len(str(page["content"])) == 97 and not page["complete"]
        snapshot = await store.knowledge.get_evidence(str(page["evidence_ref"]))
        assert snapshot is not None and len(snapshot.text) > 8000
        raw = await store.knowledge.get_evidence(str(page["raw_ref"]))
        assert raw is not None and raw.text == driver.content
        cursor = page["next_cursor"]
        assert isinstance(cursor, str)
        await browser.open("https://example.com/new")
        driver.content = "<p>新页面</p>"
        original_calls = list(driver.calls)
        second = await browser.read(cursor=cursor, max_chars=97)
        again = await browser.read(cursor=cursor, max_chars=97)
        assert second == again
        assert second["url"] == "https://example.com/page" and driver.calls == original_calls
        assert second["evidence_ref"] == page["evidence_ref"]
        assert second["range"] == {"unit": "characters", "start": 97, "end": 194}
        assert await store.pending() == ()
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_default_markdown_and_shared_page_size(store: Store, tmp_path: Path) -> None:
    driver = OfflineDriver()
    browser = session(store, tmp_path, driver)
    await browser.start()
    try:
        result = await browser.read()
        assert result["mode"] == "markdown" and len(str(result["content"])) == 8000
        assert isinstance(result["next_cursor"], str)
        assert result["renderer"] in {"trafilatura", "basic-html"}
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_cursor_rejects_new_mode_other_instance_and_unknown(
    store: Store,
    tmp_path: Path,
) -> None:
    driver = OfflineDriver()
    browser = session(store, tmp_path, driver)
    await browser.start()
    try:
        first = await browser.read(max_chars=10)
        cursor = str(first["next_cursor"])
        with pytest.raises(BrowserInputError, match="mode"):
            await browser.read(mode="markdown", cursor=cursor)
        with pytest.raises(KnowledgeInputError):
            await browser.read(cursor="unknown")
        other = BrowserSession("other", driver, browser.settings, browser.evidence)
        with pytest.raises(BrowserInputError, match="另一"):
            await other.read(cursor=cursor)
        with pytest.raises(BrowserInputError, match="正整数"):
            await browser.read(max_chars=True)
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_refs_invalidated_and_actual_typing_defaults(store: Store, tmp_path: Path) -> None:
    driver = OfflineDriver()
    browser = session(store, tmp_path, driver)
    await browser.start()
    try:
        await browser.snapshot(interactive=False)
        await browser.type_text("@e1", "你好", submit=True)
        assert ("type", ("offline:1", "你好", 40.0, True)) in driver.calls
        with pytest.raises(BrowserInputError):
            await browser.click("e1")
        await browser.snapshot(interactive=True)
        await browser.open("https://example.com/new")
        with pytest.raises(BrowserInputError):
            await browser.image(ref="e1")
        await browser.scroll("down", None, 600)
        assert ("scroll", ("down", None, 600)) in driver.calls
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_confirmed_interaction_remains_when_following_html_fails(
    store: Store,
    tmp_path: Path,
) -> None:
    driver = OfflineDriver()
    browser = session(store, tmp_path, driver)
    await browser.start()
    try:
        await browser.snapshot(interactive=True)
        driver.html_error = BrowserError("offline HTML failure")
        with pytest.raises(BrowserActionError) as raised:
            await browser.click("e1")
        assert raised.value.facts["interaction"] == "confirmed"
        assert raised.value.facts["operation"] == "click"
        assert driver.calls.count(("click", "offline:1")) == 1
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_native_screenshot_and_save_preserve_media_without_input(
    store: Store,
    tmp_path: Path,
) -> None:
    driver = OfflineDriver()
    browser = session(store, tmp_path, driver)
    await browser.start()
    try:
        screenshot = await browser.image(ref=None, full_page=True)
        await browser.snapshot(interactive=False)
        saved = await browser.image(ref="e1")
        assert screenshot.state == saved.state == "confirmed"
        assert screenshot.media == saved.media and len(saved.media) == 1
        assert await store.get_media(saved.media[0].media_id) == driver.data
        assert isinstance(saved.value, dict) and saved.value["width"] == 17
        assert await asyncio.to_thread(Path(str(saved.value["path"])).read_bytes) == driver.data
        assert await store.pending() == ()
        evidence = await store.knowledge.get_evidence(str(saved.value["evidence_ref"]))
        assert evidence is not None and evidence.media == saved.media
        assert ("screenshot", True) in driver.calls
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_conflicting_mirror_does_not_overwrite_or_erase_native_media(
    store: Store,
    tmp_path: Path,
) -> None:
    driver = OfflineDriver()
    browser = session(store, tmp_path, driver)
    digest = hashlib.sha256(driver.data).hexdigest()
    path = tmp_path / "profile" / "screenshots" / (digest + ".png")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"user file")
    await browser.start()
    try:
        outcome = await browser.image(ref=None)
        assert outcome.state == "failed" and outcome.error_type == "FileExistsError"
        assert path.read_bytes() == b"user file"
        assert await store.get_media(outcome.media[0].media_id) == driver.data
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_screenshot_cache_only_cleans_owned_files_and_retains_blob(
    store: Store,
    tmp_path: Path,
) -> None:
    driver = OfflineDriver()
    settings = BrowserSettings(user_data_dir=tmp_path / "profile", screenshot_max_files=2)
    browser = BrowserSession("browser", driver, settings, EvidenceAccess(store.knowledge, 8000))
    await browser.start()
    try:
        user_file = settings.user_data_dir / "screenshots" / "mine.png"
        user_file.parent.mkdir()
        user_file.write_bytes(b"owned by user")
        originals: list[str] = []
        for color in (0, 100, 200):
            driver.data = image_bytes(color)
            outcome = await browser.image(ref=None)
            assert isinstance(outcome.value, dict)
            originals.append(str(outcome.value["path"]))
            assert await store.get_media(outcome.media[0].media_id) == driver.data
        exists = await asyncio.gather(*(asyncio.to_thread(Path(item).exists) for item in originals))
        assert exists == [False, True, True]
        assert user_file.read_bytes() == b"owned by user"
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_cache_cleanup_failure_preserves_new_image_receipt(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = OfflineDriver()
    settings = BrowserSettings(user_data_dir=tmp_path / "profile", screenshot_max_files=1)
    browser = BrowserSession("browser", driver, settings, EvidenceAccess(store.knowledge, 8000))
    await browser.start()
    try:
        first = await browser.image(ref=None)
        assert isinstance(first.value, dict)
        expired = Path(str(first.value["path"]))
        original = Path.unlink

        def reject(path: Path, missing_ok: bool = False) -> None:
            if path == expired:
                raise PermissionError("offline locked cache")
            original(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", reject)
        driver.data = image_bytes(100)
        second = await browser.image(ref=None)
        assert second.state == "failed" and second.error_type == "PermissionError"
        assert isinstance(second.value, dict) and second.value["cache_cleanup"] == "failed"
        assert await store.get_media(second.media[0].media_id) == driver.data
        assert await asyncio.to_thread(Path(str(second.value["path"])).read_bytes) == driver.data
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_cancelled_capture_observer_close_waits_actual_blob_commit(
    store: Store,
    tmp_path: Path,
) -> None:
    class GateEvidence(EvidenceAccess):
        def __init__(self) -> None:
            super().__init__(store.knowledge, 8000)
            self.entered, self.release = asyncio.Event(), asyncio.Event()
            self.saved: EvidenceSnapshot | None = None

        async def capture(
            self,
            snapshot: EvidenceSnapshot,
            assets: tuple[MediaAsset, ...],
        ) -> EvidenceSnapshot:
            self.entered.set()
            await self.release.wait()
            self.saved = await super().capture(snapshot, assets)
            return self.saved

    evidence, driver = GateEvidence(), OfflineDriver()
    browser = BrowserSession("browser", driver, BrowserSettings(user_data_dir=tmp_path), evidence)
    await browser.start()
    capture = asyncio.create_task(browser.image(ref=None))
    await evidence.entered.wait()
    capture.cancel()
    with pytest.raises(asyncio.CancelledError):
        await capture
    closing = asyncio.create_task(browser.close())
    evidence.release.set()
    await closing
    assert evidence.saved is not None
    assert await store.get_media(evidence.saved.media[0].media_id) == driver.data
    assert driver.closed == 1 and await store.pending() == ()


@pytest.mark.asyncio
async def test_cancelled_start_and_close_observers_still_release_actual_resource(
    store: Store,
    tmp_path: Path,
) -> None:
    driver = OfflineDriver()
    driver.block_start = driver.block_close = True
    browser = session(store, tmp_path, driver)
    startup = asyncio.create_task(browser.start())
    await driver.start_entered.wait()
    startup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup
    closing = asyncio.create_task(browser.close())
    assert browser.state()["phase"] == "starting"
    driver.start_release.set()
    await driver.close_entered.wait()
    assert browser.state()["phase"] == "closing"
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    driver.close_release.set()
    await browser.close()
    assert driver.closed == 1 and browser.state()["phase"] == "closed"


@pytest.mark.asyncio
async def test_partial_build_failure_closes_taken_driver(store: Store, tmp_path: Path) -> None:
    driver = OfflineDriver()
    driver.start_error = BrowserError("offline start failure")
    blueprint = plugin.make_factory(lambda _: driver).prepare("browser", {}, tmp_path)
    with pytest.raises(BrowserError, match="start failure"):
        await blueprint.build(
            PluginPorts(Inputs(store), lambda: {}, EvidenceAccess(store.knowledge, 8000))
        )
    assert driver.closed == 1


@pytest.mark.asyncio
async def test_capability_parameters_and_partial_effect_classification(
    store: Store,
    tmp_path: Path,
) -> None:
    driver = OfflineDriver()
    blueprint = plugin.make_factory(lambda _: driver).prepare("browser", {}, tmp_path)
    runtime = await blueprint.build(
        PluginPorts(Inputs(store), lambda: {}, EvidenceAccess(store.knowledge, 8000))
    )
    methods = {item.declaration.name: item for item in runtime.capabilities}
    context = CallContext("call", "action", 1e10)
    try:
        invalid: tuple[tuple[str, JsonObject], ...] = (
            ("read", {"max_chars": True}),
            ("scroll", {"amount": -1}),
            ("screenshot", {"full_page": 1}),
            ("open", {"url": "file:///etc/passwd"}),
            ("read", {"mode": "markdown", "cursor": "old"}),
        )
        for name, parameters in invalid:
            with pytest.raises(CapabilityInputError):
                await methods[name].invoke(parameters, context)
        await methods["snapshot"].invoke({}, context)
        driver.html_error = BrowserError("failed after click")
        outcome = await methods["click"].invoke({"ref": "e1"}, context)
        assert outcome.state == "unknown" and isinstance(outcome.value, dict)
        assert outcome.value["interaction"] == "confirmed"
    finally:
        assert runtime.close is not None
        await runtime.close()


def test_pure_catalog_configured_parent_paths_and_schema(tmp_path: Path) -> None:
    driver = OfflineDriver()
    blueprint = plugin.make_factory(lambda _: driver).prepare(
        "work_browser",
        {"computer_id": "work_device", "browser_id": "web"},
        tmp_path,
    )
    catalog = Catalog(
        (
            ObjectDeclaration("subject", "me", "主体"),
            ObjectDeclaration("work_device:computer", "pc", "电脑", "subject"),
            *blueprint.objects,
        ),
        blueprint.capabilities,
    )
    assert not driver.started and not (tmp_path / ".purrcept").exists()
    assert set(catalog.bindings) == {
        "me.pc.web." + name
        for name in (
            "status",
            "open",
            "read",
            "search",
            "snapshot",
            "click",
            "type",
            "scroll",
            "save",
            "screenshot",
        )
    }
    assert "await me.pc.web.read()" in catalog.instructions
    assert "{path}" not in catalog.instructions
    with pytest.raises(ConfigError):
        Catalog(
            (ObjectDeclaration("subject", "me", "主体"), *blueprint.objects), blueprint.capabilities
        )
    with pytest.raises(ValidationError):
        BrowserSettings.model_validate({"owner_id": "removed"})


@pytest.mark.parametrize(
    "url",
    ["file:///data", "javascript:alert(1)", "https://denied.example.com", "https://example.org"],
)
def test_navigation_policy_rejects_unsafe_and_blacklist_precedes_allow(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_and_authorize_url(
            url, allowed_hosts=("*.example.com",), blocked_hosts=("denied.example.com",)
        )
    assert (
        normalize_and_authorize_url(
            "EXAMPLE.com/", allowed_hosts=("*.example.com",), blocked_hosts=()
        )
        == "https://EXAMPLE.com/"
    )


def test_search_actual_formats_and_defaults() -> None:
    settings = BrowserSettings()
    assert (
        build_search_url(settings, "猫 空间")
        == "https://www.bing.com/search?q=%E7%8C%AB+%E7%A9%BA%E9%97%B4"
    )
    bing = '<li class="b_algo"><h2><a href="https://example.com">标题</a></h2><p>摘要</p></li>'
    assert parse_search_results(bing, engine=SearchEngine.BING, limit=8) == [
        {"title": "标题", "url": "https://example.com", "snippet": "摘要"}
    ]
    duck = (
        '<a class="result__a" href="https://example.com">标题</a>'
        '<a class="result__snippet">摘要</a>'
    )
    assert parse_search_results(duck, engine=SearchEngine.DUCKDUCKGO, limit=8) == [
        {"title": "标题", "url": "https://example.com", "snippet": "摘要"}
    ]
    assert settings.typing_pause_seconds("x" * 100) == 1.2


def test_original_program_selection_is_explicit_and_does_not_retry_launch(tmp_path: Path) -> None:
    edge = tmp_path / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.write_bytes(b"offline program marker")
    chosen = resolve_launch(BrowserSettings(), environ={"PROGRAMFILES": str(tmp_path)})
    assert chosen.executable_path == str(edge) and chosen.kind == "msedge"
    with pytest.raises(FileNotFoundError):
        resolve_launch(BrowserSettings(executable_path=tmp_path / "missing"), environ={})


@pytest.mark.asyncio
async def test_real_host_worker_all_browser_paths_and_native_receipts(tmp_path: Path) -> None:
    driver = OfflineDriver()
    payload: dict[str, object] = {
        "host": {"data_dir": tmp_path / "data"},
        "console": False,
        "models": {
            "test": {
                "kind": "generation",
                "model": "openai/offline",
                "api_base": "http://127.0.0.1:9999/v1",
                "api_key": "synthetic",
                "context_window": 100000,
                "max_output_tokens": 1000,
                "capacity_rule": "reference-text-only-v1",
            }
        },
        "subject": {"foreground": "test", "reflection": "test", "summary": "test"},
        "plugins": [
            {"instance_id": "computer", "entry": "computer", "config": {}},
            {"instance_id": "browser", "entry": "offline_browser", "config": {}},
        ],
    }
    host = EngineHost(
        EngineConfig.validate(payload, base_dir=tmp_path),
        plugin_factories={"offline_browser": plugin.make_factory(lambda _: driver)},
    )
    await host.start()
    try:
        action = await host.execute(
            "from world import me\n"
            "await me.computer.browser.status()\n"
            "await me.computer.browser.open(url='https://example.com')\n"
            "await me.computer.browser.read(max_chars=97)\n"
            "await me.computer.browser.search(query='离线')\n"
            "await me.computer.browser.snapshot()\n"
            "await me.computer.browser.click(ref='e1')\n"
            "await me.computer.browser.snapshot(interactive=True)\n"
            "await me.computer.browser.type(ref='e1',text='输入',submit=True)\n"
            "await me.computer.browser.scroll()\n"
            "await me.computer.browser.snapshot()\n"
            "await me.computer.browser.save(ref='e1')\n"
            "return await me.computer.browser.screenshot(full_page=True)"
        )
        assert action.state == "completed" and len(action.calls) == 12
        assert len(action.calls[-1].media) == len(action.calls[-2].media) == 1
        saved = await host.query_action(action.action_id)
        assert saved == action
    finally:
        await host.close()
    reopened = await Store.open(tmp_path / "data" / "engine.sqlite3")
    try:
        assert await reopened.pending() == ()
        saved = await reopened.get_action(action.action_id)
        assert saved is not None and saved.calls[-1].media == action.calls[-1].media
        assert await reopened.get_media(saved.calls[-1].media[0].media_id) == driver.data
        assert saved.calls[2].result_json is not None
        page = decode_object(saved.calls[2].result_json.encode())
        evidence = await reopened.knowledge.get_evidence(str(page["evidence_ref"]))
        assert evidence is not None and len(evidence.text) > 8000
    finally:
        await reopened.close()
