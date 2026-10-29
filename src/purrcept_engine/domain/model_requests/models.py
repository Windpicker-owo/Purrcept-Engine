"""定义逻辑模型请求、传输尝试与公开失败证据。

一个逻辑请求固定材料，多次尝试按实际顺序保留独立身份和终态。
这些不可变值供提交端口和查询使用，不触发调用、重试或日志裁剪。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from purrcept_core.models import ModelRateLimitError

RequestKind = Literal["generation", "decision"]


RequestOutcome = Literal["succeeded", "failed", "timed_out", "cancelled", "interrupted"]


AttemptOutcome = Literal["succeeded", "failed", "timed_out", "cancelled", "interrupted"]


@dataclass(frozen=True, slots=True)
class BodyStatus:
    """正文的可用性与原始摘要；过期仍保留大小、时间及明确原因。"""

    available: bool
    digest: str
    original_bytes: int
    expired_at: float | None = None
    reason: str | None = None
    summary: str | None = None


@dataclass(frozen=True, slots=True)
class RequestScope:
    """由主体冻结的因果身份与选材引用，前台和反思分别创建。

    Attributes:
        owner_id: 发起本次工作并持有提交资格的所有者身份。
        turn_id: 连续会话中本轮处理的身份。
        input_ids: 本轮接管的输入身份集合，保留真实因果关联。
        material_refs: 此请求实际引用的确切材料身份，便于恢复选材依据。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        owner_id 标识会话所有者，turn_id 标识其连续批次。input_ids 是已接管输入，
        material_refs 是该请求额外选择的快照、历史或媒体引用；顺序有意义，不用
        时间接近推断关系。引用本身不代替 materials_json 中真正交给模型的材料。
    """

    owner_id: str
    turn_id: str
    input_ids: tuple[str, ...] = ()
    material_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RequestEvidence:
    """一个逻辑调用的不变材料，所有传输重试共用同一记录。

    Attributes:
        request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。
        scope: 冻结的请求因果身份、轮次和来源引用。
        kind: 此业务值所属的稳定种类。
        model: 本次调用使用的显式模型名，不在领域层推断替代模型。
        materials_json: 按 SDK 布局冻结的公开材料；淘汰查询为 None，由 materials_status 说明原因。
        compiler: 按实际 SDK 布局冻结完整请求材料的纯编译器。
        attempts: 按真实开始顺序保存的传输尝试，不创建新逻辑请求。
        attempt_seconds: 单次传输尝试的秒数上限。
        total_seconds: 一个逻辑请求含重试与退避的总秒数上限。
        capacity_json: 此完整请求采用的容量估计与规则依据；None 表示未登记。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        materials_json 是冻结 SDK 编译的公开 payload，不含凭据或客户端。compiler
        同时记录源码摘要与包版本，便于核对冻结转换。预算值记录本次约定，不代表
        每次尝试都能取得完整时长；上层截止仍可更早结束调用。capacity_json
        保存派发前的实际输入锚点、增量估算、适用范围和分母；新请求的完整
        Provider usage 仍只由其真实响应提供。
    """

    request_id: str
    scope: RequestScope
    kind: RequestKind
    model: str
    materials_json: str | None
    compiler: str
    attempts: int
    attempt_seconds: float
    total_seconds: float
    capacity_json: str | None = None


@dataclass(frozen=True, slots=True)
class AttemptStart:
    """实际进入后端前提交的身份；编号从 1 开始，deadline 使用本机单调时钟。

    Attributes:
        request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。
        attempt_id: 本次真实传输尝试的稳定身份，区别于逻辑请求身份。
        sequence: 行动内从 1 开始的调用顺序号。
        deadline: 本次尝试的绝对单调截止，仅可在原宿主的单调时钟中解释。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    request_id: str
    attempt_id: str
    sequence: int
    deadline: float


