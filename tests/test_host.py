"""真实目录锁、输入接管及关闭等待的宿主边界验证，不启动模型或设备。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from purrcept_engine import (
    EngineConfig,
    EngineHost,
    HostStateError,
    InputSource,
    InputSubmission,
    InstanceLocked,
    StorageError,
)
from purrcept_engine.app.locking import InstanceLock
from purrcept_engine.app.models import OpenAIGenerationResource
from purrcept_engine.storage.store import Store


def _config(directory: Path, *, close_seconds: float = 30.0) -> EngineConfig:
    return EngineConfig.validate(
        {
            "host": {"data_dir": directory, "close_seconds": close_seconds},
            "models": {
                "test": {
                    "kind": "generation",
                    "model": "openai/test",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic-key",
                    "context_window": 10000,
                    "max_output_tokens": 1000,
                    "capacity_rule": "test-only",
                }
            },
            "subject": {"foreground": "test", "reflection": "test", "summary": "test"},
        }
    )


def _submission() -> InputSubmission:
    now = datetime.now(UTC)
    return InputSubmission(
        InputSource("console", "local", "local", "message", native_event_id="one"),
        "你好",
        now,
        now,
    )


def test_os_lock_rejects_second_owner_and_ignores_stale_file(tmp_path: Path) -> None:
    first = InstanceLock(tmp_path)
    second = InstanceLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(InstanceLocked):
            second.acquire()
    finally:
        first.release()
    assert (tmp_path / "instance.lock").exists()
    second.acquire()
    second.release()


async def test_host_input_is_recoverable_and_duplicate_is_not_reaccepted(tmp_path: Path) -> None:
    host = EngineHost(_config(tmp_path))
    await asyncio.gather(host.start(), host.start())
    submission = _submission()
    first = await host.submit(submission)
    repeated = await host.submit(submission)
    assert repeated.duplicate and repeated.input_id == first.input_id
    record = await host.query_input(first.input_id)
    assert record is not None and record.text == submission.text and record.state == "pending"
    version = host.query().version
    assert host.query().version == version
    await asyncio.gather(host.close(), host.close())
    assert host.query().phase == "closed"
    with pytest.raises(HostStateError):
        await host.submit(submission)

    recovered = EngineHost(_config(tmp_path))
    await recovered.start()
    try:
        assert await recovered.query_input(first.input_id) == record
    finally:
        await recovered.close()


async def test_close_timeout_and_cancel_keep_resource_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = EngineHost(_config(tmp_path, close_seconds=0.01))
    await host.start()
    entered = asyncio.Event()
    release = asyncio.Event()
    original_close = Store.close
    invocations = 0

    async def delayed_close(store: Store) -> None:
        nonlocal invocations
        invocations += 1
        entered.set()
        await release.wait()
        await original_close(store)

    monkeypatch.setattr(Store, "close", delayed_close)
    snapshot = await host.close()
    assert snapshot.phase == "closing" and "storage" in snapshot.pending_resources
    await entered.wait()
    with pytest.raises(InstanceLocked):
        InstanceLock(tmp_path).acquire()
    with pytest.raises(HostStateError):
        await host.submit(_submission())

    observer = asyncio.create_task(host.wait_closed())
    await asyncio.sleep(0)
    observer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observer
    assert (await host.close()).phase == "closing"
    release.set()
    assert (await host.wait_closed(wait_seconds=1.0)).phase == "closed"
    assert invocations == 1
    unlocked = InstanceLock(tmp_path)
    unlocked.acquire()
    unlocked.release()


async def test_cancelling_start_waiter_does_not_abandon_open_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_open = Store.open
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_open(
        cls: type[Store],
        path: Path,
        *,
        max_inputs: int = 1024,
        max_input_bytes: int = 256 * 1024 * 1024,
    ) -> Store:
        del cls
        entered.set()
        await release.wait()
        return await original_open(path, max_inputs=max_inputs, max_input_bytes=max_input_bytes)

    monkeypatch.setattr(Store, "open", classmethod(delayed_open))
    host = EngineHost(_config(tmp_path))
    waiter = asyncio.create_task(host.start())
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    closing = asyncio.create_task(host.close())
    await asyncio.sleep(0)
    assert host.query().phase == "closing"
    release.set()
    assert (await closing).phase == "closed"
    assert host.query().pending_resources == ()


async def test_start_failure_does_not_hold_directory_lock(tmp_path: Path) -> None:
    (tmp_path / "engine.sqlite3").write_bytes(b"not a SQLite database")
    host = EngineHost(_config(tmp_path))
    with pytest.raises(StorageError):
        await host.start()
    assert host.query().phase == "failed"
    unlocked = InstanceLock(tmp_path)
    unlocked.acquire()
    unlocked.release()
    assert (await host.close()).phase == "closed"


async def test_model_assembly_failure_closes_storage_before_unlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_creation(*args: object, **kwargs: object) -> OpenAIGenerationResource:
        del args, kwargs
        raise ArithmeticError("synthetic assembly failure")

    monkeypatch.setattr(OpenAIGenerationResource, "create", fail_creation)
    host = EngineHost(_config(tmp_path))
    with pytest.raises(ArithmeticError, match="synthetic assembly failure"):
        await host.start()
    assert host.query().phase == "failed"
    assert host.query().pending_resources == ()
    reopened = await Store.open(tmp_path / "engine.sqlite3")
    await reopened.close()
    unlocked = InstanceLock(tmp_path)
    unlocked.acquire()
    unlocked.release()
    assert (await host.close()).phase == "closed"
