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

from purrcept_engine.app import knowledge_resources as resource_module
from purrcept_engine.app.knowledge_resources import (
    EmbeddingProtocolError,
    EmbeddingSettings,
    KnowledgeResources,
    OpenAIEmbeddingResource,
)
from purrcept_engine.domain.knowledge.models import (
    EvidenceSnapshot,
    KnowledgeCandidate,
    KnowledgeReference,
)
from purrcept_engine.errors import HostStateError
from purrcept_engine.storage.store import Store
from purrcept_engine.subject.knowledge import KnowledgeService, recall_payload
from purrcept_engine.subject.semantic import KnowledgeIndexWorker, VectorRetriever


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    database = await Store.open(tmp_path / "semantic.sqlite3")
    try:
        yield database
    finally:
        await database.close()


async def source(store: Store, identity: str, text: str, *, version: int = 1) -> None:
    """在真实同库版本账本接管本地材料，测试不建立第二份权威连接。"""
    await store.knowledge.put_evidence(
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


async def test_disabled_semantics_maintains_only_local_routes_without_http(store: Store) -> None:
    await source(store, "fact", "中文茶杯")
    resources = KnowledgeResources(
        store.knowledge, store, None, page_chars=8000, can_start=lambda: None
    )
    try:
        await resources.start()

        async def indexed() -> bool:
            if resources.failure is not None:
                raise resources.failure
            return not await store.knowledge.pending_index(route="keyword")

        await wait_for(indexed)
        result = await resources.knowledge.recall("茶杯")
        assert not result.semantic and not result.reranked
        assert result.items[0].reference.id == "fact"
        assert json.loads(recall_payload(result))["coverage"]["semantic"] == "disabled"
        assert await store.knowledge.semantic_search((1.0, 0.0), "absent") == ()
        assert resources.failure is None
    finally:
        await resources.close()
    assert resources.pending_resources == 0


@pytest.mark.parametrize("change", ["new_version", "revoke"])
async def test_external_embedding_await_cannot_install_stale_version_or_revoked_source(
    store: Store,
    change: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await source(store, "fact", "原版本")
    requests: list[tuple[str, ...]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        request_payload = json.loads(request.content)
        texts = tuple(request_payload["input"])
        requests.append(texts)
        if len(requests) == 1:
            if change == "new_version":
                await source(store, "fact", "新版本", version=2)
            else:
                await store.knowledge.revoke_evidence("fact", 1)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [1.0, float(index + 1)]}
                    for index in range(len(texts))
                ]
            },
        )

    embedding = OpenAIEmbeddingResource(settings(), transport=use_transport(monkeypatch, respond))
    resources = KnowledgeResources(
        store.knowledge,
        store,
        settings(),
        page_chars=8000,
        can_start=lambda: None,
        embedding=embedding,
    )
    try:
        await resources.start()

        async def indexed() -> bool:
            if resources.failure is not None:
                raise resources.failure
            return not await store.knowledge.pending_index(route="semantic")

        await wait_for(indexed)
        references = await store.knowledge.semantic_search((1.0, 1.0), settings().model_id)
        if change == "new_version":
            assert requests == [("原版本",), ("新版本",)]
            assert tuple(item.version for item in references) == (2,)
            old = KnowledgeReference(kind="evidence", id="fact", version=1)
            assert await store.knowledge.get_candidate(old) is None
            assert await store.knowledge.get_candidate(old, include_inactive=True) is not None
        else:
            assert requests == [("原版本",)] and references == ()
        assert resources.failure is None
    finally:
        await resources.close()
        await embedding.close()


async def test_changed_vector_space_rebuilds_finite_batches_and_empty_scans_are_readonly(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(33):
        await source(store, f"fact:{index}", f"正文 {index}")
    batches: list[int] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        count = len(payload["input"])
        batches.append(count)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [1.0, float(index + 1)]} for index in range(count)
                ]
            },
        )

    for model_id in ("declared-space-v1", "declared-space-v2"):
        configuration = settings().model_copy(update={"model_id": model_id})
        embedding = OpenAIEmbeddingResource(
            configuration, transport=use_transport(monkeypatch, respond)
        )
        resources = KnowledgeResources(
            store.knowledge,
            store,
            configuration,
            page_chars=8000,
            can_start=lambda: None,
            embedding=embedding,
        )
        try:
            await resources.start()

            async def indexed(
                expected_model: str = model_id, expected_resources: KnowledgeResources = resources
            ) -> bool:
                if expected_resources.failure is not None:
                    raise expected_resources.failure
                return (
                    not await store.knowledge.pending_index(route="semantic")
                    and len(await store.knowledge.semantic_search((1.0, 1.0), expected_model)) == 33
                )

            await wait_for(indexed)
            revision = store.change_revision
            assert await store.knowledge.queue_semantic_rebuild(model_id) == 0
            assert store.change_revision == revision
            assert resources.failure is None
        finally:
            await resources.close()
            await embedding.close()
    assert batches == [32, 1, 32, 1]


