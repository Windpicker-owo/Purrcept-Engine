"""用真实临时 SQLite 核验执行意图、回执版本和故障恢复边界。

测试只操作本次生成的数据库，不执行 Provider 或 Worker。崩溃窗口通过保留
未终态记录后重开模拟；独立连接和故障触发器核对实际提交，而非内存投影。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path
from typing import cast

import pytest

import purrcept_engine.domain.execution.models as execution_models
from purrcept_engine.errors import StorageError
from purrcept_engine.storage import execution as execution_storage
from purrcept_engine.storage.store import Store


async def _prepare(store: Store, call_id: str, *, sequence: int = 0) -> None:
    """保存同一测试行动中的完整能力绑定及原参数表示。"""
    await store.begin_call(
        call_id,
        action_id="action",
        sequence=sequence,
        path="me.console.say",
        target_id="console",
        arguments_json='{"text": "visible effect"}',
    )


async def _result(store: Store, call_id: str, state: execution_models.CallOutcome) -> None:
    """保存合成结论，不通过 Worker 自报或任何外部服务取得效果。"""
    await store.record_call_result(
        call_id,
        state=state,
        result_json='{"receipt":"platform-confirmed"}' if state == "confirmed" else None,
        error_type=None if state == "confirmed" else "synthetic",
        error=None if state == "confirmed" else "reason",
    )


async def test_unknown_resolution_preserves_history_and_pending_versions(tmp_path: Path) -> None:
    """迟到确认澄清未知，但两份事实仍各有可靠交接身份。"""
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path)
    try:
        await store.begin_action(
            "action", catalog_version="catalog-1", source="await me.console.say()"
        )
        await _prepare(store, "call")
        await store.mark_call_started("call")
        await _result(store, "call", "unknown")
        await store.finish_action(
            "action",
            state="failed",
            result_json=None,
            output="before failure",
            error="worker failed",
        )
        await _result(store, "call", "confirmed")
        await _result(store, "call", "confirmed")

        action = await store.get_action("action")
        assert action is not None and action.state == "failed"
        assert action.output == "before failure" and action.error == "worker failed"
        call = action.calls[0]
        assert call.state == "confirmed" and call.receipt_revision == 2
        assert [item.state for item in call.history] == ["unknown", "confirmed"]
        assert call.history[0].error == "reason"
        assert call.arguments_json == '{"text": "visible effect"}'
        assert call.started_at is not None and call.finished_at is not None

        pending = await store.pending_call_receipts()
        assert [(item.call_id, item.receipt_revision, item.state) for item in pending] == [
            ("call", 1, "unknown"),
            ("call", 2, "confirmed"),
        ]
        assert len(pending[0].history) == 1
        assert pending == await store.pending_call_receipts()
        with pytest.raises(StorageError) as conflict:
            await _result(store, "call", "failed")
        assert conflict.value.code == "receipt_conflict"
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT count(*) FROM call_results").fetchone() == (2,)
            assert connection.execute("SELECT count(*) FROM pending_call_receipts").fetchone() == (
                2,
            )
    finally:
        await store.close()

    reopened = await Store.open(path)
    try:
        retained = await reopened.get_action("action")
        assert retained is not None and retained.state == "failed"
        assert retained.calls[0].history == call.history
        assert await reopened.pending_call_receipts() == pending
    finally:
        await reopened.close()


async def test_late_unknown_protocol_material_has_its_own_receipt_revision(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    try:
        await store.begin_action("action", catalog_version="catalog", source="return None")
        await _prepare(store, "call")
        await store.mark_call_started("call")
        await _result(store, "call", "unknown")
        first = (await store.pending_call_receipts())[0]
        late = await store.record_call_result(
            "call",
            state="unknown",
            result_json='{"echo":"real-response"}',
            error_type="ProtocolUnconfirmed",
            error="platform did not confirm effect",
        )
        repeated = await store.record_call_result(
            "call",
            state="unknown",
            result_json='{"echo":"real-response"}',
            error_type="ProtocolUnconfirmed",
            error="platform did not confirm effect",
        )
        assert late == repeated and late.receipt_revision == 2
        assert late.history[0].result_json is None
        assert late.history[1].result_json == '{"echo":"real-response"}'
        assert (await store.pending_call_receipts())[0] == first
    finally:
        await store.close()


async def test_reopen_distinguishes_prepared_started_and_confirmed(tmp_path: Path) -> None:
    """重开只结算有依据的状态，缺失回执不能成为自动重发许可。"""
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path)
    await store.begin_action("action", catalog_version="catalog-1", source="original source")
    await _prepare(store, "prepared", sequence=0)
    await _prepare(store, "started", sequence=1)
    await store.mark_call_started("started")
    await _prepare(store, "confirmed", sequence=2)
    await store.mark_call_started("confirmed")
    await _result(store, "confirmed", "confirmed")
    await store.close()

    reopened = await Store.open(path)
    try:
        action = await reopened.get_action("action")
        assert action is not None and action.state == "failed"
        assert action.source == "original source" and action.error == "host_interrupted"
        assert [(item.call_id, item.state) for item in action.calls] == [
            ("prepared", "not_started"),
            ("started", "unknown"),
            ("confirmed", "confirmed"),
        ]
        assert action.calls[0].started_at is None
        assert action.calls[1].started_at is not None
        assert all(item.receipt_revision == 1 for item in action.calls)
        with pytest.raises(StorageError) as inactive:
            await reopened.mark_call_started("prepared")
        assert inactive.value.code == "call_inactive"
        pending = await reopened.pending_call_receipts()
        assert len(pending) == 3
    finally:
        await reopened.close()
    again = await Store.open(path)
    try:
        assert await again.pending_call_receipts() == pending
    finally:
        await again.close()


async def test_action_failure_never_rolls_back_prior_confirmed_call(tmp_path: Path) -> None:
    """行动取消与逐次效果是独立权威事实，不能整轮回滚。"""
    store = await Store.open(tmp_path / "state.sqlite3")
    try:
        await store.begin_action("action", catalog_version="catalog-1", source="source")
        await _prepare(store, "first", sequence=0)
        await store.mark_call_started("first")
        await _result(store, "first", "confirmed")
        await _prepare(store, "second", sequence=1)
        await _result(store, "second", "not_started")
        await store.finish_action(
            "action", state="cancelled", result_json=None, output="partial", error=None
        )
        action = await store.get_action("action")
        assert action is not None and action.state == "cancelled"
        assert [call.state for call in action.calls] == ["confirmed", "not_started"]
        assert len(await store.pending_call_receipts()) == 2
    finally:
        await store.close()


async def test_cancelled_result_wait_keeps_accepted_commit(tmp_path: Path) -> None:
    """取消等待的提交所有权仍在线程，查询能取回其实际结果。"""
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path)
    blocker = sqlite3.connect(path)
    try:
        await store.begin_action("action", catalog_version="catalog-1", source="source")
        await _prepare(store, "call")
        await store.mark_call_started("call")
        blocker.execute("BEGIN IMMEDIATE")
        waiting = asyncio.create_task(_result(store, "call", "confirmed"))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        blocker.rollback()
        action = await store.get_action("action")
        assert action is not None and action.calls[0].state == "confirmed"
        assert len(await store.pending_call_receipts()) == 1
    finally:
        blocker.close()
        await store.close()


async def test_result_and_pending_identity_rollback_together_on_sql_failure(tmp_path: Path) -> None:
    """可靠交接失败时整个回执事务失败，不留下不可投递的假确认。"""
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path)
    try:
        await store.begin_action("action", catalog_version="catalog-1", source="source")
        await _prepare(store, "call")
        await store.mark_call_started("call")
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TRIGGER receipt_fault BEFORE INSERT ON pending_call_receipts "
                "BEGIN SELECT RAISE(ABORT, 'synthetic receipt write failure'); END"
            )
        with pytest.raises(StorageError):
            await _result(store, "call", "confirmed")
        action = await store.get_action("action")
        assert action is not None and action.calls[0].state == "started"
        assert action.calls[0].receipt_revision == 0 and action.calls[0].history == ()
        assert await store.pending_call_receipts() == ()
        with pytest.raises(StorageError):
            await store.mark_call_started("call")
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT count(*) FROM call_results").fetchone() == (0,)
    finally:
        await store.close()


async def test_authoritative_fault_revokes_already_queued_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """首写故障撤销已排队写入资格，后续行动不能借旧入队继续启动。"""
    path = tmp_path / "state.sqlite3"
    entered = threading.Event()
    release = threading.Event()
    original = execution_storage.begin_call

    def slow_call(
        connection: sqlite3.Connection,
        call_id: str,
        action_id: str,
        sequence: int,
        path: str,
        target_id: str,
        arguments_json: str,
    ) -> None:
        entered.set()
        assert release.wait(5)
        original(connection, call_id, action_id, sequence, path, target_id, arguments_json)

    store = await Store.open(path)
    try:
        await store.begin_action("action", catalog_version="catalog-1", source="source")
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TRIGGER call_fault BEFORE INSERT ON calls "
                "BEGIN SELECT RAISE(ABORT, 'synthetic intent write failure'); END"
            )
        monkeypatch.setattr(execution_storage, "begin_call", slow_call)
        failing = asyncio.create_task(_prepare(store, "call"))
        assert await asyncio.to_thread(entered.wait, 5)
        queued = asyncio.create_task(
            store.begin_action("queued", catalog_version="catalog-1", source="source")
        )
        await asyncio.sleep(0)
        release.set()
        for waiter in (failing, queued):
            with pytest.raises(StorageError):
                await waiter
        assert await store.get_action("queued") is None
        action = await store.get_action("action")
        assert action is not None and action.calls == ()
    finally:
        release.set()
        await store.close()


async def test_old_engine_format_is_rejected_without_changing_its_data(tmp_path: Path) -> None:
    """新格式不通过新建或隐式迁移掩盖旧库中的既有事实。"""
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA application_id=1347768901")
        connection.execute("PRAGMA user_version=1")
        connection.execute("CREATE TABLE preserved(value TEXT)")
        connection.execute("INSERT INTO preserved VALUES ('existing fact')")
    before = path.read_bytes()
    with pytest.raises(StorageError) as rejected:
        await Store.open(path)
    assert rejected.value.code == "unsupported_format"
    assert path.read_bytes() == before


@pytest.mark.parametrize("invalid", ["NaN", '{"x":Infinity}', '{"x":1e999}', "[]", "not JSON"])
async def test_arguments_boundary_rejects_invalid_or_non_object_json(
    tmp_path: Path, invalid: str
) -> None:
    """非法参数在入队前拒绝，不会使权威线程或后续有效提交故障。"""
    store = await Store.open(tmp_path / "state.sqlite3")
    try:
        await store.begin_action("action", catalog_version="catalog-1", source="source")
        with pytest.raises(ValueError):
            await store.begin_call(
                "call",
                action_id="action",
                sequence=0,
                path="me.console.say",
                target_id="console",
                arguments_json=invalid,
            )
        action = await store.get_action("action")
        assert action is not None and action.calls == ()
        await _prepare(store, "valid")
    finally:
        await store.close()


async def test_control_payload_has_its_own_bounded_bytes(tmp_path: Path) -> None:
    """控制通路可收尾，但携带大代码或结果仍受有限字节额度约束。"""
    store = await Store.open(tmp_path / "state.sqlite3", max_input_bytes=64)
    try:
        with pytest.raises(StorageError) as rejected:
            await store.begin_action("large", catalog_version="catalog", source="x" * 64)
        assert rejected.value.code == "store_busy"
        assert await store.get_action("large") is None
        await store.begin_action("small", catalog_version="catalog", source="pass")
    finally:
        await store.close()


async def test_recovery_failure_does_not_partially_resolve_interrupted_calls(
    tmp_path: Path,
) -> None:
    """启动恢复也是短原子事务，第二条回执故障不能留下第一条假恢复。"""
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path)
    await store.begin_action("action", catalog_version="catalog-1", source="source")
    await _prepare(store, "first", sequence=0)
    await _prepare(store, "second", sequence=1)
    await store.mark_call_started("second")
    await store.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER recovery_fault BEFORE INSERT ON pending_call_receipts "
            "WHEN NEW.call_id='second' BEGIN SELECT RAISE(ABORT, 'synthetic recovery failure'); END"
        )
    with pytest.raises(StorageError):
        await Store.open(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT state FROM calls ORDER BY sequence").fetchall() == [
            ("prepared",),
            ("started",),
        ]
        assert connection.execute("SELECT count(*) FROM call_results").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM pending_call_receipts").fetchone() == (0,)
        connection.execute("DROP TRIGGER recovery_fault")
    recovered = await Store.open(path)
    try:
        assert [item.state for item in await recovered.pending_call_receipts()] == [
            "not_started",
            "unknown",
        ]
    finally:
        await recovered.close()


async def test_invalid_required_text_does_not_latch_an_sql_fault(tmp_path: Path) -> None:
    """缺失必需字段是提交边界错误，不让 NOT NULL 错误误停整个存储。"""
    store = await Store.open(tmp_path / "state.sqlite3")
    try:
        # 故意跨越静态类型，验证实际 Python 调用者不能把 None 交成权威故障。
        with pytest.raises(ValueError):
            await store.begin_action("invalid", catalog_version="catalog-1", source=cast(str, None))
        await store.begin_action("action", catalog_version="catalog-1", source="source")
        with pytest.raises(ValueError):
            await store.finish_action(
                "action", state="failed", result_json=None, output=cast(str, None), error=None
            )
        await store.finish_action(
            "action", state="completed", result_json=None, output="", error=None
        )
    finally:
        await store.close()


async def test_corrupt_start_marker_blocks_recovery_instead_of_inventing_effect(
    tmp_path: Path,
) -> None:
    """终态推导需要真实启动门槛，损坏字段不能被补成成功或未开始。"""
    path = tmp_path / "state.sqlite3"
    store = await Store.open(path)
    await store.begin_action("action", catalog_version="catalog-1", source="source")
    await _prepare(store, "call")
    await store.mark_call_started("call")
    await store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE calls SET started_at=NULL WHERE call_id='call'")
    with pytest.raises(StorageError) as invalid:
        await Store.open(path)
    assert invalid.value.code == "invalid_record"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT state FROM calls").fetchone() == ("started",)
        assert connection.execute("SELECT count(*) FROM call_results").fetchone() == (0,)
