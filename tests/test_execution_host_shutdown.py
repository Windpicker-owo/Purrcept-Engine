"""验证 Host 宣布退出时立即撤销旧 Worker 和准备调用的后续派发资格。

使用真实 Windows Worker、宿主装配和临时 SQLite；唯一插件的停止接入故意
保持等待，从实际调用链检查资格撤销不能依赖后续执行器清理。已经进入 Provider
的调用继续返回真实确认，持久 started 门槛本身则不代表效果已经发生。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from purrcept_engine import EngineConfig, EngineHost
from purrcept_engine.domain.execution.models import CallOutcome as StoredCallOutcome
from purrcept_engine.domain.execution.models import CallRecord
from purrcept_engine.domain.inputs.models import MediaReference
from purrcept_engine.execution.catalog import CapabilityDeclaration, ObjectDeclaration
from purrcept_engine.execution.providers import CallContext, CallOutcome, FunctionCapability
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system.contracts import (
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
)
from purrcept_engine.values import decode_object


class Parameters(BaseModel):
    """脚本给出明确步骤标签，测试不依据 Worker 自报确认外部效果。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    step: str


@dataclass
class Probe:
    """只记录实际 Provider 进入与归还；慢停止不暂停回执通路。"""

    first_entered: asyncio.Event = field(default_factory=asyncio.Event)
    first_release: asyncio.Event = field(default_factory=asyncio.Event)
    stop_entered: asyncio.Event = field(default_factory=asyncio.Event)
    stop_release: asyncio.Event = field(default_factory=asyncio.Event)
    contexts: list[CallContext] = field(default_factory=list[CallContext])
    effects: list[str] = field(default_factory=list[str])
    borrowed: int = 0
    stop_count: int = 0
    close_count: int = 0

    def factory(self) -> PluginFactory:
        """声明和装配通过实际 Host 完成，不替换执行器或能力派发。"""

        def prepare(identity: str, raw: object, base: Path) -> PluginBlueprint:
            del raw, base
            declaration = CapabilityDeclaration(
                identity + ".call",
                identity,
                "call",
                "记录明确本地步骤的实际完成。",
                Parameters,
                10.0,
                'await me.probe.call(step="first")',
            )

            async def build(ports: PluginPorts) -> PluginRuntime:
                del ports
                return PluginRuntime(
                    (FunctionCapability(declaration, self.invoke),),
                    stop_inputs=self.stop_inputs,
                    close=self.close,
                )

            return PluginBlueprint(
                (ObjectDeclaration(identity, identity, "本地测试资源", "subject"),),
                (declaration,),
                build,
            )

        return PluginFactory(prepare)

    async def invoke(self, parameters: Parameters, context: CallContext) -> CallOutcome:
        """首次能力等待显式释放，其余步骤只有实际进入后才登记效果。"""
        self.contexts.append(context)
        self.borrowed += 1
        try:
            if parameters.step == "first":
                self.first_entered.set()
                await self.first_release.wait()
            self.effects.append(parameters.step)
            return CallOutcome("confirmed", {"step": parameters.step})
        finally:
            self.borrowed -= 1

    async def stop_inputs(self) -> None:
        """保留一个真实慢停止窗口，不能由测试先关闭执行器来替代宿主顺序。"""
        self.stop_count += 1
        self.stop_entered.set()
        await self.stop_release.wait()

    async def close(self) -> None:
        """真实 Provider 已归还后才能关闭它借用的插件资源。"""
        assert self.borrowed == 0
        self.close_count += 1


@dataclass
class Rig:
    """保存宿主和实际事务通知；夹具失败时仍释放每个测试设置的障碍。"""

    host: EngineHost
    probe: Probe
    directory: Path
    committed: asyncio.Queue[CallRecord]
    barriers: list[asyncio.Event] = field(default_factory=list[asyncio.Event])

    async def receipt(self, call_id: str) -> CallRecord:
        """等待真正 record_call_result 提交后的通知，不轮询猜测数据库时序。"""
        async with asyncio.timeout(5):
            while True:
                record = await self.committed.get()
                if record.call_id == call_id:
                    return record

    async def finish(self, closing: asyncio.Task[object]) -> None:
        """解除慢停止并证明资源真正归还，包括 Worker 临时目录。"""
        self.probe.stop_release.set()
        await asyncio.wait_for(closing, 5)
        assert self.host.query().phase == "closed"
        assert self.host.query().pending_resources == ()
        assert self.probe.stop_count == self.probe.close_count == 1
        assert list((self.directory / "scratch").iterdir()) == []


@pytest.fixture
async def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    probe = Probe()
    committed: asyncio.Queue[CallRecord] = asyncio.Queue()
    original_record = Store.record_call_result

    async def record(
        store: Store,
        call_id: str,
        *,
        state: StoredCallOutcome,
        result_json: str | None,
        error_type: str | None,
        error: str | None,
        media: tuple[MediaReference, ...] = (),
    ) -> CallRecord:
        saved = await original_record(
            store,
            call_id,
            state=state,
            result_json=result_json,
            error_type=error_type,
            error=error,
            media=media,
        )
        committed.put_nowait(saved)
        return saved

    monkeypatch.setattr(Store, "record_call_result", record)
    config = EngineConfig.validate(
        {
            "host": {"data_dir": tmp_path, "close_seconds": 10.0},
            "console": False,
            "execution": {"max_provider_calls": 1},
            "models": {
                "main": {
                    "kind": "generation",
                    "model": "openai/offline",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "context_window": 100000,
                    "max_output_tokens": 1000,
                    "capacity_rule": "reference-text-only-v1",
                }
            },
            "subject": {"foreground": "main", "reflection": "main", "summary": "main"},
            "plugins": [{"instance_id": "probe", "entry": "shutdown-probe"}],
        },
        base_dir=tmp_path,
    )
    host = EngineHost(config, plugin_factories={"shutdown-probe": probe.factory()})
    await host.start()
    value = Rig(host, probe, tmp_path, committed)
    try:
        yield value
    finally:
        probe.first_release.set()
        probe.stop_release.set()
        for barrier in value.barriers:
            barrier.set()
        await host.close()