@dataclass(frozen=True, slots=True)
class FailureEvidence:
    """可公开的错误分类，不复制可能含凭据的第三方异常正文。

    Attributes:
        error_type: 回执中稳定的错误类别，无错误时为 None。
        category: 已脱敏的稳定错误分类。
        status_code: 后端实际提供的 HTTP 状态码；None 表示未取得。
        provider_code: 后端实际提供的稳定错误码；None 表示未取得。
        retry_after_seconds: 后端明确要求的退避秒数；None 表示没有提示。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        error_type 保留原异常类型，category 描述 Engine 是否具备重试依据。
        status_code、provider_code 和等待建议仅来自可取得的结构化响应；空值明确
        表示分类依据缺失。调用方仍收到原异常对象，而不是由此记录生成的新异常。
    """

    error_type: str
    category: str
    status_code: int | None = None
    provider_code: str | None = None
    retry_after_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class AttemptEvidence:
    """一次尝试的不可变结果；原参数证据与有限诊断分别保存。

    Attributes:
        start: 真实尝试开始前须持久化的编号、身份和截止事实。
        state: 当前所有者持有的状态或待提交状态值。
        response_json: 实际完整模型响应的 JSON 编码。
        failure: 当前所有者锁存的原始故障，不能转换为空成功。
        argument_events_json: 完整工具参数通道的事件编码，不受有限诊断裁剪。
        diagnostic_events_json: 按字节预算保存的流诊断事件编码。
        diagnostics_complete: 是否保留了本次全部流诊断事件。
        omitted_events: 因有限诊断容量未保存的流事件数。
        retry_wait_seconds: 本次错误之后实际采用的退避秒数；None 表示未安排重试。
        diagnostics_expired_at: 此尝试诊断正文已经过期的时刻。
        diagnostics_unavailable: 诊断正文当前无法取得的明确标记。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        response_json 是完整取得的 Core 响应。argument_events_json 保存参数修复前
        的可用事件，diagnostic_events_json 保存有限预览。诊断超限只更新覆盖标记，
        不删除响应或改变其业务结论。diagnostics_expired_at 单独说明正文淘汰；
        diagnostics_unavailable 表示诊断保存/读取缺口，两者不改原采集覆盖。
        响应和参数正文也可按保留策略淘汰，此时 body_status 明确标记缺失、原始摘要与
        大小；None 不代表空响应。usage_json 独立长期保留真实用量，不能重建正文。
        timed_out 不证明服务端已经停止计算。
    """

    start: AttemptStart
    state: AttemptOutcome
    response_json: str | None
    failure: FailureEvidence | None
    argument_events_json: str | None
    diagnostic_events_json: str
    diagnostics_complete: bool
    omitted_events: int
    # 这是已安排的等待额度；取消或截止可缩短实际等待，不声称已经等待完毕。
    retry_wait_seconds: float | None = None
    diagnostics_expired_at: float | None = None
    diagnostics_unavailable: bool = False
    body_status: BodyStatus | None = field(default=None, compare=False)
    usage_json: str | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """持久化查询投影；未结束的尝试只有 start，重启后显式标记 interrupted。

    Attributes:
        start: 真实尝试开始前须持久化的编号、身份和截止事实。
        created_at: 记录首次建立的 Unix 时间戳（秒）。
        finished_at: 此记录实际结束的 Unix 时间戳；None 表示尚未保存结束事实。
        result: 实际结束尝试的结果；None 表示尚未取得或保存结束事实。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    start: AttemptStart
    created_at: float
    finished_at: float | None
    result: AttemptEvidence | None


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """查询一个逻辑请求及按实际顺序排列的尝试，不触发新调用。

    Attributes:
        request: 本次逻辑请求冻结的完整公开材料与因果身份。
        state: 当前所有者持有的状态或待提交状态值。
        created_at: 记录首次建立的 Unix 时间戳（秒）。
        finished_at: 此记录实际结束的 Unix 时间戳；None 表示尚未保存结束事实。
        attempts: 按真实开始顺序保存的传输尝试，不创建新逻辑请求。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    request: RequestEvidence
    state: RequestOutcome | Literal["running"]
    created_at: float
    finished_at: float | None
    attempts: tuple[AttemptRecord, ...]
    materials_status: BodyStatus | None = None


class BackgroundBudgetExceeded(ModelRateLimitError):
    """后台真实派发额度已用尽；属于可观察的等待，不是存储故障。"""

    def __init__(self, retry_after_seconds: float) -> None:
        """报告额度恢复时间，调用者保留待办且不得绕过同一持久门槛。"""
        super().__init__(
            "后台小时派发额度已耗尽。", provider="engine", retry_after_seconds=retry_after_seconds
        )
