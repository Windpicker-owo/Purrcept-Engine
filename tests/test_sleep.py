"""以真实 Host/Core/Worker/临时库验证睡眠调度与一次上下文压缩。

仅模型传输由脚本替身提供。床的实际调用、输入接管、休息持久化及摘要安装均走
生产边界，不连接真实账号、模型或设备，不从自然语言猜测是否入睡。
"""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from purrcept_core.models import (
    Message,
    MessageRole,
    ModelEventSink,
    ModelRequest,
    ModelResponse,
    ToolCallBlock,
)

from purrcept_engine import EngineConfig, EngineHost, InputSource, InputSubmission
from purrcept_engine.app.panel import ControlRequest
from purrcept_engine.domain.context.checkpoints import decode_checkpoint
from purrcept_engine.domain.inputs.models import InputContext
from purrcept_engine.domain.subject.models import InputProcessing
from purrcept_engine.infrastructure.sqlite.store import Store


@dataclass
class _Backend:
    """记录真实派发，可将指定请求停在响应前以检查采用与关闭顺序。"""

    response: str = "内部结束。"
    script: deque[ModelResponse] = field(default_factory=lambda: deque[ModelResponse]())
    requests: list[ModelRequest] = field(default_factory=lambda: list[ModelRequest]())
    arrivals: asyncio.Queue[ModelRequest] = field(
        default_factory=lambda: asyncio.Queue[ModelRequest]()
    )
    blocks: dict[int, asyncio.Event] = field(default_factory=lambda: dict[int, asyncio.Event]())
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        self.requests.append(request)
        self.arrivals.put_nowait(request)
        response = (
            self.script.popleft()
            if self.script
            else ModelResponse(Message.assistant(self.response))
        )
        try:
            if blocker := self.blocks.get(len(self.requests)):
                await blocker.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return response


def _config(path: Path, *, proactive: bool = False) -> EngineConfig:
    model = {
        "kind": "generation",
        "model": "openai/offline",
        "api_base": "http://127.0.0.1:9999/v1",
        "api_key": "synthetic",
        "context_window": 100_000,
        "max_output_tokens": 1000,
    }
    raw: dict[str, object] = {
        "host": {"data_dir": path},
        "models": {name: model for name in ("main", "attention", "summary")},
        "subject": {
            "foreground": "main",
            "reflection": "main",
            "summary": "summary",
            "attention": "attention",
            "attention_batch_seconds": 0.0,
            "proactive_thinking": proactive,
            "proactive_idle_seconds": 0.15,
        },
        "plugins": [{"instance_id": "bed", "entry": "bed", "config": {}}],
    }
    return EngineConfig.validate(raw, base_dir=path)


def _input(identity: str, *, priority: bool = True) -> InputSubmission:
    now = datetime.now(UTC)
    return InputSubmission(
        InputSource("test", "account", "chat", "text", identity),
        identity + "：真实日常经历。" * 200,
        now,
        now,
        context=InputContext(minimum_wake=priority),
    )


async def _processed(host: EngineHost, identity: str, stage: str = "processed") -> None:
    """等待实际账本状态，不以请求到达代替轮次已经结案。"""
    async with asyncio.timeout(5):
        while True:
            state = await host.query_processing(identity)
            if state is not None and state.stage == stage:
                return
            await asyncio.sleep(0.01)


async def _seed(host: EngineHost) -> None:
    for index in range(3):
        admitted = await host.submit(_input(f"history-{index}"))
        await _processed(host, admitted.input_id)


async def _proactive(host: EngineHost, enabled: bool) -> None:
    """通过真实控制隔开普通输入与极短主动期限，避免测试机器速度决定谁先唤醒。"""
    state = (await host.query_panel())["proactive_thinking"]
    assert isinstance(state, dict) and isinstance(state["revision"], str)
    await host.submit_control(
        ControlRequest(
            request_id=f"sleep-proactive-{enabled}",
            instance_id=host.query().instance_id,
            target="subject",
            operation="set_proactive_thinking",
            expected_revision=state["revision"],
            arguments={"enabled": enabled},
        )
    )
    async with asyncio.timeout(5):
        while True:
            current = (await host.query_panel())["proactive_thinking"]
            if isinstance(current, dict) and current["enabled"] is enabled:
                return
            await asyncio.sleep(0.01)


