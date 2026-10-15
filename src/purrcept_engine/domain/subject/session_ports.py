"""定义连续会话的版本读取、轮次接管与结果提交契约。

ContinuousSession 借用同库实现串行推进前台或反思历史，成功后才采用检查点。
这里不复制 Core 工具循环，也不让会话失败回滚已发生的外部效果。
"""

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
        """还原已有权威版本。

        Args:
            scope: 目标连续会话，foreground 与 reflection 拥有独立历史。

        Returns:
            会话已提交版本与完整检查点，不包含尚未提交的临时历史。

        Note:
            未知或损坏格式不得以空会话替代。
        """
        ...

    async def get_turn(self, turn_id: str) -> TurnRecord | None:
        """开始等待取消后核对已接管批次，不能留下无人处理的活跃身份。

        Args:
            turn_id: 连续会话中本轮处理的身份。

        Returns:
            本轮接管身份、来源输入、生成代次与当前结论。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
        ...

    async def begin_turn(
        self,
        turn_id: str,
        scope: SessionScope,
        expected_version: int,
        generation: int,
        input_ids: tuple[str, ...],
    ) -> TurnRecord:
        """接管一个主动处理批次，拒绝版本冲突或重复输入交接。

        Args:
            turn_id: 连续会话中本轮处理的身份。
            scope: 目标连续会话，foreground 与 reflection 拥有独立历史。
            expected_version: 提交前观察到的版本，用于拒绝并发覆盖。
            generation: 当前会话或设备的代次，用于拒绝迟到结果更新新状态。
            input_ids: 本轮接管的输入身份集合，保留真实因果关联。

        Returns:
            本轮接管身份、来源输入、生成代次与当前结论。
        """
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
        """提交整轮成功或明确失败，返回实际已提交会话。

        Args:
            turn: 已接管的本轮记录，携带提交前版本与生成代次。
            outcome: 此次操作的真实结论，不由期望效果推断成功。
            checkpoint_json: 完整 Core 检查点的编码；失败轮次可不安装新历史。
            error_json: 编码后的失败证据，供后续交互追溯原轮次。
            accepted_failures: 当前成功交互明确承接的先前失败轮次身份。 默认不附加此类条目。

        Returns:
            会话已提交版本与完整检查点，不包含尚未提交的临时历史。
        """
        ...
