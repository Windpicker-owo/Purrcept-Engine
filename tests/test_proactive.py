"""真实 Host/Core/临时 SQLite 的空闲刺激、页面控制与资源生命周期回归。

模型只返回内部文字或阻塞等待，测试不接入账号和设备。通过正式接管与控制入口
检查定时事实，不把模型替身的固定回复当作真实模型语义效果。
"""

import asyncio
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from purrcept_core.models import Message, ModelEventSink, ModelRequest, ModelResponse

from purrcept_engine import EngineConfig, EngineHost, InputSource, InputSubmission
from purrcept_engine.app.panel import ControlRequest
from purrcept_engine.domain.inputs.models import InputContext
from purrcept_engine.domain.subject.models import InputProcessing
from purrcept_engine.domain.subject.settings import CoordinatorSettings
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.values import JsonObject


@dataclass
class _Model:
    """记录真实派发请求，可阻塞首轮以观察后台是否积累多份空闲输入。"""

    requests: asyncio.Queue[ModelRequest] = field(
        default_factory=lambda: asyncio.Queue[ModelRequest]()
    )
    release: asyncio.Event | None = None
    response: str = "内部想法，不对外发送。"

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        self.requests.put_nowait(request)
        if self.release is not None:
            await self.release.wait()
        return ModelResponse(Message.assistant(self.response))


def _config(
    path: Path, *, enabled: bool = False, seconds: float = 0.15, attention: bool = False
) -> EngineConfig:
    model = {
        "kind": "generation",
        "model": "openai/offline",
        "api_base": "http://127.0.0.1:9999/v1",
        "api_key": "synthetic",
        "context_window": 100_000,
        "max_output_tokens": 1000,
    }
    return EngineConfig.validate(
        {
            "host": {"data_dir": path},
            "models": {"main": model, **({"attention": model} if attention else {})},
            "subject": {
                "foreground": "main",
                "reflection": "main",
                "summary": "main",
                "attention": "attention" if attention else None,
                "attention_batch_seconds": 0.01,
                "proactive_thinking": enabled,
                "proactive_idle_seconds": seconds,
            },
        },
        base_dir=path,
    )


def _input(identity: str, *, minimum: bool = True) -> InputSubmission:
    now = datetime.now(UTC)
    return InputSubmission(
        InputSource("test", "self", "chat", "message", identity),
        "真实新刺激",
        now,
        now,
        context=InputContext(minimum_wake=minimum),
    )


async def _state(host: EngineHost) -> JsonObject:
    state = (await host.query_panel())["proactive_thinking"]
    assert isinstance(state, dict)
    return state


async def _toggle(host: EngineHost, enabled: bool, identity: str, revision: str):
    return await host.submit_control(
        ControlRequest(
            request_id=identity,
            instance_id=host.query().instance_id,
            target="subject",
            operation="set_proactive_thinking",
            expected_revision=revision,
            arguments={"enabled": enabled},
        )
    )


def _idle_count(path: Path) -> int:
    with sqlite3.connect(path / "engine.sqlite3") as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM inputs WHERE adapter_id='subject.idle'"
        ).fetchone()
        assert row is not None and isinstance(row[0], int)
        return row[0]


async def test_disabled_toggle_dedup_stale_control_and_restart_defaults(tmp_path: Path) -> None:
    model = _Model()
    config = _config(tmp_path)
    host = EngineHost(config, generation_backends={"main": model})
    await host.start()
    try:
        assert (await _state(host))["enabled"] is False
        await asyncio.sleep(0.2)
        assert _idle_count(tmp_path) == 0 and model.requests.empty()
        first = await _toggle(host, True, "on", "0")
        assert first.state == "finished"
        repeated = await _toggle(host, True, "on", "0")
        assert repeated == first and (await _state(host))["revision"] == "1"
        stale = await _toggle(host, False, "stale", "0")
        assert stale.error_code == "stale_revision" and (await _state(host))["enabled"] is True
        assert (await _toggle(host, False, "off", "1")).state == "finished"
        await asyncio.sleep(0.2)
        assert _idle_count(tmp_path) == 0 and model.requests.empty()
        assert (await _toggle(host, True, "on-again", "2")).state == "finished"
    finally:
        await host.close()
    restarted = EngineHost(config, generation_backends={"main": _Model()})
    await restarted.start()
    try:
        assert (await _state(restarted))["enabled"] is False
        assert (await _state(restarted))["revision"] == "0"
    finally:
        await restarted.close()


