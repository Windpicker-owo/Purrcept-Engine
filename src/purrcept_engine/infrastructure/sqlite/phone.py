"""在 Store 唯一线程持久保存配对、聊天事件、Bot 头像和手机确认位置。

账本由 app 按插件实例借出；不创建连接或后台任务。事件日志是传输事实，
入站原文仍归 inputs，能力结果仍归 execution；事件仅通过身份关联二者。
附加表在同库初始化事务创建，不重写已有输入、行动或 Notebook。
"""

import hashlib
import io
import sqlite3
from collections.abc import Callable

from PIL import Image, UnidentifiedImageError

from purrcept_engine.domain.inputs.models import MediaAsset
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite.evidence_port import SqliteSubmit
from purrcept_engine.values import JsonObject, JsonValue, decode_object, encode_json

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS phone_profile(plugin TEXT PRIMARY KEY,"
    "media_id TEXT REFERENCES media(media_id),media_type TEXT,revision INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS phone_commands(plugin TEXT NOT NULL,identity TEXT NOT NULL,"
    "intent TEXT NOT NULL,result TEXT,PRIMARY KEY(plugin,identity))",
    "CREATE TABLE IF NOT EXISTS phone_media(plugin TEXT NOT NULL,device_id TEXT NOT NULL,"
    "media_id TEXT NOT NULL REFERENCES media(media_id),media_type TEXT NOT NULL,"
    "PRIMARY KEY(plugin,device_id,media_id))",
    "CREATE TABLE IF NOT EXISTS phone_devices(plugin TEXT PRIMARY KEY,device_id TEXT NOT NULL,"
    "name TEXT NOT NULL,token_hash TEXT NOT NULL,ack INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS phone_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,"
    "plugin TEXT NOT NULL,device_id TEXT NOT NULL,identity TEXT NOT NULL,payload TEXT NOT NULL,"
    "UNIQUE(plugin,device_id,identity))",
    "CREATE INDEX IF NOT EXISTS phone_events_device ON phone_events(plugin,device_id,seq)",
)


