"""历史预检借用的最小读取端口；摘要事务由维护端口独立持有。"""

from typing import Protocol

from purrcept_engine.domain.subject.models import SessionRecord, SessionScope


class HistoryContext(Protocol):
    """应用借出的历史保护配置，运行期不可由模型修改。"""

    @property
    def summary_trigger(self) -> float:
        """返回估算和同历史实际用量共用的摘要触发比例。"""
        ...

    @property
    def media_cleanup_threshold_bytes(self) -> int:
        """返回完整 SDK 请求触发历史媒体批量清理的字节数。"""
        ...

    @property
    def protected_turns(self) -> int:
        """返回常规摘要保护的近期轮数；容量告警且无旧区间时允许压缩已完成近期轮次。"""
        ...


class HistoryStore(Protocol):
    """提供版本与原文保护事实，不授予数据库访问。"""

    async def get_session(self, scope: SessionScope) -> SessionRecord:
        """读取同一连续会话已提交版本。

        Args:
            scope: 目标连续会话，foreground 与 reflection 拥有独立历史。

        Returns:
            会话已提交版本与完整检查点，不包含尚未提交的临时历史。
        """
        ...

    async def protected_input_ids(self, input_ids: tuple[str, ...]) -> frozenset[str]:
        """返回尚受未决工作保护的原始输入身份。

        unknown 回执仅在宿主配置的保护窗口内自动保护原轮次；过期不改变回执
        或原文，仍关联计划、活动、监管等未决工作的输入继续受保护。

        Args:
            input_ids: 本轮接管的输入身份集合，保留真实因果关联。

        Returns:
            由完整请求材料确认的不可变身份集合，不从正文标记推断资格。
        """
        ...
