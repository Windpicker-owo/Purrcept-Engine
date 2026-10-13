"""保存本机快捷控制的接管身份与实际业务关联，不执行设备。

同一请求重传返回原控制记录，不能重复派发。页面会话身份属于宿主实例，数据库
记录跨页面断开存续；重启把未结束的页面观察标为中断，保留行动和活动命令关联，
不重放操作。这里的状态不替代执行器真实回执或专业驱动确认。
"""

from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from purrcept_engine.errors import EngineError, StorageError
from purrcept_engine.execution.protocol import JsonObject, decode_object, encode_json

type ControlState = Literal["admitted", "running", "finished", "rejected", "interrupted"]


class ControlError(EngineError):
    """页面参数、旧版本或容量拒绝；不表示权威连接损坏。"""

    def __init__(self, message: str, *, code: str = "control_arguments") -> None:
        """保留接入层可定位的类别；消息不得携带凭据或完整模型正文。"""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ControlRecord:
    """控制观察的持久记录；finished 只表示已取得对应业务返回。

    action_id 和 command_id 指向真实业务记录，后续确认必须查询该原身份。
    即使页面等待已结束，未知回执仍可被执行器的迟到事实澄清。
    """

    request_id: str
    instance_id: str
    payload_json: str
    state: ControlState
    admitted_at: float
    action_id: str | None
    command_id: str | None
    finished_at: float | None
    error_code: str | None
    error: str | None

    def projection(self) -> JsonObject:
        """输出有限接管事实，参数只含面板本次允许填写的控制值。"""
        return {
            "request_id": self.request_id,
            "instance_id": self.instance_id,
            "request": decode_object(self.payload_json.encode()),
            "state": self.state,
            "admitted_at": self.admitted_at,
            "action_id": self.action_id,
            "command_id": self.command_id,
            "finished_at": self.finished_at,
            "error_code": self.error_code,
            "error": self.error,
        }


SCHEMA = (
    "CREATE TABLE panel_controls (request_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, "
    "payload_json TEXT NOT NULL, state TEXT NOT NULL, admitted_at REAL NOT NULL, "
    "action_id TEXT, command_id TEXT, finished_at REAL, error_code TEXT, error TEXT)",
)


def initialize(connection: sqlite3.Connection) -> None:
    """借用调用方启动事务保留旧未确认控制，不提交或重放设备调用。"""
    connection.execute("SELECT * FROM panel_controls LIMIT 0")
    # 启动事务由 Store 持有；恢复失败不能单独提交任何子系统。
    connection.execute(
        "UPDATE panel_controls SET state='interrupted',finished_at=?,"
        "error_code='host_restarted',error='宿主重启；请查询原业务关联。' "
        "WHERE state IN ('admitted','running')",
        (time.time(),),
    )


