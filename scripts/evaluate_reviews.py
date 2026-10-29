"""比较固定样本上的连续反思基线与局部评审结果，不调用任何模型。

输入是同一批样本的两组已记录结果；决策指标使用样本标签，记忆质量使用独立
人工审查标签。缺失用量或后续观察明确返回 null，不能解释为零成本或已改善。
本入口可用于合成替身验证指标，也可在另行授权的模型回放之后比较真实结果。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class Sample(BaseModel):
    """独立标注的审查场景，文本材料不携带此前评审的自我解释。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: str = Field(min_length=1)
    materials: str = Field(min_length=1)
    expected: Literal["no_intervention", "suggestion", "insufficient_evidence"]


class Usage(BaseModel):
    """实际服务端用量；缓存命中已包含在 input_tokens 中，不能重复相加。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def cache_is_part_of_input(self) -> Self:
        """拒绝不能解释为输入子集的缓存计数。"""
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("缓存输入不能超过全部输入。")
        return self


class Prediction(BaseModel):
    """一项回放结果；记忆与后续观察标签须由独立审查提供。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: str = Field(min_length=1)
    decision: Literal["no_intervention", "suggestion", "insufficient_evidence"]
    usage: Usage | None = None
    memories: tuple[Literal["valid", "duplicate", "wrong_generalization"], ...] | None = None
    observations: tuple[Literal["improved", "recurred", "insufficient_samples"], ...] | None = None
    feedback_wait_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)


def evaluate(samples: tuple[Sample, ...], predictions: tuple[Prediction, ...]) -> dict[str, object]:
    """核验样本完全对应后计算指标，拒绝重复或缺失样本造成的选择偏差。

    Args:
        samples: 带独立决策标签的同一批冻结场景。
        predictions: 同一方案对所有场景的记录，缺失证据保持未知。

    Returns:
        决策、用量、记忆质量、观察结果和人工评价等待时间的可解释统计。
    """
    expected = {sample.id: sample.expected for sample in samples}
    actual = {record.id: record for record in predictions}
    if (
        len(expected) != len(samples)
        or len(actual) != len(predictions)
        or expected.keys() != actual.keys()
    ):
        raise ValueError("样本身份必须唯一且完整对应。")
    tp = fp = fn = 0
    for identity, record in actual.items():
        positive = expected[identity] == "suggestion"
        proposed = record.decision == "suggestion"
        tp += positive and proposed
        fp += not positive and proposed
        fn += positive and not proposed
    usage_complete = all(record.usage is not None for record in predictions)
    measured = tuple(record.usage for record in predictions if record.usage is not None)
    input_tokens = sum(item.input_tokens for item in measured)
    output_tokens = sum(item.output_tokens for item in measured)
    cached_tokens = sum(item.cached_input_tokens for item in measured)
    memory_complete = all(record.memories is not None for record in predictions)
    memories = tuple(label for record in predictions for label in (record.memories or ()))
    observations = tuple(label for record in predictions for label in (record.observations or ()))
    observed = sum(label != "insufficient_samples" for label in observations)
    waits = tuple(
        record.feedback_wait_seconds
        for record in predictions
        if record.feedback_wait_seconds is not None
    )
    return {
        "samples": len(samples),
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "valuable_suggestion_ratio": tp / (tp + fp) if tp + fp else None,
        "miss_rate": fn / (tp + fn) if tp + fn else None,
        "exact_decision_ratio": sum(
            record.decision == expected[record.id] for record in predictions
        )
        / len(samples)
        if samples
        else None,
        "usage_complete": usage_complete,
        "input_tokens": input_tokens if usage_complete else None,
        "output_tokens": output_tokens if usage_complete else None,
        "cached_input_tokens": cached_tokens if usage_complete else None,
        "tokens_per_valuable_suggestion": (input_tokens + output_tokens) / tp
        if usage_complete and tp
        else None,
        "memory_labels_complete": memory_complete,
        "memory_duplicate_ratio": memories.count("duplicate") / len(memories)
        if memory_complete and memories
        else None,
        "wrong_generalization_ratio": memories.count("wrong_generalization") / len(memories)
        if memory_complete and memories
        else None,
        "observed_opportunities": observed,
        "insufficient_observations": observations.count("insufficient_samples"),
        "recurrence_ratio": observations.count("recurred") / observed if observed else None,
        "feedback_wait_samples": len(waits),
        "mean_feedback_wait_seconds": sum(waits) / len(waits) if waits else None,
    }


def main() -> None:
    """读取显式给定的三份 JSON，不连接端点或读取运行数据库。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    samples = TypeAdapter(tuple[Sample, ...]).validate_json(args.samples.read_bytes())
    adapter = TypeAdapter(tuple[Prediction, ...])
    baseline = adapter.validate_json(args.baseline.read_bytes())
    candidate = adapter.validate_json(args.candidate.read_bytes())
    print(
        json.dumps(
            {"baseline": evaluate(samples, baseline), "candidate": evaluate(samples, candidate)},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
