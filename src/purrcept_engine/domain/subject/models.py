"""保存公开值及其语义校验，不打开数据库或调用设备。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type SessionScope = Literal["foreground", "reflection"]


type InputStage = Literal[
    "attention", "deferred", "foreground", "processing", "processed", "failed"
]


type AttentionLevel = Literal["observe", "defer", "wake", "urgent"]


type TurnOutcome = Literal["running", "succeeded", "failed", "cancelled", "interrupted"]


@dataclass(frozen=True, slots=True)
class InputProcessing:
    """输入处置的版本事实；筛选与处理结束不等同于模型理解或外界输出。"""

    input_id: str
    version: int
    stage: InputStage
    reason: str
    attention_request_id: str | None
    due_at: float | None
    handed_off: bool
    admitted_at: float


@dataclass(frozen=True, slots=True)
class MinimumWakeAssessment:
    """直接唤起后的一次紧急升级资格，绑定创建它的宿主与旧生成代次。

    记录不另建前台待办。input_version 冻结仍在 foreground 的处置身份；
    一旦被前台接管或宿主重启，旧结果只能留下判断事实，不能抢占新轮次。
    """

    input_id: str
    owner_id: str
    observed_generation: int
    input_version: int


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """同一会话的已提交版本及完整 Core 检查点编码，不包含临时轮次。"""

    scope: SessionScope
    version: int
    checkpoint_json: str | None
    active_turn: str | None


@dataclass(frozen=True, slots=True)
class TurnRecord:
    """一次主动处理的独立身份；失败材料与真实回执通过来源身份关联。"""

    turn_id: str
    scope: SessionScope
    session_version: int
    generation: int
    state: TurnOutcome
    input_ids: tuple[str, ...]
    started_at: float
    finished_at: float | None
    error_json: str | None
