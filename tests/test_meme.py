"""以公开 API 传输替身、真实临时状态和宿主 Worker 验证梗插件。

测试不联网、不调用模型，也不连接实体电脑。覆盖收藏持久化、日更版本比较、
拒收后的可靠重投以及实际 computer 父链，避免只验证声明文字。
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from pydantic import ValidationError

from purrcept_engine import EngineConfig, EngineHost
from purrcept_engine.app.assembly import prepare_plugins
from purrcept_engine.app.config import PluginSettings
from purrcept_engine.errors import ConfigError, StorageError
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system import (
    Admission,
    CallContext,
    CapabilityInputError,
    InputPort,
    InputProcessing,
    InputRecord,
    InputSource,
    InputSubmission,
    PluginPorts,
)
from purrcept_engine.plugins.meme.client import MemeAPIError, MemeClient
from purrcept_engine.plugins.meme.models import MemeSettings
from purrcept_engine.plugins.meme.plugin import factory
from purrcept_engine.plugins.meme.runtime import MemeRuntime, load_state
from purrcept_engine.values import JsonObject, decode_object

NOW = datetime(2026, 10, 26, 2, tzinfo=UTC)


def item(slug: str = "测试梗") -> JsonObject:
    """合成与官方 v1 列表契约一致的词条，详情另补正文。"""
    return {
        "slug": slug,
        "title": slug,
        "summary": "测试来源与使用语境。",
        "tags": ["测试"],
        "updated_at": "2026-10-26T00:00:00Z",
        "cover_url": None,
        "source_video": None,
    }


class API:
    """记录所有只读请求，并按真实分页参数返回可变公开词条。"""

    def __init__(self) -> None:
        self.items: list[JsonObject] = [item()]
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.retry_after: str | None = None
        self.schema_version = 1

    def handle(self, request: httpx.Request) -> httpx.Response:
        """模拟列表及详情，不接受账号凭据或意外站点。"""
        self.requests.append(request)
        assert request.method == "GET" and request.url.host == "cnmeme.wiki"
        assert "cookie" not in request.headers and "authorization" not in request.headers
        if self.status != 200:
            headers = {} if self.retry_after is None else {"Retry-After": self.retry_after}
            return httpx.Response(self.status, text="<html>暂不可用</html>", headers=headers)
        if request.url.path == "/api/v1/public/memes":
            limit = int(request.url.params["limit"])
            offset = int(request.url.params["offset"])
            return httpx.Response(
                200,
                json={
                    "schema_version": self.schema_version,
                    "items": self.items[offset : offset + limit],
                    "limit": limit,
                    "offset": offset,
                    "has_more": offset + limit < len(self.items),
                },
                headers={"Set-Cookie": "ignored=identity"},
            )
        slug = unquote(request.url.path.removeprefix("/api/v1/public/memes/"))
        found = next((value for value in self.items if value["slug"] == slug), None)
        if found is None:
            return httpx.Response(404, text="not public")
        return httpx.Response(
            200,
            json={
                "schema_version": self.schema_version,
                "item": {**found, "content_html": "<p>梗的出处与语境。</p>"},
            },
        )


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> API:
    """在项目禁止联网的默认传输上安装此场景的公开 API 替身。"""
    source = API()
    transport = httpx.MockTransport(source.handle)

    def route(client: httpx.AsyncClient, url: httpx.URL) -> httpx.MockTransport:
        del client, url
        return transport

    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", route)
    return source


class Inputs:
    """只向插件借出真实临时库的正式输入端口。"""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.submissions: list[InputSubmission] = []

    async def submit(self, submission: InputSubmission) -> Admission:
        """记录原文，实际去重和容量约束仍由真实 SQLite 完成。"""
        self.submissions.append(submission)
        return await self.store.admit(submission)

    async def query_input(self, input_id: str) -> InputRecord | None:
        """读取正式接管事实。"""
        return await self.store.get_input(input_id)

    async def query_processing(self, input_id: str) -> InputProcessing | None:
        """读取宿主保存的真实处理状态。"""
        return await self.store.input_processing(input_id)


def runtime(tmp_path: Path, inputs: InputPort, **settings: object) -> MemeRuntime:
    """每次重新读取实际磁盘状态，模拟同一插件的重启。"""
    configuration = MemeSettings.model_validate({"timezone": "UTC", **settings})
    path = tmp_path / "meme.json"
    return MemeRuntime(
        "meme", configuration, MemeClient(2), inputs, path, load_state(path), now=lambda: NOW
    )


async def test_public_api_unicode_pagination_detail_and_cache(api: API) -> None:
    """编码搜索和 slug，列表与详情分别缓存，Cookie 不会流入后续请求。"""
    api.items = [item("中文/梗?#"), item("另一条")]
    client = MemeClient(2)
    try:
        page = await client.search("抽象 & 梗", "测试", 1, 0)
        assert page.has_more and page.items[0].slug == "中文/梗?#"
        request = api.requests[0]
        assert request.url.params["q"] == "抽象 & 梗"
        assert request.url.params["tag"] == "测试"
        assert (await client.search("抽象 & 梗", "测试", 1, 0)) == page
        assert len(api.requests) == 1
        second = await client.search(limit=1, offset=1)
        assert not second.has_more and second.items[0].slug == "另一条"
        detail = await client.detail("中文/梗?#")
        assert detail.content_html == "<p>梗的出处与语境。</p>"
        assert b"%2F" in api.requests[-1].url.raw_path
        assert (await client.detail(detail.slug)) == detail and len(api.requests) == 3
    finally:
        await client.close()


@pytest.mark.parametrize("status", [404, 422, 429, 503])
async def test_http_errors_preserve_retry_after_without_parsing_html(api: API, status: int) -> None:
    api.status, api.retry_after = status, "120"
    client = MemeClient(2)
    try:
        with pytest.raises(MemeAPIError, match=str(status)) as failure:
            await client.detail("测试梗")
        assert failure.value.retry_after == 120
        with pytest.raises(MemeAPIError, match="退避"):
            await client.search()
        assert len(api.requests) == 1
    finally:
        await client.close()


async def test_protocol_failures_are_not_cached_as_empty_search(api: API) -> None:
    api.schema_version = 2
    client = MemeClient(2)
    try:
        with pytest.raises(MemeAPIError, match="契约"):
            await client.search()
        api.schema_version = 1
        assert len((await client.search()).items) == 1
        assert len(api.requests) == 2
    finally:
        await client.close()


async def test_favorites_survive_restart_and_duplicate_is_offline(api: API, tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "engine.sqlite3")
    original = runtime(tmp_path, Inputs(store), daily_enabled=False)
    try:
        added = await original.favorite("测试梗")
        assert added["added"] is True
    finally:
        await original.close()
    restored = runtime(tmp_path, Inputs(store), daily_enabled=False)
    api.status = 503
    try:
        assert (await restored.favorite("测试梗"))["added"] is False
        favorites = await restored.list_favorites(1, 0)
        assert favorites["total"] == 1 and favorites["has_more"] is False
        assert (await restored.list_favorites(1, 1))["items"] == []
        assert len(api.requests) == 1
    finally:
        await restored.close()
        await store.close()


async def test_changed_versions_trigger_one_stimulus_and_no_change_is_quiet(
    api: API, tmp_path: Path
) -> None:
    store = await Store.open(tmp_path / "engine.sqlite3")
    inputs = Inputs(store)
    current = runtime(tmp_path, inputs, daily_enabled=False, max_pages=2, reminder_limit=1)
    api.items = [item(str(index)) for index in range(35)]
    try:
        first = await current.refresh_updates()
        assert first["changed_count"] == 35 and first["truncated"] is False
        assert len(inputs.submissions) == 1
        submitted = inputs.submissions[0]
        assert submitted.context.minimum_wake and submitted.context.presentation is not None
        assert submitted.source.event_kind == "meme.daily"
        digest = decode_object(submitted.text.split("\n", 1)[1].encode())
        assert digest["shown_count"] == 1 and digest["changed_count"] == 35
        second = await current.refresh_updates()
        assert second["input_id"] is None and len(inputs.submissions) == 1
        assert current.state.last_day == "2026-10-26" and current.state.pending is None
    finally:
        await current.close()

    # 重启创建新 HTTP 缓存；即使远端没有更新时间也比较实际摘要变化。
    api.items[0] = {**api.items[0], "updated_at": None, "summary": "新的语境"}
    restored = runtime(tmp_path, inputs, daily_enabled=False, max_pages=2)
    try:
        assert (await restored.refresh_updates())["changed_count"] == 1
        assert len(await store.pending()) == 2
    finally:
        await restored.close()
        await store.close()


async def test_scan_window_reports_truncation(api: API, tmp_path: Path) -> None:
    api.items = [item(str(index)) for index in range(31)]
    store = await Store.open(tmp_path / "engine.sqlite3")
    current = runtime(tmp_path, Inputs(store), daily_enabled=False, max_pages=1)
    try:
        result = await current.refresh_updates()
        assert result["changed_count"] == 30 and result["truncated"] is True
    finally:
        await current.close()
        await store.close()


async def test_capacity_rejection_retains_pending_and_restart_replays_same_event(
    api: API, tmp_path: Path
) -> None:
    del api
    store = await Store.open(tmp_path / "engine.sqlite3", max_inputs=1)
    occupied = await store.admit(
        InputSubmission(InputSource("fixture", "local", "local", "test"), "占位", NOW, NOW)
    )
    inputs = Inputs(store)
    current = runtime(tmp_path, inputs, daily_enabled=False)
    try:
        with pytest.raises(StorageError) as failure:
            await current.refresh_updates()
        assert failure.value.code == "input_capacity"
        pending = load_state(current.path).pending
        assert pending is not None and current.state.last_day is None and not current.state.seen
    finally:
        await current.close()
    await store.complete_input(occupied.input_id)
    restored = runtime(tmp_path, inputs, daily_enabled=False)
    try:
        receipt = await restored.refresh_updates()
        assert receipt["duplicate"] is False
        assert receipt["changed_count"] == 1 and receipt["truncated"] is False
        assert inputs.submissions[0].text == inputs.submissions[1].text
        assert inputs.submissions[0].source == inputs.submissions[1].source
        assert restored.state.pending is None and len(await store.pending()) == 1
    finally:
        await restored.close()
        await store.close()


async def test_crash_after_admission_before_checkpoint_uses_host_deduplication(
    api: API, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api
    store = await Store.open(tmp_path / "engine.sqlite3")
    inputs = Inputs(store)
    current = runtime(tmp_path, inputs, daily_enabled=False)
    replace = os.replace
    writes = 0

    def fail_checkpoint(source: str | Path, target: str | Path) -> None:
        nonlocal writes
        if Path(target) == current.path:
            writes += 1
            if writes == 2:
                raise OSError("模拟接管后断电")
        replace(source, target)

    monkeypatch.setattr(os, "replace", fail_checkpoint)
    try:
        with pytest.raises(OSError, match="断电"):
            await current.refresh_updates()
        assert len(await store.pending()) == 1
    finally:
        await current.close()
    restored = runtime(tmp_path, inputs, daily_enabled=False)
    try:
        receipt = await restored.refresh_updates()
        assert receipt["duplicate"] is True and len(await store.pending()) == 1
        assert inputs.submissions[0].text == inputs.submissions[1].text
    finally:
        await restored.close()
        await store.close()


async def test_cancelled_observer_and_close_wait_for_inflight_admission(
    api: API, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api
    store = await Store.open(tmp_path / "engine.sqlite3")
    inputs = Inputs(store)
    entered, release = asyncio.Event(), asyncio.Event()
    original = inputs.submit

    async def held(submission: InputSubmission) -> Admission:
        entered.set()
        await release.wait()
        return await original(submission)

    monkeypatch.setattr(inputs, "submit", held)
    current = runtime(tmp_path, inputs, daily_enabled=False)
    operation = asyncio.create_task(current.refresh_updates())
    await asyncio.wait_for(entered.wait(), 2)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    closing = asyncio.create_task(current.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(closing, 2)
    assert load_state(current.path).pending is None and len(await store.pending()) == 1
    await store.close()


async def test_scheduler_uses_timezone_and_does_not_repeat_after_restart(
    api: API, tmp_path: Path
) -> None:
    del api
    store = await Store.open(tmp_path / "engine.sqlite3")
    inputs = Inputs(store)
    current = runtime(tmp_path, inputs, timezone="Asia/Shanghai", daily_at="09:00")
    try:
        await current.start()  # UTC 02:00 是北京时间 10:00，已经到期。
        for _ in range(200):
            if current.state.last_day is not None:
                break
            await asyncio.sleep(0.01)
        assert current.state.last_day == "2026-10-26" and len(inputs.submissions) == 1
    finally:
        await current.close()
    restored = runtime(tmp_path, inputs, timezone="Asia/Shanghai", daily_at="09:00")
    try:
        await restored.start()
        await asyncio.sleep(0.02)
        assert len(inputs.submissions) == 1
    finally:
        await restored.close()
        await store.close()


def test_bad_state_and_bad_configuration_do_not_overwrite_favorites(tmp_path: Path) -> None:
    path = tmp_path / "meme.json"
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_state(path)
    assert path.read_text() == "broken"
    for settings in ({"daily_at": "24:00"}, {"timezone": "missing/zone"}, {"max_pages": True}):
        with pytest.raises(ValidationError):
            MemeSettings.model_validate(settings)


def test_pure_declaration_requires_computer_parent_and_uses_custom_visible_path(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError):
        prepare_plugins(
            (PluginSettings(instance_id="meme", entry="meme"),), base_dir=tmp_path, factories={}
        )
    settings = (
        PluginSettings.model_validate(
            {"instance_id": "desktop", "entry": "computer", "config": {"computer_id": "desktop"}}
        ),
        PluginSettings.model_validate(
            {
                "instance_id": "meme",
                "entry": "meme",
                "config": {"computer_id": "desktop", "meme_id": "culture", "timezone": "UTC"},
            }
        ),
    )
    prepared = prepare_plugins(settings, base_dir=tmp_path, factories={})
    assert "me.desktop.culture.search" in prepared.catalog.bindings
    assert not list(tmp_path.iterdir())


async def test_capability_errors_preserve_failed_outcome_and_reject_bad_arguments(
    api: API, tmp_path: Path
) -> None:
    store = await Store.open(tmp_path / "engine.sqlite3")
    plugin = await factory.prepare("meme", {"timezone": "UTC"}, tmp_path).build(
        PluginPorts(Inputs(store), lambda: {})
    )
    by_name = {cap.declaration.name: cap for cap in plugin.capabilities}
    context = CallContext("call", "action", asyncio.get_running_loop().time() + 10)
    try:
        with pytest.raises(CapabilityInputError):
            await by_name["search"].invoke({"limit": True}, context)
        api.status = 404
        result = await by_name["detail"].invoke({"slug": "不存在"}, context)
        assert result.state == "failed" and result.error_type == "MemeAPIError"
    finally:
        assert plugin.close is not None
        await plugin.close()
        await store.close()


async def test_actual_host_worker_can_search_read_and_favorite_without_computer_connection(
    api: API, tmp_path: Path
) -> None:
    del api
    config = EngineConfig.validate(
        {
            "host": {"data_dir": tmp_path / "host"},
            "console": False,
            "models": {
                "test": {
                    "kind": "generation",
                    "model": "openai/offline",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "context_window": 100000,
                    "max_output_tokens": 1000,
                }
            },
            "subject": {"foreground": "test", "reflection": "test", "summary": "test"},
            "plugins": [
                {"instance_id": "computer", "entry": "computer"},
                {
                    "instance_id": "meme",
                    "entry": "meme",
                    "config": {
                        "daily_enabled": False,
                        "timezone": "UTC",
                        "state_dir": str(tmp_path / "meme"),
                    },
                },
            ],
        },
        base_dir=tmp_path,
    )
    host = EngineHost(config)
    await host.start()
    try:
        action = await host.execute(
            "from world import me\n"
            "page = await me.computer.meme.search(query='测试梗')\n"
            "slug = page['items'][0]['slug']\n"
            "detail = await me.computer.meme.detail(slug=slug)\n"
            "saved = await me.computer.meme.favorite(slug=slug)\n"
            "favorites = await me.computer.meme.list_favorites()\n"
            "return {'detail':detail,'saved':saved,'favorites':favorites}"
        )
        assert action.state == "completed" and action.result_json is not None
        value = decode_object(action.result_json.encode())
        assert isinstance(value["favorites"], dict) and value["favorites"]["total"] == 1
        assert len(action.calls) == 4 and all(call.state == "confirmed" for call in action.calls)
    finally:
        await host.close()
