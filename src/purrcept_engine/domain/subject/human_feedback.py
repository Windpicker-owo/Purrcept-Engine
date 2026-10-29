"""定义人工行为评价的输入边界，保存与调度由应用和同库反馈账本承担。

评价表达审查者对一次行动的好坏判断及改进建议，不改变行动的真实执行状态。
主脑将其作为后续行动的指导，反思将其作为有来源的评价证据。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HumanFeedback(BaseModel):
    """本机审查者提交的评价；允许只评分，留言原样保留供两个消费者阅读。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    rating: Literal["good", "bad"]
    comment: str = Field(default="", max_length=4000)
