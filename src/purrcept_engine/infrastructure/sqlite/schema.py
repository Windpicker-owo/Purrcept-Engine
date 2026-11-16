"""核验 Engine 格式和恢复事实；不启动提交线程或重放外部效果。"""

import json
import sqlite3

from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite import activities as activity_storage
from purrcept_engine.infrastructure.sqlite import analysis_work, archive, behavior_review, notebook
from purrcept_engine.infrastructure.sqlite import audio as audio_storage
from purrcept_engine.infrastructure.sqlite import controls as control_storage
from purrcept_engine.infrastructure.sqlite import evidence as evidence_storage
from purrcept_engine.infrastructure.sqlite import execution as execution_storage
from purrcept_engine.infrastructure.sqlite import feedback as feedback_storage
from purrcept_engine.infrastructure.sqlite import maintenance as maintenance_storage
from purrcept_engine.infrastructure.sqlite import model_requests as model_storage
from purrcept_engine.infrastructure.sqlite import phone as phone_storage
from purrcept_engine.infrastructure.sqlite import subject as subject_storage
from purrcept_engine.infrastructure.sqlite import time as time_storage

_FORMAT_VERSION = 17


_APPLICATION_ID = 0x50555245

# 索引只派生已有字段，不改变业务记录格式。旧格式16库在取得目录锁后首次
# 构建，随后启动仅检查存在性；与恢复检查共用事务，失败不能留下部分更新。
_QUERY_INDEXES = (
    "CREATE INDEX IF NOT EXISTS knowledge_evidence_cause ON "
    "knowledge_evidence(json_extract(payload_json,'$.kind'),"
    "json_extract(json_extract(payload_json,'$.source_json'),'$.action_id'),"
    "json_extract(json_extract(payload_json,'$.source_json'),'$.call_id'),"
    "json_extract(json_extract(payload_json,'$.source_json'),'$.receipt_revision'),created_at,"
    "evidence_id,version) WHERE valid=1",
    "CREATE INDEX IF NOT EXISTS knowledge_slices_source ON knowledge_slices(evidence_id,"
    "version,slice_ref)",
    "CREATE INDEX IF NOT EXISTS knowledge_cursors_source ON knowledge_cursors(evidence_id,"
    "version,start,kind,page_chars,cursor)",
)


_SCHEMA = (
    "CREATE TABLE media (media_id TEXT PRIMARY KEY, content BLOB NOT NULL)",
    "CREATE TABLE inputs (input_id TEXT PRIMARY KEY, adapter_id TEXT NOT NULL, account_id TEXT "
    "NOT NULL, conversation_id TEXT NOT NULL, event_kind TEXT NOT NULL, native_event_id TEXT, "
    "sender_id TEXT, display_name TEXT, text TEXT NOT NULL, occurred_at TEXT NOT NULL, "
    "received_at TEXT NOT NULL, logical_bytes INTEGER NOT NULL CHECK(logical_bytes >= 0), "
    "context_json TEXT NOT NULL, dedup_key TEXT UNIQUE, fingerprint TEXT NOT NULL)",
    "CREATE TABLE input_media (input_id TEXT NOT NULL REFERENCES inputs(input_id), ordinal "
    "INTEGER NOT NULL, media_id TEXT NOT NULL REFERENCES media(media_id), media_type TEXT NOT "
    "NULL, name TEXT, PRIMARY KEY(input_id, ordinal))",
    "CREATE TABLE pending_inputs (sequence INTEGER PRIMARY KEY AUTOINCREMENT, input_id TEXT "
    "UNIQUE NOT NULL REFERENCES inputs(input_id))",
    *execution_storage.SCHEMA,
    *model_storage.SCHEMA,
    *activity_storage.SCHEMA,
    *subject_storage.SCHEMA,
    *evidence_storage.SCHEMA,
    *maintenance_storage.SCHEMA,
    *time_storage.SCHEMA,
    *feedback_storage.SCHEMA,
    *control_storage.SCHEMA,
    *audio_storage.SCHEMA,
    *archive.SCHEMA,
    *notebook.SCHEMA,
)


