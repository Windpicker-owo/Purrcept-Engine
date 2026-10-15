"""核验 Engine 格式和恢复事实；不启动提交线程或重放外部效果。"""

import sqlite3

from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite import activities as activity_storage
from purrcept_engine.infrastructure.sqlite import audio as audio_storage
from purrcept_engine.infrastructure.sqlite import controls as control_storage
from purrcept_engine.infrastructure.sqlite import execution as execution_storage
from purrcept_engine.infrastructure.sqlite import feedback as feedback_storage
from purrcept_engine.infrastructure.sqlite import knowledge as knowledge_storage
from purrcept_engine.infrastructure.sqlite import maintenance as maintenance_storage
from purrcept_engine.infrastructure.sqlite import model_requests as model_storage
from purrcept_engine.infrastructure.sqlite import subject as subject_storage
from purrcept_engine.infrastructure.sqlite import time as time_storage

_FORMAT_VERSION = 13


_APPLICATION_ID = 0x50555245


_SCHEMA = (
    "CREATE TABLE media (media_id TEXT PRIMARY KEY, content BLOB NOT NULL)",
    "CREATE TABLE inputs ("
    "input_id TEXT PRIMARY KEY, adapter_id TEXT NOT NULL, account_id TEXT NOT NULL, "
    "conversation_id TEXT NOT NULL, event_kind TEXT NOT NULL, native_event_id TEXT, "
    "sender_id TEXT, display_name TEXT, text TEXT NOT NULL, occurred_at TEXT NOT NULL, "
    "received_at TEXT NOT NULL, logical_bytes INTEGER NOT NULL CHECK(logical_bytes >= 0), "
    "context_json TEXT NOT NULL, dedup_key TEXT UNIQUE, fingerprint TEXT NOT NULL)",
    "CREATE TABLE input_media ("
    "input_id TEXT NOT NULL REFERENCES inputs(input_id), ordinal INTEGER NOT NULL, "
    "media_id TEXT NOT NULL REFERENCES media(media_id), media_type TEXT NOT NULL, "
    "name TEXT, PRIMARY KEY(input_id, ordinal))",
    "CREATE TABLE pending_inputs ("
    "sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
    "input_id TEXT UNIQUE NOT NULL REFERENCES inputs(input_id))",
    *execution_storage.SCHEMA,
    *model_storage.SCHEMA,
    *activity_storage.SCHEMA,
    *subject_storage.SCHEMA,
    *knowledge_storage.SCHEMA,
    *maintenance_storage.SCHEMA,
    *time_storage.SCHEMA,
    *feedback_storage.SCHEMA,
    *control_storage.SCHEMA,
    *audio_storage.SCHEMA,
)


def _upgrade_minimum_wake(connection: sqlite3.Connection) -> None:
    """在格式12上原子增加升级判断表，不改写原文、关系或业务记录。

    格式12的DDL已冻结。先核对全部原有对象和完整性，再在同一短事务创建
    表及索引。Store在全部初始化通过后推进版本并提交；任何拒绝或SQLite
    失败均留下原版本与原数据。不能用IF NOT EXISTS接受名称冲突。
    """
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
    ).fetchall()
    existing_sql = {_text(row[0]) for row in rows}
    for statement in _SCHEMA:
        if statement in subject_storage.MINIMUM_WAKE_SCHEMA:
            continue
        if statement not in existing_sql:
            raise StorageError("格式12的数据库结构不完整或已被修改。", code="invalid_record")
    for statement in subject_storage.MINIMUM_WAKE_SCHEMA:
        connection.execute(statement)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise StorageError("权威记录中的字符串字段无效。", code="invalid_record")
    return value


def initialize(connection: sqlite3.Connection) -> None:
    """初始化空库或升级已知格式12；未知格式和完整性故障保留原库。"""
    connection.execute("PRAGMA foreign_keys = ON")
    version_row: tuple[object, ...] = connection.execute("PRAGMA user_version").fetchone()
    identity_row: tuple[object, ...] = connection.execute("PRAGMA application_id").fetchone()
    version = _integer(version_row[0])
    identity = _integer(identity_row[0])
    tables: list[tuple[object, ...]] = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    # Ordering: 格式、结构、业务解码和上次在途恢复共享一次提交。子系统
    # 初始化借用此事务；任何后置拒绝都不能留下升级或部分中断记录。
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if version == 0 and identity == 0 and not tables:
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
        elif version == 12 and identity == _APPLICATION_ID:
            _upgrade_minimum_wake(connection)
        elif version != _FORMAT_VERSION or identity != _APPLICATION_ID:
            raise StorageError("数据库不是支持的 Engine 格式。", code="unsupported_format")

        check: tuple[object, ...] = connection.execute("PRAGMA quick_check").fetchone()
        if check != ("ok",):
            raise StorageError("权威数据库完整性检查失败。", code="invalid_record")
        broken_reference: tuple[object, ...] | None = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchone()
        if broken_reference is not None:
            raise StorageError("权威数据库包含失效引用。", code="invalid_record")
        # 明确读取必需列，在装配时暴露缺表/缺列，避免首次输入才触发格式失败。
        connection.execute(f"SELECT {_INPUT_COLUMNS}, dedup_key, fingerprint FROM inputs LIMIT 0")
        connection.execute("SELECT media_id, content FROM media LIMIT 0")
        connection.execute(
            "SELECT input_id, ordinal, media_id, media_type, name FROM input_media LIMIT 0"
        )
        connection.execute("SELECT sequence, input_id FROM pending_inputs LIMIT 0")
        execution_storage.initialize(connection)
        model_storage.initialize(connection)
        subject_storage.initialize(connection)
        knowledge_storage.initialize(connection)
        maintenance_storage.initialize(connection)
        time_storage.initialize(connection)
        feedback_storage.initialize(connection)
        control_storage.initialize(connection)
        connection.execute(
            "SELECT sequence,plugin_id,identity,fact,occurred_at,payload_json "
            "FROM audio_facts LIMIT 0"
        )
        activity_storage.activity_records(connection)
        connection.execute(f"PRAGMA user_version = {_FORMAT_VERSION}")


_INPUT_COLUMNS = (
    "input_id, adapter_id, account_id, conversation_id, event_kind, native_event_id, "
    "sender_id, display_name, text, occurred_at, received_at, logical_bytes, context_json"
)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StorageError("权威记录中的非负整数字段无效。", code="invalid_record")
    return value
