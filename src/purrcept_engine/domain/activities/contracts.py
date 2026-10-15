"""定义领域协作契约与不可变设置；调用与所有权由应用显式连接。"""

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
        """保留接入方可查询的拒绝类别。"""
        super().__init__(message)
        self.code = code


class ActivityStore(Protocol):
    """Host 提供的同连接存储端口，不由活动模块创建第二个连接。

    所有方法只在服务所属循环调用。commit_activity 在控制队列执行短事务；
    等待取消不回滚已接管提交。失败原样传播并停止活动后续派发，不返回空成功。
    recover_activities 在驱动装配前执行，仅保存中断事实，不回放专业动作。
    """

    async def activity_records(self) -> tuple[ActivityRecord, ...]:
        """读取未结束的有限活动集合。"""
        ...

    async def get_activity(self, activity_id: str) -> ActivityRecord | None:
        """按身份读取当前控制和工作快照。"""
        ...

    async def get_activity_command(self, command_id: str) -> ActivityCommand | None:
        """读取可信命令接管和真实确认，查询不重投。"""
        ...

    async def get_activity_feedback(self, feedback_id: str) -> ActivityFeedback | None:
        """查询原始反馈，用稳定身份去重而不改写首次提交事实。"""
        ...

    async def commit_activity(
        self,
        record: ActivityRecord,
        *,
        expected_version: int | None,
        command: ActivityCommand | None = None,
        feedback: ActivityFeedback | None = None,
    ) -> None:
        """原子提交精确版本、命令与反馈；不存在通用可变状态写入口。"""
        ...

    async def recover_activities(self) -> tuple[ActivityRecord, ...]:
        """保留累计和未确认命令，仅撤销上次运行的派发资格。"""
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
        """真实共享目录/schema/Journal 路径；原型权威与宿主错误直接传播。"""
        ...


@dataclass(frozen=True, slots=True)
class DriverAcceptance:
    """驱动是否已接管控制；accepted=True 不证明已经运行或收尾完成。"""

    accepted: bool
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool) or not isinstance(self.reason, str):
            raise ValueError("驱动受理必须包含布尔结论与文本原因。")


@dataclass(frozen=True, slots=True)
class RecoveryObservation:
    """显式核对取得的真实材料；stopped=None 保留未知并禁止恢复。

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
        """接管明确命令身份；重复/迟到确认由宿主保存并核验资格。"""
        ...

    async def reconcile(self, record: ActivityRecord) -> RecoveryObservation:
        """显式恢复前只读核对实际状态，不重放旧命令或启动工作。"""
        ...

    async def close(self) -> None:
        """停止专业派发并收回实际后台任务，在途效果仍报告原归属。"""
        ...


@dataclass(frozen=True, slots=True)
class ActivityDefinition:
    """插件装配时提供的冻结定义；single_instance 约束未结束的同定义委托。"""

    definition: str
    factory: Callable[[ActivityPort], ActivityDriver]
    single_instance: bool = False
