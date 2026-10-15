"""定义领域协作契约与不可变设置；调用与所有权由应用显式连接。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from purrcept_engine.domain.model_requests.ports import RequestEvidenceSink
from purrcept_engine.domain.subject.session_ports import SessionStore


class BackgroundStore(SessionStore, RequestEvidenceSink, Protocol):
    """app 借出的单连接会话与请求事实端口，不公开 SQL 或客户端。

    变化序号只服务不漏通知的等待，不代替查询或完成事实。写入等待取消不
    撤销已接管事务，后台持有提交任务直到真实归还。
    """

    @property
    def change_revision(self) -> int:
        """返回已提交事务通知的单调序号。"""
        ...

    async def wait_for_change(self, after_revision: int) -> int:
        """等待新事实或明确关闭；调用前先读取序号，再查询账本。"""
        ...


@dataclass(frozen=True, slots=True)
class BackgroundSettings:
    """后台节奏和单批额度，由装配显式指定，不从前台模型推测能力。"""

    diagnostic_bytes: int
    page_chars: int = 8000
    batch_seconds: float = 60.0
    successful_turns: int = 8
    max_sources: int = 32
    max_requests: int = 8
    turn_seconds: float = 600.0

    def __post_init__(self) -> None:
        """在公开构造边界拒绝布尔值、无穷时间和无界扫描。"""
        for value in (self.page_chars, self.successful_turns, self.max_sources, self.max_requests):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("后台页长、批量和请求次数必须是正整数。")
        if self.max_sources > 32:
            raise ValueError("每批反思来源数不能超过有限扫描的 32 条边界。")
        if (
            isinstance(self.diagnostic_bytes, bool)
            or not isinstance(self.diagnostic_bytes, int)
            or self.diagnostic_bytes < 0
        ):
            raise ValueError("诊断额度必须是非负整数。")
        for value in (self.batch_seconds, self.turn_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("后台时间必须是有限正数。")
