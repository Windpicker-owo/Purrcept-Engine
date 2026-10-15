"""结构化单能力入口的实际 SQLite、共用调度及真实 Provider 生命周期证据。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from purrcept_engine.app.config import ExecutionSettings
from purrcept_engine.domain.execution.catalog import (
    CapabilityDeclaration,
    Catalog,
    ObjectDeclaration,
)
from purrcept_engine.domain.execution.errors import (
    ExecutionBusy,
    ExecutionClosed,
    ExecutionLimitError,
)
from purrcept_engine.domain.execution.models import CallRecord
from purrcept_engine.domain.execution.providers import CallContext, CallOutcome, FunctionCapability
from purrcept_engine.domain.inputs.models import MediaReference
from purrcept_engine.infrastructure.execution.service import ExecutionService
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.values import ActionProtocolError, JsonObject


class _Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    label: str
    block: bool = False


@dataclass
class _Rig:
    store: Store
    service: ExecutionService
    directory: Path
    release: asyncio.Event = field(default_factory=asyncio.Event)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    controls: asyncio.Event = field(default_factory=asyncio.Event)
    ordinary_full: asyncio.Event = field(default_factory=asyncio.Event)
    effects: list[str] = field(default_factory=lambda: list[str]())
    contexts: list[CallContext] = field(default_factory=lambda: list[CallContext]())


async def _rig(
    directory: Path,
    *,
    settings: ExecutionSettings | None = None,
    concurrency: Literal["parallel", "target_serial"] = "parallel",
    timeout_seconds: float = 5.0,
) -> _Rig:
    """本地 Provider 持有可控返回门，不建立 Worker、设备或外部服务。"""
    store = await Store.open(directory / "state.sqlite3")
    objects = (
        ObjectDeclaration("me", "me", "主体"),
        ObjectDeclaration("left", "left", "左目标", "me"),
        ObjectDeclaration("right", "right", "右目标", "me"),
    )
    declarations = tuple(
        CapabilityDeclaration(
            f"{target}.{name}",
            target,
            name,
            "本地确定性能力",
            _Parameters,
            timeout_seconds,
            f'await me.{target}.{name}(label="value")',
            concurrency=concurrency,
            control=name == "stop",
        )
        for target, name in (("left", "call"), ("right", "call"), ("left", "stop"))
    )

    # 闭包在首次实际 invoke 前才使用已经完成构造的 rig。
    async def invoke(parameters: _Parameters, context: CallContext) -> CallOutcome:
        value.contexts.append(context)
        if len(value.contexts) == (settings or ExecutionSettings()).max_provider_calls:
            value.ordinary_full.set()
        if parameters.label == "cancel":
            raise asyncio.CancelledError
        if parameters.label == "bug":
            raise RuntimeError("original provider failure")
        if parameters.label == "control":
            value.controls.set()
        value.entered.set()
        if parameters.block:
            await value.release.wait()
        value.effects.append(parameters.label)
        return CallOutcome("confirmed", {"actual": parameters.label})

    service = ExecutionService(
        Catalog(objects, declarations),
        tuple(FunctionCapability(declaration, invoke) for declaration in declarations),
        store,
        settings or ExecutionSettings(),
        scratch_root=directory / "scratch",
    )
    value = _Rig(store, service, directory)
    return value


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[_Rig]:
    value = await _rig(tmp_path)
    yield value
    value.release.set()
    await value.service.close()
    await value.store.close()


def _observe_prepared(rig: _Rig, monkeypatch: pytest.MonkeyPatch, *, count: int) -> asyncio.Event:
    """在真实意图提交后通知，测试不靠调度睡眠猜测请求已入队。"""
    event = asyncio.Event()
    prepared = 0
    original = rig.store.begin_call

    async def begin_call(
        call_id: str,
        *,
        action_id: str,
        sequence: int,
        path: str,
        target_id: str,
        arguments_json: str,
    ) -> None:
        nonlocal prepared
        await original(
            call_id,
            action_id=action_id,
            sequence=sequence,
            path=path,
            target_id=target_id,
            arguments_json=arguments_json,
        )
        prepared += 1
        if prepared == count:
            event.set()

    monkeypatch.setattr(rig.store, "begin_call", begin_call)
    return event


async def test_direct_intent_and_actual_receipt_share_sql_without_worker(rig: _Rig) -> None:
    result = await rig.service.invoke("me.left.call", {"label": "effect"}, input_id="activity:1")
    assert result.state == "completed" and result.output == ""
    assert json.loads(result.source) == {
        "kind": "structured_call",
        "path": "me.left.call",
        "arguments": {"label": "effect"},
    }
    call = result.calls[0]
    assert call.sequence == 1 and call.call_id == result.action_id + ":1"
    assert call.path == "me.left.call" and call.target_id == "left"
    assert call.state == "confirmed" and call.started_at is not None
    assert json.loads(call.arguments_json) == {"label": "effect"}
    assert json.loads(call.result_json or "null") == {"actual": "effect"}
    assert rig.contexts[0].input_id == "activity:1" and rig.effects == ["effect"]
    assert not (rig.directory / "scratch").exists()
    with sqlite3.connect(rig.directory / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM actions").fetchone() == (1,)
        assert connection.execute("SELECT call_id, revision FROM call_results").fetchone() == (
            call.call_id,
            1,
        )
    assert (await rig.store.pending_call_receipts())[0].call_id == call.call_id


async def test_arguments_are_frozen_before_authority_wait(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    admitted = asyncio.Event()
    proceed = asyncio.Event()
    original = rig.store.begin_action

    async def begin_action(
        action_id: str, *, catalog_version: str, source: str, input_id: str | None = None
    ) -> None:
        admitted.set()
        await proceed.wait()
        await original(action_id, catalog_version=catalog_version, source=source, input_id=input_id)

    monkeypatch.setattr(rig.store, "begin_action", begin_action)
    arguments: JsonObject = {"label": "original"}
    observer = asyncio.create_task(rig.service.invoke("me.left.call", arguments))
    await admitted.wait()
    arguments["label"] = "mutated"
    proceed.set()
    result = await observer
    assert rig.effects == ["original"]
    assert json.loads(result.calls[0].arguments_json) == {"label": "original"}


@pytest.mark.parametrize(
    ("arguments", "error_type"),
    [({"label": 17}, "CapabilityInputError"), ({"label": "bug"}, "RuntimeError")],
)
async def test_same_provider_boundary_rejects_input_and_preserves_program_error(
    rig: _Rig, arguments: JsonObject, error_type: str
) -> None:
    result = await rig.service.invoke("me.left.call", arguments)
    assert result.state == "failed" and result.calls[0].state == "failed"
    assert result.calls[0].error_type == error_type and not rig.effects


async def test_provider_cancellation_is_unknown_not_unstarted(rig: _Rig) -> None:
    result = await rig.service.invoke("me.left.call", {"label": "cancel"})
    assert result.state == "failed" and result.calls[0].state == "unknown"
    assert result.calls[0].error_type == "CancelledError"


@pytest.mark.parametrize("seconds", [0.0, -1.0, float("inf"), float("nan"), True])
async def test_invalid_timeout_does_not_admit(rig: _Rig, seconds: float) -> None:
    with pytest.raises(ValueError, match="有限正数"):
        await rig.service.invoke("me.left.call", {"label": "none"}, timeout_seconds=seconds)
    assert rig.service.pending_resources == 0 and not rig.effects


async def test_unknown_path_and_json_limits_do_not_admit(tmp_path: Path) -> None:
    value = await _rig(tmp_path, settings=ExecutionSettings(call_arguments_bytes=32))
    try:
        with pytest.raises(LookupError):
            await value.service.invoke("me.left.missing", {"label": "none"})
        with pytest.raises(ExecutionLimitError, match="参数"):
            await value.service.invoke("me.left.call", {"label": "x" * 33})
        with pytest.raises(ActionProtocolError, match="有限"):
            await value.service.invoke("me.left.call", {"label": float("nan")})
        assert value.service.pending_resources == 0 and not value.effects
        with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
            assert connection.execute("SELECT COUNT(*) FROM actions").fetchone() == (0,)
    finally:
        await value.service.close()
        await value.store.close()


async def test_observer_cancel_keeps_real_effect_and_authoritative_result(rig: _Rig) -> None:
    observer = asyncio.create_task(
        rig.service.invoke("me.left.call", {"label": "held", "block": True})
    )
    await rig.entered.wait()
    observer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observer
    assert rig.service.pending_resources > 0 and not rig.effects
    action_id = rig.contexts[0].action_id
    rig.release.set()
    await rig.service.close()
    result = await rig.service.query(action_id)
    assert result is not None and result.calls[0].state == "confirmed"
    assert rig.effects == ["held"] and rig.service.pending_resources == 0


async def test_timeout_preserves_occupancy_and_late_same_call_revision(tmp_path: Path) -> None:
    value = await _rig(
        tmp_path, settings=ExecutionSettings(max_workers=1, max_calls=1), timeout_seconds=0.08
    )
    try:
        result = await value.service.invoke("me.left.call", {"label": "held", "block": True})
        assert result.state == "timed_out" and result.calls[0].state == "unknown"
        assert value.service.pending_resources == 1 and not value.effects
        with pytest.raises(ExecutionBusy):
            await value.service.invoke("me.right.call", {"label": "not-admitted"})
        control = await value.service.invoke("me.left.stop", {"label": "control"})
        assert control.state == "completed" and value.effects == ["control"]
        value.release.set()
        await value.service.close()
        current = await value.service.query(result.action_id)
        assert current is not None and current.state == "timed_out"
        assert [item.state for item in current.calls[0].history] == ["unknown", "confirmed"]
        pending = await value.store.pending_call_receipts()
        assert [
            (item.call_id, item.receipt_revision)
            for item in pending
            if item.action_id == result.action_id
        ] == [
            (result.calls[0].call_id, 1),
            (result.calls[0].call_id, 2),
        ]
    finally:
        value.release.set()
        await value.service.close()
        await value.store.close()


async def test_queue_wait_consumes_deadline_and_cannot_start_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = await _rig(tmp_path, settings=ExecutionSettings(max_provider_calls=1))
    prepared = _observe_prepared(value, monkeypatch, count=2)
    held = asyncio.create_task(
        value.service.invoke("me.left.call", {"label": "held", "block": True})
    )
    try:
        await value.entered.wait()
        queued = asyncio.create_task(
            value.service.invoke("me.right.call", {"label": "expired"}, timeout_seconds=0.06)
        )
        await prepared.wait()
        result = await asyncio.wait_for(queued, 1)
        assert result.state == "timed_out" and result.calls[0].state == "not_started"
        assert result.calls[0].started_at is None and len(value.contexts) == 1
        value.release.set()
        await held
        assert value.effects == ["held"]
    finally:
        value.release.set()
        await value.service.close()
        await value.store.close()


async def test_authority_start_wait_rechecks_generation_and_deadline(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = rig.store.mark_call_started
    eligible = True

    async def revoke_after_commit(call_id: str) -> None:
        nonlocal eligible
        await original(call_id)
        eligible = False

    monkeypatch.setattr(rig.store, "mark_call_started", revoke_after_commit)
    result = await rig.service.invoke(
        "me.left.call", {"label": "revoked"}, can_start=lambda: eligible
    )
    assert result.state == "cancelled" and result.calls[0].state == "not_started"
    assert result.calls[0].started_at is not None and not rig.contexts


async def test_shared_ordinary_saturation_keeps_control_and_target_stop_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = await _rig(
        tmp_path,
        settings=ExecutionSettings(max_workers=1, max_calls=2, max_provider_calls=1),
        concurrency="target_serial",
    )
    prepared = _observe_prepared(value, monkeypatch, count=2)
    held = asyncio.create_task(
        value.service.invoke("me.left.call", {"label": "held", "block": True})
    )
    try:
        await value.entered.wait()
        queued = asyncio.create_task(value.service.invoke("me.left.call", {"label": "queued"}))
        await prepared.wait()
        with pytest.raises(ExecutionBusy):
            await value.service.invoke("me.right.call", {"label": "rejected"})
        control = await value.service.invoke("me.left.stop", {"label": "control"})
        assert control.state == "completed" and value.effects == ["control"]
        assert not held.done() and not queued.done()
        value.release.set()
        assert (await held).state == "completed" and (await queued).state == "completed"
        assert value.effects == ["control", "held", "queued"]
    finally:
        value.release.set()
        await value.service.close()
        await value.store.close()


async def test_target_serial_shares_target_but_not_unrelated_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = await _rig(tmp_path, concurrency="target_serial")
    prepared = _observe_prepared(value, monkeypatch, count=3)
    held = asyncio.create_task(
        value.service.invoke("me.left.call", {"label": "held", "block": True})
    )
    try:
        await value.entered.wait()
        same = asyncio.create_task(value.service.invoke("me.left.call", {"label": "same"}))
        other = asyncio.create_task(value.service.invoke("me.right.call", {"label": "other"}))
        await prepared.wait()
        assert (await other).state == "completed" and not same.done()
        assert value.effects == ["other"]
        value.release.set()
        await asyncio.gather(held, same)
        assert value.effects == ["other", "held", "same"]
    finally:
        value.release.set()
        await value.service.close()
        await value.store.close()


async def test_calls_rotate_by_real_source_while_preserving_source_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = await _rig(tmp_path, settings=ExecutionSettings(max_provider_calls=1))
    prepared = _observe_prepared(value, monkeypatch, count=4)
    held = asyncio.create_task(
        value.service.invoke("me.left.call", {"label": "held", "block": True}, input_id="held")
    )
    try:
        await value.entered.wait()
        queued = [
            asyncio.create_task(
                value.service.invoke("me.left.call", {"label": label}, input_id=source)
            )
            for label, source in (("a1", "a"), ("a2", "a"), ("b1", "b"))
        ]
        await prepared.wait()
        value.release.set()
        await asyncio.gather(held, *queued)
        assert value.effects == ["held", "a1", "b1", "a2"]
    finally:
        value.release.set()
        await value.service.close()
        await value.store.close()


async def test_close_is_one_cleanup_task_and_does_not_cancel_real_provider(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _observe_prepared(rig, monkeypatch, count=2)
    held = asyncio.create_task(rig.service.invoke("me.left.call", {"label": "held", "block": True}))
    await rig.entered.wait()
    queued = asyncio.create_task(
        rig.service.invoke("me.left.call", {"label": "queued"}, can_start=lambda: False)
    )
    await prepared.wait()
    closing = asyncio.create_task(rig.service.close())
    result = await queued
    assert result.state == "cancelled" and result.calls[0].state == "not_started"
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert rig.service.pending_resources > 0 and not held.done()
    with pytest.raises(ExecutionClosed):
        await rig.service.invoke("me.left.stop", {"label": "closed"})
    rig.release.set()
    await rig.service.close()
    result = await held
    assert result.state == "cancelled" and result.calls[0].state == "confirmed"
    assert rig.effects == ["held"] and rig.service.pending_resources == 0


async def test_authority_failure_blocks_provider_and_next_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = await _rig(tmp_path)

    async def fail_start(call_id: str) -> None:
        del call_id
        raise RuntimeError("authoritative start unavailable")

    monkeypatch.setattr(value.store, "mark_call_started", fail_start)
    try:
        with pytest.raises(RuntimeError, match="authoritative start unavailable"):
            await value.service.invoke("me.left.call", {"label": "never"})
        assert not value.effects and not value.contexts
        with pytest.raises(ExecutionClosed):
            await value.service.invoke("me.left.call", {"label": "also-never"})
        with pytest.raises(BaseExceptionGroup):
            await value.service.close()
        assert value.service.pending_resources == 0
    finally:
        await value.store.close()


async def test_direct_result_has_no_worker_delivery_limit(tmp_path: Path) -> None:
    value = await _rig(tmp_path, settings=ExecutionSettings(call_result_bytes=16))
    try:
        result = await value.service.invoke("me.left.call", {"label": "actual long payload"})
        assert result.state == "completed" and result.calls[0].state == "confirmed"
        assert json.loads(result.result_json or "null") == {"actual": "actual long payload"}
        assert not (tmp_path / "scratch").exists()
    finally:
        await value.service.close()
        await value.store.close()


async def test_default_sixteen_provider_and_256_unfinished_bounds_keep_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = await _rig(tmp_path, timeout_seconds=60.0)
    prepared = _observe_prepared(value, monkeypatch, count=256)
    observers = [
        asyncio.create_task(
            value.service.invoke("me.left.call", {"label": str(index), "block": True})
        )
        for index in range(256)
    ]
    try:
        await asyncio.wait_for(prepared.wait(), 5)
        await asyncio.wait_for(value.ordinary_full.wait(), 5)
        assert len(value.contexts) == 16 and not value.effects
        with pytest.raises(ExecutionBusy):
            await value.service.invoke("me.left.call", {"label": "257th"})
        control = await value.service.invoke("me.left.stop", {"label": "control"})
        assert control.state == "completed" and value.effects == ["control"]
        assert len(value.contexts) == 17
        with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
            assert connection.execute("SELECT COUNT(*) FROM actions").fetchone() == (257,)
        value.release.set()
        results = await asyncio.gather(*observers)
        assert len(results) == 256 and all(result.state == "completed" for result in results)
        await value.service.close()
        assert len(value.effects) == 257 and value.service.pending_resources == 0
    finally:
        value.release.set()
        await value.service.close()
        await value.store.close()


async def test_control_waiting_is_bounded_independently_of_ordinary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = await _rig(
        tmp_path, settings=ExecutionSettings(max_workers=1, max_calls=2, max_provider_calls=1)
    )
    prepared = _observe_prepared(value, monkeypatch, count=2)
    held = asyncio.create_task(
        value.service.invoke("me.left.stop", {"label": "control", "block": True})
    )
    try:
        await value.controls.wait()
        queued = asyncio.create_task(value.service.invoke("me.left.stop", {"label": "queued"}))
        await prepared.wait()
        with pytest.raises(ExecutionBusy):
            await value.service.invoke("me.left.stop", {"label": "rejected-control"})
        ordinary = await value.service.invoke("me.right.call", {"label": "ordinary"})
        assert ordinary.state == "completed" and value.effects == ["ordinary"]
        value.release.set()
        await asyncio.gather(held, queued)
        assert value.effects == ["ordinary", "control", "queued"]
    finally:
        value.release.set()
        await value.service.close()
        await value.store.close()


async def test_start_commit_that_exhausts_deadline_never_enters_provider(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = rig.store.mark_call_started

    async def delayed_start(call_id: str) -> None:
        await original(call_id)
        await asyncio.sleep(0.08)

    monkeypatch.setattr(rig.store, "mark_call_started", delayed_start)
    result = await rig.service.invoke("me.left.call", {"label": "expired"}, timeout_seconds=0.03)
    assert result.state == "timed_out" and result.calls[0].state == "not_started"
    assert result.calls[0].started_at is not None and not rig.contexts


async def test_unknown_receipt_failure_keeps_actual_provider_until_real_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = await _rig(tmp_path, timeout_seconds=0.04)
    receipt_failed = asyncio.Event()
    original = value.store.record_call_result

    async def fail_unknown(
        call_id: str,
        *,
        state: Literal["confirmed", "failed", "unknown", "not_started"],
        result_json: str | None,
        error_type: str | None,
        error: str | None,
        media: tuple[MediaReference, ...] = (),
    ) -> CallRecord:
        if state == "unknown":
            receipt_failed.set()
            raise RuntimeError("unknown receipt unavailable")
        return await original(
            call_id,
            state=state,
            result_json=result_json,
            error_type=error_type,
            error=error,
            media=media,
        )

    monkeypatch.setattr(value.store, "record_call_result", fail_unknown)
    observer = asyncio.create_task(
        value.service.invoke("me.left.call", {"label": "held", "block": True})
    )
    try:
        await asyncio.wait_for(receipt_failed.wait(), 5)
        closing = asyncio.create_task(value.service.close())
        assert value.service.pending_resources > 0 and not observer.done()
        with pytest.raises(ExecutionClosed):
            await value.service.invoke("me.left.stop", {"label": "closed"})
        assert not value.effects
        value.release.set()
        with pytest.raises(RuntimeError, match="unknown receipt unavailable"):
            await observer
        with pytest.raises(BaseExceptionGroup):
            await closing
        assert value.effects == ["held"] and value.service.pending_resources == 0
        current = await value.service.query(value.contexts[0].action_id)
        assert current is not None and current.state == "failed"
        assert current.calls[0].state == "confirmed"
        assert json.loads(current.calls[0].result_json or "null") == {"actual": "held"}
    finally:
        value.release.set()
        await value.store.close()


async def test_worker_and_structured_requests_share_dispatch_without_sharing_worker_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = await _rig(
        tmp_path,
        settings=ExecutionSettings(max_workers=1, max_calls=2, max_provider_calls=1),
        concurrency="target_serial",
    )
    prepared = _observe_prepared(value, monkeypatch, count=2)
    direct = asyncio.create_task(
        value.service.invoke("me.left.call", {"label": "direct", "block": True})
    )
    try:
        await value.entered.wait()
        worker = asyncio.create_task(
            value.service.execute('from world import me\nreturn await me.left.call(label="worker")')
        )
        await asyncio.wait_for(prepared.wait(), 5)
        assert len(value.contexts) == 1 and not worker.done()
        control = await value.service.invoke("me.left.stop", {"label": "control"})
        assert control.state == "completed" and value.effects == ["control"]
        value.release.set()
        results = await asyncio.gather(direct, worker)
        assert all(result.state == "completed" for result in results)
        assert value.effects == ["control", "direct", "worker"]
    finally:
        value.release.set()
        await value.service.close()
        await value.store.close()
