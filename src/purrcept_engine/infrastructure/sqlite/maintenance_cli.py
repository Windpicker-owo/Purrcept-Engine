"""离线数据库维护入口，使用与宿主相同的目录锁且不装配插件或模型。

检查只读数据库事实；统计更新有限缓存；清理按现行保留策略反复提交有界批次。
VACUUM 只由显式命令执行，先核验一致性再回收空闲页。这里不运行启动恢复，
也不升级旧格式；需要先以当前 Engine 完成格式迁移。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.locking import InstanceLock
from purrcept_engine.infrastructure.sqlite import archive
from purrcept_engine.values import JsonObject


def check_database(connection: sqlite3.Connection) -> JsonObject:
    """完整检查结构及外键，发现故障立即拒绝清理或压缩。"""
    rows = connection.execute("PRAGMA quick_check").fetchall()
    broken = connection.execute("PRAGMA foreign_key_check").fetchmany(20)
    if rows != [("ok",)] or broken:
        raise StorageError("数据库完整性或外键检查失败，原库已保留。", code="invalid_record")
    return {"quick_check": "ok", "foreign_key_check": "ok"}


def maintain(
    data_dir: Path, command: str, *, retention_days: int = 7, max_bytes: int = 1_073_741_824
) -> JsonObject:
    """独占已有数据目录完成一次明确维护，不启动设备或重放未决工作。

    Args:
        data_dir: 用户明确指定或从配置解析的绝对数据目录。
        command: db-check、db-stats、db-clean 或 db-vacuum。
        retention_days: 模型正文的保留天数。
        max_bytes: 全部模型正文和流诊断的合计字节上限。

    Raises:
        StorageError: 数据格式、完整性或实际维护失败。
        OSError: 目录锁或文件无法取得。
    """
    path = data_dir / "engine.sqlite3"
    if not path.is_file():
        raise StorageError("数据目录中没有 Engine 数据库。", code="database_missing")
    lock = InstanceLock(data_dir)
    lock.acquire()
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA application_id").fetchone() != (0x50555245,):
                raise StorageError("该文件不是 Engine 数据库。", code="unsupported_format")
            checked = check_database(connection)
            if command == "db-check":
                return checked
            if connection.execute("PRAGMA user_version").fetchone() != (16,):
                raise StorageError(
                    "请先用当前 Engine 升级数据库格式，再运行维护。", code="unsupported_format"
                )
            previous = path.stat().st_size
            removed = 0
            if command == "db-clean":
                while True:
                    now = time.time()
                    batch = archive.cleanup(
                        connection,
                        before=now - retention_days * 86400,
                        now=now,
                        max_bytes=max_bytes,
                    )
                    amount = batch["removed_bytes"]
                    assert isinstance(amount, int)
                    removed += amount
                    if not batch["has_more"]:
                        break
                while archive.collect_media(connection):
                    pass
            elif command == "db-vacuum":
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")
            elif command != "db-stats":
                raise ValueError("未知数据库维护命令。")
            return {
                **checked,
                **archive.statistics(connection, path),
                "removed_body_bytes": removed,
                "file_bytes_before": previous,
                "file_bytes_after": path.stat().st_size,
            }
        finally:
            connection.close()
    finally:
        lock.release()
