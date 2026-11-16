"""在宿主唯一 SQLite 队列中保存跨通道贴纸，不创建文件库或第二条连接。

图片与目录同事务提交；目录对 media 的外键同时阻止归档回收仍被收藏的字节。
MD5 仅用于 QQ 图片匹配，主媒体身份始终使用完整内容的 SHA-256。
"""

import hashlib
import io
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from purrcept_engine.domain.inputs.models import MediaAsset
from purrcept_engine.domain.stickers import StickerError, StickerRecord
from purrcept_engine.infrastructure.sqlite.evidence_port import SqliteSubmit

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS stickers(sticker_id TEXT PRIMARY KEY,tag TEXT NOT NULL UNIQUE,"
    "media_id TEXT NOT NULL UNIQUE REFERENCES media(media_id),media_type TEXT NOT NULL,"
    "digest TEXT NOT NULL,created_at TEXT NOT NULL)",
)
_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}


def _record(row: tuple[str, ...]) -> StickerRecord:
    return StickerRecord(*row)


def _save(connection: sqlite3.Connection, data: bytes, tag: str) -> StickerRecord:
    """事务内核验真实格式、标签唯一性与内容身份，任何拒绝均回滚整个导入。"""
    tag = tag.strip()
    if not tag or len(tag) > 64:
        raise StickerError("贴纸标签必须为 1—64 个字符且不能全为空白。")
    if not data or len(data) > 8 * 1024 * 1024:
        raise StickerError("贴纸图片必须非空且不超过 8 MiB。")
    try:
        with Image.open(io.BytesIO(data)) as image:
            media_type = _FORMATS.get(image.format or "")
            if media_type is None or image.width * image.height > 32_000_000:
                raise StickerError("贴纸必须是受支持且尺寸有界的图片。")
            image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise StickerError("贴纸图片损坏或格式不受支持。") from error

    media_id = hashlib.sha256(data).hexdigest()
    conflict = connection.execute("SELECT media_id FROM stickers WHERE tag=?", (tag,)).fetchone()
    if conflict is not None and conflict[0] != media_id:
        raise StickerError("贴纸标签已被另一张图片使用。")
    existing = connection.execute(
        "SELECT sticker_id,created_at FROM stickers WHERE media_id=?", (media_id,)
    ).fetchone()
    identity = existing[0] if existing else "st" + uuid4().hex
    created_at = existing[1] if existing else datetime.now(UTC).isoformat()
    digest = hashlib.md5(data, usedforsecurity=False).hexdigest()
    connection.execute(
        "INSERT OR IGNORE INTO media(media_id,content) VALUES(?,?)", (media_id, data)
    )
    connection.execute(
        "INSERT INTO stickers VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(media_id) DO UPDATE SET tag=excluded.tag",
        (identity, tag, media_id, media_type, digest, created_at),
    )
    return StickerRecord(identity, tag, media_id, media_type, digest, created_at)


class SqliteStickerLibrary:
    """Store 借出的唯一队列门面，业务契约见 domain.stickers.StickerLibrary。"""

    def __init__(self, submit: SqliteSubmit) -> None:
        """借用母 Store 的提交入口，不另持有连接或后台任务。"""
        self._submit = submit

    async def save(self, media_id: str, *, tag: str) -> StickerRecord:
        """将已持久接管的媒体加入共享库。"""

        def write(connection: sqlite3.Connection) -> StickerRecord:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT content FROM media WHERE media_id=?", (media_id,)
                ).fetchone()
                if row is None:
                    raise StickerError("指定媒体不存在，请先读取或上传实际图片。")
                return _save(connection, bytes(row[0]), tag)

        return await self._submit(write, writing=True)

    async def save_image(self, asset: MediaAsset, *, tag: str) -> StickerRecord:
        """在一次事务内导入实际图片与共享库记录。"""

        def write(connection: sqlite3.Connection) -> StickerRecord:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                return _save(connection, asset.content, tag)

        return await self._submit(write, writing=True, logical_bytes=len(asset.content))

    async def list(self) -> tuple[StickerRecord, ...]:
        """读取不含 BLOB 的有序目录。"""

        def read(connection: sqlite3.Connection) -> tuple[StickerRecord, ...]:
            rows = connection.execute(
                "SELECT sticker_id,tag,media_id,media_type,digest,created_at FROM stickers "
                # 主机时钟可能在连续写入间不前进；rowid 保留真实插入顺序。
                "ORDER BY rowid"
            )
            return tuple(_record(row) for row in rows)

        return await self._submit(read)

    async def read(
        self, *, sticker_id: str = "", tag: str = "", media_id: str = ""
    ) -> tuple[StickerRecord, MediaAsset]:
        """一次查询核对目录与完整字节，避免跨通道并发修改拼接出错误图片。"""

        def read(connection: sqlite3.Connection) -> tuple[StickerRecord, MediaAsset]:
            if sticker_id:
                key, value = "sticker_id", sticker_id
            elif media_id:
                key, value = "media_id", media_id
            else:
                key, value = "tag", tag
            row = connection.execute(
                "SELECT s.sticker_id,s.tag,s.media_id,s.media_type,s.digest,s.created_at,m.content "
                f"FROM stickers s JOIN media m USING(media_id) WHERE s.{key}=?",
                (value,),
            ).fetchone()
            if row is None:
                raise StickerError("未找到指定贴纸。")
            return _record(tuple(row[:6])), MediaAsset(bytes(row[6]), str(row[3]))

        return await self._submit(read)

    async def delete(self, sticker_id: str) -> None:
        """只删除收藏关系；其他输入、证据和手机消息继续持有原媒体。"""

        def write(connection: sqlite3.Connection) -> None:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if (
                    connection.execute(
                        "DELETE FROM stickers WHERE sticker_id=?", (sticker_id,)
                    ).rowcount
                    != 1
                ):
                    raise StickerError("未找到指定贴纸。")

        await self._submit(write, writing=True)
