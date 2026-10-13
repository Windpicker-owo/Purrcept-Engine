"""用真实 Store 通知和事件屏障覆盖关闭与固定工作器恢复的调度边界。

屏障只推迟 Store 等待的返回，不改输入状态或替代数据库事务。关闭必须在
同步撤销派发资格时同时取消工作器；正常停止不能锁存为内部故障，已经发生
的内部故障仍须按原对象报告。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from purrcept_core.models import ModelEventSink, ModelRequest, ModelResponse

from purrcept_engine.context.capacity import GenerationCapacity, check_capacity
from purrcept_engine.context.materials import media_blocks
from purrcept_engine.errors import HostStateError
from purrcept_engine.execution.catalog import Catalog, ObjectDeclaration
from purrcept_engine.execution.protocol import JsonObject
from purrcept_engine.storage import InputSource, InputSubmission
from purrcept_engine.storage.execution import ActionRecord
from purrcept_engine.storage.store import Store
from purrcept_engine.subject.coordinator import (
    CoordinatorSettings,
    SubjectCoordinator,
    SubjectModel,
)
from purrcept_engine.subject.knowledge import KnowledgeService
from purrcept_engine.subject.model_requests import RequestBudget
from purrcept_engine.subject.session import DynamicState


@dataclass(frozen=True)
class _Retry:
    attempts: int = 1
    attempt_seconds: float = 1.0
    total_seconds: float = 1.0


class _UnusedModel:
    """本组场景停在数据库等待处，任何模型调用都表示越过关闭边界。"""

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        """拒绝越过场景边界的调用，不返回伪造的模型响应。"""
        raise AssertionError("工作器已在数据库等待处停止，不应开始模型调用。")


def _coordinator(store: Store, *, coalesce_seconds: float = 0) -> SubjectCoordinator:
    """装配真实账本和认识读取，场景停止在任何模型或行动入口之前。"""

    async def refresh(budget: RequestBudget) -> DynamicState:
        return DynamicState("关闭边界测试。")

    async def background() -> JsonObject:
        return {}

    async def execute(
        source: str, *, input_id: str | None, can_start: Callable[[], bool]
    ) -> ActionRecord:
        raise AssertionError("关闭边界不应开始行动。")

    return SubjectCoordinator(
        store,
        KnowledgeService(store.knowledge),
        store.knowledge,
        Catalog((ObjectDeclaration("subject", "me", "主体"),), ()),
        subject=SubjectModel(
            _UnusedModel(),
            "openai/test",
            1000,
            lambda payload: check_capacity(
                payload, GenerationCapacity(300_000, 1000, "reference-text-only-v1")
            ),
            _Retry(),
        ),
        attention=None,
        settings=CoordinatorSettings(4096, coalesce_seconds=coalesce_seconds),
        instructions=(),
        execute=execute,
        media=lambda references: media_blocks(store, references),
        refresh=refresh,
        attention_context=background,
        wakeup_committed=lambda: None,
        can_start=lambda: None,
    )


async def _notify(store: Store) -> None:
    """保存独立的合成行动意图来通知等待者，不创建输入或执行副作用。"""
    await store.begin_action(uuid4().hex, catalog_version="close-boundary", source="return None")


@pytest.mark.parametrize("worker", ("attention", "foreground", "deferred", "coalesce"))
async def test_close_revokes_worker_before_a_ready_store_wait_can_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker: str
) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    coordinator = _coordinator(store, coalesce_seconds=10 if worker == "coalesce" else 0)
    waiting = asyncio.Event()
    notified = asyncio.Event()
    resume = asyncio.Event()
    target = f"subject-{'attention' if worker == 'coalesce' else worker}"
    original_wait = store.wait_for_change

    async def wait_for_change(after_revision: int) -> int:
        task = asyncio.current_task()
        if task is None or task.get_name() != target:
            return await original_wait(after_revision)
        waiting.set()
        revision = await original_wait(after_revision)
        notified.set()
        await resume.wait()
        return revision

    monkeypatch.setattr(store, "wait_for_change", wait_for_change)
    identity: str | None = None
    try:
        if worker == "coalesce":
            now = datetime.now(UTC)
            admission = await store.admit(
                InputSubmission(
                    InputSource("test", "account", "chat", "message", "one", "sender", "名字"),
                    "合批中的原文。",
                    now,
                    now,
                )
            )
            identity = admission.input_id
        await coordinator.start()
        async with asyncio.timeout(5):
            await waiting.wait()
            await _notify(store)
            await notified.wait()

            # 先使工作器 ready，再直接进入 close。此时下一次调度先恢复工作器，
            # 不能等另起的收尾任务运行后才撤销这个已经 ready 的等待。
            resume.set()
            await coordinator.close()

        assert coordinator.failure is None
        assert coordinator.pending_resources == 0
        assert store.authority_available
        if identity is not None:
            processing = await store.input_processing(identity)
            assert processing is not None and processing.stage == "attention"
    finally:
        resume.set()
        try:
            await coordinator.close()
        finally:
            await store.close()


@pytest.mark.parametrize("error_type", (RuntimeError, HostStateError))
async def test_internal_worker_failure_keeps_its_original_identity_during_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    coordinator = _coordinator(store)
    waiting = asyncio.Event()
    failed = asyncio.Event()
    original_wait = store.wait_for_change
    original_error = error_type("原始工作器故障。")

    async def wait_for_change(after_revision: int) -> int:
        task = asyncio.current_task()
        if task is None or task.get_name() != "subject-attention":
            return await original_wait(after_revision)
        waiting.set()
        await original_wait(after_revision)
        failed.set()
        raise original_error

    monkeypatch.setattr(store, "wait_for_change", wait_for_change)
    try:
        await coordinator.start()
        async with asyncio.timeout(5):
            await waiting.wait()
            await _notify(store)
            await failed.wait()
            with pytest.raises(error_type) as captured:
                await coordinator.close()
        assert captured.value is original_error
        assert coordinator.failure is original_error
        assert coordinator.pending_resources == 0
    finally:
        try:
            with pytest.raises(error_type) as captured:
                await coordinator.close()
            assert captured.value is original_error
        finally:
            await store.close()


async def test_repeated_close_observation_does_not_cancel_worker_cleanup_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    coordinator = _coordinator(store)
    waiting = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    original_wait = store.wait_for_change
    cancellation_counts: list[int] = []

    async def wait_for_change(after_revision: int) -> int:
        task = asyncio.current_task()
        if task is None or task.get_name() != "subject-attention":
            return await original_wait(after_revision)
        waiting.set()
        try:
            return await original_wait(after_revision)
        except asyncio.CancelledError:
            cancellation_counts.append(task.cancelling())
            cleaning.set()
            await release.wait()
            cancellation_counts.append(task.cancelling())
            raise

    monkeypatch.setattr(store, "wait_for_change", wait_for_change)
    first: asyncio.Task[None] | None = None
    second: asyncio.Task[None] | None = None
    try:
        await coordinator.start()
        async with asyncio.timeout(5):
            await waiting.wait()
            first = asyncio.create_task(coordinator.close())
            await cleaning.wait()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(coordinator.close())
            await _notify(store)
            assert not second.done()
            assert coordinator.pending_resources > 0
            release.set()
            await second
        assert cancellation_counts == [1, 1]
        assert coordinator.failure is None
        assert coordinator.pending_resources == 0
    finally:
        release.set()
        if first is not None:
            await asyncio.gather(first, return_exceptions=True)
        if second is not None:
            await asyncio.gather(second, return_exceptions=True)
        try:
            await coordinator.close()
        finally:
            await store.close()
