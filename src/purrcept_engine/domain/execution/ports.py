"""定义可替换隔离执行器与有限执行账本的共同契约。

应用显式创建 ActionExecutor，内置实现位于 infrastructure/execution/service。
执行器保留真实调用及资源所有权，只借用 ExecutionJournal 的业务提交，不取得
通用 SQL、主脑会话或插件资源关闭权，也不在宿主执行模型源码。
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
        """在启动前保存意图、冻结目录及因果来源，不从脚本文字推测来源身份。

        Args:
            action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
            catalog_version: 本次行动冻结的目录内容摘要，执行期间保持不变。
            source: 行动原始材料，保存受限源码或冻结的 structured_call JSON。
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。 None
                表示无单一输入因果关联。

        Raises:
            StorageError: 权威记录缺失、版本冲突、存储故障或提交容量不足。
        """
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
        """保存逐次调用意图，不能把此记录当作效果已经发生。

        Args:
            call_id: 实际能力调用的唯一身份，用于保存启动门槛及结果版本。
            action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
            sequence: 行动内从 1 开始的调用顺序号。
            path: 冻结目录中的完整能力调用路径，例如 me.console.output。
            target_id: 能力绑定的目标对象身份。
            arguments_json: 已编码为严格 JSON 对象的原始能力参数。

        Raises:
            StorageError: 权威记录缺失、版本冲突、存储故障或提交容量不足。
        """
        ...

    async def mark_call_started(self, call_id: str) -> None:
        """保存进入实际 Provider 前的门槛。

        Args:
            call_id: 实际能力调用的唯一身份，用于保存启动门槛及结果版本。

        Raises:
            StorageError: 权威记录缺失、版本冲突、存储故障或提交容量不足。

        Note:
            await 后仍须核验当前资格。
        """
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
        """保存真实结论、历史版本和待交接责任，再向观察者交付。

        Args:
            call_id: 实际能力调用的唯一身份，用于保存启动门槛及结果版本。
            state: 本次实际调用的确认、失败或未知结论。
            result_json: 已编码的实际结果；无可用结果时为 None。
            error_type: 回执中稳定的错误类别，无错误时为 None。
            error: 当前流程已经取得的真实故障或失败说明。
            media: 已接管媒体的引用集合，展开时必须取得原始 BLOB。 默认不附加此类条目。

        Returns:
            原调用当前的权威结果及不可变版本历史。

        Raises:
            StorageError: 权威记录缺失、版本冲突、存储故障或提交容量不足。
        """
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
        """结束行动事实，不能回滚已经保存的逐次调用。

        Args:
            action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
            state: 行动结束状态，不能抹除已经发生的逐次效果。
            result_json: 已编码的实际结果；无可用结果时为 None。
            output: 此次 Worker 实际产生的有界标准输出。
            error: 当前流程已经取得的真实故障或失败说明。

        Raises:
            StorageError: 权威记录缺失、版本冲突、存储故障或提交容量不足。
        """
        ...

    async def get_action(self, action_id: str) -> ActionRecord | None:
        """读取真实结果与独立调用历史，不启动或重放行动。

        Args:
            action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。

        Returns:
            已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。
            未取得对应记录或素材时返回 None；读取故障仍抛出。

        Raises:
            StorageError: 权威记录缺失、版本冲突、存储故障或提交容量不足。
        """
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

        Args:
            source: 待执行的受限 Python 源码。
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。 None
                表示无单一输入因果关联。
            can_start: 实际派发前重新检查的资格回调，等待后仍须复核。 None
                表示不附加调用方资格条件，宿主固定门槛仍生效。
            presented_call_ids: 主体交付门槛确认已完整呈现的调用身份；空集合不授予恢复资格。

        Returns:
            已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。

        Raises:
            StorageError: 执行接管容量不足或意图记录无法提交。
            ExecutionBusy: 行动 Worker 接管名额已满。
            ExecutionClosed: 执行器已停止接管新行动。
            ExecutionLimitError: 源码编码或字节大小超过执行器边界。

        Note:
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

        Args:
            path: 冻结目录中的完整能力调用路径，例如 me.console.output。
            arguments: 按已装配参数模型校验的能力关键字参数。
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。 None
                表示无单一输入因果关联。
            can_start: 实际派发前重新检查的资格回调，等待后仍须复核。 None
                表示不附加调用方资格条件，宿主固定门槛仍生效。
            timeout_seconds: 本次操作的等待上限（秒），不得扩大所属行动或活动期限。 None
                使用所属能力或行动的既定期限。
            before_start: Provider 启动前的异步核验；返回 None 拒绝，正有限秒数只收紧期限。
                None 表示没有额外的异步资格核验。
            on_admitted: 首次等待前接收执行器分配身份的同步回调，不授予重投资格。 None
                表示不向调用方另行通知接管身份。

        Returns:
            已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。

        Raises:
            StorageError: 执行接管容量不足或意图记录无法提交。
            ExecutionBusy: 能力调用接管名额已满。
            ExecutionClosed: 执行器已停止接管。
            LookupError: 路径不在当前冻结能力目录中。

        Note:
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

        Args:
            action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。

        Note:
            只供取得 invoke/on_admitted 身份的可信所有者使用。没有 live 所有权
            表示本实例已无该项资源，不认证 SQL 入账或效果；此事实独立于账本健康。
            等待取消只结束观察，没有历史缓存，也不重新执行调用。
        """
        ...

    async def query(self, action_id: str) -> ActionRecord | None:
        """查询权威事实，不用通知丢失推断行动未发生。

        Args:
            action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。

        Returns:
            已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。
            未取得对应记录或素材时返回 None；读取故障仍抛出。
        """
        ...

    def stop_dispatch(self) -> None:
        """同步、幂等撤销所有新派发。

        Note:
            不做 I/O，不取消已开始 Provider。

            app 在等待任何插件或资源清理之前调用。已接管队列和运行脚本均
            必须立即失去下一调用资格，真实回执和资源清理由 close 继续拥有。
        """
        ...

    async def close(self) -> None:
        """只拥有一次实际收尾，插件和账本须保留到此方法真实结束。"""
        ...
