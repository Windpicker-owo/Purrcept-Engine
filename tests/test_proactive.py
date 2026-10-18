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

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        self.requests.put_nowait(request)
        if self.release is not None:
            await self.release.wait()
        return ModelResponse(Message.assistant("内部想法，不对外发送。"))


def _config(path: Path, *, enabled: bool = False, seconds: float = 0.15) -> EngineConfig:
    return EngineConfig.validate(
        {
            "host": {"data_dir": path},
            "models": {
                "main": {
                    "kind": "generation",
                    "model": "openai/offline",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "context_window": 100_000,
                    "max_output_tokens": 1000,
                    "capacity_rule": "reference-text-only-v1",
                }
            },
            "subject": {
                "foreground": "main",
                "reflection": "main",
                "summary": "main",
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


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True])
def test_proactive_interval_is_finite_positive(value: float) -> None:
    with pytest.raises(ValueError):
        CoordinatorSettings(4096, proactive_idle_seconds=value)
