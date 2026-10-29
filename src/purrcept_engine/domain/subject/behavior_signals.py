"""从有限的实际对外文字中提取待核验线索，不自动判定不当行为。

调用方只提供确认发送的内置输出及确切证据身份。这里比较规范化开头与长度，
不读取模型的内部最终文字，也不将常见措辞直接转为监管建议。原文仍由证据端口
取得；统计截取只限制计算成本，不能代替实际呈现或作为完整历史的证明。
"""

from __future__ import annotations

from purrcept_engine.values import JsonObject, JsonValue


def repetition_signals(outputs: tuple[tuple[str, str], ...]) -> JsonObject:
    """为最近至多四十项确认输出生成有限前缀重复和长度统计。

    Args:
        outputs: 从新到旧排列的证据身份与真实发送文字，不包含未知结果。

    Returns:
        至多八个重复前缀线索及明确样本覆盖；所有线索均须由评审核验语境。
    """
    groups: dict[str, list[str]] = {}
    lengths: list[int] = []
    for identity, text in outputs[:40]:
        normalized = " ".join(text[:4096].casefold().split())
        lengths.append(len(text))
        if len(normalized) >= 12:
            groups.setdefault(normalized[:12], []).append(identity)
    repeated: list[JsonValue] = []
    for prefix, references in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
        if len(references) >= 3 and len(repeated) < 8:
            repeated.append(
                {"prefix": prefix, "occurrences": len(references), "evidence_ids": list(references)}
            )
    return {
        "signal_only": True,
        "sample_count": len(lengths),
        "repeated_prefixes": repeated,
        "recent_mean_chars": sum(lengths[:5]) / min(5, len(lengths)) if lengths else None,
        "coverage": "最近四十项已确认内置文字输出；重复开头可能合理，不能据此直接纠偏。",
    }