async def test_configured_protocol_failure_retains_jobs_and_does_not_fallback(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await source(store, "fact", "真实茶杯")

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.0, 0.0]}]})

    embedding = OpenAIEmbeddingResource(settings(), transport=use_transport(monkeypatch, respond))
    resources = KnowledgeResources(
        store.knowledge,
        store,
        settings(),
        page_chars=8000,
        can_start=lambda: None,
        embedding=embedding,
    )
    try:
        await resources.start()

        async def failed() -> bool:
            return resources.failure is not None

        await wait_for(failed)
        assert isinstance(resources.failure, EmbeddingProtocolError)
        assert len(await store.knowledge.pending_index(route="semantic")) == 1
        with pytest.raises(EmbeddingProtocolError):
            await resources.knowledge.recall("茶杯")
        assert await store.knowledge.semantic_search((1.0, 0.0), settings().model_id) == ()
    finally:
        await resources.close()
        await embedding.close()


async def test_rrf_and_injected_reranking_recheck_sources_after_external_await(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await source(store, "tea", "真实茶杯")
    await source(store, "coffee", "咖啡")
    for identity, vector in (("tea", (1.0, 0.0)), ("coffee", (0.0, 1.0))):
        await store.knowledge.apply_index(
            KnowledgeReference(kind="evidence", id=identity, version=1),
            terms=identity,
            vector=vector,
            model=settings().model_id,
        )

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    class Reranker:
        async def rank(
            self, query: str, candidates: tuple[KnowledgeCandidate, ...]
        ) -> tuple[KnowledgeReference, ...]:
            assert candidates[0].reference.id == "tea"
            assert dict(candidates[0].ranks) == {"keyword": 1, "semantic": 1}
            await store.knowledge.revoke_evidence("coffee", 1)
            return tuple(item.reference for item in reversed(candidates))

    embedding = OpenAIEmbeddingResource(settings(), transport=use_transport(monkeypatch, respond))
    try:
        service = KnowledgeService(
            store.knowledge,
            semantic=VectorRetriever(store.knowledge, embedding),
            reranker=Reranker(),
        )
        result = await service.recall("茶杯")
        assert result.semantic and result.reranked
        assert tuple(item.reference.id for item in result.items) == ("tea",)
    finally:
        await embedding.close()


async def test_worker_close_waits_all_real_commit_returns_before_reporting_original_failure(
    store: Store,
) -> None:
    """一项提交失败不能使另一个仍借用数据库的接管提交失去关闭等待。"""

    class SubmittedWorker(KnowledgeIndexWorker):
        """只暴露测试需要的接管点，真实提交所有权仍由母类实现。"""

        async def submit(self, operation: Awaitable[bool]) -> bool:
            return await self._commit(operation)

    worker = SubmittedWorker(store.knowledge, store, None, can_start=lambda: None)
    entered, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    fail_now, failure_entered = asyncio.Event(), asyncio.Event()
    original = RuntimeError("原提交错误")

    async def failed() -> bool:
        failure_entered.set()
        await fail_now.wait()
        raise original

    async def delayed() -> bool:
        entered.set()
        await release.wait()
        returned.set()
        return True

    first = asyncio.create_task(worker.submit(failed()))
    second = asyncio.create_task(worker.submit(delayed()))
    await entered.wait()
    await failure_entered.wait()
    first.cancel()
    second.cancel()
    await asyncio.gather(first, second, return_exceptions=True)
    closing = asyncio.create_task(worker.close())
    try:
        fail_now.set()
        await asyncio.sleep(0.01)
        assert not closing.done() and worker.pending_resources > 0 and not returned.is_set()
    finally:
        release.set()
    with pytest.raises(BaseExceptionGroup) as caught:
        await closing
    assert caught.value.exceptions == (original,)
    assert returned.is_set() and worker.pending_resources == 0


async def test_resource_close_recovers_owned_http_after_index_failure_and_keeps_both_errors(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已结束的索引错误不跳过自有连接池清理，两条原错误都保留。"""

    class OwnedEmbedding(OpenAIEmbeddingResource):
        """公开测试观测点，连接池仍由真实资源的关闭顺序释放。"""

        @property
        def http(self) -> httpx.AsyncClient:
            return self._http

    async def respond(request: httpx.Request) -> httpx.Response:
        raise AssertionError("清理测试不应启动模型请求。")

    embedding = OwnedEmbedding(settings(), transport=use_transport(monkeypatch, respond))

    def factory(configuration: EmbeddingSettings) -> OpenAIEmbeddingResource:
        return embedding

    monkeypatch.setattr(resource_module, "OpenAIEmbeddingResource", factory)
    resources = KnowledgeResources(
        store.knowledge, store, settings(), page_chars=8000, can_start=lambda: None
    )
    original, cleanup = RuntimeError("原索引收尾错误"), RuntimeError("原HTTP收尾错误")
    released = asyncio.Event()
    close_http = embedding.http.aclose

    async def close_index() -> None:
        raise original

    async def close_transport() -> None:
        await close_http()
        released.set()
        raise cleanup

    monkeypatch.setattr(resources.indexer, "close", close_index)
    monkeypatch.setattr(embedding.http, "aclose", close_transport)
    with pytest.raises(BaseExceptionGroup) as caught:
        await resources.close()
    assert caught.value.exceptions == (original, cleanup)
    assert released.is_set() and embedding.http.is_closed
