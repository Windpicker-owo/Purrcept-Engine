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
from purrcept_engine.storage import audio as sql

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
        """等待真实提交，取消不等于事务回滚。"""
        ...


class AudioPort(Protocol):
    """app 创建并借给输出插件的同库事实端口。

    每个身份只能追加固定登记阶段；未知之后的真实确认另存澄清阶段，
    不覆盖旧事实。内容冲突显式失败。调用方负责身份与真实语义，存储
    负责不可覆盖、阶段/材料有界和真实提交。
    """

    async def append(self, fact: audio_models.AudioFact) -> None:
        """真实提交后返回，既有事实不得被新结果覆盖。"""
        ...

    async def facts(self, plugin_id: str, identity: str) -> tuple[audio_models.AudioFact, ...]:
        """读取保存顺序，不自动合成、播放或补写确认。"""
        ...


class AudioLedger:
    """Store 创建的同寿命音频门面，不持有或关闭 SQLite。"""

    def __init__(self, submit: _Submit) -> None:
        """只借用母 Store 唯一队列，构造没有外部副作用。"""
        self._submit = submit

    async def append(self, fact: audio_models.AudioFact) -> None:
        """完整 UTF-8 事实计入控制额度，权威错误不转为空成功。"""
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
        """按播放或控制身份取得正式记录。"""
        return await self._submit(
            lambda connection: sql.facts(connection, plugin_id, identity),
            logical_bytes=len((plugin_id + identity).encode()),
        )
