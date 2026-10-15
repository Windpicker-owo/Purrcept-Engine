"""从真实 Host 接管到注意请求，验证直接 @ 的最低唤起与旧代次升级。

模型为可阻塞的离线替身，接管、处置版本、请求证据与连续历史使用真实
临时 SQLite。断言围绕生产入口及并发交接，不直接调用内部紧急升级方法。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from purrcept_core.models import (
    ChoiceAnswer,
    ChoiceProbability,
    DecisionRequest,
    DecisionResponse,
    Message,
    ModelEventSink,
    ModelRequest,
    ModelResponse,
    ModelUnavailableError,
    TextBlock,
)

from purrcept_engine import EngineConfig, EngineHost, InputContext, InputSource, InputSubmission
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.subject.attention import ATTENTION_QUESTION


@dataclass
class _Generation:
    """阻塞指定真实请求，取消仍按模型资源契约向调用者传播。"""

    blocks: dict[int, asyncio.Event] = field(default_factory=lambda: dict[int, asyncio.Event]())
    arrivals: asyncio.Queue[ModelRequest] = field(
        default_factory=lambda: asyncio.Queue[ModelRequest]()
    )
    requests: list[ModelRequest] = field(default_factory=lambda: list[ModelRequest]())
    cancelled: list[int] = field(default_factory=lambda: list[int]())

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        """返回内部文字，世界效果仍须通过生产行动工具取得。"""
        self.requests.append(request)
        index = len(self.requests)
        self.arrivals.put_nowait(request)
        try:
            if index in self.blocks:
                await self.blocks[index].wait()
        except asyncio.CancelledError:
            self.cancelled.append(index)
            raise
        return ModelResponse(Message.assistant(f"内部结束:{index}"))


@dataclass
class _Decision:
    """沿实际冻结题目返回选择或预期模型故障，不执行外部协议。"""

    choices: deque[str | Exception] = field(default_factory=lambda: deque[str | Exception]())
    release: asyncio.Event = field(default_factory=asyncio.Event)
    arrivals: asyncio.Queue[DecisionRequest] = field(
        default_factory=lambda: asyncio.Queue[DecisionRequest]()
    )
    requests: list[DecisionRequest] = field(default_factory=lambda: list[DecisionRequest]())

    async def decide(self, request: DecisionRequest, *, model: str) -> DecisionResponse:
        """保留完整请求供核对，以 Event 控制迟到判断。"""
        self.requests.append(request)
        self.arrivals.put_nowait(request)
        await self.release.wait()
        choice = self.choices.popleft() if self.choices else "wake"
        if isinstance(choice, Exception):
            raise choice
        return DecisionResponse(
            (
                ChoiceAnswer(
                    "attention",
                    choice,
                    tuple(
                        ChoiceProbability(option.value, 0.91 if option.value == choice else 0.03)
                        for option in ATTENTION_QUESTION.choices
                    ),
                    0.77,
                ),
            )
        )


@dataclass(frozen=True)
class _Rig:
    host: EngineHost
    generation: _Generation
    decision: _Decision


@asynccontextmanager
async def _running(
    directory: Path,
    *,
    choices: tuple[str | Exception, ...] = (),
    attention: bool = True,
    block_decision: bool = False,
    blocks: dict[int, asyncio.Event] | None = None,
    coalesce_seconds: float = 0.0,
) -> AsyncGenerator[_Rig]:
    """装配实际 Host 及独立反思替身，测试结束归还所有借用与目录锁。"""
    model = {
        "kind": "generation",
        "model": "openai/offline",
        "api_base": "http://127.0.0.1:9999/v1",
        "api_key": "synthetic",
        "context_window": 100_000,
        "max_output_tokens": 1000,
        "capacity_rule": "reference-text-only-v1",
    }
    config = EngineConfig.validate(
        {
            "host": {"data_dir": directory},
            "models": {
                "main": model,
                "reflection": model,
                "attention": {
                    "kind": "decision",
                    "model": "typed/offline",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "input_limit": 100_000,
                    "capacity_rule": "reference-text-only-v1",
                },
            },
            "subject": {
                "foreground": "main",
                "reflection": "reflection",
                "summary": "reflection",
                "attention": "attention" if attention else None,
                "attention_batch_seconds": coalesce_seconds,
            },
        }
    )
    generation = _Generation(blocks=blocks or {})
    decision = _Decision(choices=deque(choices))
    if not block_decision:
        decision.release.set()
    host = EngineHost(
        config,
        generation_backends={"main": generation, "reflection": _Generation()},
        decision_backends={"attention": decision},
    )
    await host.start()
    try:
        yield _Rig(host, generation, decision)
    finally:
        decision.release.set()
        for gate in generation.blocks.values():
            gate.set()
        await host.close()


def _input(identity: str, text: str, *, direct: bool = False) -> InputSubmission:
    """冻结一次原生来源，直接 @ 由接入关系而非正文关键词提供。"""
    now = datetime.now(UTC)
    return InputSubmission(
        InputSource(
            "qq" if direct else "console",
            "100",
            "group:200",
            "qq.message.received" if direct else "console.input",
            identity,
            "300",
        ),
        text,
        now,
        now,
        context=InputContext(
            '{"message":{"mentioned_self":true,"reply_to_self":null}}' if direct else "{}",
            minimum_wake=True,
        ),
    )


async def _processed(host: EngineHost, identity: str) -> None:
    """等待权威输入结案；模型返回与主脑内部文字不能替代处置结果。"""
    async with asyncio.timeout(5):
        while True:
            state = await host.query_processing(identity)
            assert state is not None
            if state.stage in ("processed", "failed"):
                return
            await asyncio.sleep(0.01)


async def _assessed(host: EngineHost, identity: str) -> str:
    """取得实际关联的判断请求身份，最低 WAKE 输入仍可以在旧生成后排队。"""
    async with asyncio.timeout(5):
        while True:
            state = await host.query_processing(identity)
            assert state is not None
            if state.attention_request_id is not None:
                return state.attention_request_id
            await asyncio.sleep(0.01)


async def test_direct_mention_wakes_immediately_then_context_urgent_interrupts_old_generation(
    tmp_path: Path,
) -> None:
    old_gate = asyncio.Event()
    async with _running(
        tmp_path,
        choices=("urgent",),
        block_decision=True,
        blocks={1: old_gate},
        coalesce_seconds=10,
    ) as rig:
        old = await rig.host.submit(_input("old", "旧轮次正在生成。"))
        await asyncio.wait_for(rig.generation.arrivals.get(), 5)
        direct = await rig.host.submit(
            _input("direct", "前面的结论有错，请立即核对。", direct=True)
        )
        state = await rig.host.query_processing(direct.input_id)
        assert state is not None and state.stage == "foreground"
        request = await asyncio.wait_for(rig.decision.arrivals.get(), 5)
        assert request.questions == (ATTENTION_QUESTION,)
        assert isinstance(request.input, tuple)
        text = "\n".join(block.text for block in request.input if isinstance(block, TextBlock))
        assert "前面的结论有错，请立即核对。" in text
        assert "mentioned_self" in text and "reply_to_self" in text
        assert not rig.generation.cancelled
        rig.decision.release.set()
        await _processed(rig.host, old.input_id)
        await _processed(rig.host, direct.input_id)
        assert rig.generation.cancelled == [1]
        assert len(rig.decision.requests) == 1 and len(rig.generation.requests) == 2


@pytest.mark.parametrize("choice", ["observe", "defer", "wake"])
async def test_direct_mention_assessment_cannot_downgrade_minimum_wake(
    tmp_path: Path, choice: str
) -> None:
    old_gate = asyncio.Event()
    async with _running(tmp_path, choices=(choice,), blocks={1: old_gate}) as rig:
        await rig.host.submit(_input("old", "旧轮次"))
        await asyncio.wait_for(rig.generation.arrivals.get(), 5)
        direct = await rig.host.submit(_input("direct", "仅仅问一个新问题。", direct=True))
        identity = await _assessed(rig.host, direct.input_id)
        state = await rig.host.query_processing(direct.input_id)
        assert state is not None and state.stage == "foreground" and state.due_at is None
        assert '"minimum_level":"wake"' in state.reason
        assert f'"assessed_level":"{choice}"' in state.reason
        evidence = await rig.host.query_request(identity)
        assert evidence is not None and evidence.state == "succeeded"
        assert not rig.generation.cancelled and len(rig.decision.requests) == 1
        old_gate.set()
        await _processed(rig.host, direct.input_id)
        assert len(rig.generation.requests) == 2


async def test_late_direct_mention_urgent_cannot_interrupt_round_processing_same_input(
    tmp_path: Path,
) -> None:
    old_gate, new_gate = asyncio.Event(), asyncio.Event()
    async with _running(
        tmp_path,
        choices=("urgent",),
        block_decision=True,
        blocks={1: old_gate, 2: new_gate},
    ) as rig:
        await rig.host.submit(_input("old", "第一轮"))
        await asyncio.wait_for(rig.generation.arrivals.get(), 5)
        direct = await rig.host.submit(_input("direct", "先唤起，判断稍后返回。", direct=True))
        await asyncio.wait_for(rig.decision.arrivals.get(), 5)
        old_gate.set()
        await asyncio.wait_for(rig.generation.arrivals.get(), 5)
        before = await rig.host.query_processing(direct.input_id)
        assert before is not None and before.stage == "processing"
        rig.decision.release.set()
        # 输入已被接管后不会再覆盖 disposition；升级请求仍在独立权威表结案。
        async with asyncio.timeout(5):
            while True:
                with sqlite3.connect(tmp_path / "engine.sqlite3") as connection:
                    row = connection.execute(
                        "SELECT state,request_id FROM minimum_wake_assessments WHERE input_id=?",
                        (direct.input_id,),
                    ).fetchone()
                if row is not None and row[0] == "completed":
                    break
                await asyncio.sleep(0.01)
        assert row[1] is not None
        assert not rig.generation.cancelled and len(rig.decision.requests) == 1
        assert await rig.host.query_processing(direct.input_id) == before
        new_gate.set()
        await _processed(rig.host, direct.input_id)
        assert len(rig.generation.requests) == 2


@pytest.mark.parametrize("attention", [True, False])
async def test_idle_direct_mention_does_not_start_extra_attention_request(
    tmp_path: Path, attention: bool
) -> None:
    async with _running(tmp_path, attention=attention) as rig:
        direct = await rig.host.submit(_input("direct", "普通直接唤起。", direct=True))
        await _processed(rig.host, direct.input_id)
        assert len(rig.generation.requests) == 1 and not rig.decision.requests


async def test_duplicate_direct_mention_does_not_request_or_queue_again(tmp_path: Path) -> None:
    old_gate = asyncio.Event()
    async with _running(tmp_path, choices=("wake",), blocks={1: old_gate}) as rig:
        await rig.host.submit(_input("old", "旧生成"))
        await asyncio.wait_for(rig.generation.arrivals.get(), 5)
        submission = _input("direct", "可重放的直接唤起。", direct=True)
        first = await rig.host.submit(submission)
        await _assessed(rig.host, first.input_id)
        duplicate = await rig.host.submit(submission)
        assert duplicate.duplicate and duplicate.input_id == first.input_id
        old_gate.set()
        await _processed(rig.host, first.input_id)
        assert len(rig.decision.requests) == 1 and len(rig.generation.requests) == 2


async def test_direct_mention_upgrade_does_not_wait_for_ordinary_coalesce_window(
    tmp_path: Path,
) -> None:
    old_gate = asyncio.Event()
    async with _running(
        tmp_path,
        choices=("urgent",),
        block_decision=True,
        blocks={1: old_gate},
        coalesce_seconds=10,
    ) as rig:
        await rig.host.submit(_input("old", "旧轮次"))
        await asyncio.wait_for(rig.generation.arrivals.get(), 5)
        ordinary = await rig.host.submit(
            replace(_input("ordinary", "普通等待组批的材料。"), context=InputContext())
        )
        direct = await rig.host.submit(_input("direct", "已直接唤起的紧急纠正。", direct=True))
        request = await asyncio.wait_for(rig.decision.arrivals.get(), 2)
        assert isinstance(request.input, tuple)
        material = "\n".join(block.text for block in request.input if isinstance(block, TextBlock))
        assert "已直接唤起的紧急纠正。" in material
        assert "普通等待组批的材料。" not in material
        pending = await rig.host.query_processing(ordinary.input_id)
        assert pending is not None and pending.stage == "attention"
        rig.decision.release.set()
        await _processed(rig.host, direct.input_id)
        assert rig.generation.cancelled == [1]


async def test_direct_mention_missing_materials_keep_wake_without_partial_judgment(
    tmp_path: Path,
) -> None:
    old_gate = asyncio.Event()
    async with _running(tmp_path, blocks={1: old_gate}) as rig:
        await rig.host.submit(_input("old", "旧轮次"))
        await asyncio.wait_for(rig.generation.arrivals.get(), 5)
        original = _input("direct", "实际尚未取得图片像素。", direct=True)
        direct = await rig.host.submit(
            replace(
                original,
                context=replace(original.context, missing_materials=("image not acquired",)),
            )
        )
        async with asyncio.timeout(5):
            while True:
                state = await rig.host.query_processing(direct.input_id)
                assert state is not None
                if "not_completed" in state.reason:
                    break
                await asyncio.sleep(0.01)
        assert state.stage == "foreground" and state.attention_request_id is None
        assert "image not acquired" in state.reason
        assert not rig.decision.requests and not rig.generation.cancelled
        old_gate.set()
        await _processed(rig.host, direct.input_id)
        assert len(rig.generation.requests) == 2 and not rig.decision.requests
        assert "image not acquired" in rig.generation.requests[1].messages[-1].text


async def test_direct_mention_model_unavailability_retains_wake_with_real_failure_evidence(
    tmp_path: Path,
) -> None:
    old_gate = asyncio.Event()
    failures = tuple(ModelUnavailableError("离线替身不可用", provider="test") for _ in range(2))
    async with _running(tmp_path, choices=failures, blocks={1: old_gate}) as rig:
        await rig.host.submit(_input("old", "旧生成"))
        await asyncio.wait_for(rig.generation.arrivals.get(), 5)
        direct = await rig.host.submit(_input("direct", "服务失败仍只交前台一次。", direct=True))
        identity = await _assessed(rig.host, direct.input_id)
        evidence = await rig.host.query_request(identity)
        assert evidence is not None and evidence.state == "failed" and len(evidence.attempts) == 2
        state = await rig.host.query_processing(direct.input_id)
        assert state is not None and state.stage == "foreground" and "not_completed" in state.reason
        assert not rig.generation.cancelled
        old_gate.set()
        await _processed(rig.host, direct.input_id)
        assert len(rig.decision.requests) == 2 and len(rig.generation.requests) == 2


async def test_restart_expires_upgrade_observation_and_retains_original_minimum_wake(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine.sqlite3"
    store = await Store.open(path)
    try:
        direct = await store.admit(_input("direct", "重启不能借用旧生成数字。", direct=True))
        expected = await store.input_processing(direct.input_id)
        assert expected is not None
        assert await store.register_minimum_wake_assessment(
            expected, owner_id="old-owner", observed_generation=1
        )
        assert not await store.register_minimum_wake_assessment(
            expected, owner_id="old-owner", observed_generation=1
        )
    finally:
        await store.close()
    async with _running(tmp_path) as rig:
        await _processed(rig.host, direct.input_id)
        assert len(rig.generation.requests) == 1 and not rig.decision.requests


async def test_upgrade_cas_retains_new_processing_version_and_does_not_requeue(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "engine.sqlite3")
    try:
        direct = await store.admit(_input("direct", "已经开始处理。", direct=True))
        expected = await store.input_processing(direct.input_id)
        assert expected is not None
        assert await store.register_minimum_wake_assessment(
            expected, owner_id="owner", observed_generation=1
        )
        observations = await store.minimum_wake_assessments("owner")
        assert len(observations) == 1
        turn = await store.begin_turn("next-turn", "foreground", 0, 2, (direct.input_id,))
        processing = await store.input_processing(direct.input_id)
        assert processing is not None and processing.stage == "processing"
        assert not await store.finish_minimum_wake_assessment(
            observations[0], "urgent", "旧判断迟到"
        )
        assert await store.input_processing(direct.input_id) == processing
        assert await store.minimum_wake_assessments("owner") == ()
        assert await store.staged_inputs("foreground") == ()
        await store.finish_turn(turn, "failed", error_json='{"error_type":"test_completed"}')
    finally:
        await store.close()
