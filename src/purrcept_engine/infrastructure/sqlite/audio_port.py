"""音频事实的有限门面，借用 Store 同一有界队列，不创建第二数据库连接。

写入是短控制事务，已接管等待取消不撤销。插件只有追加和按身份读取
事实的权利，不能提交任意 SQL 或复活播放。权威故障原样交给资源所有者。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Protocol

import purrcept_engine.domain.audio.models as audio_models
from purrcept_engine.infrastructure.sqlite import audio as sql

_logger = logging.getLogger(__name__)


class _Submit(Protocol):
    """Store 借出的单连接短事务入口，只供本模块构造固定业务操作。"""

    def __call__[Result](
        self,
        operation: Callable[[sqlite3.Connection], Result],
        *,
        logical_bytes: int = 0,
        control: bool = False,
        writing: bool = False,
    ) -> Awaitable[Result]:
        """等待真实提交，取消不等于事务回滚。

        Args:
            operation: 在专属数据库线程执行的同步短事务，返回值交回原事件循环。
            logical_bytes: 本次操作占用的逻辑载荷字节数，用于接管前背压。 默认 0。
            control: 是否使用独立控制名额，避免普通输入阻塞真实回执收尾。 默认 False。
            writing: 是否属于写操作，以便关闭或权威故障时撤销派发资格。 默认 False。

        Returns:
            当前所有者持有的工作观察入口，等待取消不等于实际资源已归还。
        """
        ...


class AudioLedger:
    """Store 创建的同寿命音频门面，不持有或关闭 SQLite。"""

    def __init__(self, submit: _Submit) -> None:
        """只借用母 Store 唯一队列，构造没有外部副作用。

        Args:
            submit: 借用同一 Store 有界队列执行同步短事务的入口。
        """
        self._submit = submit

    async def append(self, fact: audio_models.AudioFact) -> None:
        """完整 UTF-8 事实计入控制额度，权威错误不转为空成功。

        Args:
            fact: 已经发生的不可变音频阶段事实。
        """
        size = len((fact.plugin_id + fact.identity + fact.fact + fact.payload_json).encode())
        await self._submit(
            lambda connection: sql.append(connection, fact),
            logical_bytes=size,
            control=True,
            writing=True,
        )
        identity = "call_id" if fact.fact.startswith("control_") else "playback_id"
        _logger.info(
            "音频阶段已入账。",
            extra={"plugin_id": fact.plugin_id, identity: fact.identity, "outcome": fact.fact},
        )

    async def facts(self, plugin_id: str, identity: str) -> tuple[audio_models.AudioFact, ...]:
        """按播放或控制身份取得正式记录。

        Args:
            plugin_id: 事实所属插件实例的身份。
            identity: 当前事实的稳定身份，用于去重和关联。

        Returns:
            条目集合：载荷已固定为严格 JSON 的不可变音频阶段事实。
        """
        return await self._submit(
            lambda connection: sql.facts(connection, plugin_id, identity),
            logical_bytes=len((plugin_id + identity).encode()),
        )
