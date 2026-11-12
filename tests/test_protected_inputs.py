"""验证未决输入保护从业务事实出发，不随已结案历史形成逐输入关联扫描。

在正式 Store 的数据库线程构造大批已承接失败及少量未决事实，以虚拟机指令
额度验证完整 subject_work 查询成本，避免依赖机器速度。测试只借用公开
账本入口，始终归还线程；不启动模型或设备。
"""

import sqlite3
import threading
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from purrcept_engine.domain.activities.models import (
    ActivityCommand,
    ActivityRecord,
    CommandKind,
    CommandStatus,
)
from purrcept_engine.infrastructure.sqlite import queue as sqlite_queue
from purrcept_engine.infrastructure.sqlite.schema import initialize
from purrcept_engine.infrastructure.sqlite.store import Store


def _history(connection: sqlite3.Connection, count: int) -> None:
    """构造已经由成功轮次承接的失败历史，原始输入身份和外键保持真实关联。"""
    connection.executemany(
        "INSERT INTO inputs(input_id,adapter_id,account_id,conversation_id,event_kind,"
        "text,occurred_at,received_at,logical_bytes,context_json,fingerprint) "
        "VALUES(?,'test','account','room','message','original',"
        "'2026-10-28T00:00:00+00:00','2026-10-28T00:00:00+00:00',0,'{}',?)",
        ((f"input-{index}", f"fingerprint-{index}") for index in range(count)),
    )
    connection.execute(
        "INSERT INTO subject_turns VALUES('successful','foreground',0,1,'succeeded',1,2,NULL)"
    )
    connection.executemany(
        "INSERT INTO subject_turns VALUES(?,'foreground',0,1,'failed',0,1,'{}')",
        ((f"turn-{index}",) for index in range(count)),
    )
    connection.executemany(
        "INSERT INTO turn_inputs VALUES(?,0,?)",
        ((f"turn-{index}", f"input-{index}") for index in range(count)),
    )
    connection.executemany(
        "INSERT INTO failed_turns VALUES(?,'successful')",
        ((f"turn-{index}",) for index in range(count)),
    )
    connection.commit()


