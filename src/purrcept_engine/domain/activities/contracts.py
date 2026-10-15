"""定义专业活动驱动、控制接管和只读恢复的插件契约。

应用从 ActivityDefinition 构造驱动，ActivityService 独占状态提交；驱动通过
ActivityPort 确认实际工作。这里不取得设备资源，也不执行领域步骤。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from purrcept_engine.domain.activities.models import (
    ActivityCommand,
    ActivityFeedback,
    ActivityRecord,
)
from purrcept_engine.domain.activities.port import ActivityPort
from purrcept_engine.domain.execution.models import ActionRecord
from purrcept_engine.domain.execution.record_validation import validate_json
from purrcept_engine.errors import EngineError
from purrcept_engine.values import JsonObject


class ActivityError(EngineError):
    """可定位的活动参数、状态或容量拒绝，不代表已接管工作被撤销。"""

    def __init__(self, message: str, *, code: str = "activity_state") -> None:
        """保留接入方可查询的拒绝类别。

        Args:
            message: 对调用方公开的原因或错误说明。
            code: 供调用方分类处理的稳定错误码。 默认 'activity_state'。
        """
        super().__init__(message)
        self.code = code


class ActivityStore(Protocol):
    """Host 提供的同连接存储端口，不由活动模块创建第二个连接。

    所有方法只在服务所属循环调用。commit_activity 在控制队列执行短事务；
    等待取消不回滚已接管提交。失败原样传播并停止活动后续派发，不返回空成功。
    recover_activities 在驱动装配前执行，仅保存中断事实，不回放专业动作。
    """

    async def activity_records(self) -> tuple[ActivityRecord, ...]:
        """读取未结束的有限活动集合。

        Returns:
            条目集合：活动的控制、预算、运行代次与工作快照。
        """
        ...

    async def get_activity(self, activity_id: str) -> ActivityRecord | None:
        """按身份读取当前控制和工作快照。

        Args:
            activity_id: 已创建活动的稳定身份。

        Returns:
            活动的控制、预算、运行代次与工作快照。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
        ...

    async def get_activity_command(self, command_id: str) -> ActivityCommand | None:
        """读取可信命令接管和真实确认，查询不重投。

        Args:
            command_id: 控制命令的稳定身份；重复提交须沿用原身份和内容。

        Returns:
            命令接管与实际确认记录；受理成功不等于驱动已完成。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
        ...

    async def get_activity_feedback(self, feedback_id: str) -> ActivityFeedback | None:
        """查询原始反馈，用稳定身份去重而不改写首次提交事实。

        Args:
            feedback_id: 驱动反馈的稳定身份，用于去重和查询实际确认。

        Returns:
            已经按身份去重并保存的真实驱动反馈。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
        ...

    async def commit_activity(
        self,
        record: ActivityRecord,
        *,
        expected_version: int | None,
        command: ActivityCommand | None = None,
        feedback: ActivityFeedback | None = None,
    ) -> None:
        """原子提交精确版本、命令与反馈。

        Args:
            record: 待查询、核对或提交的当前活动快照。
            expected_version: 提交前观察到的版本，用于拒绝并发覆盖。
            command: 与状态变化同事务保存的控制命令；None 表示本次没有命令。 None
                表示本次没有伴随命令。
            feedback: 与活动状态同事务保存的驱动反馈；None 表示没有新反馈。

        Note:
            不存在通用可变状态写入口。
        """
        ...

    async def recover_activities(self) -> tuple[ActivityRecord, ...]:
        """保留累计和未确认命令，仅撤销上次运行的派发资格。

        Returns:
            条目集合：活动的控制、预算、运行代次与工作快照。
        """
        ...


class ActivityInvoker(Protocol):
    """Host 注入的单能力交接，只供活动所有者借用，不提供脚本或 Provider。

    宿主同步关联已预留的行动身份，再进入真实共享执行器。before_start 在
    实际 Provider 前重新查询本活动资格，返回剩余时长或拒绝；on_admitted
    不能修改身份或重投。返回首版 ActionRecord 不代表 Provider 已归还，故
    ActivityService 同时借用配对的 wait_invocation_released 资源查询。
    """

    async def __call__(
        self,
        path: str,
        arguments: JsonObject,
        *,
        input_id: str,
        before_start: Callable[[], Awaitable[float | None]],
        on_admitted: Callable[[str], None],
        timeout_seconds: float | None,
    ) -> ActionRecord:
        """真实共享目录/schema/Journal 路径。

        Args:
            path: 冻结目录中的完整能力调用路径，例如 me.console.output。
            arguments: 按已装配参数模型校验的能力关键字参数。
            input_id: 正式接管返回的输入身份，用于关联原文与后续处理。
            before_start: Provider 启动前的异步核验；返回 None 拒绝，正有限秒数只收紧期限。
            on_admitted: 首次等待前接收执行器分配身份的同步回调，不授予重投资格。
            timeout_seconds: 本次操作的等待上限（秒），不得扩大所属行动或活动期限。

        Returns:
            已保存的行动记录，包含逐次真实调用及结果历史；unknown 不证明效果未发生。

        Note:
            原型权威与宿主错误直接传播。
        """
        ...


