"""用实际 Store/Core 请求核验未决尾部、来源保护及明确容量失败。

材料由正式输入、逐次回执和反思监管提交产生；模型只用离线响应替身。
测试不启动接入工作器或设备，所有数据库线程均在同一测试内实际关闭。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from purrcept_core.models import (
    ConversationCheckpoint,
    ConversationState,
    Message,
    ModelEventSink,
    ModelRequest,
    ModelResponse,
    ReminderState,
    ToolSet,
)

from purrcept_engine.app.assembly import prepare_plugins
from purrcept_engine.app.config import EngineConfig
from purrcept_engine.app.subject import SubjectAssembly, build_subject
from purrcept_engine.context.capacity import CapacityError
from purrcept_engine.context.checkpoints import encode_checkpoint
from purrcept_engine.errors import StorageError
from purrcept_engine.execution.protocol import JsonObject, JsonValue, decode_object
from purrcept_engine.storage import knowledge as knowledge_sql
from purrcept_engine.storage.activities import ActivityRecord
from purrcept_engine.storage.execution import ActionRecord
from purrcept_engine.storage.knowledge import EvidenceSnapshot, SupervisionNotice, WakeupPlan
from purrcept_engine.storage.model_requests import encode
from purrcept_engine.storage.models import InputContext, InputSource, InputSubmission
from purrcept_engine.storage.store import Store
from purrcept_engine.subject.knowledge import KnowledgeService
from purrcept_engine.subject.model_requests import compile_materials


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    """收回真实专属线程，断言失败不泄露存储所有者。"""
    instance = await Store.open(tmp_path / "subject.sqlite3")
    try:
        yield instance
    finally:
        await instance.close()


async def admit(store: Store, event: str) -> str:
    """正式接管一项有独立来源身份的输入，不靠正文相同去重。"""
    now = datetime.now(UTC)
    result = await store.admit(
        InputSubmission(
            InputSource("test", "account", "conversation", "text", event),
            "实际输入 " + event,
            now,
            now,
            context=InputContext(minimum_wake=True),
        )
    )
    return result.input_id


async def notices(store: Store, source: str, count: int = 1) -> tuple[str, ...]:
    """按真实呈现资格提交有据监管，调用本身不等于前台已经回应。"""
    page = await store.knowledge.read_evidence(source)
    assert page.slice is not None
    batch = await store.knowledge.begin_reflection("batch:" + source, (page.slice.slice_ref,))
    await store.knowledge.mark_presented(batch.batch_id, batch.version, (page.slice.slice_ref,))
    ids: list[str] = []
    for ordinal in range(count):
        identity = f"notice:{source}:{ordinal}"
        await store.knowledge.supervise(
            batch.batch_id,
            batch.version,
            SupervisionNotice(
                id=identity,
                issue="尚需核对的事项",
                suggestion=f"明确意见 {ordinal}",
                evidence=(page.slice.slice_ref,),
            ),
        )
        ids.append(identity)
    return tuple(ids)


def object_(value: JsonValue) -> JsonObject:
    """断言测试取得真实对象，不用宽泛类型掩盖投影形状。"""
    assert isinstance(value, dict)
    return value


def array(value: JsonValue) -> list[JsonValue]:
    """断言模型实际 JSON 中的列表结构。"""
    assert isinstance(value, list)
    return value


class Backend:
    """只交回内部完整响应，实际材料编译与工具循环仍由生产入口执行。"""

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def generate(
        self,
        request: ModelRequest,
        *,
        model: str,
        emit: ModelEventSink | None = None,
    ) -> ModelResponse:
        """记录请求并正常结束，不访问服务或执行对外行动。"""
        self.requests.append(request)
        return ModelResponse(Message.assistant("内部结束"))


def assembly(store: Store, directory: Path, backend: Backend) -> SubjectAssembly:
    """使用实际 app 装配路径，但不启动注意/反思接入工作器。"""
    config = EngineConfig.validate(
        {
            "host": {"data_dir": directory},
            "models": {
                "main": {
                    "kind": "generation",
                    "model": "openai/offline",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "context_window": 300_000,
                    "max_output_tokens": 1000,
                    "capacity_rule": "reference-text-only-v1",
                }
            },
            "subject": {"foreground": "main", "reflection": "main", "summary": "main"},
        }
    )

    async def execute(
        source: str,
        *,
        input_id: str | None,
        can_start: Callable[[], bool],
        presented_call_ids: frozenset[str],
    ) -> ActionRecord:
        raise AssertionError("本场景不应派发世界行动。")

    catalog = prepare_plugins((), base_dir=directory, factories={}).catalog
    return build_subject(
        config,
        store,
        catalog,
        {"main": backend},
        {},
        execute=execute,
        plugins={},
        can_start=lambda: None,
        rules={},
        wakeup_committed=lambda: None,
        knowledge=KnowledgeService(store.knowledge),
    )


async def run(assembled: SubjectAssembly, store: Store, identity: str, turn: str) -> None:
    """进入实际 Core 连续会话，每个请求使用 app 的完整当前状态刷新。"""
    coordinator = assembled.coordinator
    await coordinator.session.run(
        Message.user("本轮实际输入", metadata={"evidence": (identity,)}),
        turn_id=turn,
        input_ids=(identity,),
        backend=coordinator.subject.backend,
        evidence=store,
        model=coordinator.subject.model,
        instructions=coordinator.instructions,
        tools=ToolSet(),
        output_tokens=coordinator.subject.output_tokens,
        max_requests=8,
        turn_seconds=600,
        retry=coordinator.subject.retry,
        diagnostic_bytes=1024,
        refresh=coordinator.refresh,
        capacity=coordinator.subject.capacity,
        prepare_history=assembled.foreground_history,
    )


def work(request: ModelRequest) -> JsonObject:
    """从实际 SDK 编译的最后 user 状态材料读出宿主投影。"""
    material, _ = compile_materials(request, model="openai/offline")
    payload = object_(decode_object(material.encode())["payload"])
    message = object_(array(payload["messages"])[-1])
    text = message["content"]
    assert isinstance(text, str)
    state = decode_object(text.encode())
    return object_(state["work"])


async def test_actual_request_contains_all_open_notices_and_plans_then_refreshes_state(
    store: Store, tmp_path: Path
) -> None:
    source = await admit(store, "source")
    ids = await notices(store, source, 35)
    for ordinal in range(35):
        await store.knowledge.plan_wakeup(
            WakeupPlan(
                id=f"plan:{ordinal}",
                due=time.time() + 3600,
                reason=f"尚未完成 {ordinal}",
                evidence=source,
            )
        )
    backend = Backend()
    assembled = assembly(store, tmp_path, backend)
    try:
        first = await admit(store, "first")
        await run(assembled, store, first, "turn:first")
        initial = work(backend.requests[0])
        assert len(array(initial["supervision"])) == 35
        assert len(array(initial["plans"])) == 35
        assert initial["foreground_session_version"] == 0
        assert isinstance(initial["assembled_at"], float)
        await store.knowledge.respond_supervision(ids[-1], "reject", "已核对真实来源")
        await store.knowledge.resolve_plan("plan:34", "事项已处理", source)
        second = await admit(store, "second")
        await run(assembled, store, second, "turn:second")
        refreshed = work(backend.requests[1])
        assert len(array(refreshed["supervision"])) == 34
        assert len(array(refreshed["plans"])) == 34
        assert refreshed["foreground_session_version"] == 1
        assert all(
            object_(object_(notice)["notice"])["id"] != ids[-1]
            for notice in array(refreshed["supervision"])
        )
        attention = await assembled.coordinator.attention_context()
        assert len(array(attention["plans"])) == 32
        assert "supervision" not in attention and "calls" not in attention
        assert "first_32" in str(attention["coverage"])
    finally:
        await assembled.coordinator.close()
        await assembled.background.close()
        await assembled.foreground_history.close()
        await assembled.reflection_history.close()


async def test_unanswered_supervision_protects_source_and_delivered_input_until_response(
    store: Store,
) -> None:
    source = await admit(store, "source")
    identity = (await notices(store, source))[0]
    delivery = await store.feedback.bridge(now=time.time())
    assert len(delivery.foreground_ids) == 1
    derived = delivery.foreground_ids[0]
    assert await store.protected_input_ids((source, derived, "unrelated")) == {source, derived}
    await store.knowledge.respond_supervision(identity, "accept", "按实际来源处理")
    assert not await store.protected_input_ids((source, derived))
    assert array((await store.subject_work())["supervision"]) == []


async def test_revoked_source_retires_current_supervision_without_deleting_evidence(
    store: Store,
) -> None:
    source = await admit(store, "source")
    await notices(store, source)
    assert len(array((await store.subject_work())["supervision"])) == 1
    await store.knowledge.revoke_evidence(source, 1)
    assert array((await store.subject_work())["supervision"]) == []
    assert not await store.protected_input_ids((source,))
    notice = await store.knowledge.pending_supervision()
    assert notice == ()
    snapshot = await store.knowledge.get_evidence(source, version=1, include_inactive=True)
    assert snapshot is not None and not snapshot.valid


@pytest.mark.parametrize("invalidated_by", ["expiry", "activity_version"])
async def test_current_supervision_rechecks_time_and_exact_activity_version(
    store: Store, monkeypatch: pytest.MonkeyPatch, invalidated_by: str
) -> None:
    source = await admit(store, "source")
    page = await store.knowledge.read_evidence(source)
    assert page.slice is not None
    batch = await store.knowledge.begin_reflection("batch", (page.slice.slice_ref,))
    await store.knowledge.mark_presented(batch.batch_id, batch.version, (page.slice.slice_ref,))
    now = time.time()
    activity = ActivityRecord(
        activity_id="activity",
        definition="local",
        goal="实际目标",
        state="paused",
        version=1,
        run_generation=0,
        granted_seconds=3600,
        used_seconds=0,
        checkpoint_at=None,
        clock_running=False,
        timing_incomplete=False,
        needs_reconciliation=False,
        last_command_id="command",
        work_json="{}",
        progress_json=None,
        created_at=now,
        updated_at=now,
    )
    await store.commit_activity(activity, expected_version=None)
    await store.knowledge.supervise(
        batch.batch_id,
        batch.version,
        SupervisionNotice(
            id="notice",
            issue="版本适用的意见",
            suggestion="核对当前事实",
            evidence=(page.slice.slice_ref,),
            activity_id=activity.activity_id,
            activity_version=activity.version,
            expires_at=now + 3600,
        ),
    )
    assert len(array((await store.subject_work())["supervision"])) == 1
    assert await store.protected_input_ids((source,)) == {source}
    if invalidated_by == "expiry":
        monkeypatch.setattr(knowledge_sql.time, "time", lambda: now + 7200)
    else:
        await store.commit_activity(
            replace(activity, version=2, updated_at=now + 1), expected_version=1
        )
    assert array((await store.subject_work())["supervision"]) == []
    assert not await store.protected_input_ids((source,))


async def test_open_supervision_protects_source_older_than_recent_turns_during_real_maintenance(
    store: Store, tmp_path: Path
) -> None:
    messages: list[Message] = []
    boundaries: list[int] = []
    sources: list[str] = []
    for ordinal in range(5):
        source = await admit(store, f"old:{ordinal}")
        sources.append(source)
        messages.extend(
            (
                Message.user("原始完整正文" + "x" * 50_000, metadata={"evidence": (source,)}),
                Message.assistant("已完成内部轮次"),
            )
        )
        boundaries.append(len(messages))
        checkpoint = ConversationCheckpoint(
            ConversationState(
                tuple(messages), turn_index=ordinal + 1, turn_boundaries=tuple(boundaries)
            ),
            ReminderState(),
        )
        turn = await store.begin_turn(
            f"old-turn:{ordinal}", "foreground", ordinal, ordinal + 1, (source,)
        )
        await store.finish_turn(turn, "succeeded", checkpoint_json=encode_checkpoint(checkpoint))
    await notices(store, sources[0])
    assert await store.protected_input_ids(tuple(sources)) == {sources[0]}
    backend = Backend()
    assembled = assembly(store, tmp_path, backend)
    try:
        current = await admit(store, "current")
        await run(assembled, store, current, "turn:current")
        # 若第一轮失去未决来源保护，达到80%时会额外派发摘要请求。这里只
        # 派发一次前台，完整来源留在实际历史中，未为满足50%目标改写它。
        assert len(backend.requests) == 1
        assert "x" * 50_000 in backend.requests[0].messages[0].text
        assert work(backend.requests[0])["foreground_session_version"] == 5
    finally:
        await assembled.coordinator.close()
        await assembled.background.close()
        await assembled.foreground_history.close()
        await assembled.reflection_history.close()


async def call(store: Store, source: str, identity: str, state: str = "confirmed") -> None:
    """保存实际启动门槛与结果，未知事实可以在以后被新回执澄清。"""
    await store.begin_action(
        "action:" + identity, catalog_version="catalog", source="return 1", input_id=source
    )
    await store.begin_call(
        identity,
        action_id="action:" + identity,
        sequence=0,
        path="me.phone.qq.send",
        target_id="qq",
        arguments_json='{"chat":"private","target_id":42,"text":"实际参数"}',
    )
    await store.mark_call_started(identity)
    assert state in ("confirmed", "unknown")
    await store.record_call_result(
        identity,
        state="confirmed" if state == "confirmed" else "unknown",
        result_json='{"message_id":123,"platform_report":"ok"}' if state == "confirmed" else None,
        error_type="Uncertain" if state == "unknown" else None,
        error="断线后未知" if state == "unknown" else None,
    )
    await store.finish_action(
        "action:" + identity, state="completed", result_json=None, output="", error=None
    )


@pytest.mark.parametrize("source_kind", ["input", "call_receipt", "action_result"])
async def test_confirmed_effect_stays_current_while_real_linked_plan_is_pending(
    store: Store, source_kind: str
) -> None:
    source = await admit(store, "source")
    await call(store, source, "call")
    evidence = source
    if source_kind == "call_receipt":
        await store.feedback.bridge(now=time.time())
        evidence = "call:call:1"
    elif source_kind == "action_result":
        action = await store.get_action("action:call")
        assert action is not None and action.finished_at is not None
        evidence = "actual-immediate-result"
        await store.knowledge.put_evidence(
            EvidenceSnapshot(
                evidence_id=evidence,
                version=1,
                kind="action_result",
                text=encode(action),
                source_json=encode(
                    {"source": "actual_action_result", "action_id": action.action_id}
                ),
                occurred_at=action.finished_at,
            ),
            needs_reflection=False,
        )
    await store.knowledge.plan_wakeup(
        WakeupPlan(id="plan", due=time.time() + 3600, reason="效果尚需后续处理", evidence=evidence)
    )
    assert await store.protected_input_ids((source,)) == {source}
    current = object_(array((await store.subject_work())["calls"])[0])
    assert current["state"] == "confirmed" and current["receipt_revision"] == 1
    assert current["result_json"] == '{"message_id":123,"platform_report":"ok"}'
    assert current["arguments_json"] == '{"chat":"private","target_id":42,"text":"实际参数"}'
    assert current["original_ref"] == (
        None if source_kind == "input" else {"id": evidence, "version": 1}
    )
    await store.knowledge.resolve_plan("plan", "明确结束后续处理", evidence)
    assert not await store.protected_input_ids((source,))
    assert array((await store.subject_work())["calls"]) == []
    assert (await store.get_action("action:call")) is not None


async def test_unknown_effect_is_not_retired_by_action_end_and_late_receipt_is_visible(
    store: Store,
) -> None:
    source = await admit(store, "source")
    await call(store, source, "call", "unknown")
    assert object_(array((await store.subject_work())["calls"])[0])["original_ref"] is None
    action = await store.get_action("action:call")
    assert action is not None and action.finished_at is not None
    await store.knowledge.put_evidence(
        EvidenceSnapshot(
            evidence_id="immediate",
            version=1,
            kind="action_result",
            text=encode(action),
            source_json=encode({"source": "actual_action_result", "action_id": action.action_id}),
            occurred_at=action.finished_at,
        ),
        needs_reflection=False,
    )
    await store.knowledge.plan_wakeup(
        WakeupPlan(id="plan", due=time.time() + 3600, reason="核对未知", evidence="immediate")
    )
    first = object_(array((await store.subject_work())["calls"])[0])
    assert first["state"] == "unknown"
    assert first["original_ref"] == {"id": "immediate", "version": 1}
    await store.record_call_result(
        "call", state="confirmed", result_json='{"message_id":999}', error_type=None, error=None
    )
    latest = object_(array((await store.subject_work())["calls"])[0])
    assert latest["state"] == "confirmed" and latest["receipt_revision"] == 2
    assert latest["original_ref"] is None
    await store.feedback.bridge(now=time.time())
    bridged = object_(array((await store.subject_work())["calls"])[0])
    assert bridged["original_ref"] == {"id": "call:call:2", "version": 1}
    assert bridged["result_json"] == '{"message_id":999}'
    assert await store.protected_input_ids((source,)) == {source}
    await store.knowledge.resolve_plan("plan", "已核对最新回执", "immediate")
    assert array((await store.subject_work())["calls"]) == []


async def test_original_ref_rejects_action_snapshot_with_different_actual_values(
    store: Store,
) -> None:
    source = await admit(store, "source")
    await call(store, source, "call", "unknown")
    action = await store.get_action("action:call")
    assert action is not None and action.finished_at is not None
    await store.knowledge.put_evidence(
        EvidenceSnapshot(
            evidence_id="false-reference",
            version=1,
            kind="action_result",
            text=encode(action).replace("实际参数", "被改写参数"),
            source_json=encode({"source": "actual_action_result", "action_id": action.action_id}),
            occurred_at=action.finished_at,
        ),
        needs_reflection=False,
    )
    with pytest.raises(StorageError, match="冻结实际结果"):
        await store.subject_work()
    assert await store.get_action("action:call") == action


async def test_declared_call_receipt_without_reliable_host_registration_is_not_original_ref(
    store: Store,
) -> None:
    source = await admit(store, "source")
    await call(store, source, "call", "unknown")
    await store.knowledge.put_evidence(
        EvidenceSnapshot(
            evidence_id="unregistered-receipt",
            version=1,
            kind="call_receipt",
            text="外部材料不是宿主回执",
            source_json=encode(
                {"call_id": "call", "action_id": "action:call", "receipt_revision": 1}
            ),
            occurred_at=time.time(),
        ),
        needs_reflection=False,
    )
    current = object_(array((await store.subject_work())["calls"])[0])
    assert current["original_ref"] is None and current["state"] == "unknown"


async def test_full_current_state_exceeds_actual_request_capacity_without_silent_omission(
    store: Store, tmp_path: Path
) -> None:
    source = await admit(store, "source")
    for ordinal in range(35):
        await store.knowledge.plan_wakeup(
            WakeupPlan(
                id=f"plan:{ordinal}",
                due=time.time() + 3600,
                reason="必须保留的承诺" + "x" * 10_000,
                evidence=source,
            )
        )
    backend = Backend()
    assembled = assembly(store, tmp_path, backend)
    try:
        identity = await admit(store, "current")
        with pytest.raises(CapacityError) as caught:
            await run(assembled, store, identity, "turn:oversized")
        assert caught.value.code == "over_capacity"
        assert not backend.requests
        assert len(array((await store.subject_work())["plans"])) == 35
        processing = await store.input_processing(identity)
        assert processing is not None and processing.stage == "failed"
    finally:
        await assembled.coordinator.close()
        await assembled.background.close()
        await assembled.foreground_history.close()
        await assembled.reflection_history.close()
