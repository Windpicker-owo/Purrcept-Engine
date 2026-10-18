"""验证来源取消不丢失已接管 VAD 的停音和下一轮通知，不撤销当前生成。

使用真实Host、Core和临时SQLite，合成音频端口只记录宿主交接；数据库
完成后暂留提交等待，复现来源观察消失但后续交接仍必须发生的窗口。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from purrcept_core.models import Message, ModelEventSink, ModelRequest, ModelResponse

from purrcept_engine import EngineConfig, EngineHost, InputSource, InputSubmission
from purrcept_engine.domain.execution.providers import CallContext, CallOutcome
from purrcept_engine.domain.inputs.models import Admission, InputContext
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system.body_port import BodyPort
from purrcept_engine.plugin_system.contracts import (
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
)
from purrcept_engine.values import JsonObject


class Audio:
    """有限音频端口只记录正式停音身份，没有真实输出或网络。"""

    def __init__(self) -> None:
        self.controls: list[CallContext] = []
        self.stopped = asyncio.Event()

    def bind_body(self, body: BodyPort | None) -> None:
        assert body is None

    async def interrupt(self, context: CallContext) -> CallOutcome:
        self.controls.append(context)
        self.stopped.set()
        return CallOutcome("confirmed", {"stopped": True})

    def snapshot(self) -> JsonObject:
        return {"state": "idle"}


class Backend:
    """首个真实 Core 调用保持借用，明确记录 VAD 是否误取消当前生成。"""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.following = asyncio.Event()
        self.requests = 0
        self.materials: list[ModelRequest] = []

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        del model, emit
        self.requests += 1
        self.materials.append(request)
        if self.requests == 1:
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        else:
            self.following.set()
        return ModelResponse(Message.assistant("内部结束"))


async def test_cancelled_admission_observer_still_stops_audio_and_delivers_next_turn_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio, backend = Audio(), Backend()

    async def build(ports: PluginPorts) -> PluginRuntime:
        del ports
        return PluginRuntime((), audio_output=audio)

    def prepare(identity: str, raw: object, base: Path) -> PluginBlueprint:
        del identity, raw, base
        return PluginBlueprint((), (), build)

    config = EngineConfig.validate(
        {
            "host": {"data_dir": tmp_path},
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
            "plugins": [{"instance_id": "audio", "entry": "synthetic"}],
        },
        base_dir=tmp_path,
    )
    host = EngineHost(
        config,
        generation_backends={"main": backend},
        plugin_factories={"synthetic": PluginFactory(prepare)},
    )
    original_admit = Store.admit
    committed, release = asyncio.Event(), asyncio.Event()
    accepted: list[Admission] = []

    async def delayed(store: Store, submission: InputSubmission) -> Admission:
        result = await original_admit(store, submission)
        if submission.context.speech_started and not result.duplicate:
            accepted.append(result)
            committed.set()
            await release.wait()
        return result

    monkeypatch.setattr(Store, "admit", delayed)
    await host.start()
    try:
        now = datetime.now(UTC)
        first = await host.submit(
            InputSubmission(
                InputSource("qq", "a", "private", "message", "first"),
                "QQ 私聊提问",
                now,
                now,
                context=InputContext(minimum_wake=True),
            )
        )
        await asyncio.wait_for(backend.entered.wait(), 3)
        vad = InputSubmission(
            InputSource("voice", "microphone", "session", "speech_started", "vad-1"),
            "可信VAD",
            now,
            now,
            context=InputContext(minimum_wake=True, speech_started=True),
        )
        observer = asyncio.create_task(host.submit(vad))
        await asyncio.wait_for(committed.wait(), 3)
        observer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observer
        record = await host.query_input(accepted[0].input_id)
        assert record is not None and record.text == "可信VAD"
        release.set()
        await asyncio.wait_for(audio.stopped.wait(), 3)
        assert not backend.cancelled.is_set() and backend.requests == 1
        repeated = await host.submit(vad)
        assert repeated.duplicate and repeated.input_id == accepted[0].input_id
        assert len(audio.controls) == 1
        context = audio.controls[0]
        assert context.input_id == repeated.input_id
        assert context.call_id != context.action_id and context.deadline > 0
        assert (await host.query_panel())["input_handoff_failure"] is None
        backend.release.set()
        await asyncio.wait_for(backend.following.wait(), 3)
        async with asyncio.timeout(3):
            while True:
                processing = await host.query_processing(repeated.input_id)
                if processing is not None and processing.stage == "processed":
                    break
                await asyncio.sleep(0.005)
        assert not backend.cancelled.is_set() and backend.requests == 2
        assert "可信VAD" in backend.materials[1].messages[-1].text
        original = await host.query_processing(first.input_id)
        assert original is not None and original.stage == "processed"
    finally:
        release.set()
        backend.release.set()
        await host.close()