async def test_idle_stimulus_is_dated_wakes_without_attention_and_does_not_pile_up(
    tmp_path: Path,
) -> None:
    release = asyncio.Event()
    model = _Model(release=release)
    host = EngineHost(_config(tmp_path, enabled=True), generation_backends={"main": model})
    await host.start()
    try:
        request = await asyncio.wait_for(model.requests.get(), 5)
        text = "\n".join(message.text for message in request.messages)
        assert "空闲刺激" in text and "不是任何人的发言" in text
        assert "occurred_at" in text and "+00:00" in text
        assert _idle_count(tmp_path) == 1
        await asyncio.sleep(0.35)
        assert _idle_count(tmp_path) == 1 and model.requests.empty()
        # 关闭只阻止新的定时输入，不取消已经接手的一轮。
        assert (await _toggle(host, False, "off", "0")).state == "finished"
        release.set()
        await asyncio.sleep(0.2)
        assert _idle_count(tmp_path) == 1 and model.requests.empty()
        assert (await _toggle(host, True, "on", "1")).state == "finished"
        await asyncio.wait_for(model.requests.get(), 5)
        assert _idle_count(tmp_path) == 2
    finally:
        release.set()
        await host.close()
    count = _idle_count(tmp_path)
    await asyncio.sleep(0.2)
    assert _idle_count(tmp_path) == count


async def test_new_stimulus_resets_idle_interval_after_processing(tmp_path: Path) -> None:
    model = _Model()
    seconds = 0.3
    host = EngineHost(
        _config(tmp_path, enabled=True, seconds=seconds), generation_backends={"main": model}
    )
    await host.start()
    try:
        await asyncio.sleep(0.1)
        submission = _input("external")
        admitted = await host.submit(submission)
        await asyncio.wait_for(model.requests.get(), 5)
        duplicate = await host.submit(submission)
        assert duplicate.duplicate and duplicate.input_id == admitted.input_id
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(model.requests.get(), seconds / 2)
        request = await asyncio.wait_for(model.requests.get(), 5)
        assert "空闲刺激" in request.messages[-1].text and _idle_count(tmp_path) == 1
    finally:
        await host.close()


async def test_continuous_observe_inputs_wake_since_last_foreground_activation(
    tmp_path: Path,
) -> None:
    """连续背景输入只重置无刺激期限，实际主脑唤醒独立重置另一个期限。"""
    seconds = 0.5
    model = _Model()
    attention = _Model(response='{"attention":"observe"}')
    host = EngineHost(
        _config(tmp_path, enabled=True, seconds=seconds, attention=True),
        generation_backends={"main": model, "attention": attention},
    )
    await host.start()
    producer: asyncio.Task[None] | None = None
    stop = asyncio.Event()
    try:
        # 先让启动期限经过一部分，再用真实唤醒建立新起点。
        await asyncio.sleep(seconds * 0.5)
        await host.submit(_input("wake"))
        await asyncio.wait_for(model.requests.get(), 5)
        activated_at = asyncio.get_running_loop().time()
        admitted = await host.submit(_input("observed-first", minimum=False))

        async def feed() -> None:
            """始终在静默期限内接入新刺激，避免误走原有无刺激分支。"""
            for index in range(30):
                if stop.is_set():
                    return
                await host.submit(_input(f"background-{index}", minimum=False))
                await asyncio.sleep(seconds / 10)

        producer = asyncio.create_task(feed())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(model.requests.get(), seconds * 0.65)
        assert _idle_count(tmp_path) == 0
        request = await asyncio.wait_for(model.requests.get(), seconds * 2)
        # 主脑取批早于传输到达，允许两次材料准备耗时不同，仍不能沿启动旧期限早醒。
        assert asyncio.get_running_loop().time() - activated_at >= seconds * 0.9
        text = request.messages[-1].text
        assert "没有激活主脑" in text and "没有新的刺激" not in text
        assert admitted.input_id in text and "真实新刺激" in text
        assert attention.requests.qsize() >= 3 and _idle_count(tmp_path) == 1
    finally:
        if producer is not None:
            stop.set()
            await asyncio.wait_for(producer, 5)
        await host.close()


