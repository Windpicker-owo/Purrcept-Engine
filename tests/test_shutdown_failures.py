"""用真实 Host 和临时 SQLite 验证故障关闭的资源所有权边界。

合成 Provider 与本地模型只制造可控制的等待或程序故障，不连接设备、网络或
真实模型。测试同时检查原始异常、真实存储线程结束、目录锁和重复关闭；
故意保留资源的场景由测试夹具最终归还，避免失败用例留下非 daemon 线程。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from purrcept_core.models import Message, ModelEventSink, ModelRequest, ModelResponse
from pydantic import BaseModel, ConfigDict

from purrcept_engine import EngineConfig, EngineHost, InputSource, InputSubmission, InstanceLocked
from purrcept_engine.domain.execution.catalog import CapabilityDeclaration, ObjectDeclaration
from purrcept_engine.domain.execution.providers import CallContext, CallOutcome, FunctionCapability
from purrcept_engine.domain.knowledge.semantic import KnowledgeIndexWorker
from purrcept_engine.infrastructure.locking import InstanceLock
from purrcept_engine.infrastructure.models.embedding import OpenAIEmbeddingResource
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system.contracts import (
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
)


@dataclass
class _Resources:
    """记录真实存储与锁，只在测试场景结束后兜底收回故意保留的资源。"""

    stores: list[Store] = field(default_factory=list[Store])
    locks: list[InstanceLock] = field(default_factory=list[InstanceLock])
    threads: list[threading.Thread] = field(default_factory=list[threading.Thread])
    store_closes: int = 0

    async def assert_released(self, directory: Path) -> None:
        """实际 join 存储线程并重新取得 OS 锁，字段清空不作为释放证据。"""
        for thread in self.threads:
            await asyncio.to_thread(thread.join, 1.0)
            assert not thread.is_alive()
        probe = InstanceLock(directory)
        probe.acquire()
        probe.release()


@pytest.fixture
async def resources(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Resources]:
    """观察生产资源取得与释放；异常断言失败也不遗留真实 SQLite 线程。"""
    owned = _Resources()
    original_open, original_close = Store.open, Store.close
    original_acquire = InstanceLock.acquire

    async def open_store(
        cls: type[Store],
        path: Path,
        *,
        max_inputs: int = 1024,
        max_input_bytes: int = 256 * 1024 * 1024,
    ) -> Store:
        del cls
        previous = set(threading.enumerate())
        store = await original_open(path, max_inputs=max_inputs, max_input_bytes=max_input_bytes)
        owned.stores.append(store)
        owned.threads.extend(
            thread
            for thread in threading.enumerate()
            if thread not in previous and thread.name == "purrcept-storage"
        )
        return store

    async def close_store(store: Store) -> None:
        owned.store_closes += 1
        await original_close(store)

    def acquire_lock(lock: InstanceLock) -> None:
        original_acquire(lock)
        owned.locks.append(lock)

    monkeypatch.setattr(Store, "open", classmethod(open_store))
    monkeypatch.setattr(Store, "close", close_store)
    monkeypatch.setattr(InstanceLock, "acquire", acquire_lock)
    try:
        yield owned
    finally:
        # 某些用例故意证明 Host 仍须持有 Store；只有场景和借用已经结束后
        # 夹具才显式收回这些真实资源，不冒充 Host 已完成关闭。
        for store in owned.stores:
            await original_close(store)
        for thread in owned.threads:
            await asyncio.to_thread(thread.join, 1.0)
        for lock in owned.locks:
            lock.release()


class _Backend:
    """只返回本地完整响应，指定时保留同一个原始程序错误对象。"""

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.entered = asyncio.Event()

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        """真实 Core 仍拥有请求包装，本替身不取得网络或设备资源。"""
        del request, model, emit
        self.entered.set()
        if self.failure is not None:
            raise self.failure
        return ModelResponse(Message.assistant("本地结束"))


class _Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    text: str


class _Plugin:
    """独立本地 Provider，实际借用由事件控制，生命周期故障可显式注入。"""

    def __init__(
        self,
        *,
        held: bool = False,
        stop_failure: Exception | None = None,
        close_failure: Exception | None = None,
        synchronous_close_failure: bool = False,
        released_after_close: bool | None = None,
        release_proof_failure: Exception | None = None,
    ) -> None:
        self.held, self.stop_failure, self.close_failure = held, stop_failure, close_failure
        self.synchronous_close_failure = synchronous_close_failure
        self.released_after_close = released_after_close
        self.release_proof_failure = release_proof_failure
        self.released = False
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.returned = False
        self.stop_count, self.close_count = 0, 0
        self.contexts: list[CallContext] = []

    def factory(self) -> PluginFactory:
        """纯声明为每个实例建立自己的能力，不取得 Provider 运行资源。"""

        def prepare(identity: str, raw: object, base_dir: Path) -> PluginBlueprint:
            del raw, base_dir
            declaration = CapabilityDeclaration(
                f"{identity}.call",
                identity,
                "call",
                "记录本地调用的真实结束。",
                _Parameters,
                5.0,
                f'await me.{identity}.call(text="local")',
            )

            async def build(ports: PluginPorts) -> PluginRuntime:
                del ports

                def close() -> Awaitable[None]:
                    # 生命周期可在构造 Awaitable 前失败，这仍是唯一关闭的结果。
                    if self.synchronous_close_failure:
                        self.close_count += 1
                        self.released = self.released_after_close is True
                        assert self.close_failure is not None
                        raise self.close_failure
                    return self.close()

                return PluginRuntime(
                    (FunctionCapability(declaration, self.invoke),),
                    stop_inputs=self.stop_inputs,
                    close=close,
                    resources_released=(
                        self.release_proof if self.released_after_close is not None else None
                    ),
                )

            return PluginBlueprint(
                (ObjectDeclaration(identity, identity, "本地资源", "subject"),),
                (declaration,),
                build,
            )

        return PluginFactory(prepare)

    async def invoke(self, parameters: _Parameters, context: CallContext) -> CallOutcome:
        """观察者离开后仍持有真实借用，释放事件才表示 Provider 已归还。"""
        self.contexts.append(context)
        self.entered.set()
        if self.held:
            await self.release.wait()
        self.returned = True
        return CallOutcome("confirmed", {"text": parameters.text})

    async def stop_inputs(self) -> None:
        """记录唯一停止请求；停止失败不产生额外工作或新的真实资源。"""
        self.stop_count += 1
        if self.stop_failure is not None:
            raise self.stop_failure

    async def close(self) -> None:
        """没有归还的 Provider 借用时拒绝关闭；指定故障保留资源未确认。"""
        self.close_count += 1
        assert not self.held or self.returned
        self.released = self.released_after_close is True
        if self.close_failure is not None:
            raise self.close_failure

    def release_proof(self) -> bool:
        """只报告本替身实际关闭后的明证，损坏的明证保持原始错误。"""
        if self.release_proof_failure is not None:
            raise self.release_proof_failure
        return self.released


def _config(
    directory: Path,
    plugins: tuple[str, ...] = (),
    *,
    close_seconds: float = 3.0,
    embedding: bool = False,
) -> EngineConfig:
    """使用纯本地模型与 Provider；关闭期限缩短以验证所有权观察。"""
    raw: dict[str, object] = {
        "host": {"data_dir": directory, "close_seconds": close_seconds},
        "console": False,
        "models": {
            "main": {
                "kind": "generation",
                "model": "openai/local",
                "api_base": "http://127.0.0.1:9999/v1",
                "api_key": "synthetic",
                "context_window": 100000,
                "max_output_tokens": 1000,
                "capacity_rule": "reference-text-only-v1",
            }
        },
        "subject": {
            "foreground": "main",
            "reflection": "main",
            "summary": "main",
        },
        "plugins": [{"instance_id": identity, "entry": identity} for identity in plugins],
    }
    if embedding:
        raw["knowledge"] = {
            "embedding": {
                "model": "openai/local-embedding",
                "model_id": "local-space",
                "api_base": "http://127.0.0.1:9999/v1",
                "api_key": "synthetic",
            }
        }
    return EngineConfig.validate(raw, base_dir=directory)


def _submission() -> InputSubmission:
    now = datetime.now(UTC)
    return InputSubmission(
        InputSource("local", "local", "local", "message", "shutdown-test"),
        "本地输入",
        now,
        now,
    )


def _leaves(error: BaseException) -> tuple[BaseException, ...]:
    """保留异常组中的对象身份，用于确认关闭没有翻译或遗失原始错误。"""
    if isinstance(error, BaseExceptionGroup):
        # 运行时异常组只接受 BaseException；isinstance 不保留这个泛型约束。
        group = cast(BaseExceptionGroup[BaseException], error)
        return tuple(leaf for child in group.exceptions for leaf in _leaves(child))
    return (error,)


async def test_original_subject_failure_releases_real_store_and_lock(
    tmp_path: Path, resources: _Resources
) -> None:
    failure = ArithmeticError("synthetic original subject failure")
    backend, plugin = _Backend(failure), _Plugin()
    host = EngineHost(
        _config(tmp_path, ("local",)),
        generation_backends={"main": backend},
        plugin_factories={"local": plugin.factory()},
    )
    await host.start()
    submission = _submission()
    await host.submit(submission)
    await asyncio.wait_for(backend.entered.wait(), 3.0)

    # 工作器将 Core 原错锁存后停止派发；重复来源只观察同一接管身份。
    async with asyncio.timeout(3.0):
        while True:
            try:
                await host.submit(submission)
            except ArithmeticError as error:
                assert error is failure
                break
            await asyncio.sleep(0.005)
    with pytest.raises(BaseException) as reported:
        await host.close()
    assert failure in _leaves(reported.value)
    assert host.query().phase == "failed" and host.query().pending_resources == ()
    assert resources.store_closes == 1 and plugin.close_count == 1
    await resources.assert_released(tmp_path)
    with pytest.raises(BaseException) as repeated:
        await host.wait_closed(wait_seconds=1.0)
    assert repeated.value is reported.value
    assert resources.store_closes == 1 and plugin.close_count == 1


async def test_original_execution_failure_does_not_skip_plugin_or_store_close(
    tmp_path: Path, resources: _Resources, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure, plugin = ArithmeticError("synthetic call preparation failure"), _Plugin()
    host = EngineHost(
        _config(tmp_path, ("local",)),
        generation_backends={"main": _Backend()},
        plugin_factories={"local": plugin.factory()},
    )
    await host.start()

    async def reject_call(store: Store, *args: object, **kwargs: object) -> None:
        del store, args, kwargs
        raise failure

    monkeypatch.setattr(Store, "begin_call", reject_call)
    with pytest.raises(ArithmeticError) as invoked:
        await host.invoke("me.local.call", {"text": "never-started"})
    assert invoked.value is failure and not plugin.entered.is_set()
    with pytest.raises(BaseException) as reported:
        await host.close()
    assert failure in _leaves(reported.value)
    assert host.query().phase == "failed" and host.query().pending_resources == ()
    assert plugin.close_count == 1 and resources.store_closes == 1
    await resources.assert_released(tmp_path)


async def test_ingress_fault_waits_for_real_provider_before_releasing_resources(
    tmp_path: Path, resources: _Resources
) -> None:
    failure = ArithmeticError("synthetic ingress stop failure")
    plugin = _Plugin(held=True, stop_failure=failure)
    host = EngineHost(
        _config(tmp_path, ("local",), close_seconds=0.01),
        generation_backends={"main": _Backend()},
        plugin_factories={"local": plugin.factory()},
    )
    await host.start()
    invocation = asyncio.create_task(host.invoke("me.local.call", {"text": "held"}))
    try:
        await asyncio.wait_for(plugin.entered.wait(), 3.0)
        snapshot = await host.close()
        assert snapshot.phase == "closing" and "storage" in snapshot.pending_resources
        assert resources.store_closes == 0 and plugin.close_count == 0
        assert await host.query_action(plugin.contexts[0].action_id) is not None
        with pytest.raises(InstanceLocked):
            InstanceLock(tmp_path).acquire()
        observer = asyncio.create_task(host.wait_closed())
        await asyncio.sleep(0)
        observer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observer
        assert (await host.close()).phase == "closing"
        plugin.release.set()
        with pytest.raises(ArithmeticError) as reported:
            await host.wait_closed(wait_seconds=3.0)
        assert reported.value is failure and plugin.returned
        assert host.query().phase == "failed" and host.query().pending_resources == ()
        assert plugin.stop_count == plugin.close_count == resources.store_closes == 1
        await resources.assert_released(tmp_path)
    finally:
        plugin.release.set()
        await asyncio.gather(invocation, return_exceptions=True)


@pytest.mark.parametrize("synchronous_failure", [False, True])
async def test_failed_plugin_owner_preserves_store_but_independent_plugin_closes(
    tmp_path: Path, resources: _Resources, synchronous_failure: bool
) -> None:
    failure = ArithmeticError("synthetic plugin close failure")
    independent = _Plugin()
    broken = _Plugin(close_failure=failure, synchronous_close_failure=synchronous_failure)
    host = EngineHost(
        _config(tmp_path, ("independent", "broken")),
        generation_backends={"main": _Backend()},
        plugin_factories={
            "independent": independent.factory(),
            "broken": broken.factory(),
        },
    )
    await host.start()
    with pytest.raises(ArithmeticError) as reported:
        await host.close()
    assert reported.value is failure
    snapshot = host.query()
    assert snapshot.phase == "failed"
    assert set(snapshot.pending_resources) == {"storage", "plugin:broken", "data_directory_lock"}
    assert independent.close_count == broken.close_count == 1
    assert resources.store_closes == 0 and await host.query_action("missing") is None
    assert all(thread.is_alive() for thread in resources.threads)
    with pytest.raises(InstanceLocked):
        InstanceLock(tmp_path).acquire()
    with pytest.raises(ArithmeticError) as repeated:
        await host.close()
    assert repeated.value is failure and broken.close_count == 1


@pytest.mark.parametrize("synchronous_failure", [False, True])
async def test_plugin_reports_original_failure_after_release_without_holding_store(
    tmp_path: Path, resources: _Resources, synchronous_failure: bool
) -> None:
    failure = ArithmeticError("synthetic completed plugin failure")
    plugin = _Plugin(
        close_failure=failure,
        synchronous_close_failure=synchronous_failure,
        released_after_close=True,
    )
    host = EngineHost(
        _config(tmp_path, ("local",)),
        generation_backends={"main": _Backend()},
        plugin_factories={"local": plugin.factory()},
    )
    await host.start()
    with pytest.raises(ArithmeticError) as reported:
        await host.close()
    assert reported.value is failure and plugin.released
    assert host.query().phase == "failed" and host.query().pending_resources == ()
    assert plugin.close_count == resources.store_closes == 1
    await resources.assert_released(tmp_path)
    with pytest.raises(ArithmeticError) as repeated:
        await host.close()
    assert repeated.value is failure and plugin.close_count == resources.store_closes == 1


@pytest.mark.parametrize("broken_proof", [False, True])
async def test_unconfirmed_plugin_release_preserves_store_and_original_errors(
    tmp_path: Path, resources: _Resources, broken_proof: bool
) -> None:
    failure = ArithmeticError("synthetic unconfirmed plugin close")
    proof_failure = LookupError("synthetic release proof failure") if broken_proof else None
    plugin = _Plugin(
        close_failure=failure,
        released_after_close=False,
        release_proof_failure=proof_failure,
    )
    host = EngineHost(
        _config(tmp_path, ("local",)),
        generation_backends={"main": _Backend()},
        plugin_factories={"local": plugin.factory()},
    )
    await host.start()
    with pytest.raises(BaseException) as reported:
        await host.close()
    expected = (failure, proof_failure) if proof_failure is not None else (failure,)
    assert _leaves(reported.value) == expected
    assert set(host.query().pending_resources) == {"storage", "plugin:local", "data_directory_lock"}
    assert resources.store_closes == 0 and not plugin.released
    assert all(thread.is_alive() for thread in resources.threads)
    with pytest.raises(InstanceLocked):
        InstanceLock(tmp_path).acquire()
    with pytest.raises(BaseException) as repeated:
        await host.close()
    assert repeated.value is reported.value and plugin.close_count == 1


async def test_knowledge_reported_failure_after_real_release_does_not_hold_store(
    tmp_path: Path, resources: _Resources, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = ArithmeticError("synthetic completed index submission failure")
    original_close = KnowledgeIndexWorker.close

    async def close_with_original_failure(indexer: KnowledgeIndexWorker) -> None:
        await original_close(indexer)
        raise failure

    monkeypatch.setattr(KnowledgeIndexWorker, "close", close_with_original_failure)
    host = EngineHost(_config(tmp_path, embedding=True), generation_backends={"main": _Backend()})
    await host.start()
    with pytest.raises(BaseException) as reported:
        await host.close()
    assert _leaves(reported.value) == (failure,)
    assert host.query().phase == "failed" and host.query().pending_resources == ()
    assert resources.store_closes == 1
    await resources.assert_released(tmp_path)


async def test_embedding_pool_close_failure_keeps_owner_even_without_inflight_request(
    tmp_path: Path, resources: _Resources, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = ArithmeticError("synthetic HTTP pool close failure")
    original_close = OpenAIEmbeddingResource.close
    transports: list[OpenAIEmbeddingResource] = []

    async def reject_close(resource: OpenAIEmbeddingResource) -> None:
        transports.append(resource)
        raise failure

    monkeypatch.setattr(OpenAIEmbeddingResource, "close", reject_close)
    host = EngineHost(_config(tmp_path, embedding=True), generation_backends={"main": _Backend()})
    await host.start()
    try:
        with pytest.raises(BaseException) as reported:
            await host.close()
        assert _leaves(reported.value) == (failure,)
        snapshot = host.query()
        assert snapshot.phase == "failed"
        assert set(snapshot.pending_resources) == {"storage", "knowledge:0", "data_directory_lock"}
        assert resources.store_closes == 0 and await host.query_action("missing") is None
        with pytest.raises(InstanceLocked):
            InstanceLock(tmp_path).acquire()
        with pytest.raises(BaseException) as repeated:
            await host.close()
        assert repeated.value is reported.value
        assert len(transports) == 1
    finally:
        # 测试已证明 Host 保留未知连接池；明确解除本地注入后再由夹具收库。
        for resource in transports:
            await original_close(resource)
