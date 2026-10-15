"""定义领域协作契约与不可变设置；调用与所有权由应用显式连接。"""

from __future__ import annotations

from typing import Protocol

from purrcept_engine.domain.subject.models import (
    SessionRecord,
    SessionScope,
    TurnOutcome,
    TurnRecord,
)


class SessionStore(Protocol):
    """主体借用的版本提交端口，生产由 Store 单连接事务实现。

    begin_turn 先于 Core；finish_turn 只接受原会话版本及在途身份，成功时
    检查点/媒体/输入结案原子安装。失败保留旧检查点和真实失败经历。取消
    等待不能撤销已接管事务，宿主须持有提交任务直至实际结束。
    """

    async def get_session(self, scope: SessionScope) -> SessionRecord:
        """还原已有权威版本；未知或损坏格式不得以空会话替代。"""
        ...

    async def get_turn(self, turn_id: str) -> TurnRecord | None:
        """开始等待取消后核对已接管批次，不能留下无人处理的活跃身份。"""
        ...

    async def begin_turn(
        self,
        turn_id: str,
        scope: SessionScope,
        expected_version: int,
        generation: int,
        input_ids: tuple[str, ...],
    ) -> TurnRecord:
        """接管一个主动处理批次，拒绝版本冲突或重复输入交接。"""
        ...

    async def finish_turn(
        self,
        turn: TurnRecord,
        outcome: TurnOutcome,
        *,
        checkpoint_json: str | None = None,
        error_json: str | None = None,
        accepted_failures: tuple[str, ...] = (),
    ) -> SessionRecord:
        """提交整轮成功或明确失败，返回实际已提交会话。"""
        ...
