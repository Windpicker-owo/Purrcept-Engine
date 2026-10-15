"""定义输入处置、最低唤起判断和连续会话的版本事实。

注意选择与前台接管使用确切版本和代次，前台与反思分别保存历史检查点。
本模块不推进模型、派发行动或根据处理结束推断已经对外回复。
"""

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
    """输入处置的版本事实；筛选与处理结束不等同于模型理解或外界输出。

    Attributes:
        input_id: 正式接管返回的输入身份，用于关联原文与后续处理。
        version: 目标记录的确切版本，区别于可变的当前版本。
        stage: 输入当前或待查询的处理阶段。
        reason: 供后续核对的明确处置原因。
        attention_request_id: 做出本次注意选择的真实逻辑模型请求身份。
        due_at: 明确延后的到期时刻；None 表示无延期。
        handed_off: 是否已经按原输入身份完成前台交接。
        admitted_at: 权威接管此工作的 Unix 时间戳（秒）。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

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

    Attributes:
        input_id: 正式接管返回的输入身份，用于关联原文与后续处理。
        owner_id: 发起本次工作并持有提交资格的所有者身份。
        observed_generation: 接管或判断开始时观察到的前台生成代次。
        input_version: 输入处置在登记升级判断时的确切版本。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        记录不另建前台待办。input_version 冻结仍在 foreground 的处置身份；
        一旦被前台接管或宿主重启，旧结果只能留下判断事实，不能抢占新轮次。
    """

    input_id: str
    owner_id: str
    observed_generation: int
    input_version: int


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """同一会话的已提交版本及完整 Core 检查点编码，不包含临时轮次。

    Attributes:
        scope: 目标连续会话，foreground 与 reflection 拥有独立历史。
        version: 目标记录的确切版本，区别于可变的当前版本。
        checkpoint_json: 完整 Core 检查点的编码；失败轮次可不安装新历史。
        active_turn: 当前被接管且尚未结束的轮次身份；None 表示会话空闲。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    scope: SessionScope
    version: int
    checkpoint_json: str | None
    active_turn: str | None


@dataclass(frozen=True, slots=True)
class TurnRecord:
    """一次主动处理的独立身份；失败材料与真实回执通过来源身份关联。

    Attributes:
        turn_id: 连续会话中本轮处理的身份。
        scope: 目标连续会话，foreground 与 reflection 拥有独立历史。
        session_version: 轮次开始前的已提交会话版本。
        generation: 当前会话或设备的代次，用于拒绝迟到结果更新新状态。
        state: 当前所有者持有的状态或待提交状态值。
        input_ids: 本轮接管的输入身份集合，保留真实因果关联。
        started_at: 本次真实工作开始的时刻，未开始时保留 None。
        finished_at: 此记录实际结束的 Unix 时间戳；None 表示尚未保存结束事实。
        error_json: 编码后的失败证据，供后续交互追溯原轮次。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    turn_id: str
    scope: SessionScope
    session_version: int
    generation: int
    state: TurnOutcome
    input_ids: tuple[str, ...]
    started_at: float
    finished_at: float | None
    error_json: str | None
