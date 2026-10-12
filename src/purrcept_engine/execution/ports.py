"""执行器替换接口与有限账本端口，具体平台实现仍由 app 选择。

替换实现不能在宿主执行未经约束的模型代码，也不能用脚本结果覆盖实际能力
回执。共享接口表达所有权与真实效果提交；Windows 内置实现位于 service。
它只借用执行记录操作，不取得任意 SQL 或主脑会话。
"""

from collections.abc import Callable
from typing import Protocol

from purrcept_engine.storage.execution import ActionOutcome, ActionRecord, CallOutcome, CallRecord


class ExecutionJournal(Protocol):
    """权威库实现的执行专用提交口，事务确认先于实际启动或交付。

    执行器在同一宿主事件循环调用；取消等待不会撤销已经接管的事务。存储
    错误原样停止派发，不能在内存中继续充当权威记录。未知可由新实际材料
    追加版本，已确认结果不可覆盖。Store 是内置实现。
    """

    async def begin_action(self, action_id: str, *, catalog_version: str, source: str) -> None:
        """在启动任何脚本前保存代码及冻结目录身份。"""
        ...

    async def begin_call(
        self,
        call_id: str,
        *,
        action_id: str,
        sequence: int,
        path: str,
        target_id: str,
        arguments_json: str,
    ) -> None:
        """保存逐次调用意图，不能把此记录当作效果已经发生。"""
        ...

    async def mark_call_started(self, call_id: str) -> None:
        """保存进入实际 Provider 前的门槛；await 后仍须核验当前资格。"""
        ...

    async def record_call_result(
        self,
        call_id: str,
        *,
        state: CallOutcome,
        result_json: str | None,
        error_type: str | None,
        error: str | None,
    ) -> CallRecord:
        """保存真实结论、历史版本和待交接责任，再向 Worker 交付。"""
        ...

    async def finish_action(
        self,
        action_id: str,
        *,
        state: ActionOutcome,
        result_json: str | None,
        output: str,
        error: str | None,
    ) -> None:
        """结束脚本事实，不能回滚已经保存的逐次调用。"""
        ...

    async def get_action(self, action_id: str) -> ActionRecord | None:
        """读取真实结果与独立调用历史，不启动或重放行动。"""
        ...


class ActionExecutor(Protocol):
    """app 构造的执行资源所有者，替换实现须保留相同启动与关闭契约。

    接管前有界拒收；接管后任务独立于观察者取消。can_start 只影响尚未
    实际开始的行动，已开始效果由执行器保留至真实回执提交。close 停止新
    行动及下一项能力，等待实际借用结束，不重置宿主总关闭期限。平台限制
    不成立时明确失败，不退回宿主 exec。内置实现为 ExecutionService。
    """

    @property
    def pending_resources(self) -> int:
        """返回尚未实际收回的工作资源数，供关闭超时查询。"""
        ...

    async def execute(
        self,
        source: str,
        *,
        input_id: str | None = None,
        can_start: Callable[[], bool] | None = None,
    ) -> ActionRecord:
        """接管并观察一次行动，观察取消不能冒充执行已终止。"""
        ...

    async def query(self, action_id: str) -> ActionRecord | None:
        """查询权威事实，不用通知丢失推断行动未发生。"""
        ...

    async def close(self) -> None:
        """只拥有一次实际收尾，插件和账本须保留到此方法真实结束。"""
        ...
