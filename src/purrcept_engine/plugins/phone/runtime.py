"""拥有一个手机连接、可靠聊天交接与显式操作会话。

聊天接入进入已有 InputPort，模型发言通过持久事件日志投递。网络在线与操作
资格分开；重连不会恢复操控。命令断线返回未知，禁止自动再次派发。
传输实现通过 Peer 借用发送入口，不取得主脑或 Worker 对象。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from purrcept_core.models import TextBlock

from purrcept_engine.domain.phone.contracts import PhoneStore
from purrcept_engine.errors import HostStateError
from purrcept_engine.plugin_system import (
    CallContext,
    CallOutcome,
    EvidencePort,
    EvidenceSnapshot,
    InputContext,
    InputPort,
    InputSource,
    InputSubmission,
    LateResultPort,
    MediaReference,
)
from purrcept_engine.plugins.phone.protocol import ChatSubmission
from purrcept_engine.values import JsonObject, encode_json


class Peer(Protocol):
    """当前认证连接的发送端，由网络入口创建，Runtime 只在所属循环借用。"""

    device_id: str
    connection_id: str

    async def send(self, kind: str, payload: JsonObject, reply_to: str | None = None) -> None:
        """串行发送一帧，网络失败原样传播，不在内部重发命令。"""
        ...


class PhoneRuntime:
    """插件独占的消息和设备状态，网络及能力调用都在宿主循环。"""

    def __init__(
        self,
        instance_id: str,
        inputs: InputPort,
        store: PhoneStore,
        late_results: LateResultPort | None = None,
        evidence: EvidencePort | None = None,
        *,
        subject_name: str | None = None,
        host_snapshot: Callable[[], JsonObject] | None = None,
    ) -> None:
        """保存有限端口；构造不打开网络或采集手机。"""
        self.instance_id, self.inputs, self.store = instance_id, inputs, store
        self._late_results = late_results
        self._evidence = evidence
        self._subject_name = None if subject_name is None else subject_name.strip()[:120]
        self._host_snapshot = host_snapshot
        self.peer: Peer | None = None
        self.control_session: str | None = None
        self.device_state: JsonObject = {}
        self.changed = asyncio.Event()
        self.stopping = False
        self._pending: dict[str, asyncio.Future[JsonObject]] = {}

    def attach(self, peer: Peer) -> None:
        """只允许一条认证连接，避免两个消费者争用操作和事件确认。"""
        if self.stopping or self.peer is not None:
            raise HostStateError("手机连接已占用或宿主正在关闭。")
        self.peer = peer
        self.control_session = None
        self.changed.set()

    def detach(self, peer: Peer) -> None:
        """撤销原连接及操作资格，不能将发送成功推断成手机未执行。"""
        if self.peer is not peer:
            return
        self.peer = None
        self.control_session = None
        self.device_state = {}
        for future in self._pending.values():
            if not future.done():
                future.set_result({"state": "unknown", "error": "connection_lost"})

    def snapshot(self) -> JsonObject:
        """提供不含凭据的动态状态，查询不会连接或采集。"""
        return {
            "online": self.peer is not None,
            "control_connected": self.control_session is not None,
            "device": self.device_state,
        }

    async def experience(self, device_id: str) -> JsonObject:
        """取得手机可公开呈现的名字与真实阶段，不能从已接管推断已思考。"""
        progress = await self.store.progress(device_id)
        host = {} if self._host_snapshot is None else self._host_snapshot()
        return {
            **progress,
            "subject_name": self._subject_name,
            "host_phase": host.get("phase", "unknown"),
        }

    async def deliver(self, peer: Peer, after: int) -> None:
        """补送聊天并发布当前状态，寿命由当前传输连接拥有。

        Args:
            peer: 已认证的唯一连接；发送在同一宿主循环串行化。
            after: 对端已落盘的聊天游标，传输层已核验其范围。

        Note:
            调用方在断线时取消此协程并等待退出；取消与发送失败原样传播。
            只有已提交事实变化才发送 engine.state；连接内 revision 单调增加，
            状态不占聊天序号、不要求确认、不在重连时重放。轮询不能保证捕获
            每个短暂阶段；最近真实调用的摘要由账本保留，界面不能人为延长忙碌。
        """
        previous: JsonObject | None = None
        revision = 0
        while True:
            self.changed.clear()
            events = await self.store.events(peer.device_id, after)
            if events:
                for event in events:
                    await peer.send("sync.event", event)
                    sequence = event["seq"]
                    assert isinstance(sequence, int)
                    after = sequence
            current = await self.experience(peer.device_id)
            if current != previous:
                revision += 1
                await peer.send(
                    "engine.state",
                    {**current, "revision": revision, "observed_at": datetime.now(UTC).timestamp()},
                )
                previous = current
            if events:
                continue
            # 输入、模型和执行账本由原来的所有者提交。手机只轮询已提交的
            # 有限投影，不增加订阅总线或反向驱动主脑。忙碌时提高采样频率，
            # 空闲时每秒最多一次；重复采样不产生网络帧。
            activity = current["activity"]
            assert isinstance(activity, dict)
            interval = 0.25 if activity["phase"] != "idle" else 1.0
            try:
                async with asyncio.timeout(interval):
                    await self.changed.wait()
            except TimeoutError:
                pass

    async def submit_chat(self, device_id: str, message: ChatSubmission) -> JsonObject:
        """先正式接管，再补齐展示索引；重投沿原生身份取得同一次输入。"""
        if self.stopping:
            raise HostStateError("手机插件已停止新输入。")
        submission = InputSubmission(
            InputSource(
                self.instance_id,
                device_id,
                "chat",
                "phone.chat.message",
                message.message_id,
                device_id,
            ),
            message.text,
            datetime.fromisoformat(message.occurred_at),
            datetime.now(UTC),
            (),
            InputContext(minimum_wake=True),
        ).with_content("me.phone.chat", (TextBlock(message.text),))
        admitted = await self.inputs.submit(submission)
        event: JsonObject = {
            "type": "chat.message",
            "message_id": message.message_id,
            "direction": "incoming",
            "text": message.text,
            "occurred_at": datetime.fromisoformat(message.occurred_at).astimezone(UTC).isoformat(),
            "input_id": admitted.input_id,
        }
        await self.store.append(device_id, "in:" + message.message_id, event)
        self.changed.set()
        return {
            "message_id": message.message_id,
            "input_id": admitted.input_id,
            "duplicate": admitted.duplicate,
        }

    async def send_chat(self, text: str, context: CallContext) -> CallOutcome:
        """以真实 call_id 持久接管对外消息，离线时明确返回 queued。"""
        device = await self.store.device()
        device_id = device.get("device_id")
        if not isinstance(device_id, str):
            return CallOutcome("failed", {"error": "not_paired"})
        event = await self.store.append(
            device_id,
            "out:" + context.call_id,
            {
                "type": "chat.message",
                "message_id": context.call_id,
                "direction": "outgoing",
                "text": text,
            },
        )
        self.changed.set()
        return CallOutcome(
            "confirmed",
            {"message_id": context.call_id, "seq": event["seq"], "delivery_state": "queued"},
        )

    async def command(
        self, operation: str, arguments: JsonObject, context: CallContext
    ) -> CallOutcome:
        """单次派发命令并等待真实回执；超时或断线绝不重新执行。"""
        peer = self.peer
        if peer is None or self.stopping:
            return CallOutcome("failed", {"error": "device_offline"})
        if operation != "connect" and self.control_session is None:
            return CallOutcome("failed", {"error": "control_not_connected"})
        remaining = context.deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return CallOutcome("failed", {"error": "deadline_exceeded"})

        session = uuid4().hex if operation == "connect" else self.control_session
        future: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        self._pending[context.call_id] = future
        try:
            intent: JsonObject = {
                "device_id": peer.device_id,
                "operation": operation,
                "arguments": arguments,
                "action_id": context.action_id,
                "input_id": context.input_id,
                "deadline": context.deadline,
            }
            if not await self.store.begin_command(context.call_id, intent):
                return CallOutcome(
                    "unknown", {"command_id": context.call_id, "error": "already_dispatched"}
                )
            if self.peer is not peer or future.done():
                return CallOutcome(
                    "failed",
                    {"command_id": context.call_id, "error": "connection_changed_before_dispatch"},
                )
            # 持久化等待也消耗原调用预算，不能用旧 remaining 延长手机执行期限。
            remaining = context.deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return CallOutcome(
                    "failed", {"command_id": context.call_id, "error": "deadline_exceeded"}
                )
            await peer.send(
                "device.command",
                {
                    "command_id": context.call_id,
                    "control_session_id": session,
                    "operation": operation,
                    "arguments": arguments,
                    "expires_at": datetime.now(UTC).timestamp() + remaining,
                },
            )
            try:
                async with asyncio.timeout(remaining):
                    result = await asyncio.shield(future)
            except TimeoutError:
                return CallOutcome(
                    "unknown", {"command_id": context.call_id, "error": "result_timeout"}
                )
            state = result.get("state")
            if state not in ("confirmed", "failed", "unknown"):
                raise ValueError("手机返回无效操作状态。")
            if (
                operation == "connect"
                and state == "confirmed"
                and self.peer is peer
                and self.device_state.get("control_allowed") is True
            ):
                self.control_session = session
            if operation == "disconnect":
                self.control_session = None
            return await self._outcome(peer, operation, result)
        except ConnectionError:
            # 网络写失败不证明对端没有取得命令；登记后的身份只允许查询。
            return CallOutcome(
                "unknown", {"command_id": context.call_id, "error": "connection_lost"}
            )
        finally:
            self._pending.pop(context.call_id, None)

    async def receive_result(self, peer: Peer, result: JsonObject) -> None:
        """仅当前连接可完成其在途调用；迟到未知调用不产生新派发。"""
        if self.peer is not peer:
            return
        command_id = result.get("command_id")
        if not isinstance(command_id, str):
            raise ValueError("手机回执缺少命令身份。")
        records = await self.store.commands(identity=command_id)
        if not records:
            return
        intent = records[0]["intent"]
        if not isinstance(intent, dict) or intent.get("device_id") != peer.device_id:
            raise ValueError("手机回执设备不匹配。")
        await self.store.complete_command(command_id, result)
        if (
            isinstance(command_id, str)
            and (future := self._pending.get(command_id)) is not None
            and not future.done()
        ):
            future.set_result(result)
        elif self._late_results is not None:
            action_id, input_id, deadline = (
                intent["action_id"],
                intent["input_id"],
                intent["deadline"],
            )
            state = result["state"]
            if (
                not isinstance(action_id, str)
                or not isinstance(deadline, (int, float))
                or state not in ("confirmed", "failed", "unknown")
            ):
                raise ValueError("持久命令身份无效。")
            if input_id is not None and not isinstance(input_id, str):
                raise ValueError("持久输入关联无效。")
            operation = intent.get("operation")
            if not isinstance(operation, str):
                raise ValueError("持久命令缺少操作名称。")
            outcome = await self._outcome(peer, operation, result)
            await self._late_results.record(
                CallContext(command_id, action_id, deadline, input_id), outcome
            )

    async def _outcome(self, peer: Peer, operation: str, result: JsonObject) -> CallOutcome:
        """设备图片先进入证据再交模型；摘要不能代替已经取得的原生图像。"""
        state = result.get("state")
        if state not in ("confirmed", "failed", "unknown"):
            raise ValueError("手机结果状态无效。")
        if operation != "screenshot" or state != "confirmed":
            return CallOutcome(state, result)
        data = result.get("data")
        if not isinstance(data, dict):
            raise ValueError("手机截图缺少实际图片。")
        media_id, captured_at = data.get("media_id"), data.get("captured_at")
        if not isinstance(media_id, str) or not isinstance(captured_at, (int, float)):
            raise ValueError("手机截图缺少媒体身份或采集时间。")
        asset = await self.store.media(peer.device_id, media_id)
        if asset is None or self._evidence is None:
            return CallOutcome("failed", {**result, "error": "screenshot_evidence_unavailable"})
        snapshot = EvidenceSnapshot(
            evidence_id="phone-" + str(result["command_id"]),
            version=1,
            kind="phone.screenshot",
            text="远程实体手机的明确单次截图；不是用户发言。",
            source_json=encode_json({"device_id": peer.device_id}).decode(),
            occurred_at=captured_at,
            media=(MediaReference(media_id, asset.media_type, len(asset.content)),),
        )
        committed = await self._evidence.capture(snapshot, (asset,))
        return CallOutcome(
            "confirmed", {**result, "evidence_ref": committed.evidence_id}, media=committed.media
        )

    async def stop_inputs(self) -> None:
        """停止新业务接管，在途网络回执仍由当前连接接收。"""
        self.stopping = True

    async def history(self, after: int) -> JsonObject:
        """读取当前配对设备的聊天投递页，不修改确认位置。"""
        device = await self.store.device()
        device_id = device.get("device_id")
        events = [] if not isinstance(device_id, str) else await self.store.events(device_id, after)
        return {"events": [event for event in events]}

    def set_device_state(self, peer: Peer, state: JsonObject) -> None:
        """采用当前连接的权限事实，手机撤销操控时同步撤销 Engine 资格。"""
        if self.peer is peer:
            self.device_state = state
            if state.get("control_allowed") is not True:
                self.control_session = None

    def source_details(self) -> str:
        """生成有限诊断字段，不复制认证信息。"""
        return encode_json(self.snapshot()).decode()