def get(connection: sqlite3.Connection, request_id: str) -> ControlRecord | None:
    """按稳定身份读取，查询不改变设备或重新派发。"""
    row: tuple[object, ...] | None = connection.execute(
        "SELECT request_id,instance_id,payload_json,state,admitted_at,action_id,command_id,"
        "finished_at,error_code,error FROM panel_controls WHERE request_id=?",
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    state = _text(row[3])
    if state not in ("admitted", "running", "finished", "rejected", "interrupted"):
        raise StorageError("面板控制状态损坏。", code="invalid_record")
    return ControlRecord(
        _text(row[0]),
        _text(row[1]),
        _text(row[2]),
        state,
        _number(row[4]),
        _optional_text(row[5]),
        _optional_text(row[6]),
        None if row[7] is None else _number(row[7]),
        _optional_text(row[8]),
        _optional_text(row[9]),
    )


def admit(
    connection: sqlite3.Connection, request_id: str, instance_id: str, payload_json: str
) -> tuple[ControlRecord, bool]:
    """同事务去重和限额；不同参数复用身份明确拒绝，不覆盖原操作。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        previous = get(connection, request_id)
        if previous is not None:
            if (previous.instance_id, previous.payload_json) != (instance_id, payload_json):
                raise ControlError("控制身份对应不同请求。", code="control_conflict")
            return previous, False
        pending: tuple[int, ...] = connection.execute(
            "SELECT count(*) FROM panel_controls WHERE state IN ('admitted','running')"
        ).fetchone()
        if pending[0] >= 16:
            raise ControlError("快捷控制接管已满。", code="control_busy")
        connection.execute(
            "INSERT INTO panel_controls(request_id,instance_id,payload_json,state,admitted_at) "
            "VALUES(?,?,?,'admitted',?)",
            (request_id, instance_id, payload_json, time.time()),
        )
        return _required(connection, request_id), True


def link(
    connection: sqlite3.Connection,
    request_id: str,
    *,
    action_id: str | None = None,
    command_id: str | None = None,
) -> None:
    """记录执行器实际分配的身份，不能用页面自填值或重新派发恢复关联。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        record = _required(connection, request_id)
        if record.action_id not in (None, action_id) or record.command_id not in (None, command_id):
            raise StorageError("控制业务关联重复分配。", code="invalid_record")
        connection.execute(
            "UPDATE panel_controls SET action_id=?,command_id=?,state='running' WHERE request_id=?",
            (action_id, command_id, request_id),
        )


def finish(
    connection: sqlite3.Connection, request_id: str, *, error_code: str | None, error: str | None
) -> ControlRecord:
    """只结束页面业务观察，保留真实行动回执；拒绝不伪造业务成功。"""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        _required(connection, request_id)
        connection.execute(
            "UPDATE panel_controls SET state=?,finished_at=?,error_code=?,error=? "
            "WHERE request_id=?",
            (
                "finished" if error_code is None else "rejected",
                time.time(),
                error_code,
                error,
                request_id,
            ),
        )
        return _required(connection, request_id)


def _required(connection: sqlite3.Connection, request_id: str) -> ControlRecord:
    result = get(connection, request_id)
    if result is None:
        raise StorageError("已接管面板控制缺失。", code="invalid_record")
    return result


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise StorageError("面板控制文字损坏。", code="invalid_record")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise StorageError("面板控制时间损坏。", code="invalid_record")
    return float(value)


class _Submit(Protocol):
    """Store 注入的同连接执行队列，短事务不取得设备或模型。"""

    def __call__[Result](
        self,
        operation: Callable[[sqlite3.Connection], Result],
        *,
        logical_bytes: int = 0,
        control: bool = False,
        writing: bool = False,
    ) -> Awaitable[Result]:
        """接管真实 UTF-8 字节，取消等待不撤销已入队事务。"""
        ...


class ControlLedger:
    """Store 创建的有限控制账本，与母 Store 同循环及同资源寿命。"""

    def __init__(self, submit: _Submit) -> None:
        """借用唯一数据库队列，不创建第二连接或业务工作器。"""
        self._submit = submit

    async def get(self, request_id: str) -> ControlRecord | None:
        """读取持久控制事实，不在查询中恢复设备。"""
        return await self._submit(lambda connection: get(connection, request_id))

    async def admit(
        self, request_id: str, instance_id: str, payload: JsonObject
    ) -> tuple[ControlRecord, bool]:
        """原子接管或返回已有身份，参数冲突保留原事实。"""
        encoded = encode_json(payload)
        return await self._submit(
            lambda connection: admit(connection, request_id, instance_id, encoded.decode()),
            logical_bytes=len(encoded) + len(request_id.encode()) + len(instance_id.encode()),
            control=True,
            writing=True,
        )

    async def link(
        self, request_id: str, *, action_id: str | None = None, command_id: str | None = None
    ) -> None:
        """提交真实业务关联，观察者取消后仍由 app 持有提交。"""
        await self._submit(
            lambda connection: link(
                connection, request_id, action_id=action_id, command_id=command_id
            ),
            logical_bytes=len(encode_json((request_id, action_id, command_id))),
            control=True,
            writing=True,
        )

    async def finish(
        self, request_id: str, *, error_code: str | None = None, error: str | None = None
    ) -> ControlRecord:
        """保存取得业务返回或明确拒绝；权威错误原样停止工作。"""
        return await self._submit(
            lambda connection: finish(connection, request_id, error_code=error_code, error=error),
            logical_bytes=len(encode_json((request_id, error_code, error))),
            control=True,
            writing=True,
        )
