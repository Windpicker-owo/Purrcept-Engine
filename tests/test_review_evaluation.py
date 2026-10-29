"""验证回放指标的分母、缓存计数和未知证据；样本不代表真实模型效果。"""

from __future__ import annotations

import pytest

from scripts.evaluate_reviews import Prediction, Sample, Usage, evaluate


def test_metrics_keep_unknown_evidence_and_do_not_double_count_cache() -> None:
    samples = (
        Sample(id="normal", materials="正常", expected="no_intervention"),
        Sample(id="issue", materials="问题", expected="suggestion"),
    )
    results = (
        Prediction(
            id="normal",
            decision="suggestion",
            usage=Usage(input_tokens=100, output_tokens=10, cached_input_tokens=80),
            memories=("duplicate",),
        ),
        Prediction(
            id="issue",
            decision="suggestion",
            usage=Usage(input_tokens=50, output_tokens=5),
            memories=("wrong_generalization",),
            observations=("recurred", "insufficient_samples"),
        ),
    )
    result = evaluate(samples, results)
    assert result["valuable_suggestion_ratio"] == 0.5
    assert result["tokens_per_valuable_suggestion"] == 165
    assert result["cached_input_tokens"] == 80
    assert result["wrong_generalization_ratio"] == 0.5
    assert result["recurrence_ratio"] == 1
    unknown = evaluate(
        samples, tuple(Prediction(id=sample.id, decision=sample.expected) for sample in samples)
    )
    assert unknown["input_tokens"] is None and unknown["recurrence_ratio"] is None


def test_metric_inputs_must_cover_every_frozen_sample_once() -> None:
    sample = Sample(id="one", materials="样本", expected="no_intervention")
    with pytest.raises(ValueError, match="完整对应"):
        evaluate((sample,), ())
    with pytest.raises(ValueError, match="完整对应"):
        evaluate(
            (sample,),
            (
                Prediction(id="one", decision="no_intervention"),
                Prediction(id="one", decision="no_intervention"),
            ),
        )
