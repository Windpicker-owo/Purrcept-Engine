"""以真实临时库验证手机的名字、输入处置和行动阶段；不发起模型或设备调用。"""

import asyncio
from pathlib import Path

import pytest
from purrcept_core.models import ConversationCheckpoint, ConversationState, Message, ReminderState

from purrcept_engine.domain.context.checkpoints import encode_checkpoint
from purrcept_engine.domain.model_requests.models import RequestEvidence, RequestScope
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.plugin_system import CallContext
from purrcept_engine.plugins.phone.protocol import ChatSubmission
from purrcept_engine.plugins.phone.runtime import PhoneRuntime
from purrcept_engine.values import JsonObject, encode_json
from tests.test_phone import Inputs
from tests.test_subject_storage import submission


@pytest.mark.parametrize("reply", ["none", "same", "other_device", "other_plugin", "unrelated"])
async def test_completed_batch_reply_fact_uses_actual_phone_events(
    tmp_path: Path, reply: str
) -> None:
    """只有实际接管到本手机的因果回复才计入；普通工具、其他输入和其他设备不冒充。"""
    store = await Store.open(tmp_path / "reply.sqlite3")
    try:
        ledger = store.phone("phone")
        await ledger.pair("device", "Phone", "synthetic")
        runtime = PhoneRuntime("phone", Inputs(store), ledger)
        inputs: list[str] = []
        for native_id in ("one", "two"):
            admitted = await runtime.submit_chat(
                "device",
                ChatSubmission(
                    message_id=native_id,
                    text="同批输入",
                    occurred_at="2026-11-17T00:00:00Z",
                ),
            )
            inputs.append(str(admitted["input_id"]))
        turn = await store.begin_turn("turn", "foreground", 0, 1, tuple(inputs))
        in_flight = (await ledger.progress("device"))["messages"]
        assert isinstance(in_flight, list)
        assert all(isinstance(item, dict) and "replied" not in item for item in in_flight)

        anchor = inputs[0]
        if reply == "unrelated":
            anchor = (await store.admit(submission("unrelated", wake=True))).input_id
        await store.begin_action(
            "action", catalog_version="catalog", source="private", input_id=anchor
        )
        await store.begin_call(
            "call",
            action_id="action",
            sequence=1,
            path="me.phone.screen" if reply == "none" else "me.phone.chat.send",
            target_id="phone",
            arguments_json="{}",
        )
        await store.mark_call_started("call")
        if reply != "none":
            sender = runtime
            if reply == "other_plugin":
                other = store.phone("other")
                await other.pair("device", "Other", "synthetic-other")
                sender = PhoneRuntime("other", Inputs(store), other)
            elif reply == "other_device":
                await ledger.revoke()
                await ledger.pair("other-device", "Other phone", "synthetic-other")
            outcome = await sender.send_chat(
                "实际回复", CallContext("call", "action", 999999999, anchor)
            )
            assert outcome.state == "confirmed"
        await store.record_call_result(
            "call", state="confirmed", result_json="{}", error_type=None, error=None
        )
        await store.finish_action(
            "action", state="completed", result_json=None, output="", error=None
        )
        checkpoint = ConversationCheckpoint(
            ConversationState(
                (Message.user("输入"), Message.assistant("内部结果")),
                turn_index=1,
                turn_boundaries=(2,),
            ),
            ReminderState(),
        )
        await store.finish_turn(turn, "succeeded", checkpoint_json=encode_checkpoint(checkpoint))
        completed = (await ledger.progress("device"))["messages"]
        assert isinstance(completed, list) and len(completed) == 2
        for item in completed:
            assert isinstance(item, dict)
            assert item["stage"] == "processed"
            assert item["replied"] is (reply == "same")
    finally:
        await store.close()


