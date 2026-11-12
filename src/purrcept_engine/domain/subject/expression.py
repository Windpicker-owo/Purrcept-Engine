"""注意判断附带的身体语义及有限交接契约，不拥有动作或设备。

评估器验证模型的情绪和语境选择，协调者在批次采用后才调用 ExpressionSink。
连续姿态、空间坐标、置信门槛和衰减由接收方决定；本层不把判断变成世界行动。
"""

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

type Emotion = Literal[
    "unchanged", "neutral", "warm", "concerned", "curious", "excited", "displeased"
]
type Focus = Literal["unchanged", "audience", "chat", "game", "work", "speaker", "away"]


class ExpressionAssessment(BaseModel):
    """一次评估的不可变语义，不携带模型可任意延长的时间或设备坐标。

    Attributes:
        emotion: 主体面对刺激的情绪倾向；unchanged 表示没有新的依据。
        emotion_confidence: 后端提供的置信指标，不是跨模型校准的正确率。
        focus: 当前值得关注的语境对象；不是指令，不要求执行相关活动。
        focus_confidence: 同一次判断中注视选择的独立置信指标。

    Note:
        decision 使用原始 ChoiceAnswer.confidence；generation 使用模型显式报告值。
        不从唤醒级别或文本语气伪造置信度，未报告数值属于协议错误。
        专用判断明确拒答某一附加问题时，该字段用 unchanged/0 表示不可用，
        其中 0 是不采用标记，不是后端置信估计。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)
    emotion: Emotion
    emotion_confidence: float = Field(ge=0, le=1)
    focus: Focus
    focus_confidence: float = Field(ge=0, le=1)


class ExpressionSink(Protocol):
    """应用绑定唯一身体所有者，由协调者同步交付已采用的判断。

    内置实现是 Live2DRuntime.observe_expression。宿主同一事件循环串行调用，
    不等待网络、不创建模型请求、不转移设备所有权。离线或已到期直接拒绝，
    不保留恢复队列；内部错误原样传播，不能伪装为外围不可用。
    """

    def __call__(self, assessment: ExpressionAssessment, *, deadline: float, urgent: bool) -> None:
        """提交短期观察，是否采用各字段由身体本地策略决定。

        Args:
            assessment: 已通过模型协议和批次版本校验的不可变判断。
            deadline: 宿主单调时钟中的最晚失效时间，不得延长。
            urgent: 本批被判为 urgent，可打断语境注视保持，不能抢占显式动作。

        Note:
            关闭主体借用后才能释放身体资源。返回不证明 VTS 已发送或真实效果已验收。
        """
        ...
