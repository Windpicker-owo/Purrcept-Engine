"""拥有一个手机连接、可靠聊天交接与显式操作会话。

聊天接入进入已有 InputPort（支持纯文本及已接管的手机图片、贴纸与语音多模态块），
模型发言通过持久事件日志投递（支持文字、Bot 共享贴纸、内置贴纸、语音及引用回复）。
Bot 头像通过持久版本同步，用户手机收藏不由本模块管理。网络在线与操作
资格分开；重连不会恢复操控。命令断线返回未知，禁止自动再次派发。
传输实现通过 Peer 借用发送入口，不取得主脑或 Worker 对象。
"""

from __future__ import annotations

import array
import asyncio
import base64
import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from purrcept_core.models import AudioBlock, AudioBytes, ImageBlock, ImageBytes, TextBlock

from purrcept_engine.domain.phone.contracts import PhoneStore
from purrcept_engine.domain.stickers import StickerError, StickerLibrary
from purrcept_engine.errors import HostStateError, StorageError
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
from purrcept_engine.values import JsonObject, JsonValue, encode_json


def _wav_waveform(wav_bytes: bytes, bars: int = 24) -> list[int]:
    """从 16kHz 16-bit 单声道 WAV 数据提取归一化波形高度（8..96），供移动端语音气泡展示。"""
    pcm = wav_bytes[44:] if len(wav_bytes) > 44 else b""
    if len(pcm) < 4:
        return [24] * bars
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    total = len(samples)
    step = max(1, total // bars)
    out: list[int] = []
    for idx in range(bars):
        chunk = samples[idx * step : min(total, (idx + 1) * step)]
        if not chunk:
            out.append(18)
            continue
        rms = math.sqrt(sum(int(s) * int(s) for s in chunk) / len(chunk))
        norm = min(96, max(12, round((rms / 9000.0) * 88 + 12)))
        out.append(norm)
    return out


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
        stickers: StickerLibrary | None = None,
    ) -> None:
        """保存有限端口；构造不打开网络或采集手机。"""
        self.instance_id, self.inputs, self.store = instance_id, inputs, store
        self._late_results = late_results
        self._evidence = evidence
        self._subject_name = None if subject_name is None else subject_name.strip()[:120]
        self._host_snapshot = host_snapshot
        self.stickers = stickers
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
            "avatar": await self.store.avatar(),
            "host_phase": host.get("phase", "unknown"),
        }

    async def deliver(self, peer: Peer, after: int) -> None:
        """补送聊天并发布当前状态，寿命由当前传输连接拥有。"""
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

        blocks: list[TextBlock | ImageBlock | AudioBlock] = [TextBlock(message.text)]
        resolved_media_type = message.media_type
        if message.media_id is not None:
            asset = await self.store.media(device_id, message.media_id)
            if asset is None:
                raise StorageError("手机消息引用的媒体不存在。", code="media_missing")
            resolved_media_type = asset.media_type
            if asset.media_type in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                alt = "手机贴纸" if message.kind == "sticker" else "手机图片"
                blocks.append(ImageBlock(ImageBytes(asset.content, asset.media_type), alt_text=alt))
            elif asset.media_type.startswith("audio/"):
                blocks.append(
                    AudioBlock(AudioBytes(asset.content, asset.media_type), alt_text="手机语音消息")
                )

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
        ).with_content("me.phone.chat", tuple(blocks))
        admitted = await self.inputs.submit(submission)
        event: JsonObject = {
            "type": "chat.message",
            "message_id": message.message_id,
            "direction": "incoming",
            "text": message.text,
            "occurred_at": datetime.fromisoformat(message.occurred_at).astimezone(UTC).isoformat(),
            "input_id": admitted.input_id,
        }
        if message.kind is not None:
            event["kind"] = message.kind
        if message.sticker_id is not None:
            event["sticker_id"] = message.sticker_id
        if message.media_id is not None:
            event["media_id"] = message.media_id
        if resolved_media_type is not None:
            event["media_type"] = resolved_media_type
        if message.duration_ms is not None:
            event["duration_ms"] = message.duration_ms
        if message.waveform is not None:
            event["waveform"] = list(message.waveform)
        if message.reply_to_id is not None:
            event["reply_to_id"] = message.reply_to_id

        await self.store.append(device_id, "in:" + message.message_id, event)
        self.changed.set()
        return {
            "message_id": message.message_id,
            "input_id": admitted.input_id,
            "duplicate": admitted.duplicate,
        }

    async def send_chat(
        self,
        text: str,
        context: CallContext,
        *,
        sticker: str | None = None,
        voice_base64: str | None = None,
        reply_to: str | None = None,
        sticker_id: str | None = None,
    ) -> CallOutcome:
        """以真实 call_id 持久接管对外消息（支持文字、内置陪伴贴纸、语音与引用回复）。"""
        from purrcept_engine.plugins.phone.transport import normalize_phone_voice

        device = await self.store.device()
        device_id = device.get("device_id")
        if not isinstance(device_id, str):
            return CallOutcome("failed", {"error": "not_paired"})

        payload: JsonObject = {
            "type": "chat.message",
            "message_id": context.call_id,
            "direction": "outgoing",
            "text": text,
        }
        if reply_to is not None:
            payload["reply_to_id"] = reply_to
        if sticker_id is not None:
            if self.stickers is None:
                return CallOutcome("failed", {"error": "sticker_library_unavailable"})
            try:
                record, asset = await self.stickers.read(sticker_id=sticker_id)
            except StickerError as error:
                return CallOutcome(
                    "failed", {"error": "sticker_missing"}, type(error).__name__, str(error)
                )
            media_id = await self.store.put_media(device_id, asset.content, asset.media_type)
            payload.update(
                kind="sticker",
                sticker_id=record.sticker_id,
                media_id=media_id,
                media_type=asset.media_type,
            )
            if not text:
                payload["text"] = "[贴纸：" + record.tag + "]"
        elif voice_base64 is not None:
            try:
                raw_audio = base64.b64decode(voice_base64, validate=True)
                wav_bytes, duration_ms = normalize_phone_voice(raw_audio)
            except (ValueError, Exception):
                return CallOutcome("failed", {"error": "invalid_voice_audio"})
            media_id = await self.store.put_media(device_id, wav_bytes, "audio/wav")
            payload["kind"] = "voice"
            payload["media_id"] = media_id
            payload["media_type"] = "audio/wav"
            payload["duration_ms"] = duration_ms
            # 波形计算保留整数约束；写入通用事件时再构造 JSON 数组。
            waveform: list[JsonValue] = [height for height in _wav_waveform(wav_bytes)]
            payload["waveform"] = waveform
        elif sticker is not None:
            payload["kind"] = "sticker"
            payload["sticker_id"] = sticker

        event = await self.store.append(device_id, "out:" + context.call_id, payload)
        self.changed.set()
        return CallOutcome(
            "confirmed",
            {"message_id": context.call_id, "seq": event["seq"], "delivery_state": "queued"},
        )

    async def set_avatar(self, media_id: str | None) -> CallOutcome:
        """接管 Bot 头像设置；手机离线时保存，联网后的状态同步交付最新版本。"""
        try:
            avatar = await self.store.set_avatar(media_id)
        except StorageError as error:
            if error.code != "phone_invalid_avatar":
                raise
            return CallOutcome("failed", {"error": error.code, "message": str(error)})
        self.changed.set()
        return CallOutcome("confirmed", {"avatar": avatar, "delivery_state": "queued"})

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