async def test_phone_progress_follows_committed_causal_stages_without_private_content(
    tmp_path: Path,
) -> None:
    """接管、前台、请求、行动分别需要真实记录，不能从上一步提前点亮下一步。"""
    store = await Store.open(tmp_path / "experience.sqlite3")
    try:
        ledger = store.phone("phone")
        runtime = PhoneRuntime(
            "phone",
            Inputs(store),
            ledger,
            subject_name="  小白  ",
            host_snapshot=lambda: {"phase": "running", "private": "not-for-phone"},
        )
        admitted = await runtime.submit_chat(
            "device",
            ChatSubmission(
                message_id="m1",
                text="private-message",
                occurred_at="2026-11-16T00:00:00Z",
            ),
        )
        input_id = str(admitted["input_id"])
        initial = await runtime.experience("device")
        assert initial["subject_name"] == "小白"
        assert initial["activity"] == {"phase": "idle"}
        state = await store.input_processing(input_id)
        assert state is not None
        assert await store.submit_inputs((state,), minimum_wake=True)
        records = await store.take_foreground_inputs()
        turn = await store.begin_turn(
            "turn", "foreground", 0, 1, tuple(r.input_id for r in records)
        )
        processing = await runtime.experience("device")
        assert isinstance(processing["activity"], dict)
        assert processing["activity"]["phase"] == "processing"

        await store.begin_request(
            RequestEvidence(
                "request",
                RequestScope("foreground", "turn", (input_id,)),
                "generation",
                "synthetic",
                '{"payload":{"private_model_material":"must-not-leak"}}',
                "frozen",
                1,
                10,
                10,
            )
        )
        thinking = await runtime.experience("device")
        assert isinstance(thinking["activity"], dict)
        assert thinking["activity"]["phase"] == "thinking"
        await store.finish_request("request", "succeeded")
        await store.begin_action(
            "action", catalog_version="catalog", source="private-code", input_id=input_id
        )
        await store.begin_call(
            "call",
            action_id="action",
            sequence=1,
            path="me.phone.screen",
            target_id="phone",
            arguments_json='{"private":"argument"}',
        )
        await store.mark_call_started("call")
        acting = await runtime.experience("device")
        assert isinstance(acting["activity"], dict)
        assert acting["activity"]["phase"] == "acting"
        assert acting["activity"]["call_path"] == "me.phone.screen"
        messages = acting["messages"]
        assert isinstance(messages, list) and len(messages) == 1
        assert isinstance(messages[0], dict)
        assert messages[0]["turn_id"] == "turn"
        assert messages[0]["stage"] == "processing"
        assert messages[0]["last_action"] == {"path": "me.phone.screen", "state": "started"}
        await store.record_call_result(
            "call",
            state="confirmed",
            result_json='{"private":"receipt"}',
            error_type=None,
            error=None,
        )
        after_call = await runtime.experience("device")
        after_messages = after_call["messages"]
        assert isinstance(after_messages, list) and isinstance(after_messages[0], dict)
        assert after_messages[0]["last_action"] == {"path": "me.phone.screen", "state": "confirmed"}
        assert "receipt" not in encode_json(after_call).decode()
        encoded = encode_json(acting).decode()
        for private in (
            "private-message",
            "private-code",
            "argument",
            "must-not-leak",
            "not-for-phone",
        ):
            assert private not in encoded
        assert (await runtime.experience("other-device"))["messages"] == []
        assert (await store.phone("other-plugin").progress("device"))["messages"] == []
        await store.finish_turn(turn, "cancelled", error_json='{"error_type":"synthetic"}')
        cancelled = await runtime.experience("device")
        assert cancelled["activity"] == {"phase": "idle"}
        cancelled_messages = cancelled["messages"]
        assert isinstance(cancelled_messages, list)
        assert all(isinstance(item, dict) and "replied" not in item for item in cancelled_messages)
    finally:
        await store.close()


async def test_state_push_is_ephemeral_and_changed_only(tmp_path: Path) -> None:
    """空闲轮询不会不断重发同一状态，聊天序号也不被 UI 状态占用。"""
    store = await Store.open(tmp_path / "delivery.sqlite3")
    runtime = PhoneRuntime("phone", Inputs(store), store.phone("phone"), subject_name="小白")
    sent = asyncio.Queue[tuple[str, object]]()

    class Peer:
        device_id = "device"
        connection_id = "connection"

        async def send(self, kind: str, payload: JsonObject, reply_to: str | None = None) -> None:
            await sent.put((kind, payload))

    task = asyncio.create_task(runtime.deliver(Peer(), 0))
    try:
        kind, _ = await asyncio.wait_for(sent.get(), 2)
        assert kind == "engine.state"
        runtime.changed.set()
        await asyncio.sleep(0.03)
        assert sent.empty()
        assert await runtime.store.events("device", 0) == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await store.close()
