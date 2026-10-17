"""验证格式12/13到14的显式迁移原子保留旧事实，失败保持原版本和实际数据。

基准库由当前 Store 在临时目录生成，关闭后去掉新增表并标为冻结格式12。
比较覆盖全部原表、FTS影子表、SQLite序号和原始JSON/BLOB；基准样本先结案，
后置拒绝样本另外核对恢复的原子回退。所有故障只在关闭后的隔离样本注入。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from purrcept_engine.domain.activities.models import ActivityCommand, ActivityRecord
from purrcept_engine.domain.inputs.models import InputSource, InputSubmission, MediaAsset
from purrcept_engine.domain.knowledge.models import (
    EvidenceSnapshot,
    MemoryConflictTarget,
    MemoryPatch,
)
from purrcept_engine.domain.model_requests.models import AttemptStart, RequestEvidence, RequestScope
from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite import activities as activity_storage
from purrcept_engine.infrastructure.sqlite import execution as execution_storage
from purrcept_engine.infrastructure.sqlite import model_requests as request_storage
from purrcept_engine.infrastructure.sqlite.store import Store

type SqlValue = int | float | str | bytes | None
type StoredCell = tuple[str, SqlValue]
type StoredRows = tuple[tuple[StoredCell, ...], ...]
type SchemaRow = tuple[str, str, str, str | None]


@dataclass(frozen=True)
class Snapshot:
    """完整已关闭样本的可核对状态；类型、正文和BLOB不经重新序列化。"""

    version: int
    application_id: int
    schema: tuple[SchemaRow, ...]
    rows: dict[str, StoredRows]


def snapshot(path: Path) -> Snapshot:
    """逐表读取实际值，忽略行扫描顺序但保留值的类型及完整字节。

    包含内部 sqlite_sequence 与FTS影子表，防止只比较业务投影掩盖关系或
    派生索引丢失。名称来自实际sqlite_master，引用转义仅用于原表完整读取。
    """
    with closing(sqlite3.connect(path)) as connection:
        schema: list[SchemaRow] = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        rows: dict[str, StoredRows] = {}
        for kind, name, _, _ in schema:
            if kind != "table":
                continue
            identifier = '"' + name.replace('"', '""') + '"'
            values: list[tuple[SqlValue, ...]] = connection.execute(
                f"SELECT * FROM {identifier}"
            ).fetchall()
            rows[name] = tuple(
                sorted(
                    (tuple((type(value).__name__, value) for value in row) for row in values),
                    key=repr,
                )
            )
        version: tuple[int, ...] = connection.execute("PRAGMA user_version").fetchone()
        identity: tuple[int, ...] = connection.execute("PRAGMA application_id").fetchone()
        return Snapshot(version[0], identity[0], tuple(schema), rows)


async def refused_open(path: Path) -> StorageError:
    """核对真实启动拒绝；实现若意外成功也收回其SQLite线程再让断言失败。"""
    opened: Store | None = None
    try:
        with pytest.raises(StorageError) as failed:
            opened = await Store.open(path)
    finally:
        if opened is not None:
            await opened.close()
    return failed.value


# 冻结的旧表定义独立于当前 DDL，避免升级测试无意中使用新版表形状。
_PROCESSING_V13 = (
    "CREATE TABLE input_processing (input_id TEXT PRIMARY KEY REFERENCES inputs(input_id), "
    "version INTEGER NOT NULL, stage TEXT NOT NULL, reason TEXT NOT NULL, "
    "attention_request_id TEXT, due_at REAL, handed_off INTEGER NOT NULL DEFAULT 0, "
    "admitted_at REAL NOT NULL)"
)
_ASSESSMENT_V13 = (
    "CREATE TABLE minimum_wake_assessments (input_id TEXT PRIMARY KEY REFERENCES inputs(input_id), "
    "owner_id TEXT NOT NULL, observed_generation INTEGER NOT NULL, input_version INTEGER NOT NULL, "
    "state TEXT NOT NULL, level TEXT, reason TEXT, request_id TEXT)"
)


def restore_old_subject_schema(connection: sqlite3.Connection, *, version: int = 12) -> None:
    """仅在关闭后的隔离样本恢复冻结旧表，真实原文和其余账本保持原值。"""
    rows = connection.execute(
        "SELECT input_id,version,stage,reason,attention_request_id,admitted_at "
        "FROM input_processing"
    ).fetchall()
    connection.execute("DROP TABLE minimum_wake_assessments")
    connection.execute("DROP TABLE input_processing")
    connection.execute(_PROCESSING_V13)
    connection.executemany(
        "INSERT INTO input_processing(input_id,version,stage,reason,"
        "attention_request_id,admitted_at) "
        "VALUES(?,?,?,?,?,?)",
        rows,
    )
    if version == 13:
        connection.execute(_ASSESSMENT_V13)
        connection.execute(
            "CREATE INDEX minimum_wake_pending ON minimum_wake_assessments(owner_id,state)"
        )
    connection.execute(f"PRAGMA user_version={version}")


@pytest.fixture
async def old_database(tmp_path: Path) -> AsyncIterator[Path]:
    """生成原文、共享BLOB、待办、回执历史和未决认识，所有在途状态先结案。"""
    path = tmp_path / "engine.sqlite3"
    store = await Store.open(path)
    now = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
    asset = MediaAsset(
        b"\x00\xff\xfe\x80\x01\n\r\x00binary", "application/octet-stream", "原文.bin"
    )
    try:
        first = await store.admit(
            InputSubmission(
                InputSource("qq", "100", "group:123", "message", "event-one", "456"),
                "已处置原文，含完整共享媒体。",
                now,
                now,
                (asset,),
            )
        )
        await store.complete_input(first.input_id)
        await store.admit(
            InputSubmission(
                InputSource("qq", "100", "group:123", "message", "event-two", "456"),
                "尚未处置原文，沿用同一字节。",
                now,
                now,
                (MediaAsset(asset.content, "image/example", "第二次引用"),),
            )
        )
        await store.begin_action(
            "action",
            catalog_version="catalog",
            source="explicit local action",
            input_id=first.input_id,
        )
        await store.begin_call(
            "call",
            action_id="action",
            sequence=1,
            path="me.phone.qq.send",
            target_id="qq",
            arguments_json='{"text":"原始调用参数","target_id":"123"}',
        )
        await store.mark_call_started("call")
        await store.record_call_result(
            "call", state="unknown", result_json=None, error_type="TimeoutError", error="未获确认"
        )
        await store.finish_action(
            "action",
            state="failed",
            result_json=None,
            output="已发生部分效果",
            error="脚本后续失败",
        )
        await store.record_call_result(
            "call",
            state="confirmed",
            result_json='{"message_id":-123,"raw":"迟到真实确认"}',
            error_type=None,
            error=None,
        )
        original = await store.get_input(first.input_id)
        assert original is not None
        await store.knowledge.put_evidence(
            EvidenceSnapshot(
                evidence_id="source",
                version=1,
                kind="input",
                text="原文有两个未决的解释，不能仅凭相似度解决。",
                source_json='{"adapter":"qq","native_event":"event-one"}',
                occurred_at=now.timestamp(),
                media=original.media,
            )
        )
        page = await store.knowledge.read_evidence("source")
        assert page.slice is not None
        reference = page.slice.slice_ref
        batch = await store.knowledge.begin_reflection("batch", (reference,))
        await store.knowledge.mark_presented(batch.batch_id, batch.version, (reference,))
        for identity, content in (
            ("left", "倾向原解释。"),
            ("right", "另一解释与其冲突，仍未决。"),
        ):
            await store.knowledge.revise_memory(
                batch.batch_id,
                batch.version,
                MemoryPatch(
                    id=identity,
                    content=content,
                    sources=(reference,),
                    conflicts=(MemoryConflictTarget(kind="memory", id="left", version=1),)
                    if identity == "right"
                    else (),
                ),
            )
        await store.knowledge.abandon_reflection(batch.batch_id, batch.version, "样本主动结案")
        assert await store.knowledge.refresh_keyword_index() > 0
    finally:
        await store.close()

    with closing(sqlite3.connect(path)) as connection, connection:
        restore_old_subject_schema(connection)
        # 格式12已有关系表，但修订信封还没有 conflicts 字段。恢复历史形状
        # 可以同时核对新增模型字段的默认值不会改写已有JSON或隐藏旧关系。
        connection.execute(
            "UPDATE memories SET payload_json=json_remove(payload_json,'$.conflicts')"
        )
        connection.execute("PRAGMA user_version=12")
    before = snapshot(path)
    assert before.version == 12
    assert len(before.rows["inputs"]) == len(before.rows["input_media"]) == 2
    assert len(before.rows["media"]) == len(before.rows["pending_inputs"]) == 1
    assert len(before.rows["call_results"]) == len(before.rows["memories"]) == 2
    assert len(before.rows["memory_conflicts"]) == 1
    assert before.rows["knowledge_search"] and before.rows["knowledge_search_data"]
    yield path


async def test_known_format12_upgrade_preserves_all_old_rows_and_reopens_once(
    old_database: Path,
) -> None:
    """实际Store启动完成迁移，原表包括FTS影子与历史回执逐行保持。"""
    before = snapshot(old_database)
    store = await Store.open(old_database)
    try:
        pending = await store.pending()
        assert len(pending) == 1 and pending[0].source.native_event_id == "event-two"
        assert (
            await store.get_media(pending[0].media[0].media_id)
            == b"\x00\xff\xfe\x80\x01\n\r\x00binary"
        )
        action = await store.get_action("action")
        assert action is not None and action.state == "failed"
        assert [receipt.state for receipt in action.calls[0].history] == ["unknown", "confirmed"]
        left = await store.knowledge.get_memory("left")
        assert left is not None and left.conflicts[0].id == "right"
    finally:
        await store.close()
    after = snapshot(old_database)
    assert after.version == 14 and after.application_id == before.application_id
    assert {name: after.rows[name] for name in before.rows if name != "input_processing"} == {
        name: rows for name, rows in before.rows.items() if name != "input_processing"
    }
    expected_processing = tuple(
        tuple(cell for index, cell in enumerate(row) if index not in (5, 6))
        for row in before.rows["input_processing"]
    )
    assert after.rows["input_processing"] == expected_processing
    assert set(after.rows) - set(before.rows) == {"minimum_wake_assessments"}
    assert after.rows["minimum_wake_assessments"] == ()
    added_schema = set(after.schema) - set(before.schema)
    assert {row[1] for row in added_schema} == {
        "minimum_wake_assessments",
        "minimum_wake_pending",
        "sqlite_autoindex_minimum_wake_assessments_1",
        "input_processing",
    }
    assert all(row[2] in ("minimum_wake_assessments", "input_processing") for row in added_schema)
    reopened = await Store.open(old_database)
    await reopened.close()
    assert snapshot(old_database) == after


@pytest.mark.parametrize("damage", ("column", "table", "index", "foreign_key", "check_constraint"))
async def test_invalid_old_schema_or_integrity_refuses_without_migration(
    old_database: Path, damage: str
) -> None:
    """原表缺列/缺表/缺索引或失效关系不被升级掩盖，所有已有事实仍保留。"""
    with closing(sqlite3.connect(old_database)) as connection, connection:
        if damage == "column":
            connection.execute("ALTER TABLE inputs DROP COLUMN context_json")
        elif damage == "table":
            connection.execute("DROP TABLE audio_facts")
        elif damage == "index":
            connection.execute("DROP INDEX actions_input")
        elif damage == "foreign_key":
            connection.execute(
                "INSERT INTO input_media SELECT 'missing-input',0,media_id,"
                "'application/octet-stream',NULL FROM media"
            )
        else:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute("UPDATE inputs SET logical_bytes=-1")
    before = snapshot(old_database)
    failed = await refused_open(old_database)
    assert failed.code == "invalid_record"
    assert snapshot(old_database) == before
    assert "minimum_wake_assessments" not in before.rows


@pytest.mark.parametrize(
    "damage",
    (
        "activity_state",
        "activity_work_json",
        "activity_progress_json",
        "model_start_json",
        "prepared_arguments_json",
    ),
)
async def test_late_business_validation_failure_rolls_back_upgrade_and_prior_recovery(
    old_database: Path, damage: str
) -> None:
    """迁移后的业务读取拒绝同样回退版本与数据，不能只保护新增DDL失败。

    模型启动损坏样例还含一项合法 prepared 调用：模型初始化前已经执行的
    调用恢复也必须撤回，避免拒绝打开却留下部分 host_interrupted 回执。
    """
    with closing(sqlite3.connect(old_database)) as connection:
        if damage.startswith("activity_"):
            record = ActivityRecord(
                "activity",
                "fixture",
                "保留已暂停活动",
                "paused",
                1,
                1,
                60.0,
                10.0,
                None,
                False,
                False,
                False,
                "activity-command",
                "{}",
                "{}",
                1.0,
                1.0,
            )
            command = ActivityCommand(
                "activity-command",
                "activity",
                "start",
                "fixture",
                "保留已暂停活动",
                None,
                None,
                "explicit",
                "starting",
                1,
                1,
                "confirmed",
                1.0,
                accepted_at=1.0,
                confirmed_at=1.0,
                actual_state="paused",
                applied=True,
            )
            activity_storage.commit_activity(
                connection, record, expected_version=None, command=command
            )
            column = {
                "activity_state": "state",
                "activity_work_json": "work_json",
                "activity_progress_json": "progress_json",
            }[damage]
            with connection:
                connection.execute(
                    f"UPDATE activities SET {column}=?",
                    ("invalid-state" if column == "state" else "broken-json",),
                )
        else:
            execution_storage.begin_action(
                connection, "recoverable-action", "catalog", "尚未结束的独立行动"
            )
            execution_storage.begin_call(
                connection,
                "prepared-call",
                "recoverable-action",
                1,
                "me.phone.qq.send",
                "qq",
                '{"text":"合法准备、尚未开始"}',
            )
            if damage == "prepared_arguments_json":
                with connection:
                    connection.execute(
                        "UPDATE calls SET arguments_json='broken-json' "
                        "WHERE call_id='prepared-call'"
                    )
            else:
                request_storage.begin_request(
                    connection,
                    RequestEvidence(
                        "request",
                        RequestScope("foreground", "turn"),
                        "generation",
                        "offline-model",
                        '{"payload":{"messages":[]}}',
                        "fixture",
                        1,
                        10.0,
                        10.0,
                    ),
                )
                request_storage.begin_attempt(
                    connection, AttemptStart("request", "attempt", 1, 10.0)
                )
                with connection:
                    connection.execute("UPDATE model_attempts SET start_json='broken-json'")
    before = snapshot(old_database)
    failed = await refused_open(old_database)
    assert failed.code == "invalid_record"
    assert snapshot(old_database) == before
    assert before.version == 12 and "minimum_wake_assessments" not in before.rows


@pytest.mark.parametrize("conflict", ("table", "index"))
async def test_new_object_name_conflict_keeps_original_database(
    old_database: Path, conflict: str
) -> None:
    """同名新表或索引拒绝升级；第二DDL失败也不留下第一DDL创建的表。"""
    with closing(sqlite3.connect(old_database)) as connection, connection:
        if conflict == "table":
            connection.execute("CREATE TABLE minimum_wake_assessments (original TEXT)")
            connection.execute("INSERT INTO minimum_wake_assessments VALUES ('原有冲突对象数据')")
        else:
            connection.execute("CREATE INDEX minimum_wake_pending ON inputs(account_id)")
    before = snapshot(old_database)
    failed = await refused_open(old_database)
    assert isinstance(failed.__cause__, sqlite3.OperationalError)
    assert snapshot(old_database) == before
    assert before.version == 12


async def test_second_ddl_failure_rolls_back_created_table_and_version(
    old_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite授权器只拒第二个索引DDL，实际事务必须回退先前建表和版本。"""
    before = snapshot(old_database)
    original_connect = sqlite3.connect
    denied = threading.Event()

    def authorize(
        action: int,
        first: str | None,
        second: str | None,
        database: str | None,
        trigger: str | None,
    ) -> int:
        del second, database, trigger
        if action == sqlite3.SQLITE_CREATE_INDEX and first == "minimum_wake_pending":
            denied.set()
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def connect(path: str | Path) -> sqlite3.Connection:
        connection = original_connect(path)
        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    failed = await refused_open(old_database)
    assert denied.is_set()
    assert isinstance(failed.__cause__, sqlite3.DatabaseError)
    assert snapshot(old_database) == before
    assert before.version == 12 and "minimum_wake_assessments" not in before.rows


