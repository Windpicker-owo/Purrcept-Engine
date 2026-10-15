"""执行器替换接口与有限账本端口，具体平台实现仍由 app 选择。

替换实现不能在宿主执行未经约束的模型代码，也不能用脚本结果覆盖实际能力
回执。共享接口表达所有权与真实效果提交；Windows 内置实现位于 service。
它只借用执行记录操作，不取得任意 SQL 或主脑会话。
"""

from collections.abc import Awaitable, Callable
from typing import Protocol

from purrcept_engine.domain.execution.models import (
    ActionOutcome,
    ActionRecord,
    CallOutcome,
    CallRecord,
)
from purrcept_engine.domain.inputs.models import MediaReference
from purrcept_engine.values import JsonObject


class ExecutionJournal(Protocol):
    """权威库实现的执行专用提交口，事务确认先于实际启动或交付。

    执行器在同一宿主事件循环调用；取消等待不会撤销已经接管的事务。存储
    错误原样停止派发，不能在内存中继续充当权威记录。未知可由新实际材料
    追加版本，已确认结果不可覆盖。Store 是内置实现。
    """

    async def begin_action(
        self, action_id: str, *, catalog_version: str, source: str, input_id: str | None = None
    ) -> None:
        """在启动前保存意图、冻结目录及因果来源，不从脚本文字推测来源身份。"""
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
        media: tuple[MediaReference, ...] = (),
    ) -> CallRecord:
        """保存真实结论、历史版本和待交接责任，再向观察者交付。"""
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
        """结束行动事实，不能回滚已经保存的逐次调用。"""
        ...

    async def get_action(self, action_id: str) -> ActionRecord | None:
        """读取真实结果与独立调用历史，不启动或重放行动。"""
        ...


class ActionExecutor(Protocol):
    """app 构造的执行资源所有者，替换实现须保留相同启动与关闭契约。

    接管前有界拒收；接管后任务独立于观察者取消。can_start 只影响尚未
    实际开始的行动，已开始效果由执行器保留至真实回执提交。宿主在发布
    closing 的同一同步段调用 stop_dispatch 撤销新行动和下一能力资格，
    再由 close 等待实际借用结束，不重置宿主总关闭期限。平台限制
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
        presented_call_ids: frozenset[str] = frozenset(),
    ) -> ActionRecord:
        """接管并观察一次行动，观察取消不能冒充执行已终止。

        presented_call_ids 是主体交付门槛冻结的完整回执资格，不由脚本填写，
        不能凭来源身份或不同行动身份推断主脑已经看过核对材料。
        """
        ...

    async def invoke(
        self,
        path: str,
        arguments: JsonObject,
        *,
        input_id: str | None = None,
        can_start: Callable[[], bool] | None = None,
        timeout_seconds: float | None = None,
        before_start: Callable[[], Awaitable[float | None]] | None = None,
        on_admitted: Callable[[str], None] | None = None,
    ) -> ActionRecord:
        """接管一次冻结目录内的结构化能力调用，无需创建脚本或 Worker。

        新请求须先取得有限等待名额，普通饱和不占用控制名额。参数走同一真实
        Provider 校验，资格在实际启动前复核；timeout_seconds 只缩小既定期限，
        包含排队和提交等待。观察取消不撤销已接管所有权，未知和迟到真实结果
        保存在同一调用历史中。活动驱动只能借此有限端口调用插件能力。
        before_start 在真实 Provider 前查询当前资格，None 拒绝，正有限秒数仅
        收紧原期限；资格等待超时没有效果，内部权威错误原样传播。on_admitted
        在首次 await 前关联执行器分配的身份，不授予重投；回调故障撤销派发。
        """
        ...

    async def wait_invocation_released(self, action_id: str) -> None:
        """等待本实例结构化调用的真实资源归还，不把 unknown 当作已经释放。

        只供取得 invoke/on_admitted 身份的可信所有者使用。没有 live 所有权
        表示本实例已无该项资源，不认证 SQL 入账或效果；此事实独立于账本健康。
        等待取消只结束观察，没有历史缓存，也不重新执行调用。
        """
        ...

    async def query(self, action_id: str) -> ActionRecord | None:
        """查询权威事实，不用通知丢失推断行动未发生。"""
        ...

    def stop_dispatch(self) -> None:
        """同步、幂等撤销所有新派发；不做 I/O，不取消已开始 Provider。

        app 在等待任何插件或资源清理之前调用。已接管队列和运行脚本均
        必须立即失去下一调用资格，真实回执和资源清理由 close 继续拥有。
        """
        ...

    async def close(self) -> None:
        """只拥有一次实际收尾，插件和账本须保留到此方法真实结束。"""
        ...