class PhoneLedger:
    """绑定插件身份的有限门面，所有同步 SQL 经母 Store 提交并实际完成。"""

    def __init__(self, submit: SqliteSubmit, instance_id: str) -> None:
        """借用母 Store 队列，当前对象不拥有数据库连接。"""
        self._submit, self._plugin = submit, instance_id

    async def progress(self, device_id: str) -> JsonObject:
        """投影有界手机输入阶段及前台真实活动，不读模型正文或行动参数。"""

        def read(c: sqlite3.Connection) -> JsonObject:
            # 同一个只读事务固定跨表版本；前台不能在读取一半时切换轮次，
            # 否则旧输入和新行动会被拼成并不存在的 UI 状态。
            c.execute("BEGIN")
            try:
                rows = c.execute(
                    "SELECT json_extract(payload,'$.message_id'),"
                    "json_extract(payload,'$.input_id') FROM phone_events "
                    "WHERE plugin=? AND device_id=? AND identity LIKE 'in:%' "
                    "ORDER BY seq DESC LIMIT 24",
                    (self._plugin, device_id),
                ).fetchall()
                messages: list[JsonValue] = []
                for message_id, input_id in rows:
                    processing = c.execute(
                        "SELECT version,stage,reason,admitted_at FROM input_processing "
                        "WHERE input_id=?",
                        (input_id,),
                    ).fetchone()
                    if processing is None:
                        continue
                    message: JsonObject = {
                        "message_id": str(message_id),
                        "input_id": str(input_id),
                        "version": int(processing[0]),
                        "stage": str(processing[1]),
                        "reason": str(processing[2]),
                        "admitted_at": float(processing[3]),
                    }
                    associated = c.execute(
                        "SELECT t.turn_id,t.state FROM turn_inputs ti "
                        "JOIN subject_turns t USING(turn_id) "
                        "WHERE ti.input_id=? AND t.scope='foreground' "
                        "ORDER BY t.started_at DESC LIMIT 1",
                        (input_id,),
                    ).fetchone()
                    if associated is not None:
                        message.update(turn_id=str(associated[0]), turn_state=str(associated[1]))
                    # 最近一次真实派发保留在消息详情中。短行动可能已在两次实时
                    # 采样之间结束，不能为了播放动画而伪造仍在执行。
                    last_call = c.execute(
                        "SELECT calls.path,calls.state FROM actions a "
                        "JOIN calls ON calls.action_id=a.action_id "
                        "WHERE a.input_id=? AND calls.started_at IS NOT NULL "
                        "ORDER BY a.created_at DESC,calls.sequence DESC LIMIT 1",
                        (input_id,),
                    ).fetchone()
                    if last_call is not None:
                        message["last_action"] = {
                            "path": str(last_call[0]),
                            "state": str(last_call[1]),
                        }
                    messages.append(message)

                activity: JsonObject = {"phase": "idle"}
                turn = c.execute(
                    "SELECT t.turn_id,t.started_at FROM subject_sessions s "
                    "JOIN subject_turns t ON t.turn_id=s.active_turn "
                    "WHERE s.scope='foreground' AND t.state='running'"
                ).fetchone()
                if turn is not None:
                    turn_id = str(turn[0])
                    activity = {
                        "phase": "processing",
                        "turn_id": turn_id,
                        "started_at": float(turn[1]),
                    }
                    # 因果交接使用 turn_inputs 的确切输入身份，不用时间接近
                    # 将后台或另一个轮次的行动归给手机消息。
                    action = c.execute(
                        "SELECT a.action_id,a.created_at FROM actions a "
                        "JOIN turn_inputs ti ON ti.input_id=a.input_id "
                        "WHERE ti.turn_id=? AND a.state='running' "
                        "ORDER BY a.created_at DESC LIMIT 1",
                        (turn_id,),
                    ).fetchone()
                    request = c.execute(
                        "SELECT created_at FROM model_requests WHERE "
                        "json_extract(evidence_json,'$.scope.owner_id')='foreground' AND "
                        "json_extract(evidence_json,'$.scope.turn_id')=? AND state='running' "
                        "ORDER BY created_at DESC LIMIT 1",
                        (turn_id,),
                    ).fetchone()
                    if action is not None:
                        activity.update(
                            phase="acting", action_id=str(action[0]), started_at=float(action[1])
                        )
                        call = c.execute(
                            "SELECT path FROM calls WHERE action_id=? AND state='started' "
                            "ORDER BY sequence DESC LIMIT 1",
                            (action[0],),
                        ).fetchone()
                        if call is not None:
                            activity["call_path"] = str(call[0])
                    elif request is not None:
                        activity.update(phase="thinking", started_at=float(request[0]))
                return {"activity": activity, "messages": messages}
            finally:
                c.rollback()

        return await self._submit(read)

    async def _write[T](
        self,
        operation: Callable[[sqlite3.Connection], T],
        *,
        logical_bytes: int = 0,
        control: bool = False,
    ) -> T:
        """每个写操作独占一次短事务，异常回滚，不能遗留隐式事务给输入接管。"""

        def commit(connection: sqlite3.Connection) -> T:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                return operation(connection)

        return await self._submit(
            commit, writing=True, logical_bytes=logical_bytes, control=control
        )

    async def begin_command(self, identity: str, intent: JsonObject) -> bool:
        """派发门槛实际提交后才允许写网络；重复身份不能再次启动。"""
        payload = encode_json(intent).decode()

        def write(c: sqlite3.Connection) -> bool:
            old = c.execute(
                "SELECT intent FROM phone_commands WHERE plugin=? AND identity=?",
                (self._plugin, identity),
            ).fetchone()
            if old is not None:
                if old[0] != payload:
                    raise StorageError("命令身份冲突。", code="phone_identity_conflict")
                return False
            c.execute(
                "INSERT INTO phone_commands(plugin,identity,intent) VALUES(?,?,?)",
                (self._plugin, identity, payload),
            )
            return True

        return await self._write(write, logical_bytes=len(payload.encode()))

    async def complete_command(self, identity: str, result: JsonObject) -> JsonObject:
        """只保存有原派发意图的设备事实，不能捏造另一个调用。"""
        payload = encode_json(result).decode()

        def write(c: sqlite3.Connection) -> JsonObject:
            old = c.execute(
                "SELECT intent,result FROM phone_commands WHERE plugin=? AND identity=?",
                (self._plugin, identity),
            ).fetchone()
            if old is None:
                raise StorageError("手机回执没有原派发身份。", code="phone_command_missing")
            if old[1] is not None and old[1] != payload:
                raise StorageError("手机回执与首次事实冲突。", code="phone_identity_conflict")
            c.execute(
                "UPDATE phone_commands SET result=? WHERE plugin=? AND identity=?",
                (payload, self._plugin, identity),
            )
            return decode_object(str(old[0]).encode())

        return await self._write(write, logical_bytes=len(payload.encode()), control=True)

    async def commands(self, *, identity: str | None = None) -> list[JsonObject]:
        """只读派发事实，未决结果只能查询，不能重发命令。"""

        def read(c: sqlite3.Connection) -> list[JsonObject]:
            if identity is None:
                rows = c.execute(
                    "SELECT identity,intent,result FROM phone_commands "
                    "WHERE plugin=? AND result IS NULL LIMIT 100",
                    (self._plugin,),
                )
            else:
                rows = c.execute(
                    "SELECT identity,intent,result FROM phone_commands "
                    "WHERE plugin=? AND identity=?",
                    (self._plugin, identity),
                )
            return [
                {
                    "command_id": str(row[0]),
                    "intent": decode_object(str(row[1]).encode()),
                    "result": None if row[2] is None else decode_object(str(row[2]).encode()),
                }
                for row in rows
            ]

        return await self._submit(read)

    async def recover_inputs(self, device_id: str) -> int:
        """修复接管后、展示提交前崩溃留下的缺口，原文和调度状态不变。"""

        def write(c: sqlite3.Connection) -> int:
            rows = c.execute(
                "SELECT i.native_event_id,i.text,i.occurred_at,i.input_id FROM inputs i "
                "WHERE i.adapter_id=? AND i.account_id=? AND i.event_kind='phone.chat.message' "
                "AND NOT EXISTS(SELECT 1 FROM phone_events p WHERE p.plugin=i.adapter_id "
                "AND p.device_id=i.account_id AND p.identity='in:'||i.native_event_id) "
                "ORDER BY i.rowid LIMIT 100",
                (self._plugin, device_id),
            ).fetchall()
            for identity, text, occurred_at, input_id in rows:
                event: JsonObject = {
                    "type": "chat.message",
                    "message_id": str(identity),
                    "direction": "incoming",
                    "text": str(text),
                    "occurred_at": str(occurred_at),
                    "input_id": str(input_id),
                }
                c.execute(
                    "INSERT INTO phone_events(plugin,device_id,identity,payload) VALUES(?,?,?,?)",
                    (self._plugin, device_id, "in:" + str(identity), encode_json(event).decode()),
                )
            return len(rows)

        return await self._write(write)

    async def put_media(self, device_id: str, content: bytes, media_type: str) -> str:
        """完整字节及设备归属提交后，后续输入才能引用。"""
        identity = hashlib.sha256(content).hexdigest()

        def write(c: sqlite3.Connection) -> str:
            c.execute(
                "INSERT OR IGNORE INTO media(media_id,content) VALUES(?,?)", (identity, content)
            )
            c.execute(
                "INSERT OR IGNORE INTO phone_media(plugin,device_id,media_id,media_type) "
                "VALUES(?,?,?,?)",
                (self._plugin, device_id, identity, media_type),
            )
            return identity

        return await self._write(write, logical_bytes=len(content))

    async def media(self, device_id: str, media_id: str) -> MediaAsset | None:
        """同次查询核验设备附件或当前公开头像，不开放任意宿主媒体。"""

        def read(c: sqlite3.Connection) -> MediaAsset | None:
            row = c.execute(
                "SELECT m.content,p.media_type FROM phone_media p JOIN media m USING(media_id) "
                "WHERE p.plugin=? AND p.device_id=? AND p.media_id=? "
                "UNION ALL SELECT m.content,p.media_type FROM phone_profile p "
                "JOIN media m USING(media_id) WHERE p.plugin=? AND p.media_id=? LIMIT 1",
                (self._plugin, device_id, media_id, self._plugin, media_id),
            ).fetchone()
            return None if row is None else MediaAsset(bytes(row[0]), str(row[1]))

        return await self._submit(read)

    async def set_avatar(self, media_id: str | None) -> JsonObject:
        """核验实际图片后替换头像；外键保留原始媒体，重启后仍可同步。"""

        def write(c: sqlite3.Connection) -> JsonObject:
            media_type = None
            if media_id is not None:
                row = c.execute(
                    "SELECT content FROM media WHERE media_id=?", (media_id,)
                ).fetchone()
                formats = {
                    "PNG": "image/png",
                    "JPEG": "image/jpeg",
                    "GIF": "image/gif",
                    "WEBP": "image/webp",
                }
                if row is None or not 0 < len(row[0]) <= 8 * 1024 * 1024:
                    raise StorageError("头像图片缺失或超过 8 MiB。", code="phone_invalid_avatar")
                try:
                    with Image.open(io.BytesIO(row[0])) as image:
                        media_type = formats.get(image.format or "")
                        if media_type is None or image.width * image.height > 32_000_000:
                            raise StorageError(
                                "头像格式或尺寸不受支持。", code="phone_invalid_avatar"
                            )
                        image.verify()
                except (
                    UnidentifiedImageError,
                    OSError,
                    SyntaxError,
                    Image.DecompressionBombError,
                ) as error:
                    raise StorageError("头像图片损坏。", code="phone_invalid_avatar") from error
            # 版本按显式设置递增，即使重复选择同图，也能覆盖手机上随后修改的头像。
            revision = c.execute(
                "INSERT INTO phone_profile VALUES(?,?,?,1) ON CONFLICT(plugin) DO UPDATE SET "
                "media_id=excluded.media_id,media_type=excluded.media_type,revision=revision+1 "
                "RETURNING revision",
                (self._plugin, media_id, media_type),
            ).fetchone()[0]
            return {"media_id": media_id, "media_type": media_type, "revision": int(revision)}

        return await self._write(write)

    async def avatar(self) -> JsonObject | None:
        """返回插件级头像资料；media 查询只向认证设备公开当前头像字节。"""

        def read(c: sqlite3.Connection) -> JsonObject | None:
            row = c.execute(
                "SELECT media_id,media_type,revision FROM phone_profile WHERE plugin=?",
                (self._plugin,),
            ).fetchone()
            return (
                None
                if row is None
                else {"media_id": row[0], "media_type": row[1], "revision": int(row[2])}
            )

        return await self._submit(read)

    async def pair(self, device_id: str, name: str, token_hash: str) -> None:
        """原子绑定唯一设备，避免并发配对替换正在使用的手机。"""

        def write(c: sqlite3.Connection) -> None:
            old = c.execute(
                "SELECT device_id FROM phone_devices WHERE plugin=?", (self._plugin,)
            ).fetchone()
            if old is not None and old[0] != device_id:
                raise StorageError("请先撤销已有手机配对。", code="phone_already_paired")
            c.execute(
                "INSERT INTO phone_devices(plugin,device_id,name,token_hash) VALUES(?,?,?,?) "
                "ON CONFLICT(plugin) DO UPDATE SET "
                "name=excluded.name,token_hash=excluded.token_hash",
                (self._plugin, device_id, name, token_hash),
            )

        await self._write(write, logical_bytes=512)

    async def authenticate(self, token_hash: str) -> str | None:
        """只在当前插件命名空间查询凭据摘要。"""

        def read(c: sqlite3.Connection) -> str | None:
            row = c.execute(
                "SELECT device_id FROM phone_devices WHERE plugin=? AND token_hash=?",
                (self._plugin, token_hash),
            ).fetchone()
            return None if row is None else str(row[0])

        return await self._submit(read)

    async def revoke(self) -> None:
        """撤销凭据，不抹去聊天事实。"""

        def write(c: sqlite3.Connection) -> None:
            c.execute("DELETE FROM phone_devices WHERE plugin=?", (self._plugin,))

        await self._write(write)

    async def append(self, device_id: str, identity: str, event: JsonObject) -> JsonObject:
        """事件先提交再暴露 seq；同身份冲突不会被当成重试吞掉。"""
        payload = encode_json(event).decode()

        def write(c: sqlite3.Connection) -> JsonObject:
            row = c.execute(
                "SELECT seq,payload FROM phone_events "
                "WHERE plugin=? AND device_id=? AND identity=?",
                (self._plugin, device_id, identity),
            ).fetchone()
            if row is not None:
                if row[1] != payload:
                    raise StorageError("消息身份与首次内容冲突。", code="phone_identity_conflict")
                return {**event, "seq": int(row[0])}
            cursor = c.execute(
                "INSERT INTO phone_events(plugin,device_id,identity,payload) VALUES(?,?,?,?)",
                (self._plugin, device_id, identity, payload),
            )
            assert cursor.lastrowid is not None
            return {**event, "seq": cursor.lastrowid}

        return await self._write(write, logical_bytes=len(payload.encode()))

    async def events(self, device_id: str, after: int, limit: int = 100) -> list[JsonObject]:
        """读取有界连续页面，游标按设备过滤后保持全局序号。"""
        if after < 0 or not 1 <= limit <= 100:
            raise ValueError("手机事件分页超出范围。")

        def read(c: sqlite3.Connection) -> list[JsonObject]:
            return [
                {**decode_object(str(payload).encode()), "seq": int(seq)}
                for seq, payload in c.execute(
                    "SELECT seq,payload FROM phone_events "
                    "WHERE plugin=? AND device_id=? AND seq>? ORDER BY seq LIMIT ?",
                    (self._plugin, device_id, after, limit),
                )
            ]

        return await self._submit(read)

    async def acknowledge(self, device_id: str, through: int) -> None:
        """确认不能越过当前设备日志，序号间隙可属于其他设备。"""

        def write(c: sqlite3.Connection) -> None:
            maximum = c.execute(
                "SELECT COALESCE(MAX(seq),0) FROM phone_events WHERE plugin=? AND device_id=?",
                (self._plugin, device_id),
            ).fetchone()[0]
            if through < 0 or through > maximum:
                raise StorageError("手机确认游标无效。", code="phone_invalid_cursor")
            c.execute(
                "UPDATE phone_devices SET ack=MAX(ack,?) WHERE plugin=? AND device_id=?",
                (through, self._plugin, device_id),
            )

        await self._write(write, control=True)

    async def device(self) -> JsonObject:
        """只返回业务状态，不向快照泄露凭据。"""

        def read(c: sqlite3.Connection) -> JsonObject:
            row = c.execute(
                "SELECT device_id,name,ack FROM phone_devices WHERE plugin=?", (self._plugin,)
            ).fetchone()
            return (
                {}
                if row is None
                else {"device_id": str(row[0]), "name": str(row[1]), "ack": int(row[2])}
            )

        return await self._submit(read)