@pytest.mark.parametrize("history_size", [64, 1200])
async def test_protection_scans_unresolved_facts_with_linear_work(
    tmp_path: Path, history_size: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """保护未承接失败和未知效果，已结案历史不得使查询成本退化为平方级。"""
    measuring = threading.Event()
    instructions = 0

    def progress() -> int:
        """只计本次公开查询的工作量；额度拒绝逐输入遍历全部轮次。"""
        nonlocal instructions
        if not measuring.is_set():
            return 0
        instructions += 1000
        return int(instructions > history_size * 200)

    def prepare(connection: sqlite3.Connection) -> None:
        """测试初始化在真实所有者线程完成，进度回调不跨线程借用连接。"""
        initialize(connection)
        _history(connection, history_size)
        connection.execute(
            "UPDATE failed_turns SET presented_in=NULL WHERE turn_id IN ('turn-1','turn-2')"
        )
        connection.commit()
        connection.set_progress_handler(progress, 1000)

    monkeypatch.setattr(sqlite_queue, "initialize", prepare)
    path = tmp_path / "protected.sqlite3"
    store = await Store.open(path)
    try:
        await store.begin_action(
            "unknown-action",
            catalog_version="catalog",
            source="return None",
            input_id="input-2",
        )
        await store.begin_call(
            "unknown-call",
            action_id="unknown-action",
            sequence=0,
            path="me.test.send",
            target_id="test",
            arguments_json="{}",
        )
        await store.mark_call_started("unknown-call")
        await store.record_call_result(
            "unknown-call",
            state="unknown",
            result_json=None,
            error_type="Unconfirmed",
            error="not yet confirmed",
        )

        measuring.set()
        try:
            work = await store.subject_work()
        finally:
            measuring.clear()
        assert isinstance(work["calls"], list) and len(work["calls"]) == 1
        assert await store.protected_input_ids(("input-0", "input-1", "input-2")) == {
            "input-1",
            "input-2",
        }
        assert await store.protected_input_ids(("input-0", "input-2")) == {"input-2"}
        assert await store.protected_input_ids(()) == frozenset()

        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("UPDATE failed_turns SET presented_in='successful'")
        assert await store.protected_input_ids(("input-1", "input-2")) == {"input-2"}
        await store.record_call_result(
            "unknown-call",
            state="confirmed",
            result_json="{}",
            error_type=None,
            error=None,
        )
        assert await store.protected_input_ids(("input-1", "input-2")) == frozenset()
    finally:
        await store.close()


@pytest.mark.parametrize("kind", ["pause", "resume", "adjust", "extend"])
@pytest.mark.parametrize("status", ["confirmed", "rejected", "unknown", "accepted"])
async def test_only_superseded_settled_controls_leave_current_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: CommandKind,
    status: CommandStatus,
) -> None:
    """长活动不永久保护旧控制；计时更新、未知效果、目标和预算仍不能被误退役。"""

    def prepare(connection: sqlite3.Connection) -> None:
        initialize(connection)
        _history(connection, 3)

    monkeypatch.setattr(sqlite_queue, "initialize", prepare)
    store = await Store.open(tmp_path / "controls.sqlite3")
    record = ActivityRecord(
        activity_id="activity",
        definition="test",
        goal="完成长活动",
        state="running",
        version=1,
        run_generation=1,
        granted_seconds=100,
        used_seconds=0,
        checkpoint_at=None,
        clock_running=False,
        timing_incomplete=False,
        needs_reconciliation=False,
        last_command_id="call-0",
        work_json="{}",
        progress_json=None,
        created_at=1,
        updated_at=1,
        control_version=1,
    )
    try:
        controls: tuple[tuple[int, CommandKind, CommandStatus], ...] = (
            (0, "start", "confirmed"),
            (1, kind, status),
            (2, "resume", "confirmed"),
        )
        for index, command_kind, command_status in controls:
            command = ActivityCommand(
                command_id=f"call-{index}",
                activity_id="activity",
                command=command_kind,
                definition="test" if index == 0 else None,
                goal=record.goal,
                additional_seconds=10 if command_kind == "extend" else None,
                reason=None,
                cause="explicit",
                prior_state="paused",
                run_generation=1,
                version=index + 1,
                status=command_status,
                admitted_at=float(index + 1),
                confirmed_at=float(index + 1) if command_status == "confirmed" else None,
                actual_state="running" if command_status == "confirmed" else None,
                applied=True if command_status == "confirmed" else None,
            )
            record = replace(
                record,
                version=index + 1,
                control_version=index + 1,
                last_command_id=command.command_id,
                updated_at=float(index + 1),
            )
            await store.commit_activity(
                record, expected_version=None if index == 0 else index, command=command
            )
            await store.begin_action(
                f"action-{index}",
                catalog_version="catalog",
                source="return None",
                input_id=f"input-{index}",
            )
            await store.begin_call(
                command.command_id,
                action_id=f"action-{index}",
                sequence=0,
                path="me.activity",
                target_id="activity",
                arguments_json="{}",
            )
            await store.mark_call_started(command.command_id)
            await store.record_call_result(
                command.command_id,
                state="confirmed",
                result_json="{}",
                error_type=None,
                error=None,
            )
            await store.finish_action(
                f"action-{index}", state="completed", result_json="{}", output="", error=None
            )
            if index == 1:
                # 同控制代次的进度版本不会让该命令提前成为历史。
                assert "input-1" in await store.protected_input_ids(("input-1",))

        superseded = kind in ("pause", "resume") and status in ("confirmed", "rejected")
        expected = {"input-0", "input-2"} if superseded else {"input-0", "input-1", "input-2"}
        assert await store.protected_input_ids(tuple(expected | {"input-1"})) == expected
        work = await store.subject_work()
        calls = work["calls"]
        assert isinstance(calls, list)
        projected_inputs: list[str] = []
        for call in calls:
            assert isinstance(call, dict)
            identity = call["input_id"]
            assert isinstance(identity, str)
            projected_inputs.append(identity)
        assert set(projected_inputs) == expected
        old = await store.get_activity_command("call-1")
        assert old is not None and old.status == status

        # 最新确认经过普通计时版本推进后仍受保护；control_version没有变化。
        record = replace(record, version=4, updated_at=4)
        await store.commit_activity(record, expected_version=3)
        assert await store.protected_input_ids(("input-2",)) == {"input-2"}

        # 同一旧输入上的独立未知效果仍保护整轮，不能借控制退役将其丢掉。
        await store.begin_action(
            "unknown-action", catalog_version="catalog", source="return None", input_id="input-1"
        )
        await store.begin_call(
            "unknown-call",
            action_id="unknown-action",
            sequence=0,
            path="me.test.send",
            target_id="test",
            arguments_json="{}",
        )
        await store.mark_call_started("unknown-call")
        await store.record_call_result(
            "unknown-call",
            state="unknown",
            result_json=None,
            error_type="Unconfirmed",
            error="awaiting receipt",
        )
        assert await store.protected_input_ids(("input-1",)) == {"input-1"}
    finally:
        await store.close()
