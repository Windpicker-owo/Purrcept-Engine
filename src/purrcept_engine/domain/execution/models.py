"""定义行动、逐次能力调用与不可变结果历史。

脚本终态和 Provider 真实回执分别保存，后续失败保留先前效果；记录通过
执行账本交接。本模块不启动 Worker、不调用插件或自行重放未知效果。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from purrcept_engine.domain.inputs.models import MediaReference

CallOutcome = Literal["confirmed", "failed", "unknown", "not_started"]


CallState = Literal["prepared", "started", "confirmed", "failed", "unknown", "not_started"]


ActionOutcome = Literal["completed", "failed", "timed_out", "cancelled"]


ActionState = Literal["running", "completed", "failed", "timed_out", "cancelled"]


@dataclass(frozen=True, slots=True)
class CallResultRecord:
    """一次不可变效果结论；版本身份也是可靠交接的确认范围。

    Attributes:
        call_id: 实际能力调用的唯一身份，用于保存启动门槛及结果版本。
        revision: 已观察的状态修订号。
        state: 本次实际调用的确认、失败或未知结论。
        finished_at: 此记录实际结束的 Unix 时间戳；None 表示尚未保存结束事实。
        result_json: 已编码的实际结果；无可用结果时为 None。
        error_type: 回执中稳定的错误类别，无错误时为 None。
        error: 当前流程已经取得的真实故障或失败说明。
        media: 已接管媒体的引用集合，展开时必须取得原始 BLOB。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        首次结论为版本 1。未知升级为确认或失败生成新版本，原未知事实不删除。
        finished_at 为存储提交时的 UTC Unix 时间，不替代平台提供的实际发生时间。
    """

    call_id: str
    revision: int
    state: CallOutcome
    finished_at: float
    result_json: str | None
    error_type: str | None
    error: str | None
    media: tuple[MediaReference, ...] = ()


@dataclass(frozen=True, slots=True)
class CallRecord:
    """派发意图与某个回执版本的快照，历史包含到该版本的全部结论。

    Attributes:
        call_id: 实际能力调用的唯一身份，用于保存启动门槛及结果版本。
        action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
        sequence: 行动内从 1 开始的调用顺序号。
        path: 冻结目录中的完整能力调用路径，例如 me.console.output。
        target_id: 能力绑定的目标对象身份。
        arguments_json: 已编码为严格 JSON 对象的原始能力参数。
        state: 当前所有者持有的状态或待提交状态值。
        prepared_at: 调用意图正式保存的 Unix 时间戳（秒）。
        started_at: 本次真实工作开始的时刻，未开始时保留 None。
        finished_at: 此记录实际结束的 Unix 时间戳；None 表示尚未保存结束事实。
        receipt_revision: 原调用实际结果的不可变版本序号。
        result_json: 已编码的实际结果；无可用结果时为 None。
        error_type: 回执中稳定的错误类别，无错误时为 None。
        error: 当前流程已经取得的真实故障或失败说明。
        history: 按实际提交顺序保存的结果版本，后续未知不能抹掉已有确认。
        media: 已接管媒体的引用集合，展开时必须取得原始 BLOB。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        started_at 记录进入 Provider 前的持久门槛，并非外界操作已发生的证据。
        实时查询使用最新版本；待交接查询返回对应历史版本，不让新回执覆盖未交接
        的旧未知结论。参数 JSON 保留调用方原表示，查询不执行或重新投递调用。
    """

    call_id: str
    action_id: str
    sequence: int
    path: str
    target_id: str
    arguments_json: str
    state: CallState
    prepared_at: float
    started_at: float | None
    finished_at: float | None
    receipt_revision: int
    result_json: str | None
    error_type: str | None
    error: str | None
    history: tuple[CallResultRecord, ...]
    media: tuple[MediaReference, ...] = ()


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """行动代码、目录版本和整体结果，逐次能力回执拥有独立提交寿命。

    Attributes:
        action_id: 执行器分配的行动身份，用于关联逐次调用和真实回执。
        catalog_version: 本次行动冻结的目录内容摘要，执行期间保持不变。
        source: 行动原始材料，保存受限源码或冻结的 structured_call JSON。
        state: 当前所有者持有的状态或待提交状态值。
        created_at: 记录首次建立的 Unix 时间戳（秒）。
        finished_at: 此记录实际结束的 Unix 时间戳；None 表示尚未保存结束事实。
        output: 此次 Worker 实际产生的有界标准输出。
        result_json: 已编码的实际结果；无可用结果时为 None。
        error: 当前流程已经取得的真实故障或失败说明。
        calls: 当前行动已实际接管的逐次调用记录。
        input_id: 正式接管返回的输入身份，用于关联原文与后续处理。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        running 只表示行动意图已接管，不证明 Worker 已运行。行动失败、超时或取消
        不撤销调用回执；迟到真实结果仍可澄清原调用，而不会重写行动的原结束结果。
    """

    action_id: str
    catalog_version: str
    source: str
    state: ActionState
    created_at: float
    finished_at: float | None
    output: str
    result_json: str | None
    error: str | None
    calls: tuple[CallRecord, ...]
    input_id: str | None = None