async def test_old_worker_cannot_start_next_step_while_plugin_stop_is_blocked(rig: Rig) -> None:
    """宿主已经 closing 的同一窗口，首调用收尾不授予旧脚本第二调用资格。"""
    action = asyncio.create_task(
        rig.host.execute(
            'from world import me\nawait me.probe.call(step="first")\n'
            'return await me.probe.call(step="next")'
        )
    )
    await asyncio.wait_for(rig.probe.first_entered.wait(), 5)
    first_context = rig.probe.contexts[0]
    closing = asyncio.create_task(rig.host.close())
    await asyncio.wait_for(rig.probe.stop_entered.wait(), 5)
    assert rig.host.query().phase == "closing" and not closing.done()
    rig.probe.first_release.set()
    first_receipt = await rig.receipt(first_context.call_id)
    await asyncio.wait_for(action, 5)
    assert first_receipt.state == "confirmed"
    assert rig.probe.effects == ["first"]
    assert len(rig.probe.contexts) == 1
    assert not rig.probe.stop_release.is_set()
    current = await rig.host.query_action(first_context.action_id)
    assert current is not None and current.calls[0].state == "confirmed"
    await rig.finish(closing)

    store = await Store.open(rig.directory / "engine.sqlite3")
    try:
        persisted = await store.get_action(first_context.action_id)
        assert persisted is not None and persisted.calls[0].state == "confirmed"
        assert decode_object((persisted.calls[0].result_json or "{}").encode()) == {"step": "first"}
    finally:
        await store.close()


async def test_close_revokes_worker_call_waiting_after_persistent_start_barrier(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """数据库启动门槛完成后仍须重新核验，关闭不能让障碍后的 Provider 开始。"""
    committed, release = asyncio.Event(), asyncio.Event()
    rig.barriers.append(release)
    original_start = Store.mark_call_started

    async def delayed(store: Store, call_id: str) -> None:
        await original_start(store, call_id)
        committed.set()
        await release.wait()

    monkeypatch.setattr(Store, "mark_call_started", delayed)
    action = asyncio.create_task(
        rig.host.execute('from world import me\nreturn await me.probe.call(step="next")')
    )
    await asyncio.wait_for(committed.wait(), 5)
    closing = asyncio.create_task(rig.host.close())
    await asyncio.wait_for(rig.probe.stop_entered.wait(), 5)
    assert rig.host.query().phase == "closing"
    release.set()
    result = await asyncio.wait_for(action, 5)
    assert rig.probe.contexts == [] and rig.probe.effects == []
    assert len(result.calls) == 1
    receipt = await rig.receipt(result.calls[0].call_id)
    assert receipt.state == "not_started"
    assert receipt.started_at is not None
    assert not rig.probe.stop_release.is_set()
    await rig.finish(closing)


async def test_close_revokes_already_prepared_queued_worker_call(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """普通名额释放不能使关闭前已准备、但尚未进入 Provider 的调用续跑。"""
    prepared = asyncio.Event()
    original_begin = Store.begin_call

    async def begin(
        store: Store,
        call_id: str,
        *,
        action_id: str,
        sequence: int,
        path: str,
        target_id: str,
        arguments_json: str,
    ) -> None:
        await original_begin(
            store,
            call_id,
            action_id=action_id,
            sequence=sequence,
            path=path,
            target_id=target_id,
            arguments_json=arguments_json,
        )
        if decode_object(arguments_json.encode()).get("step") == "queued":
            prepared.set()

    monkeypatch.setattr(Store, "begin_call", begin)
    first = asyncio.create_task(rig.host.invoke("me.probe.call", {"step": "first"}))
    await asyncio.wait_for(rig.probe.first_entered.wait(), 5)
    queued = asyncio.create_task(
        rig.host.execute('from world import me\nreturn await me.probe.call(step="queued")')
    )
    await asyncio.wait_for(prepared.wait(), 5)
    closing = asyncio.create_task(rig.host.close())
    await asyncio.wait_for(rig.probe.stop_entered.wait(), 5)
    assert rig.host.query().phase == "closing" and not closing.done()
    rig.probe.first_release.set()
    first_record = await asyncio.wait_for(first, 5)
    queued_record = await asyncio.wait_for(queued, 5)
    assert first_record.calls[0].state == "confirmed"
    assert rig.probe.effects == ["first"] and len(rig.probe.contexts) == 1
    assert len(queued_record.calls) == 1
    receipt = await rig.receipt(queued_record.calls[0].call_id)
    assert receipt.state == "not_started"
    assert receipt.started_at is None
    assert not rig.probe.stop_release.is_set()
    await rig.finish(closing)