async def test_sleep_pauses_attention_and_proactive_priority_wakes_and_leave_restores(
    tmp_path: Path,
) -> None:
    main = _Backend()
    attention = _Backend(response='{"attention":"wake"}')
    summary = _Backend(response='{"summary":"历史摘要。"}')
    config = _config(tmp_path, proactive=True)
    config = config.model_copy(
        update={"execution": config.execution.model_copy(update={"max_pending_inputs": 256})}
    )
    host = EngineHost(
        config,
        generation_backends={
            "main": main,
            "attention": attention,
            "summary": summary,
        },
    )
    await host.start()
    try:
        assert (
            await host.invoke("me.bed.rest", {"expected_duration_minutes": 30})
        ).state == "completed"
        ordinary_ids: list[str] = []
        # 完整宿主使用真实接管额度；小到两条的 SQLite 额度另由存储测试验证，
        # 因为宿主还有独立后台读取，不能把瞬时线程队列压小当作积压模拟。
        for index in range(config.execution.max_pending_inputs + 4):
            ordinary = await host.submit(_input(f"sleep-background-{index}", priority=False))
            await _processed(host, ordinary.input_id)
            ordinary_ids.append(ordinary.input_id)
        await asyncio.sleep(0.35)
        assert not main.requests and not attention.requests and not summary.requests
        options = (await host.query_panel())["proactive_thinking"]
        assert isinstance(options, dict) and options["enabled"] is True
        priority = await host.submit(_input("high-priority"))
        await _processed(host, priority.input_id)
        assert len(main.requests) == 1 and not attention.requests
        assert not any(identity in main.requests[0].messages[-1].text for identity in ordinary_ids)
        original = await host.query_input(ordinary_ids[-1])
        assert original is not None and "sleep-background" in original.text
        await asyncio.sleep(0.25)
        assert len(main.requests) == 1
        # 先独立核对离床后普通输入的注意路线，再开启主动空闲唤醒。
        await _proactive(host, False)
        assert (await host.invoke("me.bed.leave", {})).state == "completed"
        await asyncio.sleep(0.05)
        assert len(main.requests) == 1
        awake = await host.submit(_input("awake-background", priority=False))
        await _processed(host, awake.input_id)
        assert len(attention.requests) == 1 and len(main.requests) == 2
        await _proactive(host, True)
        for _ in range(3):
            await asyncio.wait_for(main.arrivals.get(), 5)
        assert "空闲刺激" in main.requests[-1].messages[-1].text
    finally:
        await host.close()


@pytest.mark.parametrize("valid", [True, False])
async def test_bed_tool_compacts_after_turn_once_and_claim_survives_restart(
    tmp_path: Path,
    valid: bool,
) -> None:
    release = asyncio.Event()
    main = _Backend(blocks={5: release})
    summary = _Backend(
        response='{"summary":"已有三次日常交流，保留事实。"}' if valid else "无效摘要"
    )
    config = _config(tmp_path)
    backends = {"main": main, "attention": _Backend(), "summary": summary}
    host = EngineHost(config, generation_backends=backends)
    await host.start()
    try:
        await _seed(host)
        main.script.append(
            ModelResponse(
                Message(
                    MessageRole.ASSISTANT,
                    (
                        ToolCallBlock(
                            "sleep-action",
                            "play_action",
                            arguments={
                                "action_code": (
                                    "from world import me\n"
                                    "await me.bed.rest(expected_duration_minutes=30)"
                                ),
                            },
                        ),
                    ),
                )
            )
        )
        admitted = await host.submit(_input("go-to-bed"))
        for _ in range(5):
            await asyncio.wait_for(main.arrivals.get(), 5)
        await asyncio.sleep(0.1)
        assert not summary.requests
        release.set()
        await _processed(host, admitted.input_id)
        await asyncio.wait_for(summary.arrivals.get(), 5)
        await asyncio.sleep(0.15)
        assert (
            await host.invoke("me.bed.rest", {"expected_duration_minutes": 60})
        ).state == "completed"
        await asyncio.sleep(0.1)
        assert len(summary.requests) == 1 and not summary.requests[0].tools
    finally:
        release.set()
        await host.close()
    store = await Store.open(tmp_path / "engine.sqlite3")
    try:
        rest = await store.time.get_rest()
        assert rest is not None and rest.history_compaction_claimed
        session = await store.get_session("foreground")
        assert session.checkpoint_json is not None
        checkpoint = decode_checkpoint(session.checkpoint_json)
        assert len(checkpoint.history.turn_boundaries) == (2 if valid else 4)
    finally:
        await store.close()
    restarted_summary = _Backend()
    restarted = EngineHost(config, generation_backends={**backends, "summary": restarted_summary})
    await restarted.start()
    try:
        await asyncio.sleep(0.2)
        assert not restarted_summary.requests
    finally:
        await restarted.close()


async def test_entering_sleep_cancels_inflight_attention_without_waking_main(
    tmp_path: Path,
) -> None:
    release = asyncio.Event()
    main = _Backend()
    attention = _Backend(response='{"attention":"urgent"}', blocks={1: release})
    host = EngineHost(
        _config(tmp_path),
        generation_backends={
            "main": main,
            "attention": attention,
            "summary": _Backend(),
        },
    )
    await host.start()
    try:
        ordinary = await host.submit(_input("inflight", priority=False))
        await asyncio.wait_for(attention.arrivals.get(), 5)
        await host.invoke("me.bed.rest", {"expected_duration_minutes": 30})
        await asyncio.wait_for(attention.cancelled.wait(), 5)
        await _processed(host, ordinary.input_id)
        await asyncio.sleep(0.1)
        assert not main.requests
    finally:
        release.set()
        await host.close()


