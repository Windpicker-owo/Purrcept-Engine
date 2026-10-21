"""仅持久化SC撤回ID，跨插件重启阻止已删除留言复活；不保存正文或凭据。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from purrcept_engine.values import decode_object

from .bilibili import ProtocolError

LIMIT = 20_000


def load_deleted(path: Path | None) -> set[int]:
    """读取指定插件/房间撤回记录，损坏时停止而不是丢弃防重放状态。

    Args:
        path: 装配指定的单个本机文件；None供无持久化隔离测试使用。

    Returns:
        已删除SC的数字留言ID集合。

    Raises:
        ProtocolError: 文件版本、容量或ID不合法。
        OSError: 已有文件无法读取。
    """
    if path is None or not path.exists():
        return set()
    with path.open("rb") as stream:
        raw = stream.read(512_001)
    if len(raw) > 512_000:
        raise ProtocolError("SC撤回记录超限。")
    data = decode_object(raw)
    ids = data.get("message_ids")
    if data.get("version") != 1 or not isinstance(ids, list) or len(ids) > LIMIT:
        raise ProtocolError("SC撤回记录格式无效。")
    result: set[int] = set()
    for value in ids:
        if type(value) is not int or not 0 < value < 2**53:
            raise ProtocolError("SC撤回身份无效。")
        result.add(value)
    return result


def save_deleted(path: Path | None, identities: set[int]) -> None:
    """原子保存撤回身份，再允许运行时确认删除和接收后续事件。

    Args:
        path: 装配指定的单个文件；None不落盘。
        identities: 有界的已校验SC留言ID，包含本次新增撤回。

    Raises:
        OSError: 写入或替换失败；保留原文件，不继续接收。
        ProtocolError: 容量耗尽，不能淘汰旧ID而复活留言。
    """
    if len(identities) > LIMIT:
        raise ProtocolError("SC撤回记录容量已满。")
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temp = stream.name
            json.dump({"version": 1, "message_ids": sorted(identities)}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        temp = None
    finally:
        if temp is not None:
            Path(temp).unlink(missing_ok=True)