@dataclass(frozen=True, slots=True)
class DriverAcceptance:
    """驱动是否已接管控制；accepted=True 不证明已经运行或收尾完成。

    Attributes:
        accepted: 接受新连接时移交实际连接所有权的异步函数。
        reason: 供后续核对的明确处置原因。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    accepted: bool
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool) or not isinstance(self.reason, str):
            raise ValueError("驱动受理必须包含布尔结论与文本原因。")


@dataclass(frozen=True, slots=True)
class RecoveryObservation:
    """显式核对取得的真实材料；stopped=None 保留未知并禁止恢复。

    Attributes:
        stopped: 实际派发是否已停止；None 保留未知并禁止恢复。
        evidence_json: 本次只读核对取得的真实证据 JSON。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        stopped=True 只证明专业驱动已停止派发，不抹掉原命令或未知外部效果。
        evidence_json 留下核对材料，不能通过重做旧写操作生成这个观察。
    """

    stopped: bool | None
    evidence_json: str

    def __post_init__(self) -> None:
        if self.stopped is not None and not isinstance(self.stopped, bool):
            raise ValueError("核对停止状态只能是 bool 或 None。")
        validate_json(self.evidence_json)


@dataclass(frozen=True, slots=True)
class ActivityRecovery:
    """一次模型调用取得的只读核对材料，没有接管恢复或结束命令。

    Attributes:
        activity_id: 已创建活动的稳定身份。
        observation: 显式核对取得的实际观察，不能通过重放旧写操作生成。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        同一可信调用身份重投复用首次材料。主脑看到材料后须用新的调用明确选择，
        普通控制入口不能消费本结果保存的精确版本资格。
    """

    activity_id: str
    observation: RecoveryObservation


class ActivityDriver(Protocol):
    """插件工厂创建的专业流程，接收宿主控制并通过端口报告真实事实。

    工厂不得启动领域步骤。accept 只受理控制，必须有界返回；pause/end 与
    其他 accept 可并发，驱动按命令版本采用最新控制，不能被普通能力队列阻塞。
    受理后通过 port.confirm 报告实际状态，不靠返回字符串宣称已经完成。
    标准能力使用 port.invoke，共用实际 schema/回执与资源所有者；其他本地
    专业步骤经 begin_step/finish_step。取得 Step 不授予绕过执行器调用 Provider
    的权限，不能只检查旧提示快照。
    completed/failed/needs_decision 反馈表示专业驱动已停止继续推进；尚在途的
    宿主能力仍由执行所有者收尾，不能借反馈撤销其真实回执。
    """

    async def accept(self, command: ActivityCommand, port: ActivityPort) -> DriverAcceptance:
        """接管明确命令身份。

        Args:
            command: 宿主已接管的不可变控制命令。
            port: 宿主借给专业驱动的控制确认、步骤与受控能力端口。

        Returns:
            驱动是否接管命令的确认及理由，不证明已经执行完成。

        Note:
            重复/迟到确认由宿主保存并核验资格。
        """
        ...

    async def reconcile(self, record: ActivityRecord) -> RecoveryObservation:
        """显式恢复前只读核对实际状态，不重放旧命令或启动工作。

        Args:
            record: 待查询、核对或提交的当前活动快照。

        Returns:
            显式只读核对的实际证据；停止状态未知时禁止恢复。
        """
        ...

    async def close(self) -> None:
        """停止专业派发并收回实际后台任务，在途效果仍报告原归属。"""
        ...


@dataclass(frozen=True, slots=True)
class ActivityDefinition:
    """插件装配时提供的冻结定义；single_instance 约束未结束的同定义委托。

    Attributes:
        definition: 新活动使用的已装配定义名。
        factory: 应用按定义创建专业驱动的工厂，不在声明阶段启动工作。
        single_instance: 是否限制同一定义同时只有一个未结束活动。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    definition: str
    factory: Callable[[ActivityPort], ActivityDriver]
    single_instance: bool = False