async def test_priority_and_shutdown_do_not_wait_for_sleep_summary(tmp_path: Path) -> None:
    release = asyncio.Event()
    main = _Backend()
    summary = _Backend(response='{"summary":"简短历史。"}', blocks={1: release})
    host = EngineHost(
        _config(tmp_path),
        generation_backends={
            "main": main,
            "attention": _Backend(),
            "summary": summary,
        },
    )
    await host.start()
    try:
        await _seed(host)
        await host.invoke("me.bed.rest", {"expected_duration_minutes": 30})
        await asyncio.wait_for(summary.arrivals.get(), 5)
        priority = await host.submit(_input("wake-during-summary"))
        await _processed(host, priority.input_id)
        assert len(main.requests) == 4 and not release.is_set()
    finally:
        assert (await asyncio.wait_for(host.close(), 5)).phase == "closed"
        release.set()
    assert summary.cancelled.is_set()


async def test_sleep_policy_records_old_wake_and_idle_but_preserves_trusted_priority(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "sleep.sqlite3")
    try:
        ordinary = await store.admit(_input("old-wake", priority=False))
        processing = await store.input_processing(ordinary.input_id)
        assert processing is not None and await store.submit_inputs(
            (processing,), minimum_wake=False
        )
        processing = await store.input_processing(ordinary.input_id)
        assert processing is not None and await store.decide_inputs((processing,), "wake", "旧判断")
        now = datetime.now(UTC)
        idle = await store.admit(
            InputSubmission(
                InputSource("subject.idle", "subject", "self", "subject.idle", "queued"),
                "睡前已经接管的主动思考",
                now,
                now,
                context=InputContext(minimum_wake=True),
            )
        )
        priority = await store.admit(_input("trusted"))
        await store.time.rest("bed", 30, now, "rest")
        # 即使协调者还没重新观察休息，实际取批和定时接管也须在事务中挡住旧意图。
        assert not await store.take_foreground_inputs()
        sequence, _ = await store.idle_input_state()
        assert (
            await store.admit_idle_input(
                _input("late-idle"), expected_sequence=sequence, foreground_overdue=True
            )
            is None
        )
        rest = await store.apply_sleep_policy("owner")
        assert rest is not None and await store.claim_sleep_compaction(rest)
        await store.time.rest("bed", 60, now, "duration-update")
        updated = await store.time.get_rest()
        assert updated is not None and updated.history_compaction_claimed
        assert not await store.claim_sleep_compaction(updated)
        for identity in (ordinary.input_id, idle.input_id):
            state = await store.input_processing(identity)
            assert state is not None and state.stage == "processed"
            assert state.reason == "subject.sleeping.recorded"
        state = await store.input_processing(priority.input_id)
        assert state is not None and state.stage == "priority"
        assert await store.submit_inputs((state,), minimum_wake=True)
        taken = await store.take_foreground_inputs()
        assert {record.input_id for record in taken} == {priority.input_id}
        await store.time.leave(now, "leave")
        await store.time.rest("bed", 30, now, "new-rest")
        new_rest = await store.time.get_rest()
        assert new_rest is not None and not new_rest.history_compaction_claimed
    finally:
        await store.close()


@pytest.mark.parametrize("priority_batch", [False, True])
async def test_sleep_admission_invalidates_snapshot_without_stopping_coordinator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    priority_batch: bool,
) -> None:
    """原文已选中但版本尚未读取时上床，协调者须重新取批而不是提交终态。"""
    store = await Store.open(tmp_path / "engine.sqlite3")
    try:
        ordinary = await store.admit(_input("selected-before-sleep", priority=False))
        direct = await store.admit(_input("direct", priority=True)) if priority_batch else None
    finally:
        await store.close()
    actual_processing = Store.input_processing
    changed = False
    changed_event = asyncio.Event()

    async def processing(owner: Store, input_id: str) -> InputProcessing | None:
        nonlocal changed
        if input_id == ordinary.input_id and not changed:
            changed = True
            await owner.time.rest("bed", 60, datetime.now(UTC), "racing-rest")
            # 正式接管采用新睡眠状态，在协调者两次读取之间结案旧普通记录。
            await owner.admit(_input("racing-background", priority=False))
            changed_event.set()
        return await actual_processing(owner, input_id)

    monkeypatch.setattr(Store, "input_processing", processing)
    main, attention = _Backend(), _Backend(response='{"attention":"wake"}')
    host = EngineHost(
        _config(tmp_path),
        generation_backends={
            "main": main,
            "attention": attention,
            "summary": _Backend(),
        },
    )
    await host.start()
    try:
        await asyncio.wait_for(changed_event.wait(), 3)
        await _processed(host, ordinary.input_id)
        assert changed
        if direct is None:
            direct = await host.submit(_input("after-sleep", priority=True))
        await _processed(host, direct.input_id)
        assert len(main.requests) == 1 and not attention.requests
        assert ordinary.input_id not in main.requests[0].messages[-1].text
    finally:
        await host.close()
