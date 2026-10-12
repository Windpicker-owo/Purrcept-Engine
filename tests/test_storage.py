"""用真实临时 SQLite 验证接管事实、容量、故障和线程收尾边界。"""

import asyncio
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from purrcept_engine.errors import StorageError
from purrcept_engine.storage import (
    InputSource,
    InputSubmission,
    MediaAsset,
)
from purrcept_engine.storage.store import Store

_TIME = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)


def submission(
    event_id: str | None = "event-1",
    *,
    text: str = "原文",
    media: tuple[MediaAsset, ...] = (),
) -> InputSubmission:
    """创建带原生身份的可重放事实，不依赖真实账号或用户数据库。"""
    return InputSubmission(
        source=InputSource("qq-1", "test-account", "group:1", "message", event_id, "sender-1"),
        text=text,
        occurred_at=_TIME,
        received_at=_TIME + timedelta(seconds=1),
        media=media,
    )


def table_counts(path: Path) -> tuple[int, int, int, int]:
    """从独立连接核对事务结果，避免只验证服务的内存投影。"""
    with sqlite3.connect(path) as connection:
        values: list[int] = []
        for table in ("inputs", "media", "input_media", "pending_inputs"):
            row: tuple[int, ...] = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
            values.append(row[0])
        return values[0], values[1], values[2], values[3]