def initialize(connection: sqlite3.Connection) -> None:
    """初始化空库或原子升级格式16；普通重开不执行全库检查。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。

    Raises:
        StorageError: 权威记录缺失、版本不一致、载荷无效或提交条件不成立。

    Note:
        格式17旧库首次重开时补齐派生查询索引，后续只检查存在性。
        索引、恢复和升级共用事务；未知格式或后置检查失败保留原库。
    """
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
        elif version == 16 and identity == _APPLICATION_ID:
            _migrate_notebook(connection)
        elif version != _FORMAT_VERSION or identity != _APPLICATION_ID:
            raise StorageError("数据库不是支持的 Engine 格式。", code="unsupported_format")

        # 原文反查必须先定位来源再核验正文；不能在每次前台刷新或每条续页时
        # 扫描全库 JSON 和范围关系。派生索引同样覆盖格式16升级后的真实历史。
        for statement in _QUERY_INDEXES:
            connection.execute(statement)

        # 手机账本是可选接入的附加事实表；与原库恢复共用当前事务。
        for statement in phone_storage.SCHEMA:
            connection.execute(statement)

        if version != _FORMAT_VERSION:
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
        evidence_storage.initialize(connection)
        maintenance_storage.initialize(connection)
        time_storage.initialize(connection)
        feedback_storage.initialize(connection)
        analysis_work.initialize(connection)
        archive.initialize(connection)
        behavior_review.initialize(connection)
        control_storage.initialize(connection)
        connection.execute(
            "SELECT sequence,plugin_id,identity,fact,occurred_at,payload_json FROM audio_facts "
            "LIMIT 0"
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


def _migrate_notebook(connection: sqlite3.Connection) -> None:
    """在外层事务内移除旧认识，保留原文、回执、会话与行为评审关系。

    Ordering: 先删子表和记忆用途关系，再删批次；证据片段不随旧认识删除。
    旧会话正文和请求审计作为历史保留，不再被解码为可运行的记忆会话。
    """
    for table in (
        "memory_sources",
        "memory_conflicts",
        "reflection_writes",
        "memories",
        "pending_knowledge",
        "feedback_reflection_attempts",
        "feedback_reflection_epochs",
        "feedback_reflection_cursors",
    ):
        connection.execute(f"DROP TABLE {table}")
    connection.execute("DELETE FROM knowledge_cursors WHERE kind='memory'")
    for table in ("knowledge_index", "pending_knowledge_index", "knowledge_search"):
        connection.execute(f"DROP TABLE {table}")
    for table in ("analysis_work", "analysis_claims", "analysis_policy"):
        connection.execute(f"DELETE FROM {table} WHERE purpose='memory'")
    # Ordering: 先删除记忆批次的子关系，再删批次；行为评审关系保持原样。
    predicate = (
        "SELECT batch_id FROM reflection_batches WHERE coalesce(json_extract(payload_json,"
        "'$.purpose'),'memory')='memory'"
    )
    connection.execute(
        "DELETE FROM reflection_delivery_slices WHERE delivery_id IN(SELECT delivery_id FROM "
        "reflection_deliveries WHERE batch_id IN(" + predicate + "))"
    )
    connection.execute(
        "DELETE FROM reflection_delivery_media WHERE delivery_id IN(SELECT delivery_id FROM "
        "reflection_deliveries WHERE batch_id IN(" + predicate + "))"
    )
    for table in ("reflection_deliveries", "reflection_presented", "knowledge_review_ranges"):
        connection.execute(f"DELETE FROM {table} WHERE batch_id IN(" + predicate + ")")
    connection.execute("DELETE FROM reflection_batches WHERE batch_id IN(" + predicate + ")")
    for identity, version, payload, receipt in connection.execute(
        "SELECT batch_id,version,payload_json,receipt_json FROM reflection_batches"
    ).fetchall():

        def clean(value: str) -> str:
            record = json.loads(value)
            for candidate in (record, record.get("receipt")):
                if candidate is None:
                    continue
                candidate.pop("written", None)
                submission = candidate.get("submission")
                if submission is not None:
                    submission.pop("abandoned_writes", None)
                    for review in submission.get("reviews", []):
                        review.pop("memory_decision", None)
            return json.dumps(record, ensure_ascii=False)

        connection.execute(
            "UPDATE reflection_batches SET payload_json=?,receipt_json=? WHERE batch_id=? AND "
            "version=?",
            (clean(payload), None if receipt is None else clean(receipt), identity, version),
        )
    for identity, version in connection.execute(
        "SELECT evidence_id,version FROM knowledge_evidence"
    ).fetchall():
        evidence_storage.get_evidence(connection, identity, version=version, include_inactive=True)
    for (encoded,) in connection.execute("SELECT evidence_json FROM model_requests").fetchall():
        model_storage.decode_request(encoded)
    for statement in notebook.SCHEMA:
        connection.execute(statement)
