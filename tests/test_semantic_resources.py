"""用真实 HTTPX 边界及同库版本事务验证可选语义资源，不调用真实服务。

MockTransport 只替代远端响应，解析、取消、关闭和派生索引均使用 Engine 实现。
固定测试向量只证明顺序、版本和资源契约，不作为语义质量或模型体验证据。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from purrcept_engine.domain.evidence.models import (
    EvidenceSnapshot,
)
from purrcept_engine.domain.notebook.embedding import (
    EmbeddingProtocolError,
)
from purrcept_engine.domain.notebook.settings import EmbeddingSettings
from purrcept_engine.errors import HostStateError
from purrcept_engine.infrastructure.models.embedding import OpenAIEmbeddingResource
from purrcept_engine.infrastructure.sqlite.store import Store


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    database = await Store.open(tmp_path / "semantic.sqlite3")
    try:
        yield database
    finally:
        await database.close()


async def source(store: Store, identity: str, text: str, *, version: int = 1) -> None:
    """在真实同库版本账本接管本地材料，测试不建立第二份权威连接。"""
    await store.evidence.put_evidence(
        EvidenceSnapshot(
            evidence_id=identity,
            version=version,
            kind="test",
            text=text,
            source_json='{"adapter":"local"}',
            occurred_at=10.0,
        ),
    )


async def wait_for(predicate: Callable[[], Awaitable[bool]]) -> None:
    """有界等候真实提交，不把模型替身返回等同于数据库已安装。"""
    async with asyncio.timeout(3):
        while not await predicate():
            try:
                async with asyncio.timeout(0.005):
                    await asyncio.Event().wait()
            except TimeoutError:
                pass


def settings(*, key: str | None = "explicit-key", timeout: float = 30.0) -> EmbeddingSettings:
    """只声明本测试传输替身的固定端点，不读取用户 TOML 或环境凭据。"""
    return EmbeddingSettings(
        model="openai/declared-embedding",
        model_id="declared-space-v1",
        api_base="https://embedding.invalid/v1",
        api_key=None if key is None else SecretStr(key),
        timeout_seconds=timeout,
    )


def use_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], Coroutine[None, None, httpx.Response]],
) -> httpx.MockTransport:
    """覆盖全局离线拒绝器，只把本测试明确声明的请求交给记录替身。"""
    transport = httpx.MockTransport(handler)

    def select(client: httpx.AsyncClient, url: httpx.URL) -> httpx.MockTransport:
        return transport

    def initialize(
        client: httpx.AsyncClient, *args: object, **kwargs: object
    ) -> httpx.MockTransport:
        return transport

    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", select)
    monkeypatch.setattr(httpx.AsyncClient, "_init_transport", initialize)
    return transport


@pytest.mark.parametrize("key", [None, "explicit-key"])
async def test_actual_embedding_request_has_explicit_origin_and_restores_index_order(
    key: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """环境账号和代理不能进入请求，Provider 返回的倒序由真实 index 还原。"""
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key")
    monkeypatch.setenv("OPENAI_ORG_ID", "ambient-organization")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "ambient-project")
    monkeypatch.setenv("HTTP_PROXY", "http://ambient.invalid")
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url == "https://embedding.invalid/v1/embeddings"
        assert json.loads(request.content) == {
            "model": "declared-embedding",
            "input": ["第一份", "第二份"],
        }
        assert request.headers.get("authorization") == (
            None if key is None else "Bearer explicit-key"
        )
        assert (
            "openai-organization" not in request.headers and "openai-project" not in request.headers
        )
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 2.0]},
                    {"index": 0, "embedding": [1.0, 0.5]},
                ]
            },
        )

    resource = OpenAIEmbeddingResource(
        settings(key=key), transport=use_transport(monkeypatch, respond)
    )
    try:
        assert resource.model_id == "declared-space-v1"
        assert await resource.embed(("第一份", "第二份")) == ((1.0, 0.5), (0.0, 2.0))
        assert len(requests) == 1 and resource.pending_resources == 0
        assert await resource.embed(()) == () and len(requests) == 1
    finally:
        await asyncio.gather(resource.close(), resource.close())
    with pytest.raises(HostStateError):
        await resource.embed(("关闭后",))


@pytest.mark.parametrize(
    "body",
    [
        '{"data":[{"index":0,"embedding":[1.0]}]}',
        '{"data":[{"index":0,"embedding":[1.0]},{"index":0,"embedding":[2.0]}]}',
        '{"data":[{"index":false,"embedding":[1.0]},{"index":1,"embedding":[2.0]}]}',
        '{"data":[{"index":0,"embedding":[0.0]},{"index":1,"embedding":[2.0]}]}',
        '{"data":[{"index":0,"embedding":[1.0]},{"index":1,"embedding":[2.0,3.0]}]}',
        '{"data":[{"index":0,"embedding":["1"]},{"index":1,"embedding":[2.0]}]}',
        '{"data":[{"index":0,"embedding":[NaN]},{"index":1,"embedding":[2.0]}]}',
        '{"data":[],"data":[{"index":0,"embedding":[1.0]},{"index":1,"embedding":[2.0]}]}',
    ],
)
async def test_malformed_provider_vectors_are_not_repaired_or_retried(
    body: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=body, request=request)

    resource = OpenAIEmbeddingResource(settings(), transport=use_transport(monkeypatch, respond))
    try:
        with pytest.raises(EmbeddingProtocolError):
            await resource.embed(("one", "two"))
        assert calls == 1
    finally:
        await resource.close()


async def test_network_failure_and_http_rejection_have_no_hidden_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("本地传输替身故障", request=request)
        return httpx.Response(429, json={"error": {"message": "capacity"}})

    resource = OpenAIEmbeddingResource(settings(), transport=use_transport(monkeypatch, respond))
    try:
        with pytest.raises(httpx.ConnectError):
            await resource.embed(("request",))
        assert calls == 1
        with pytest.raises(httpx.HTTPStatusError):
            await resource.embed(("another independent request",))
        assert calls == 2
    finally:
        await resource.close()


@pytest.mark.parametrize("operation", ["cancel", "close", "timeout"])
async def test_request_cancellation_close_and_timeout_wait_for_actual_transport(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, returned = asyncio.Event(), asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        entered.set()
        try:
            await asyncio.Event().wait()
            return httpx.Response(200, json={"data": []})
        finally:
            returned.set()

    resource = OpenAIEmbeddingResource(
        settings(timeout=0.05 if operation == "timeout" else 30.0),
        transport=use_transport(monkeypatch, respond),
    )
    pending = asyncio.create_task(resource.embed(("request",)))
    try:
        async with asyncio.timeout(1):
            await entered.wait()
        if operation == "cancel":
            pending.cancel()
        elif operation == "close":
            await resource.close()
        with pytest.raises(TimeoutError if operation == "timeout" else asyncio.CancelledError):
            await pending
        assert returned.is_set() and resource.pending_resources == 0
    finally:
        await resource.close()


@pytest.mark.parametrize(
    "patch",
    [
        {"model": "other/provider"},
        {"model": "openai/"},
        {"api_base": ""},
        {"api_base": "https://user:password@embedding.invalid"},
        {"api_base": "https://embedding.invalid?key=secret"},
        {"timeout_seconds": 0.0},
        {"timeout_seconds": float("inf")},
        {"api_key": SecretStr("")},
    ],
)
def test_embedding_configuration_is_pure_strict_and_explicit(patch: dict[str, object]) -> None:
    raw = settings().model_dump()
    with pytest.raises(ValidationError):
        EmbeddingSettings.model_validate({**raw, **patch})