@pytest.mark.asyncio
async def test_admission_media_dedup_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    first_asset = MediaAsset(b"shared media", "image/png", "first.png")
    second_asset = MediaAsset(first_asset.content, "application/octet-stream", "second.bin")
    store = await Store.open(path)
    try:
        first = await store.admit(submission(media=(first_asset,)))
        second = await store.admit(submission("event-2", media=(second_asset,)))
        repeated = await store.admit(
            replace(submission(media=(first_asset,)), received_at=_TIME + timedelta(minutes=1))
        )
        assert repeated.input_id == first.input_id
        assert repeated.duplicate is True
        assert first.input_id != second.input_id
        assert table_counts(path) == (2, 1, 2, 2)

        record = await store.get_input(first.input_id)
        assert record is not None
        assert record.text == "原文"
        assert record.state == "pending"
        assert record.logical_bytes == len("原文".encode()) + len(first_asset.content)
        assert record.media[0].name == "first.png"
        assert await store.get_media(record.media[0].media_id) == first_asset.content
        await store.complete_input(first.input_id)
        await store.complete_input(first.input_id)
        assert tuple(item.input_id for item in await store.pending()) == (second.input_id,)
    finally:
        await store.close()

    reopened = await Store.open(path)
    try:
        history = await reopened.get_input(first.input_id)
        assert history is not None
        assert history.state == "processed"
        assert history.media[0].name == "first.png"
        pending = await reopened.pending()
        assert pending[0].input_id == second.input_id
        assert pending[0].media[0].media_type == "application/octet-stream"
        assert pending[0].media[0].name == "second.bin"
        assert table_counts(path) == (2, 1, 2, 1)
        repeated = await reopened.admit(submission(media=(first_asset,)))
        assert repeated == replace(first, duplicate=True)
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_source_conflict_and_scope_do_not_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    original = submission(media=(MediaAsset(b"original", "image/png"),))
    store = await Store.open(path)
    try:
        first = await store.admit(original)
        for changed in (
            replace(original, text="changed"),
            replace(original, occurred_at=_TIME + timedelta(seconds=1)),
            replace(original, source=replace(original.source, sender_id="sender-2")),
            replace(original, media=(MediaAsset(b"changed", "image/png"),)),
        ):
            with pytest.raises(StorageError) as conflict:
                await store.admit(changed)
            assert conflict.value.code == "source_conflict"
        assert table_counts(path) == (1, 1, 1, 1)
        record = await store.get_input(first.input_id)
        assert record is not None and record.text == original.text

        for source in (
            replace(original.source, adapter_id="qq-2"),
            replace(original.source, account_id="other-account"),
            replace(original.source, conversation_id="group:2"),
            replace(original.source, event_kind="edited_message"),
        ):
            result = await store.admit(replace(original, source=source))
            assert result.duplicate is False
            assert result.input_id != first.input_id
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_no_native_identity_keeps_legal_repetition(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    try:
        first = await store.admit(submission(None))
        second = await store.admit(submission(None))
        assert first.input_id != second.input_id
        assert first.duplicate is False and second.duplicate is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_count_limit_releases_only_on_explicit_completion(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path)
    try:
        first_id = ""
        for index in range(1024):
            result = await store.admit(submission(str(index), text="x"))
            if index == 0:
                first_id = result.input_id
        assert len(await store.pending()) == 8
        assert len(await store.pending(limit=1024)) == 1024
        with pytest.raises(StorageError) as full:
            await store.admit(submission("overflow", text="x"))
        assert full.value.code == "input_capacity"
        assert table_counts(path)[0] == 1024
        assert (await store.admit(submission("0", text="x"))).duplicate is True

        await store.complete_input(first_id)
        next_input = await store.admit(submission("overflow", text="x"))
        assert next_input.duplicate is False
        assert table_counts(path)[0] == 1025
        assert len(await store.pending(limit=1024)) == 1024
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_default_byte_quota_counts_logical_media_not_dedup_size(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    # 同一不可变媒体引用重复出现，逻辑量达到 256 MiB，实际 BLOB 只有 256 KiB。
    # 这同时验证字节边界和去重后磁盘占用不能成为输入额度的计量依据。
    asset = MediaAsset(b"x" * (256 * 1024), "application/octet-stream")
    store = await Store.open(path)
    try:
        first = await store.admit(submission(text="", media=(asset,) * 1024))
        record = await store.get_input(first.input_id)
        assert record is not None and record.logical_bytes == 256 * 1024 * 1024
        assert table_counts(path) == (1, 1, 1024, 1)
        with pytest.raises(StorageError) as full:
            await store.admit(submission("overflow", text="x"))
        assert full.value.code == "input_capacity"
        await store.complete_input(first.input_id)
        await store.admit(submission("overflow", text="x"))
        assert table_counts(path) == (2, 1, 1024, 1)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_media_shared_across_inputs_counts_for_each_input(tmp_path: Path) -> None:
    asset = MediaAsset(b"abcd", "application/octet-stream")
    store = await Store.open(tmp_path / "state.sqlite3", max_input_bytes=8)
    try:
        await store.admit(submission("1", text="", media=(asset,)))
        await store.admit(submission("2", text="", media=(asset,)))
        with pytest.raises(StorageError) as full:
            await store.admit(submission("3", text="x"))
        assert full.value.code == "input_capacity"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_failed_pending_insert_rolls_back_all_facts_and_latches_fault(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path)
    try:
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TRIGGER reject_pending BEFORE INSERT ON pending_inputs "
                "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
            )
        with pytest.raises(StorageError) as failed:
            await store.admit(submission(media=(MediaAsset(b"must roll back", "image/png"),)))
        assert failed.value.code == "storage_failure"
        assert isinstance(failed.value.__cause__, sqlite3.Error)
        assert table_counts(path) == (0, 0, 0, 0)
        assert await store.pending() == ()
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TRIGGER reject_pending")
        with pytest.raises(StorageError) as faulted:
            await store.admit(submission("later"))
        assert faulted.value.code == "storage_failure"
        assert table_counts(path) == (0, 0, 0, 0)
    finally:
        await store.close()

    reopened = await Store.open(path)
    try:
        await reopened.admit(submission("after-recovery"))
        assert table_counts(path) == (1, 0, 0, 1)
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_cancelled_wait_does_not_cancel_accepted_transaction(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path)
    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN IMMEDIATE")
    task = asyncio.create_task(store.admit(submission()))
    try:
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        blocker.rollback()
        # 重投与原事务在同一串行线程执行，取得同一接管，而不是第二条原文。
        result = await store.admit(submission())
        assert result.duplicate is True
        assert table_counts(path) == (1, 0, 0, 1)
    finally:
        blocker.close()
        await store.close()


@pytest.mark.asyncio
async def test_cancelled_close_keeps_drain_and_repeated_close(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path)
    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN IMMEDIATE")
    admission = asyncio.create_task(store.admit(submission()))
    await asyncio.sleep(0)
    closing = asyncio.create_task(store.close())
    try:
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        with pytest.raises(StorageError) as closed:
            await store.admit(submission("late"))
        assert closed.value.code == "store_closed"
        reading = asyncio.create_task(store.get_input("not-yet-present"))
        await asyncio.sleep(0)
        assert reading.done() is False
        blocker.rollback()
        result = await admission
        assert await reading is None
        await store.close()
        await store.close()
        assert table_counts(path) == (1, 0, 0, 1)
    finally:
        blocker.close()
        await store.close()

    reopened = await Store.open(path)
    try:
        assert (await reopened.get_input(result.input_id)) is not None
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_control_has_reserved_queue_space_and_precedes_queued_read(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path, max_inputs=2)
    first = await store.admit(submission())
    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN IMMEDIATE")
    writing = asyncio.create_task(store.admit(submission("second")))
    await asyncio.sleep(0)
    reading = asyncio.create_task(store.get_input(first.input_id))
    await asyncio.sleep(0)
    completion = asyncio.create_task(store.complete_input(first.input_id))
    try:
        await asyncio.sleep(0)
        with pytest.raises(StorageError) as saturated:
            await store.get_input(first.input_id)
        assert saturated.value.code == "store_busy"
        # 控制有独立位置；两个普通请求都在队列/执行中时仍可接管明确处置。
        assert completion.done() is False
        blocker.rollback()
        await writing
        await completion
        observed = await reading
        assert observed is not None and observed.state == "processed"
    finally:
        blocker.close()
        await store.close()


@pytest.mark.asyncio
async def test_connection_and_statements_stay_on_one_owned_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[int] = []
    statements: list[str] = []
    original_connect = sqlite3.connect

    def connect(path: Path) -> sqlite3.Connection:
        observed.append(threading.get_ident())
        connection = original_connect(path)

        def trace(statement: str) -> None:
            observed.append(threading.get_ident())
            statements.append(statement)

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    store = await Store.open(tmp_path / "state.sqlite3")
    try:
        admitted = await store.admit(submission())
        await store.pending()
        await store.get_input(admitted.input_id)
        await store.complete_input(admitted.input_id)
    finally:
        await store.close()
    assert len(set(observed)) == 1
    assert observed[0] != threading.get_ident()
    assert any(statement.startswith("INSERT INTO inputs") for statement in statements)
    assert any(statement.startswith("DELETE FROM pending_inputs") for statement in statements)


@pytest.mark.asyncio
async def test_cancelled_open_waits_for_connection_cleanup(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    workers: list[threading.Thread] = []

    class SlowStore(Store):
        @staticmethod
        def _initialize(connection: sqlite3.Connection) -> None:
            workers.append(threading.current_thread())
            entered.set()
            release.wait(2)
            Store._initialize(connection)

    opening = asyncio.create_task(SlowStore.open(tmp_path / "state.sqlite3"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        opening.cancel()
        await asyncio.sleep(0)
        assert opening.done() is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await opening
        await asyncio.to_thread(workers[0].join, 2)
        assert workers[0].is_alive() is False
    finally:
        release.set()

    reopened = await Store.open(tmp_path / "state.sqlite3")
    await reopened.close()


@pytest.mark.asyncio
async def test_future_format_and_non_engine_database_are_not_replaced(tmp_path: Path) -> None:
    for future in (False, True):
        path = tmp_path / f"foreign-{future}.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE preserved (value TEXT)")
            connection.execute("INSERT INTO preserved VALUES ('original')")
            if future:
                connection.execute("PRAGMA application_id = 1347768901")
                connection.execute("PRAGMA user_version = 999")
        with pytest.raises(StorageError) as failed:
            await Store.open(path)
        assert failed.value.code == "unsupported_format"
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT value FROM preserved").fetchone() == ("original",)


@pytest.mark.asyncio
async def test_corrupt_database_is_not_replaced(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    original = b"not a sqlite database"
    path.write_bytes(original)
    with pytest.raises(StorageError) as failed:
        await Store.open(path)
    assert failed.value.code == "storage_failure"
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_broken_media_reference_is_not_silently_omitted(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path)
    first = await store.admit(submission(media=(MediaAsset(b"original", "image/png"),)))
    try:
        with sqlite3.connect(path) as connection:
            connection.execute("DELETE FROM media")
        with pytest.raises(StorageError) as invalid:
            await store.get_input(first.input_id)
        assert invalid.value.code == "invalid_record"
        with pytest.raises(StorageError):
            await store.admit(submission("later"))
    finally:
        await store.close()

    with pytest.raises(StorageError) as failed:
        await Store.open(path)
    assert failed.value.code == "invalid_record"
    assert table_counts(path) == (1, 0, 1, 1)


@pytest.mark.asyncio
async def test_invalid_unicode_rejects_without_stranding_thread(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path)
    try:
        for malformed in (
            submission(text="\ud800"),
            replace(submission(), source=replace(submission().source, display_name="\ud800")),
            submission(media=(MediaAsset(b"content", "image/png", "\ud800"),)),
        ):
            with pytest.raises(StorageError) as failed:
                await store.admit(malformed)
            assert failed.value.code == "invalid_input"
        assert table_counts(path) == (0, 0, 0, 0)
        await store.admit(submission("valid"))
        assert table_counts(path) == (1, 0, 0, 1)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_configuration_and_batch_boundaries_reject_bool_and_zero(tmp_path: Path) -> None:
    for limit in (True, 0, -1):
        with pytest.raises(ValueError):
            await Store.open(tmp_path / "state.sqlite3", max_inputs=limit)
        with pytest.raises(ValueError):
            await Store.open(tmp_path / "state.sqlite3", max_input_bytes=limit)
    store = await Store.open(tmp_path / "state.sqlite3", max_inputs=1)
    try:
        for limit in (True, 0, -1):
            with pytest.raises(ValueError):
                await store.pending(limit=limit)
        await store.admit(submission())
        assert len(await store.pending()) == 1
    finally:
        await store.close()


def test_submission_rejects_naive_time_and_empty_identity() -> None:
    with pytest.raises(ValueError):
        InputSource("", "account", "conversation", "message")
    with pytest.raises(ValueError):
        replace(submission(), occurred_at=datetime(2026, 10, 12))
    with pytest.raises(ValueError):
        replace(submission(), received_at=datetime(2026, 10, 12))


async def test_internal_thread_error_reaches_all_waiters_and_closes_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """程序错误不变成业务拒收，也不能令当前或排队的交接永久悬挂。"""
    entered = threading.Event()
    release = threading.Event()

    def broken_admit(
        store: Store, connection: sqlite3.Connection, item: InputSubmission, logical_bytes: int
    ) -> object:
        del store, connection, item, logical_bytes
        entered.set()
        assert release.wait(5)
        raise RuntimeError("synthetic internal failure")

    store = await Store.open(tmp_path / "state.sqlite3")
    monkeypatch.setattr(Store, "_admit", broken_admit)
    first = asyncio.create_task(store.admit(submission("first")))
    await asyncio.to_thread(entered.wait, 5)
    second = asyncio.create_task(store.admit(submission("second")))
    await asyncio.sleep(0)
    release.set()
    for waiter in (first, second):
        with pytest.raises(RuntimeError, match="synthetic internal failure"):
            async with asyncio.timeout(5):
                await waiter
    await store.close()
    with pytest.raises(StorageError) as failed:
        await store.admit(submission("later"))
    assert failed.value.code == "store_closed"
