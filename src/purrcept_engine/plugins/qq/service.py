"""连接 QQ 原生事实与宿主输入、证据和迟到回执的唯一业务所有者。

QQClient 只拥有传输，Service 在过滤后补取转发原文并原子提交 InputSubmission。
工具应答先保存完整原响应，再检查 status/retcode 和投影；echo 超时不重发，
真实迟到回包使用原 CallContext 追加版本。历史续读只访问宿主旧快照。
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from purrcept_engine.errors import HostStateError, StorageError
from purrcept_engine.execution.protocol import JsonObject, JsonValue, decode_object, encode_json
from purrcept_engine.execution.providers import CallContext, CallOutcome
from purrcept_engine.storage.knowledge import EvidencePage, EvidenceSnapshot, KnowledgeInputError
from purrcept_engine.storage.models import InputContext, InputSource, InputSubmission

from ..base import EvidencePort, PluginPorts
from .client import QQClient
from .media import MediaBusy, MediaOwner
from .messages import (
    HistoryCache,
    filter_reason,
    forward_ids,
    id_text,
    image_references,
    message_text,
    object_value,
    project_message,
)
from .parameters import Read
from .ports import ConnectionRejected, Connector, QQCallError, Reply
from .settings import QQSettings
from .stickers import StickerLibrary


def platform_data(reply: Reply) -> JsonValue:
    """检查标准 SnowLuma 信封，缺字段和伪整数不能成为成功或缓存降级依据。"""
    response = reply.response
    status, retcode = response.get("status"), response.get("retcode")
    if status not in ("ok", "failed") or type(retcode) is not int or "data" not in response:
        raise ConnectionRejected("QQ 回包不符合已冻结 status/retcode/data 信封。")
    if status != "ok" or retcode != 0:
        message = response.get("message") or response.get("wording") or "平台明确拒绝"
        raise QQCallError(
            str(message),
            action=reply.action,
            phase="rejected",
            generation=reply.generation,
            echo=reply.echo,
            response=response,
        )
    return response["data"]


class QQService:
    """build 创建，Runtime 生命周期持有；不取得 Store 或主脑。

    EvidencePort 与 LateResultPort 是实际启用所必需的有限端口。诊断仅保留来源
    标识与原因，过滤推送的正文/媒体不进入隐藏缓存或主体权威经历。
    """

    def __init__(
        self,
        instance_id: str,
        settings: QQSettings,
        base_dir: Path,
        ports: PluginPorts,
        connector: Connector,
    ) -> None:
        """只建立本地所有权；连接在 start 后发生。"""
        if ports.evidence is None or ports.late_results is None:
            raise RuntimeError("QQ 必须装配宿主原文与迟到回执端口。")
        self.instance_id = instance_id
        self.settings = settings
        self.ports = ports
        self.evidence: EvidencePort = ports.evidence
        self.media = MediaOwner(settings, base_dir)
        self.stickers = StickerLibrary(self.media.sticker_dir)
        self.cache = HistoryCache(settings)
        self.client = QQClient(settings, connector, self.event, self.gap)
        self._account = ""
        self._account_generation = 0
        self._diagnostics: deque[JsonObject] = deque(maxlen=64)
        self._counts: dict[str, int] = {}

    def snapshot(self) -> JsonObject:
        """返回实际接入与有限缺口，不执行隐式登录/版本探测。"""
        return {
            **self.client.snapshot(),
            "account_id": self._account or None,
            "diagnostics": list(self._diagnostics),
            "gap_counts": dict(self._counts),
        }

    def gap(self, payload: JsonObject, reason: str) -> None:
        """未接管诊断不含正文、媒体 URL 或转发内容。"""
        self._counts[reason] = self._counts.get(reason, 0) + 1
        self._diagnostics.append(
            {
                "reason": reason,
                "self_id": id_text(payload.get("self_id")),
                "group_id": id_text(payload.get("group_id")),
                "user_id": id_text(payload.get("user_id")),
                "message_id": id_text(payload.get("message_id")),
                "received_at": time.time(),
            }
        )

    def scope(self, generation: int) -> str:
        """未知新连接账号不能借用旧账号缓存；确认账号后才跨同账号代次查阅。"""
        return (
            self._account
            if self._account and self._account_generation == generation
            else "connection:" + str(generation)
        )

    def confirm_account(self, account: str, generation: int) -> None:
        """实际平台事件/显式登录回应确认账号，不以请求或旧连接状态猜账号。"""
        self._account, self._account_generation = account, generation

    async def event(self, payload: JsonObject, generation: int) -> None:
        """先过滤、后补原文、最后原子接管；图片推送沿旧行为只保存引用。"""
        post_type = payload.get("post_type")
        if post_type not in ("message", "message_sent", "notice"):
            return
        if reason := filter_reason(payload, self.settings):
            self.gap(payload, reason)
            return
        account = id_text(payload.get("self_id"))
        sender = id_text(payload.get("user_id"))
        group = id_text(payload.get("group_id"))
        if not account.isascii() or not account.isdigit() or int(account) <= 0 or not sender:
            self.gap(payload, "invalid_source_identity")
            return
        self._account, self._account_generation = account, generation
        chat = "group" if group else "private"
        target = group or sender
        message = project_message(payload, chat=chat, target_id=target)
        received = datetime.now(UTC)
        native_identity = id_text(payload.get("message_id"))
        previous = (
            self.cache.submission(account, chat, target, native_identity)
            if post_type == "message" and native_identity
            else None
        )
        if (
            previous is not None
            and decode_object(previous.context.details_json.encode()).get("raw_event") == payload
        ):
            # 原生重放借用首次实际信封；接收时刻改变不会重新补取或重新解释本地标签。
            replay = InputSubmission(
                previous.source,
                previous.text,
                previous.occurred_at,
                received,
                previous.media,
                previous.context,
            )
            await self.ports.inputs.submit(replay)
            return
        missing: list[str] = []
        raw_time = payload.get("time")
        if (
            type(raw_time) in (int, float)
            and isinstance(raw_time, (int, float))
            and math.isfinite(raw_time)
        ):
            try:
                occurred = datetime.fromtimestamp(raw_time, UTC)
            except (ValueError, OSError, OverflowError) as error:
                raise ConnectionRejected("QQ 事件时间超出实际时间范围。") from error
        elif raw_time is None:
            occurred = received
            missing.append("平台没有提供发生时间；occurred_at 使用明确接收时间。")
        else:
            raise ConnectionRejected("QQ 事件 time 必须为有限 Unix 秒。")
        if post_type == "notice":
            notice = payload.get("notice_type")
            if notice == "notify" and payload.get("sub_type") == "poke":
                kind, text = "qq.poke.received", "收到戳一戳"
            elif notice == "group_msg_emoji_like":
                if sender == account:
                    return
                kind, text = "qq.reaction.received", "收到消息回应"
            else:
                return
            native_id = None  # notice 的 message_id 是目标消息，不能作为该通知的去重身份。
        else:
            try:
                message = await self.resolve_message(
                    payload, chat=chat, target_id=target, generation=generation
                )
            except MediaBusy:
                self.gap(payload, "media_work_capacity")
                return
            kind, text = "qq.message.received", str(message["text"])
            native_id = id_text(payload.get("message_id")) or None
            if post_type == "message_sent":
                self.cache.remember(account, message)
                return
        details: JsonObject = {
            "plugin": self.instance_id,
            "message": message,
            "raw_event": payload,
            "pixels_acquired": False,
            "media_acquired_count": 0,
            "media_expected_count": len(image_references(payload.get("message"))),
        }
        submission = InputSubmission(
            InputSource(
                self.instance_id,
                account,
                chat + ":" + target,
                kind,
                native_id,
                sender,
                id_text(message.get("sender_name")) or None,
            ),
            text,
            occurred,
            received,
            (),
            InputContext(
                encode_json(details).decode(),
                minimum_wake=message["mentioned_self"] is True,
                missing_materials=tuple(missing),
            ),
        )
        try:
            await self.ports.inputs.submit(submission)
        except StorageError as error:
            if error.code != "input_capacity":
                raise
            self.gap(payload, "host_input_capacity")
            return
        except HostStateError:
            if self.client.snapshot()["state"] not in ("stopping", "closed"):
                raise
            self.gap(payload, "host_ingress_stopped")
            return
        if post_type == "message":
            self.cache.remember(account, message, submission=submission)

    async def resolve_message(
        self,
        payload: JsonObject,
        *,
        chat: str,
        target_id: str,
        generation: int,
        context: CallContext | None = None,
    ) -> JsonObject:
        """有限补取旧转发入口，完整节点仍保留；超界/平台拒绝显式列出缺口。

        原事件先由调用者过滤，本方法不递归过滤被引用发送者。最多 12 次补取、
        嵌套 3 层沿用旧展开边界；请求仅绑定原连接代次，换代不借新账号续取。
        """
        result = project_message(payload, chat=chat, target_id=target_id)
        image_items = result.get("images")
        if isinstance(image_items, list) and image_items:
            for image in image_items:
                if isinstance(image, dict):
                    matched = await self.media.io(lambda image=image: self.stickers.match(image))
                    if matched is not None:
                        image.update(
                            {
                                "kind": "sticker",
                                "sticker_id": matched.sticker_id,
                                "tag": matched.tag,
                            }
                        )
        reply_id = id_text(result.get("reply_message_id"))
        if reply_id:
            quoted = self.cache.find(self.scope(generation), chat, target_id, reply_id)
            result["reply_to_self"] = (
                None
                if quoted is None
                else quoted.get("sender_id") in (self.scope(generation), "self")
            )
        pending = [(identity, 0) for identity in forward_ids(payload.get("message"))]
        seen: set[str] = set()
        forwards: list[JsonValue] = []
        issues: list[JsonValue] = []
        while pending:
            identity, depth = pending.pop(0)
            if identity in seen:
                continue
            if depth >= 3 or len(seen) >= 12:
                issues.append({"id": identity, "reason": "转发展开已达到既定层数/调用边界"})
                continue
            seen.add(identity)
            try:
                reply = await self.client.request(
                    "get_forward_msg",
                    {"id": identity, "message_id": identity},
                    context=context,
                    generation=generation,
                )
                data = platform_data(reply)
            except QQCallError as error:
                issues.append({"id": identity, "phase": error.phase, "reason": str(error)})
                continue
            nodes = object_value(data).get("messages", object_value(data).get("message"))
            if not isinstance(nodes, list):
                raise ConnectionRejected("get_forward_msg 缺少实际节点数组。")
            forwards.append(
                {
                    "id": identity,
                    "nodes": nodes,
                    "node_count": len(nodes),
                    "depth": depth,
                    "raw_response": {
                        key: value for key, value in reply.response.items() if key != "echo"
                    },
                }
            )
            lines: list[str] = []
            for node in nodes:
                if not isinstance(node, dict):
                    issues.append({"id": identity, "reason": "转发节点不是对象"})
                    continue
                body = object_value(node.get("data")) if node.get("type") == "node" else node
                content = body.get("content", body.get("message"))
                sender = object_value(body.get("sender"))
                label = id_text(sender.get("nickname", body.get("nickname", body.get("name"))))
                lines.append((label + ": " if label else "") + message_text(content))
                pending.extend((child, depth + 1) for child in forward_ids(content))
            result["text"] = (
                str(result["text"]) + "\n[合并转发 id=" + identity + "]\n" + "\n".join(lines)
            )
        if forwards or issues:
            result["forward"] = {"nodes": forwards, "issues": issues, "complete": not issues}
        return result

    async def raw_reply(self, reply: Reply) -> JsonObject:
        """完整信封先入快照，拒绝/缺字段也不丢实际平台原文。"""
        snapshot = EvidenceSnapshot(
            evidence_id=self.instance_id + ":qq:rpc:" + reply.echo,
            version=1,
            kind="qq.rpc",
            text=encode_json(reply.response).decode(),
            source_json=encode_json(
                {
                    "plugin": self.instance_id,
                    "action": reply.action,
                    "generation": reply.generation,
                    "echo": reply.echo,
                }
            ).decode(),
            occurred_at=reply.received_at,
        )
        saved = await self.evidence.put_evidence(snapshot)
        return {
            "evidence_ref": saved.evidence_id,
            "version": saved.version,
            "level": "evidence",
            "entry": "read_evidence",
            "arguments": {"id": saved.evidence_id, "level": "evidence"},
        }

    async def reply_outcome(
        self, reply: Reply, project: Callable[[JsonValue], JsonObject]
    ) -> CallOutcome:
        """保存后核验协议；缺少确认字段不是默认成功。"""
        reference = await self.raw_reply(reply)
        try:
            data = platform_data(reply)
        except QQCallError as error:
            return self.error_outcome(error, reference)
        except ConnectionRejected as error:
            return CallOutcome(
                "unknown",
                {
                    "action": reply.action,
                    "phase": "received_invalid_response",
                    "raw_response": reference,
                },
                type(error).__name__,
                str(error),
            )
        value = project(data)
        value.update(
            {
                "action": reply.action,
                "connection_generation": reply.generation,
                "echo": reply.echo,
                "raw_response": reference,
            }
        )
        return CallOutcome("confirmed", value)

    def error_outcome(self, error: QQCallError, reference: JsonObject | None = None) -> CallOutcome:
        """发送阶段决定未开始/未知/拒绝，断线和 timeout 不撤销真实效果。"""
        value: JsonObject = {
            "action": error.action,
            "phase": error.phase,
            "connection_generation": error.generation,
            "echo": error.echo,
            "raw_response": reference,
            "retcode": None if error.response is None else error.response.get("retcode"),
        }
        return CallOutcome(
            "unknown" if error.phase == "unknown" else "failed",
            value,
            type(error).__name__,
            str(error),
        )

    async def rpc(
        self,
        action: str,
        params: JsonObject,
        context: CallContext,
        project: Callable[[JsonValue], JsonObject],
        *,
        temporary: tuple[Path, ...] = (),
    ) -> CallOutcome:
        """一次真实调用与同身份迟到提交；不在迟到查询后继续产生新写入。"""
        late_port = self.ports.late_results
        if late_port is None:
            raise RuntimeError("QQ 迟到回执端口消失。")

        async def late(reply: Reply) -> None:
            try:
                await late_port.record(context, await self.reply_outcome(reply, project))
            finally:
                if temporary:
                    await self.media.io(lambda: self.media.release(temporary))

        try:
            reply = await self.client.request(action, params, context=context, on_late=late)
        except QQCallError as error:
            if error.phase != "unknown" and temporary:
                await self.media.io(lambda: self.media.release(temporary))
            return self.error_outcome(error)
        outcome = await self.reply_outcome(reply, project)
        if temporary:
            await self.media.io(lambda: self.media.release(temporary))
        return outcome

    async def lookup(
        self, action: str, params: JsonObject, context: CallContext
    ) -> tuple[JsonValue, JsonObject]:
        """多阶段写入的前置只读步骤超时后不续写，实际原响应仍保存。"""
        reply = await self.client.request(action, params, context=context)
        reference = await self.raw_reply(reply)
        return platform_data(reply), reference

    async def file_result(
        self, context: CallContext, operation: Callable[[], Awaitable[CallOutcome]]
    ) -> CallOutcome:
        """有限拥有一次真实贴纸写入，取消观察后仍把实际结果交原调用回执。

        observed 门槛让完成与取消竞争保持同一个所有者：首版正常结果直接返回，
        观察已取消才等待宿主首版提交并追加结果；不回滚文件或重新执行操作。
        """
        late_port = self.ports.late_results
        if late_port is None:
            raise RuntimeError("QQ 迟到回执端口消失。")
        future: asyncio.Future[CallOutcome] = asyncio.get_running_loop().create_future()
        observed = asyncio.Event()
        detached = False

        def consume(owner: asyncio.Future[CallOutcome]) -> None:
            if not owner.cancelled():
                owner.exception()

        future.add_done_callback(consume)

        async def actual() -> None:
            try:
                outcome = await operation()
            except BaseException as error:
                future.set_exception(error)
                await observed.wait()
                if detached:
                    raise
                return
            future.set_result(outcome)
            await observed.wait()
            if detached:
                await late_port.record(context, outcome)

        self.client.own_file_result(actual)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            detached = True
            raise
        finally:
            observed.set()

    async def read(self, parameters: Read, context: CallContext) -> CallOutcome:
        """平台首读保存完整消息快照；续读既不连平台也不混用新路由。"""
        if parameters.cursor is not None:
            page = await self.evidence.read_evidence_cursor(
                parameters.cursor, max_chars=self.evidence.page_chars
            )
            return self.history_page(page)
        chat, target = parameters.chat, parameters.target_id
        if chat is None or target is None:
            raise RuntimeError("QQ 首读参数未经真实边界校验。")
        count = min(100, max(1, parameters.count if parameters.count is not None else 20))
        action = "get_group_msg_history" if chat == "group" else "get_friend_msg_history"
        params: JsonObject = {
            "group_id" if chat == "group" else "user_id": int(target),
            "count": count,
        }
        source = "platform"
        failure: JsonObject | None = None
        raw_reference: JsonObject | None = None
        generation = self.client.generation
        try:
            data, raw_reference = await self.lookup(action, params, context)
            items = data if isinstance(data, list) else object_value(data).get("messages")
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ConnectionRejected("QQ 历史响应没有完整 messages 对象数组。")
            messages: list[JsonValue] = []
            for item in items:
                message = await self.resolve_message(
                    object_value(item),
                    chat=chat,
                    target_id=target,
                    generation=generation,
                    context=context,
                )
                messages.append(message)
                self.cache.remember(self.scope(generation), message)
        except QQCallError as error:
            if error.response is not None and error.echo is not None:
                raw_reference = {
                    "evidence_ref": self.instance_id + ":qq:rpc:" + error.echo,
                    "version": 1,
                    "entry": "read_evidence",
                }
            messages = self.cache.read(self.scope(generation), chat, target, count)
            if not messages:
                value = self.error_outcome(error).value
                return CallOutcome(
                    "failed",
                    {
                        "chat": chat,
                        "target_id": target,
                        "source": "unavailable",
                        "messages": None,
                        "platform_failure": value,
                        "raw_response": raw_reference,
                        "coverage": {"complete": None, "cache_available": False},
                    },
                    type(error).__name__,
                    str(error),
                )
            source = "cache"
            failure = {
                "type": type(error).__name__,
                "message": str(error),
                "phase": error.phase,
                "retcode": None if error.response is None else error.response.get("retcode"),
            }
        metadata: JsonObject = {
            "plugin": self.instance_id,
            "kind": "qq.history",
            "chat": chat,
            "target_id": target,
            "source": source,
            "platform_failure": failure,
            "raw_response": raw_reference,
            "requested_count": count,
            "coverage": {
                "known_count": len(messages),
                "complete": None,
                "remote_total": None,
                "times": [object_value(item).get("time") for item in messages],
            },
        }
        page_items: list[JsonValue] = []
        body_refs: list[JsonValue] = []
        for item in messages:
            projected = await self.history_item(object_value(item), metadata)
            page_items.append(projected)
            if "body_ref" in projected:
                body_refs.append(projected["body_ref"])
        metadata["large_message_body_refs"] = body_refs
        snapshot = EvidenceSnapshot(
            evidence_id=self.instance_id + ":qq:history:" + uuid4().hex,
            version=1,
            kind="qq.history",
            text=encode_json(messages).decode(),
            items_json=encode_json(page_items).decode(),
            source_json=encode_json(metadata).decode(),
            occurred_at=time.time(),
        )
        saved = await self.evidence.put_evidence(snapshot)
        try:
            page = await self.evidence.read_evidence(
                saved.evidence_id, max_chars=self.evidence.page_chars
            )
        except KnowledgeInputError as error:
            return CallOutcome(
                "failed",
                {
                    **metadata,
                    "messages": None,
                    "capacity_error": str(error),
                    "evidence_ref": saved.evidence_id,
                    "version": saved.version,
                    "entry": "read_evidence",
                },
                type(error).__name__,
                str(error),
            )
        return self.history_page(page)

    async def history_item(self, message: JsonObject, source: JsonObject) -> JsonObject:
        """过大的结构化消息另存完整正文和原字段，不截断对象制造完整页面。

        返回卡片明确没有正文；read_evidence 可按字符读取独立 body_ref，因此
        原结构对象再大也不会让正文只能通过永远失败的列表分页访问。
        """
        if len(encode_json(message).decode()) + 2 <= self.evidence.page_chars:
            return message
        identity = self.instance_id + ":qq:message:" + uuid4().hex
        metadata = {**source, "message_id": message.get("message_id")}
        original = EvidenceSnapshot(
            evidence_id=identity,
            version=1,
            kind="qq.history.message",
            text=encode_json(message).decode(),
            source_json=encode_json(metadata).decode(),
            occurred_at=time.time(),
        )
        body = EvidenceSnapshot(
            evidence_id=identity + ":body",
            version=1,
            kind="qq.history.body",
            text=id_text(message.get("text")),
            source_json=encode_json({**metadata, "original_ref": identity}).decode(),
            occurred_at=time.time(),
        )
        await self.evidence.put_evidence(original)
        await self.evidence.put_evidence(body)
        return {
            "chat": message.get("chat"),
            "target_id": message.get("target_id"),
            "message_id": message.get("message_id"),
            "sender_id": message.get("sender_id"),
            "time": message.get("time"),
            "text": None,
            "raw_message": None,
            "level": "overview",
            "body_ref": {
                "evidence_ref": body.evidence_id,
                "version": 1,
                "entry": "read_evidence",
                "arguments": {"id": body.evidence_id},
            },
            "original_ref": {
                "evidence_ref": original.evidence_id,
                "version": 1,
                "entry": "read_evidence",
            },
            "coverage": "完整正文和原字段在独立快照；本卡片未呈现正文。",
        }

    def history_page(self, page: EvidencePage) -> CallOutcome:
        """核验宿主快照实际归属，返回旧字段和确切本地续读范围。"""
        snapshot = page.snapshot
        if snapshot is None or snapshot.kind != "qq.history":
            raise KnowledgeInputError("游标不是 QQ 历史快照。")
        metadata = decode_object(snapshot.source_json.encode())
        if metadata.get("plugin") != self.instance_id or metadata.get("kind") != "qq.history":
            raise KnowledgeInputError("QQ 历史游标不属于当前插件。")
        raw_items: object = json.loads(page.text)
        items = decode_object(encode_json({"items": raw_items}))["items"]
        value: JsonObject = {
            **metadata,
            "messages": items,
            "evidence_ref": snapshot.evidence_id,
            "version": snapshot.version,
            "slice_ref": None if page.slice is None else page.slice.slice_ref,
            "range": None
            if page.slice is None
            else {"unit": page.slice.unit, "start": page.slice.start, "end": page.slice.end},
            "next_cursor": page.next_cursor,
            "level": page.level,
        }
        return CallOutcome("confirmed", value, media=snapshot.media)

    async def close(self) -> None:
        """先结束 echo/迟到提交，再收文件；一个资源失败不跳过另一资源收尾。"""
        results = list(await asyncio.gather(self.client.close(), return_exceptions=True))
        results.extend(await asyncio.gather(self.media.close(), return_exceptions=True))
        errors = [result for result in results if isinstance(result, BaseException)]
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("QQ 插件资源收尾失败。", errors)
