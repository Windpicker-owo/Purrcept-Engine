"""保存公开值及其语义校验，不打开数据库或调用设备。"""

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
