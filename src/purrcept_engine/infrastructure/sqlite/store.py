"""SQLite 权威存储门面：聚合连接生命周期、子账本借出与各业务域存储混入。

具体业务持久化逻辑按职责拆分为三个内聚模块：
- ``store_inputs.py``：输入准入、原生去重、睡眠分流与收件箱调度
- ``store_sessions.py``：连续会话、轮次事务、摘要占位、用量锚点与未决工作投影
- ``store_execution.py``：行动与能力调用回执、模型请求证据与长时活动状态
"""

import math
import sqlite3
from pathlib import Path

from purrcept_engine.errors import StorageError
from purrcept_engine.infrastructure.sqlite import archive
from purrcept_engine.infrastructure.sqlite import projections as projection_storage
from purrcept_engine.infrastructure.sqlite import retention as retention_storage
from purrcept_engine.infrastructure.sqlite.audio_port import AudioLedger
from purrcept_engine.infrastructure.sqlite.controls import ControlLedger
from purrcept_engine.infrastructure.sqlite.evidence_port import EvidenceLedger
from purrcept_engine.infrastructure.sqlite.feedback_port import FeedbackLedger
from purrcept_engine.infrastructure.sqlite.human_feedback import HumanFeedbackLedger
from purrcept_engine.infrastructure.sqlite.notebook import NotebookLedger
from purrcept_engine.infrastructure.sqlite.phone import PhoneLedger
from purrcept_engine.infrastructure.sqlite.rows import require_positive
from purrcept_engine.infrastructure.sqlite.stickers import SqliteStickerLibrary
from purrcept_engine.infrastructure.sqlite.store_execution import ExecutionStoreMixin
from purrcept_engine.infrastructure.sqlite.store_inputs import (
    InputStoreMixin,
    admit_in_transaction,
    input_logical_bytes,
)
from purrcept_engine.infrastructure.sqlite.store_sessions import SessionStoreMixin
from purrcept_engine.infrastructure.sqlite.time_port import TimeLedger
from purrcept_engine.values import JsonObject

__all__ = ["Store", "admit_in_transaction", "input_logical_bytes"]


class Store(InputStoreMixin, SessionStoreMixin, ExecutionStoreMixin):
    """拥有专属连接线程与有界提交队列的统一权威存储服务。"""

    @classmethod
    async def open(
        cls,
        path: Path,
        *,
        max_inputs: int = 1024,
        max_input_bytes: int = 256 * 1024 * 1024,
        unknown_call_protection_seconds: float = 86400.0,
    ) -> "Store":
        """打开权威库；未知回执只在指定秒数内自动保护整轮历史。

        窗口从最新 unknown 回执的落库时间起算，过期不修改回执状态或原文。
        未完成计划、活动及监管仍独立决定保护资格。
        """
        if not isinstance(path, Path):
            raise ValueError("path 必须是 Path。")
        require_positive(max_inputs, "max_inputs")
        require_positive(max_input_bytes, "max_input_bytes")
        if (
            isinstance(unknown_call_protection_seconds, bool)
            or not isinstance(unknown_call_protection_seconds, (int, float))
            or not math.isfinite(unknown_call_protection_seconds)
            or unknown_call_protection_seconds <= 0
        ):
            raise ValueError("unknown_call_protection_seconds 必须是有限正数。")
        store = cls(path, max_inputs, max_input_bytes, unknown_call_protection_seconds)
        await store._queue.start()
        return store

    async def close(self) -> None:
        """停止新写入、排空已受理操作并关闭唯一连接。"""
        await self._queue.close()

    def notebook(self, instance_id: str) -> NotebookLedger:
        """借出按插件实例隔离的 Notebook 派生索引账本。"""
        return NotebookLedger(self._submit, instance_id)

    def phone(self, instance_id: str) -> PhoneLedger:
        """借出按插件实例隔离的手机会话账本。"""
        return PhoneLedger(self._submit, instance_id)

    @property
    def stickers(self) -> SqliteStickerLibrary:
        """借出跨 QQ、手机及通用能力共享的贴纸库，同库事务串行执行。"""
        return SqliteStickerLibrary(self._submit)

    @property
    def evidence(self) -> EvidenceLedger:
        """借出同连接共享证据端口。"""
        return self._evidence

    @property
    def time(self) -> TimeLedger:
        """借出同队列时钟与睡眠休息账本。"""
        return TimeLedger(self._submit, self._admit_business)

    @property
    def feedback(self) -> FeedbackLedger:
        """借出回执、活动与计划可靠反馈交接账本。"""
        return FeedbackLedger(self._submit, self._admit_business)

    @property
    def controls(self) -> ControlLedger:
        """借出面板控制去重账本。"""
        return ControlLedger(self._submit)

    @property
    def human_feedback(self) -> HumanFeedbackLedger:
        """借出人工行为审查账本。"""
        return HumanFeedbackLedger(self._submit)

    @property
    def audio(self) -> AudioLedger:
        """借出同连接语音播放状态账本。"""
        return AudioLedger(self._submit)

    async def runtime_projection(self) -> projection_storage.JsonObject:
        """读取面板所需的轻量运行概况投影。"""
        return await self._submit(projection_storage.runtime)

    async def chat_projection(self, *, limit: int = 40) -> projection_storage.JsonObject:
        """读取近期对话输入与主体能力输出流水。"""
        return await self._submit(lambda conn: projection_storage.chat_feed(conn, limit=limit))

    async def expire_model_bodies(
        self, *, before: float, now: float, max_bytes: int = 1_073_741_824
    ) -> JsonObject:
        """按保留期限与总容量上限清理过期模型请求正文。"""
        return await self._submit(
            lambda connection: archive.cleanup(
                connection, before=before, now=now, max_bytes=max_bytes
            ),
            writing=True,
        )

    async def collect_unused_media(self, *, limit: int = 128) -> int:
        """分批回收无任何引用关系的内容寻址媒体 BLOB。"""
        return await self._submit(
            lambda connection: archive.collect_media(connection, limit=limit), writing=True
        )

    async def expire_diagnostics(
        self, *, before: float, expired_at: float, limit: int = 128
    ) -> projection_storage.JsonObject:
        """淘汰一批过期诊断事件并返回清理统计。"""

        def cleanup(connection: sqlite3.Connection) -> projection_storage.JsonObject:
            try:
                result = retention_storage.expire_diagnostics(
                    connection, before=before, expired_at=expired_at, limit=limit
                )
            except (sqlite3.Error, StorageError) as error:
                if isinstance(error, StorageError) and error.code != "invalid_diagnostics":
                    raise
                return {"state": "failed", "error_type": type(error).__name__}
            return {
                "state": "confirmed",
                "expired_attempts": result.expired_attempts,
                "removed_events": result.removed_events,
                "removed_bytes": result.removed_bytes,
                "has_more": result.has_more,
            }

        return await self._submit(cleanup, writing=True)

    @property
    def change_revision(self) -> int:
        """返回提交器已发布的事务通知代次。"""
        return self._queue.change_revision

    async def wait_for_change(self, after_revision: int) -> int:
        """等待晚于指定代次的下一次事务提交或关闭通知。"""
        return await self._queue.wait_for_change(after_revision)

    def health_snapshot(self) -> JsonObject:
        """返回存储层健康状态快照。"""
        return self._queue.health.snapshot()

    @property
    def authority_failure(self) -> BaseException | None:
        """返回锁存的底层权威存储故障（无故障时为 None）。"""
        return self._queue.failure

    @property
    def authority_available(self) -> bool:
        """同步检查提交器当前是否仍具备写入资格。"""
        return self._queue.authority_available
