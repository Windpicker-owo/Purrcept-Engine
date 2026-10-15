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
from typing import Protocol

from purrcept_engine.domain.controls.models import ControlError, ControlRecord
from purrcept_engine.errors import StorageError
from purrcept_engine.values import JsonObject, encode_json

SCHEMA = (
    "CREATE TABLE panel_controls (request_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, "
    "payload_json TEXT NOT NULL, state TEXT NOT NULL, admitted_at REAL NOT NULL, "
    "action_id TEXT, command_id TEXT, finished_at REAL, error_code TEXT, error TEXT)",
)


def initialize(connection: sqlite3.Connection) -> None:
    """借用调用方启动事务保留旧未确认控制，不提交或重放设备调用。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
    """
    connection.execute("SELECT * FROM panel_controls LIMIT 0")
    # 启动事务由 Store 持有；恢复失败不能单独提交任何子系统。
    connection.execute(
        "UPDATE panel_controls SET state='interrupted',finished_at=?,"
        "error_code='host_restarted',error='宿主重启；请查询原业务关联。' "
        "WHERE state IN ('admitted','running')",
        (time.time(),),
    )


def get(connection: sqlite3.Connection, request_id: str) -> ControlRecord | None:
    """按稳定身份读取，查询不改变设备或重新派发。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。

    Returns:
        稳定请求身份的受理记录与当前实际业务关联。 未取得对应记录或素材时返回 None；
        读取故障仍抛出。

    Raises:
        StorageError: 面板控制状态损坏。
    """
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
    """同事务去重和限额。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。
        instance_id: 本次装配中唯一的插件或宿主实例身份。
        payload_json: 已编码的业务事实，不包含任意 Python 对象。

    Returns:
        权威接管记录及是否为重复请求；重复身份不会重新派发。

    Raises:
        ControlError: 控制身份对应不同请求；快捷控制接管已满。

    Note:
        不同参数复用身份明确拒绝，不覆盖原操作。
    """
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
    """记录执行器实际分配的身份，不能用页面自填值或重新派发恢复关联。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。
        action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
        command_id: 控制命令的稳定身份；重复提交须沿用原身份和内容。

    Raises:
        StorageError: 控制业务关联重复分配。
    """
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
    """只结束页面业务观察，保留真实行动回执。

    Args:
        connection: 仅在 Store 专属线程当前短事务中借用的连接，不在回调中等待设备或模型。
        request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。
        error_code: 供查询和调用方分类处理的稳定错误码。
        error: 当前流程已经取得的真实故障或失败说明。

    Returns:
        稳定请求身份的受理记录与当前实际业务关联。

    Note:
        拒绝不伪造业务成功。
    """
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
        """接管真实 UTF-8 字节，取消等待不撤销已入队事务。

        Args:
            operation: 在专属数据库线程执行的同步短事务，返回值交回原事件循环。
            logical_bytes: 本次操作占用的逻辑载荷字节数，用于接管前背压。 默认 0。
            control: 是否使用独立控制名额，避免普通输入阻塞真实回执收尾。 默认 False。
            writing: 是否属于写操作，以便关闭或权威故障时撤销派发资格。 默认 False。

        Returns:
            当前所有者持有的工作观察入口，等待取消不等于实际资源已归还。
        """
        ...


class ControlLedger:
    """Store 创建的有限控制账本，与母 Store 同循环及同资源寿命。"""

    def __init__(self, submit: _Submit) -> None:
        """借用唯一数据库队列，不创建第二连接或业务工作器。

        Args:
            submit: 借用同一 Store 有界队列执行同步短事务的入口。
        """
        self._submit = submit

    async def get(self, request_id: str) -> ControlRecord | None:
        """读取持久控制事实，不在查询中恢复设备。

        Args:
            request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。

        Returns:
            稳定请求身份的受理记录与当前实际业务关联。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
        return await self._submit(lambda connection: get(connection, request_id))

    async def admit(
        self, request_id: str, instance_id: str, payload: JsonObject
    ) -> tuple[ControlRecord, bool]:
        """原子接管或返回已有身份，参数冲突保留原事实。

        Args:
            request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。
            instance_id: 本次装配中唯一的插件或宿主实例身份。
            payload: 当前边界采用的业务 JSON 或完整模型请求材料。

        Returns:
            权威接管记录及是否为重复请求；重复身份不会重新派发。
        """
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
        """提交真实业务关联，观察者取消后仍由 app 持有提交。

        Args:
            request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。
            action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
            command_id: 控制命令的稳定身份；重复提交须沿用原身份和内容。
        """
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
        """保存取得业务返回或明确拒绝。

        Args:
            request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。
            error_code: 供查询和调用方分类处理的稳定错误码。
            error: 当前流程已经取得的真实故障或失败说明。 None 表示没有错误说明。

        Returns:
            稳定请求身份的受理记录与当前实际业务关联。

        Note:
            权威错误原样停止工作。
        """
        return await self._submit(
            lambda connection: finish(connection, request_id, error_code=error_code, error=error),
            logical_bytes=len(encode_json((request_id, error_code, error))),
            control=True,
            writing=True,
        )
