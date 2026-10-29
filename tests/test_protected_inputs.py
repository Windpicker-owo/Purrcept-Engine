"""验证未决输入保护从业务事实出发，不随已结案历史形成逐输入关联扫描。

在正式 Store 的数据库线程构造大批已承接失败及少量未决事实，以虚拟机指令
额度验证完整 subject_work 查询成本，避免依赖机器速度。测试只借用公开
账本入口，始终归还线程；不启动模型或设备。
"""

import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

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