@pytest.mark.parametrize("late_choice", ["observe", "urgent"])
async def test_foreground_timeout_wakes_during_attention_and_late_result_is_stale(
    tmp_path: Path, late_choice: str
) -> None:
    """注意调用不能阻塞到期唤醒，迟到判断也不能撤销已取走批次或抢占新轮次。"""
    foreground_release, attention_release = asyncio.Event(), asyncio.Event()
    model = _Model(release=foreground_release)
    attention = _Model(release=attention_release, response='{"attention":"' + late_choice + '"}')
    host = EngineHost(
        _config(tmp_path, enabled=True, seconds=0.2, attention=True),
        generation_backends={"main": model, "attention": attention},
    )
    await host.start()
    try:
        admitted = await host.submit(_input("held-by-attention", minimum=False))
        await asyncio.wait_for(attention.requests.get(), 5)
        request = await asyncio.wait_for(model.requests.get(), 1)
        assert admitted.input_id in request.messages[-1].text
        assert "没有激活主脑" in request.messages[-1].text
        attention_release.set()
        await asyncio.sleep(0.45)
        assert model.requests.empty() and _idle_count(tmp_path) == 1
        foreground = (await host.query_panel())["foreground"]
        assert isinstance(foreground, dict) and foreground["busy"] is True
    finally:
        foreground_release.set()
        attention_release.set()
        await host.close()


async def test_idle_admission_atomically_rejects_new_input_and_allows_observed_inbox(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "idle.sqlite3", max_inputs=8, max_input_bytes=100_000)
    try:
        sequence, pending = await store.idle_input_state()
        assert sequence == 0 and not pending
        admitted = await store.admit(_input("external", minimum=False))
        idle = _input("synthetic")
        assert await store.admit_idle_input(idle, expected_sequence=sequence) is None
        sequence, pending = await store.idle_input_state()
        assert pending
        assert await store.admit_idle_input(idle, expected_sequence=sequence) is None
        processing = await store.input_processing(admitted.input_id)
        assert processing is not None
        assert await store.submit_inputs((processing,), minimum_wake=False)
        processing = await store.input_processing(admitted.input_id)
        assert processing is not None
        assert await store.decide_inputs((processing,), "observe", "只提交不唤醒")
        assert await store.idle_input_state() == (sequence, False)
        accepted = await store.admit_idle_input(idle, expected_sequence=sequence)
        assert accepted is not None
        records = await store.buffered_inputs()
        expected: list[InputProcessing] = []
        for item in records:
            processing = await store.input_processing(item.input_id)
            assert processing is not None
            expected.append(processing)
        assert await store.submit_inputs(tuple(expected), minimum_wake=True)
        assert {item.input_id for item in await store.take_foreground_inputs()} == {
            accepted.input_id,
            admitted.input_id,
        }
    finally:
        await store.close()


@pytest.mark.parametrize("stage", ["attention", "assessing", "submitted"])
async def test_overdue_admission_allows_attention_but_never_duplicates_scheduled_foreground(
    tmp_path: Path, stage: str
) -> None:
    """到期分支可越过新普通输入或未决注意，但优先、唤醒及已取批都原子挡住重复。"""
    store = await Store.open(tmp_path / "overdue.sqlite3")
    try:
        sequence, _ = await store.idle_input_state()
        admitted = await store.admit(_input("new-background", minimum=False))
        if stage != "attention":
            processing = await store.input_processing(admitted.input_id)
            assert processing is not None
            assert await store.submit_inputs((processing,), minimum_wake=False)
        if stage == "submitted":
            processing = await store.input_processing(admitted.input_id)
            assert processing is not None
            assert await store.decide_inputs((processing,), "observe", "背景观察")
        idle = _input("overdue")
        assert await store.admit_idle_input(idle, expected_sequence=sequence) is None
        accepted = await store.admit_idle_input(
            idle, expected_sequence=sequence, foreground_overdue=True
        )
        assert accepted is not None
        another = _input("another-overdue")
        for step in ("priority", "foreground", "claimed"):
            if step == "foreground":
                records = await store.buffered_inputs()
                expected: list[InputProcessing] = []
                for record in records:
                    processing = await store.input_processing(record.input_id)
                    assert processing is not None
                    expected.append(processing)
                assert await store.submit_inputs(tuple(expected), minimum_wake=True)
            elif step == "claimed":
                taken = await store.take_foreground_inputs()
                assert {record.input_id for record in taken} == {
                    admitted.input_id,
                    accepted.input_id,
                }
            assert (
                await store.admit_idle_input(
                    another, expected_sequence=sequence, foreground_overdue=True
                )
                is None
            )
    finally:
        await store.close()


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True])
def test_proactive_interval_is_finite_positive(value: float) -> None:
    with pytest.raises(ValueError):
        CoordinatorSettings(4096, proactive_idle_seconds=value)
