"""历史预检借用的最小读取端口；摘要事务由维护端口独立持有。"""

from typing import Protocol

from purrcept_engine.domain.subject.models import SessionRecord, SessionScope


class HistoryContext(Protocol):
    """应用借出的历史保护配置，运行期不可由模型修改。"""

    @property
    def protected_turns(self) -> int:
        """返回必须原样保留的近期完整轮次数。"""
        ...


class HistoryStore(Protocol):
    """提供版本与原文保护事实，不授予数据库访问。"""

    async def get_session(self, scope: SessionScope) -> SessionRecord:
        """读取同一连续会话已提交版本。"""
        ...

    async def protected_input_ids(self, input_ids: tuple[str, ...]) -> frozenset[str]:
        """返回尚受未决工作保护的原始输入身份。"""
        ...