@pytest.mark.parametrize("version", (0, 11, 15, 999))
async def test_unknown_database_version_is_not_changed(old_database: Path, version: int) -> None:
    """只有明确格式12获得局部升级，已有数据的未知版本不能按空库初始化。"""
    with closing(sqlite3.connect(old_database)) as connection, connection:
        connection.execute(f"PRAGMA user_version={version}")
    before = snapshot(old_database)
    failed = await refused_open(old_database)
    assert failed.code == "unsupported_format"
    assert snapshot(old_database) == before


async def test_format13_pending_defer_migrates_once_and_assessment_history_is_retained(
    tmp_path: Path,
) -> None:
    """未执行延期升级为保底唤醒，旧 observe 终态不重放，原升级证据逐字保留。"""
    from purrcept_engine.domain.inputs.models import InputContext

    path = tmp_path / "format13.sqlite3"
    store = await Store.open(path)
    now = datetime.now(UTC)
    delayed = await store.admit(
        InputSubmission(InputSource("test", "a", "r", "m", "delay"), "延期原文", now, now)
    )
    observed = await store.admit(
        InputSubmission(InputSource("test", "a", "r", "m", "observed"), "旧记录", now, now)
    )
    direct = await store.admit(
        InputSubmission(
            InputSource("test", "a", "r", "m", "high"),
            "已保底输入",
            now,
            now,
            context=InputContext(minimum_wake=True),
        )
    )
    await store.complete_input(observed.input_id)
    await store.close()
    with closing(sqlite3.connect(path)) as connection, connection:
        restore_old_subject_schema(connection, version=13)
        connection.execute(
            "UPDATE input_processing SET stage='deferred',due_at=9999999999,reason='old.defer' "
            "WHERE input_id=?",
            (delayed.input_id,),
        )
        connection.execute(
            "UPDATE input_processing SET stage='foreground' WHERE input_id=?", (direct.input_id,)
        )
        connection.execute(
            "INSERT INTO minimum_wake_assessments "
            "VALUES(?, 'old-owner', 7, 1, 'pending', NULL, '原始升级观察', NULL)",
            (direct.input_id,),
        )
    before = snapshot(path)
    store = await Store.open(path)
    try:
        assert not await store.minimum_wake_assessments("old-owner")
        records = await store.take_foreground_inputs()
        assert tuple(record.input_id for record in records) == (delayed.input_id, direct.input_id)
        assert not await store.take_foreground_inputs()
        state = await store.input_processing(delayed.input_id)
        assert state is not None and "9999999999" in state.reason
        state = await store.input_processing(observed.input_id)
        assert state is not None and state.stage == "processed"
    finally:
        await store.close()
    after = snapshot(path)
    assert after.version == 14
    assert after.rows["minimum_wake_assessments_v13"] == before.rows["minimum_wake_assessments"]
    assert after.rows["inputs"] == before.rows["inputs"]
