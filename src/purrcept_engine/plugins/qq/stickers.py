"""持久贴纸的 JSON 索引与完整字节副本，由 QQ 媒体文件所有者串行调用。

标签和路径保留旧公开字段，索引损坏明确失败而不是悄悄当成空库。字节文件
先发布、索引后原子替换；失败可能留下已发布的字节，不能声称磁盘自动回滚。
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from purrcept_engine.plugin_system import MediaAsset
from purrcept_engine.plugins.qq.media import QQMediaError
from purrcept_engine.values import JsonObject, decode_object, encode_json


class StickerRecord(BaseModel):
    """实际库记录；绝对 path 仍需在使用时核对属于本库。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    sticker_id: str = Field(min_length=1)
    tag: str = Field(pattern=r"^\S{1,64}$")
    digest: str = Field(pattern=r"^[0-9a-f]{32}$")
    filename: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    path: str = Field(min_length=1)
    created_at: str = Field(min_length=1)

    def value(self) -> JsonObject:
        """保留原有工具元数据，不加入像素。"""
        return decode_object(self.model_dump_json().encode("utf-8"))


class _Index(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    stickers: list[StickerRecord]


class StickerPublicationError(OSError):
    """字节/索引发布可能部分完成，恢复必须核对实际 index/file。"""

    def __init__(self, error: OSError, recovery: JsonObject) -> None:
        """保留原生错误和实际恢复位置，不建议自动覆写或重发。"""
        super().__init__(str(error))
        self.recovery = recovery


class StickerLibrary:
    """仅由 MediaOwner.io 的串行文件工作访问；主体模型不持有本对象。"""

    def __init__(self, root: Path) -> None:
        """绑定存储目录，构造阶段不读写磁盘。"""
        self.root = root

    def list(self) -> tuple[StickerRecord, ...]:
        """每次读取实际索引，外部损坏不由陈旧内存掩盖。"""
        index = self.root / "index.json"
        if not index.exists():
            return ()
        if not index.resolve().is_relative_to(self.root.resolve()):
            raise QQMediaError("贴纸索引经 symlink 指向库外。")
        if index.stat().st_size > 4 * 1024 * 1024:
            raise QQMediaError("贴纸索引超过 4 MiB。")
        parsed = _Index.model_validate(decode_object(index.read_bytes()))
        records = tuple(sorted(parsed.stickers, key=lambda record: record.created_at))
        if len({record.sticker_id for record in records}) != len(records) or len(
            {record.tag for record in records}
        ) != len(records):
            raise QQMediaError("贴纸索引存在重复身份或标签。")
        for record in records:
            self.path(record)
        return records

    def path(self, record: StickerRecord) -> Path:
        """索引路径不能把读取或发送导向用户文件，缺失副本明确失败。"""
        path = Path(record.path).resolve(strict=True)
        if not path.is_relative_to((self.root / "files").resolve()) or not path.is_file():
            raise QQMediaError("贴纸副本缺失或超出本库。")
        return path

    def get(self, *, sticker_id: str = "", tag: str = "") -> StickerRecord:
        """沿旧入口 id 优先；查无实际记录不构造虚拟贴纸。"""
        for record in self.list():
            if (record.sticker_id == sticker_id) if sticker_id else (record.tag == tag):
                return record
        raise QQMediaError("未找到指定贴纸。")

    def match(self, data: JsonObject) -> StickerRecord | None:
        """匹配 OneBot 原生 md5 或文件名中的摘要，只返回已保存的真实标签。"""
        digests: set[str] = set()
        for key in ("md5", "file_md5", "file_unique", "file", "file_id", "filename", "name"):
            value = data.get(key)
            if isinstance(value, str) and (match := re.search(r"[0-9a-fA-F]{32}", value)):
                digests.add(match.group().lower())
        return next((record for record in self.list() if record.digest in digests), None)

    def save(self, asset: MediaAsset, *, tag: str) -> StickerRecord:
        """保留同字节贴纸身份、更新标签，两个实际文件按明确顺序发布。"""
        records = self.list()
        digest = hashlib.md5(asset.content, usedforsecurity=False).hexdigest()
        existing = next((record for record in records if record.digest == digest), None)
        suffixes = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }
        suffix = suffixes.get(asset.media_type)
        if suffix is None:
            raise QQMediaError("不支持的贴纸媒体类型。")
        files = self.root / "files"
        files.mkdir(parents=True, exist_ok=True)
        target = files / (digest + suffix)
        record = StickerRecord(
            sticker_id=existing.sticker_id if existing else "st" + uuid4().hex[:10],
            tag=tag,
            digest=digest,
            filename=target.name,
            media_type=asset.media_type,
            path=str(target.resolve()),
            created_at=existing.created_at if existing else datetime.now(UTC).isoformat(),
        )
        updated = [item.value() for item in records if item.digest != digest and item.tag != tag]
        updated.append(record.value())
        descriptor, staged_name = tempfile.mkstemp(prefix=".index-", suffix=".json", dir=self.root)
        staged = Path(staged_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encode_json({"stickers": updated}))
                stream.flush()
                os.fsync(stream.fileno())
            if not target.exists():
                with target.open("xb") as stream:
                    stream.write(asset.content)
                    stream.flush()
                    os.fsync(stream.fileno())
            elif target.read_bytes() != asset.content:
                raise QQMediaError("贴纸同摘要副本内容冲突，不能覆写。")
            os.replace(staged, self.root / "index.json")
        except OSError as error:
            raise StickerPublicationError(
                error,
                {
                    "file": str(target),
                    "staged_index": str(staged),
                    "index": str(self.root / "index.json"),
                    "requires_observation": True,
                },
            ) from error
        return record
