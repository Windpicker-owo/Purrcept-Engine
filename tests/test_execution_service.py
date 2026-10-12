"""真实 Windows Worker 与临时 SQLite 的执行闭环，不连接设备或模型服务。

本地能力替身只控制真实 invoke 的等待与结果。测试检查独立效果回执、派发
门槛、资源额度和关闭顺序，不通过 Worker 自报推断世界效果。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

import pytest
from pydantic import BaseModel, ConfigDict

from purrcept_engine.app.config import ExecutionSettings
from purrcept_engine.errors import ConfigError
from purrcept_engine.execution.catalog import CapabilityDeclaration, Catalog, ObjectDeclaration
from purrcept_engine.execution.protocol import FrameChannel, JsonObject
from purrcept_engine.execution.providers import (
    CallContext,
    CallOutcome,
    CapabilityImplementation,
    FunctionCapability,
)
from purrcept_engine.execution.service import (
    ExecutionBusy,
    ExecutionClosed,
    ExecutionLimitError,
    ExecutionService,
)
from purrcept_engine.execution.windows import WindowsProcess
from purrcept_engine.storage import Store


class _Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    text: str


@dataclass
class _Rig:
    store: Store
    service: ExecutionService
    catalog: Catalog
    implementations: tuple[CapabilityImplementation, ...]
    effects: list[str] = field(default_factory=list[str])
    contexts: list[CallContext] = field(default_factory=list[CallContext])
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    control_entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    output: str | None = None
    outcome_state: Literal["confirmed", "failed", "unknown"] = "confirmed"


async def _rig(
    tmp_path: Path,
    *,
    settings: ExecutionSettings | None = None,
    concurrency: Literal["parallel", "target_serial"] = "parallel",
    timeout_seconds: float = 5.0,
) -> _Rig:
    store = await Store.open(tmp_path / "state.sqlite3")
    objects = (
        ObjectDeclaration("subject", "me", "主体"),
        ObjectDeclaration("console", "console", "测试控制台", "subject"),
    )
    ordinary = CapabilityDeclaration(
        "console.echo",
        "console",
        "echo",
        "记录一个真实测试效果。",
        _Parameters,
        timeout_seconds,
        'await me.console.echo(text="hello")',
        concurrency=concurrency,
    )
    control = CapabilityDeclaration(
        "console.stop",
        "console",
        "stop",
        "独立测试控制通道。",
        _Parameters,
        5.0,
        'await me.console.stop(text="stop")',
        control=True,
    )
    catalog = Catalog(objects, (ordinary, control))
    rig: _Rig

    async def echo(parameters: _Parameters, context: CallContext) -> CallOutcome:
        rig.contexts.append(context)
        rig.entered.set()
        if parameters.text == "hold":
            await rig.release.wait()
        if parameters.text == "cancel":
            raise asyncio.CancelledError
        if parameters.text == "bug":
            raise RuntimeError("plugin implementation failure")
        rig.effects.append(parameters.text)
        return CallOutcome(rig.outcome_state, rig.output or {"text": parameters.text})

    async def stop(parameters: _Parameters, context: CallContext) -> CallOutcome:
        rig.contexts.append(context)
        rig.effects.append(parameters.text)
        rig.control_entered.set()
        return CallOutcome("confirmed", {"stopped": True})

    implementations = (FunctionCapability(ordinary, echo), FunctionCapability(control, stop))
    service = ExecutionService(
        catalog,
        implementations,
        store,
        settings or ExecutionSettings(),
        scratch_root=tmp_path / "scratch",
    )
    rig = _Rig(store, service, catalog, implementations)
    return rig


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[_Rig]:
    value = await _rig(tmp_path)
    try:
        yield value
    finally:
        value.release.set()
        await value.service.close()
        await value.store.close()


async def test_real_worker_gather_uses_frozen_paths_and_receipts(rig: _Rig, tmp_path: Path) -> None:
    result = await rig.service.execute(
        "from world import me\nimport asyncio\n"
        'return await asyncio.gather(me.console.echo(text="hello"), '
        'me.console.echo(text="world"))',
        input_id="input-1",
    )
    assert result.state == "completed"
    assert json.loads(result.result_json or "null") == [{"text": "hello"}, {"text": "world"}]
    assert [call.path for call in result.calls] == ["me.console.echo", "me.console.echo"]
    assert [call.sequence for call in result.calls] == [1, 2]
    assert all(call.state == "confirmed" and call.receipt_revision == 1 for call in result.calls)
    assert all(context.input_id == "input-1" for context in rig.contexts)
    assert result.catalog_version == rig.catalog.version
    assert rig.catalog.bindings["me.console.echo"].schema_json in rig.catalog.instructions
    assert list((tmp_path / "scratch").iterdir()) == []
    assert rig.service.pending_resources == 0


async def test_script_failure_preserves_prior_effect(rig: _Rig) -> None:
    result = await rig.service.execute(
        'from world import me\nawait me.console.echo(text="sent")\n'
        'raise ValueError("later script failure")'
    )
    assert result.state == "failed" and "later script failure" in (result.error or "")
    assert rig.effects == ["sent"]
    assert result.calls[0].state == "confirmed"
    assert (await rig.store.pending_call_receipts())[0].call_id == result.calls[0].call_id


async def test_waiter_cancellation_does_not_cancel_started_action(rig: _Rig) -> None:
    waiter = asyncio.create_task(
        rig.service.execute('from world import me\nreturn await me.console.echo(text="hold")')
    )
    await asyncio.wait_for(rig.entered.wait(), 5)
    action_id = rig.contexts[0].action_id
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert rig.service.pending_resources > 0
    rig.release.set()
    # Windows 默认时钟分辨率可高于 10ms，短 sleep 会被事件循环提前唤醒。
    for _ in range(100):
        if not rig.service.pending_resources:
            break
        await asyncio.sleep(0.05)
    record = await rig.service.query(action_id)
    assert rig.service.pending_resources == 0, (rig.effects, record)
    assert record is not None and record.state == "completed"
    assert record.calls[0].state == "confirmed" and rig.effects == ["hold"]


async def test_worker_full_rejects_without_hidden_queue(tmp_path: Path) -> None:
    value = await _rig(tmp_path, settings=ExecutionSettings(max_workers=1))
    task = asyncio.create_task(
        value.service.execute('from world import me\nreturn await me.console.echo(text="hold")')
    )
    try:
        await asyncio.wait_for(value.entered.wait(), 5)
        with pytest.raises(ExecutionBusy):
            await value.service.execute("return 2")
        value.release.set()
        assert (await task).state == "completed"
    finally:
        value.release.set()
        await value.service.close()
        await value.store.close()


async def test_generation_gate_is_checked_before_script_dispatch(rig: _Rig) -> None:
    result = await rig.service.execute(
        'from world import me\nreturn await me.console.echo(text="forbidden")',
        can_start=lambda: False,
    )
    assert result.state == "cancelled" and result.error == "GenerationRevoked"
    assert not result.calls and not rig.effects


@pytest.mark.parametrize(
    ("source", "updates", "reason"),
    [
        ('print("x" * 100)\nreturn None', {"output_bytes": 32}, "stdout/stderr"),
        ('return "x" * 100', {"result_bytes": 32}, "最终结果"),
        (
            'from world import me\nreturn await me.console.echo(text="x" * 100)',
            {"call_arguments_bytes": 32},
            "能力参数",
        ),
        (
            "from world import me\nfor i in range(3):\n await me.console.echo(text=str(i))",
            {"max_calls": 2},
            "调用次数",
        ),
    ],
)
async def test_worker_payload_limits_are_explicit(
    tmp_path: Path, source: str, updates: dict[str, int], reason: str
) -> None:
    value = await _rig(tmp_path, settings=ExecutionSettings(**updates))
    try:
        result = await value.service.execute(source)
        assert result.state == "failed" and reason in (result.error or "")
        assert len(value.effects) == (2 if "max_calls" in updates else 0)
    finally:
        await value.service.close()
        await value.store.close()


async def test_source_limit_prevents_admission(rig: _Rig) -> None:
    with pytest.raises(ExecutionLimitError):
        await rig.service.execute("x" * (64 * 1024 + 1))
    assert rig.service.pending_resources == 0


@pytest.mark.parametrize("delivery_bytes", [16, 256])
async def test_large_provider_result_keeps_effect_and_original_result(
    tmp_path: Path, delivery_bytes: int
) -> None:
    value = await _rig(tmp_path, settings=ExecutionSettings(call_result_bytes=delivery_bytes))
    value.output = "r" * 2048
    try:
        result = await value.service.execute(
            'from world import me\nreturn await me.console.echo(text="effect")'
        )
        assert result.state == "failed" and "ResultDeliveryLimit" in (result.error or "")
        assert value.effects == ["effect"]
        assert result.calls[0].state == "confirmed"
        assert json.loads(result.calls[0].result_json or "null") == value.output
        assert result.calls[0].call_id in (result.error or "")
    finally:
        await value.service.close()
        await value.store.close()


async def test_timeout_keeps_slot_until_late_confirmation(tmp_path: Path) -> None:
    value = await _rig(tmp_path, timeout_seconds=0.15)
    try:
        result = await value.service.execute(
            'from world import me\nreturn await me.console.echo(text="hold")'
        )
        assert result.state == "failed" and result.calls[0].state == "unknown"
        assert value.service.pending_resources == 1
        value.release.set()
        await value.service.close()
        current = await value.service.query(result.action_id)
        assert current is not None and current.state == "failed"
        assert [receipt.state for receipt in current.calls[0].history] == ["unknown", "confirmed"]
        assert value.effects == ["hold"]
    finally:
        value.release.set()
        await value.service.close()
        await value.store.close()


async def test_control_bypasses_ordinary_slots_and_target_serialization(tmp_path: Path) -> None:
    value = await _rig(
        tmp_path, settings=ExecutionSettings(max_provider_calls=1), concurrency="target_serial"
    )
    first = asyncio.create_task(
        value.service.execute('from world import me\nreturn await me.console.echo(text="hold")')
    )
    try:
        await asyncio.wait_for(value.entered.wait(), 5)
        second = asyncio.create_task(
            value.service.execute(
                "from world import me\nimport asyncio\n"
                'return await asyncio.gather(me.console.echo(text="second"), '
                'me.console.stop(text="control"))'
            )
        )
        await asyncio.wait_for(value.control_entered.wait(), 5)
        assert value.effects == ["control"] and not second.done()
        value.release.set()
        assert (await first).state == "completed"
        assert (await second).state == "completed"
        assert value.effects == ["control", "hold", "second"]
    finally:
        value.release.set()
        await value.service.close()
        await value.store.close()


@pytest.mark.parametrize(
    ("source", "settings", "reason"),
    [
        (
            "import asyncio\nawait asyncio.sleep(5)",
            ExecutionSettings(action_seconds=0.5),
            "ActionWallLimit",
        ),
        ("while True:\n pass", ExecutionSettings(worker_cpu_seconds=0.3), "WorkerCpuLimit"),
        (
            "from world import me\ntry:\n data = [0] * 100000000\n"
            'except Exception:\n await me.console.echo(text="after-memory")',
            ExecutionSettings(worker_memory_bytes=64 * 1024 * 1024),
            "WorkerMemoryLimit",
        ),
    ],
)
async def test_resources_revoke_calls_and_reclaim_worker(
    tmp_path: Path, source: str, settings: ExecutionSettings, reason: str
) -> None:
    value = await _rig(tmp_path, settings=settings)
    try:
        result = await asyncio.wait_for(value.service.execute(source), 8)
        assert result.state in {"failed", "timed_out"} and result.error == reason
        assert not value.effects and not result.calls
        assert list((tmp_path / "scratch").iterdir()) == []
        assert value.service.pending_resources == 0
    finally:
        await value.service.close()
        await value.store.close()


async def test_close_cancelled_waiter_continues_real_provider_cleanup(rig: _Rig) -> None:
    action = asyncio.create_task(
        rig.service.execute('from world import me\nreturn await me.console.echo(text="hold")')
    )
    await asyncio.wait_for(rig.entered.wait(), 5)
    closing = asyncio.create_task(rig.service.close())
    await asyncio.sleep(0.05)
    assert not closing.done() and rig.service.pending_resources > 0
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    with pytest.raises(ExecutionClosed):
        await rig.service.execute("return 1")
    rig.release.set()
    await rig.service.close()
    result = await action
    current = await rig.service.query(result.action_id)
    assert current is not None and current.state == "cancelled"
    assert current.calls[0].state == "confirmed" and rig.effects == ["hold"]
    assert rig.service.pending_resources == 0


async def test_missing_and_duplicate_implementations_fail_at_start(
    rig: _Rig, tmp_path: Path
) -> None:
    with pytest.raises(ConfigError, match="实现缺失"):
        ExecutionService(
            rig.catalog, (), rig.store, ExecutionSettings(), scratch_root=tmp_path / "other"
        )
    with pytest.raises(ValueError, match="重复"):
        ExecutionService(
            rig.catalog,
            (*rig.implementations, rig.implementations[0]),
            rig.store,
            ExecutionSettings(),
            scratch_root=tmp_path / "other",
        )


@pytest.mark.parametrize(
    ("text", "state", "error_type"),
    [("cancel", "unknown", "CancelledError"), ("bug", "failed", "RuntimeError")],
)
async def test_provider_program_failure_and_cancellation_keep_original_classification(
    rig: _Rig, text: str, state: str, error_type: str
) -> None:
    result = await rig.service.execute(
        f'from world import me\nreturn await me.console.echo(text="{text}")'
    )
    assert result.state == "failed" and result.calls[0].state == state
    assert result.calls[0].error_type == error_type
    assert not rig.effects


async def test_invalid_parameters_fail_at_provider_boundary_without_effect(rig: _Rig) -> None:
    result = await rig.service.execute(
        "from world import me\nreturn await me.console.echo(text=42)"
    )
    assert result.state == "failed" and result.calls[0].state == "failed"
    assert result.calls[0].error_type == "CapabilityInputError" and not rig.effects


async def test_late_unknown_retains_new_protocol_material(tmp_path: Path) -> None:
    value = await _rig(tmp_path, timeout_seconds=0.1)
    value.outcome_state = "unknown"
    value.output = "late platform status still uncertain"
    try:
        result = await value.service.execute(
            'from world import me\nreturn await me.console.echo(text="hold")'
        )
        assert result.calls[0].state == "unknown" and result.calls[0].receipt_revision == 1
        value.release.set()
        await value.service.close()
        current = await value.service.query(result.action_id)
        assert current is not None and current.calls[0].receipt_revision == 2
        assert current.calls[0].state == "unknown"
        assert json.loads(current.calls[0].result_json or "null") == value.output
        assert len(await value.store.pending_call_receipts()) == 2
    finally:
        value.release.set()
        await value.service.close()
        await value.store.close()


async def test_internal_dispatch_failure_finishes_action_before_reporting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = await _rig(tmp_path)

    async def reject_preparation(*arguments: object, **keywords: object) -> None:
        del arguments, keywords
        raise RuntimeError("synthetic dispatch failure")

    monkeypatch.setattr(value.store, "begin_call", reject_preparation)
    try:
        with pytest.raises(RuntimeError, match="synthetic dispatch failure"):
            await value.service.execute(
                'from world import me\nreturn await me.console.echo(text="must-not-run")'
            )
        assert not value.effects and value.service.pending_resources == 0
        with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
            row = connection.execute("SELECT state,error FROM actions").fetchone()
        assert row == ("failed", "RuntimeError: synthetic dispatch failure")
        with pytest.raises(BaseExceptionGroup, match="原始故障"):
            await value.service.close()
    finally:
        await value.store.close()


async def test_global_pending_bound_includes_providers_from_exited_workers(tmp_path: Path) -> None:
    value = await _rig(
        tmp_path,
        settings=ExecutionSettings(max_workers=2, max_calls=2, max_provider_calls=4),
        timeout_seconds=0.15,
    )
    source = (
        "from world import me\nimport asyncio\n"
        'return await asyncio.gather(me.console.echo(text="hold"), '
        'me.console.echo(text="hold"))'
    )
    try:
        results = await asyncio.gather(value.service.execute(source), value.service.execute(source))
        assert all(result.state == "failed" for result in results)
        assert value.service.pending_resources == 4
        rejected = await value.service.execute(
            'from world import me\nreturn await me.console.echo(text="must-not-start")'
        )
        assert rejected.state == "failed" and rejected.calls[0].state == "not_started"
        assert rejected.calls[0].error_type == "CallQueueFull"
        assert value.service.pending_resources == 4 and not value.effects
        value.release.set()
        await value.service.close()
        assert value.effects == ["hold"] * 4
    finally:
        value.release.set()
        await value.service.close()
        await value.store.close()


@pytest.mark.parametrize("field_name", ["action_id", "sequence", "path", "arguments"])
async def test_worker_call_identity_sequence_path_and_shape_are_checked_before_provider(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, field_name: str
) -> None:
    original_read = FrameChannel.read

    def corrupt_call(channel: FrameChannel) -> JsonObject:
        message = original_read(channel)
        if message.get("kind") == "call":
            if field_name == "sequence":
                message[field_name] = True
            elif field_name == "arguments":
                message[field_name] = ["not an object"]
            else:
                message[field_name] = "unbound"
        return message

    monkeypatch.setattr(FrameChannel, "read", corrupt_call)
    result = await rig.service.execute(
        'from world import me\nreturn await me.console.echo(text="must-not-run")'
    )
    assert result.state == "failed" and "ActionProtocolError" in (result.error or "")
    assert not result.calls and not rig.effects


async def test_ordinary_waiting_calls_rotate_between_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = await _rig(tmp_path, settings=ExecutionSettings(max_provider_calls=1))
    prepared = asyncio.Event()
    count = 0
    original_begin = value.store.begin_call

    async def observe_preparation(
        call_id: str,
        *,
        action_id: str,
        sequence: int,
        path: str,
        target_id: str,
        arguments_json: str,
    ) -> None:
        nonlocal count
        await original_begin(
            call_id,
            action_id=action_id,
            sequence=sequence,
            path=path,
            target_id=target_id,
            arguments_json=arguments_json,
        )
        count += 1
        if count == 5:
            prepared.set()

    monkeypatch.setattr(value.store, "begin_call", observe_preparation)
    first = asyncio.create_task(
        value.service.execute(
            "from world import me\nimport asyncio\n"
            'return await asyncio.gather(me.console.echo(text="hold"), '
            'me.console.echo(text="a1"), me.console.echo(text="a2"))'
        )
    )
    try:
        await asyncio.wait_for(value.entered.wait(), 5)
        second = asyncio.create_task(
            value.service.execute(
                "from world import me\nimport asyncio\n"
                'return await asyncio.gather(me.console.echo(text="b1"), '
                'me.console.echo(text="b2"))'
            )
        )
        await asyncio.wait_for(prepared.wait(), 5)
        assert value.effects == []
        value.release.set()
        assert (await first).state == "completed"
        assert (await second).state == "completed"
        assert value.effects == ["hold", "a1", "b1", "a2", "b2"]
    finally:
        value.release.set()
        await value.service.close()
        await value.store.close()


async def test_complete_catalog_crosses_frames_without_hiding_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    objects = (
        ObjectDeclaration("subject", "me", "主体"),
        ObjectDeclaration("console", "console", "控制台", "subject"),
    )
    declarations = tuple(
        CapabilityDeclaration(
            f"echo-{index}",
            "console",
            f"echo_{index}",
            "保存原样文本。",
            _Parameters,
            5.0,
            f'await me.console.echo_{index}(text="ok")',
        )
        for index in range(30)
    )
    catalog = Catalog(objects, declarations)

    async def echo(parameters: _Parameters, context: CallContext) -> CallOutcome:
        del context
        return CallOutcome("confirmed", {"text": parameters.text})

    store = await Store.open(tmp_path / "state.sqlite3")
    settings = ExecutionSettings(
        source_bytes=512,
        output_bytes=512,
        result_bytes=512,
        call_arguments_bytes=512,
        call_result_bytes=512,
        frame_bytes=1024,
    )
    service = ExecutionService(
        catalog,
        tuple(FunctionCapability(item, echo) for item in declarations),
        store,
        settings,
        scratch_root=tmp_path / "scratch",
    )
    original_send = FrameChannel.send
    chunks: list[int] = []

    def observe_chunk(channel: FrameChannel, payload: object) -> None:
        if isinstance(payload, dict):
            observed = cast(dict[object, object], payload)
            if observed.get("kind") == "binding_chunk":
                index = observed["index"]
                if isinstance(index, int):
                    chunks.append(index)
        original_send(channel, cast(object, payload))

    monkeypatch.setattr(FrameChannel, "send", observe_chunk)
    try:
        result = await service.execute(
            'from world import me\nreturn await me.console.echo_29(text="last binding")'
        )
        assert result.state == "completed" and len(chunks) > 1
        assert chunks == list(range(len(chunks)))
        assert result.calls[0].path == "me.console.echo_29"
        assert result.catalog_version == catalog.version and len(catalog.bindings) == 30
        assert all(
            binding.schema_json in catalog.instructions for binding in catalog.bindings.values()
        )
    finally:
        await service.close()
        await store.close()


async def test_pipe_close_failure_still_reclaims_job_and_preserves_call_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = await _rig(tmp_path)
    original_pipe_close = FrameChannel.close
    original_process_close = WindowsProcess.close
    process_closed = asyncio.Event()

    def fail_pipe_close(channel: FrameChannel) -> None:
        original_pipe_close(channel)
        raise OSError("synthetic pipe close failure")

    def observe_process_close(process: WindowsProcess) -> None:
        original_process_close(process)
        process_closed.set()

    monkeypatch.setattr(FrameChannel, "close", fail_pipe_close)
    monkeypatch.setattr(WindowsProcess, "close", observe_process_close)
    try:
        with pytest.raises(BaseExceptionGroup, match="Worker 资源回收失败"):
            await value.service.execute(
                'from world import me\nawait me.console.echo(text="before-cleanup-failure")'
            )
        assert process_closed.is_set()
        assert not list((tmp_path / "scratch").iterdir())
        pending = await value.store.pending_call_receipts()
        assert len(pending) == 1 and pending[0].state == "confirmed"
        record = await value.store.get_action(pending[0].action_id)
        assert record is not None and record.state == "failed"
        assert "ExceptionGroup" in (record.error or "")
        with pytest.raises(BaseExceptionGroup, match="原始故障"):
            await value.service.close()
    finally:
        await value.store.close()
