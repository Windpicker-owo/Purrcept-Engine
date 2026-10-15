"""保存公开值及其语义校验，不打开数据库或调用设备。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RequestKind = Literal["generation", "decision"]


RequestOutcome = Literal["succeeded", "failed", "timed_out", "cancelled", "interrupted"]


AttemptOutcome = Literal["succeeded", "failed", "timed_out", "cancelled", "interrupted"]


@dataclass(frozen=True, slots=True)
class RequestScope:
    """由主体冻结的因果身份与选材引用，前台和反思分别创建。

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

    materials_json 是冻结 SDK 编译的公开 payload，不含凭据或客户端。compiler
    同时记录源码摘要与包版本，便于核对冻结转换。预算值记录本次约定，不代表
    每次尝试都能取得完整时长；上层截止仍可更早结束调用。capacity_json
    保存派发前的纯计量方法、适用范围和分母，与 Provider usage 分开。
    """

    request_id: str
    scope: RequestScope
    kind: RequestKind
    model: str
    materials_json: str
    compiler: str
    attempts: int
    attempt_seconds: float
    total_seconds: float
    capacity_json: str | None = None


@dataclass(frozen=True, slots=True)
class AttemptStart:
    """实际进入后端前提交的身份；编号从 1 开始，deadline 使用本机单调时钟。"""

    request_id: str
    attempt_id: str
    sequence: int
    deadline: float


@dataclass(frozen=True, slots=True)
class FailureEvidence:
    """可公开的错误分类，不复制可能含凭据的第三方异常正文。

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

    response_json 是完整取得的 Core 响应。argument_events_json 保存参数修复前
    的可用事件，diagnostic_events_json 保存有限预览。诊断超限只更新覆盖标记，
    不删除响应或改变其业务结论。diagnostics_expired_at 单独说明正文淘汰；
    diagnostics_unavailable 表示诊断保存/读取缺口，两者不改原采集覆盖。
    timed_out 不证明服务端已经停止计算。
    """

    start: AttemptStart
    state: AttemptOutcome
    response_json: str | None
    failure: FailureEvidence | None
    argument_events_json: str
    diagnostic_events_json: str
    diagnostics_complete: bool
    omitted_events: int
    # 这是已安排的等待额度；取消或截止可缩短实际等待，不声称已经等待完毕。
    retry_wait_seconds: float | None = None
    diagnostics_expired_at: float | None = None
    diagnostics_unavailable: bool = False


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """持久化查询投影；未结束的尝试只有 start，重启后显式标记 interrupted。"""

    start: AttemptStart
    created_at: float
    finished_at: float | None
    result: AttemptEvidence | None


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """查询一个逻辑请求及按实际顺序排列的尝试，不触发新调用。"""

    request: RequestEvidence
    state: RequestOutcome | Literal["running"]
    created_at: float
    finished_at: float | None
    attempts: tuple[AttemptRecord, ...]
