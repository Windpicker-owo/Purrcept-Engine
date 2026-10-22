"""用真实 Core 轮次边界验证历史保护、候选缩小及原子安装端口。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from purrcept_core.models import (
    ConversationCheckpoint,
    ConversationState,
    Message,
    ModelContinuation,
    ReminderState,
    SystemReminder,
    TextBlock,
)

from purrcept_engine.domain.context.capacity import GenerationCapacity, assess_capacity
from purrcept_engine.domain.context.maintenance import (
    Reservation,
    SummaryError,
    SummaryProposal,
    SummarySource,
    build_summary_proposal,
    candidate_checkpoint,
    freeze_summary_source,
    install_summary,
    validate_summary_proposal,
)
from purrcept_engine.errors import StorageError


def checkpoint() -> ConversationCheckpoint:
    """六个完整真实轮次后还有当前未提交输入，不按消息条数保护。"""
    messages: list[Message] = []
    boundaries: list[int] = []
    for index in range(6):
        messages.extend(
            (Message.user(f"source-{index}:" + "x" * 100), Message.assistant("response"))
        )
        boundaries.append(len(messages))
    messages.append(Message.user("current"))
    return ConversationCheckpoint(
        ConversationState(
            tuple(messages),
            turn_index=6,
            turn_boundaries=tuple(boundaries),
            continuation=ModelContinuation("provider", {"cursor": "opaque"}),
        ),
        ReminderState((SystemReminder((TextBlock("tail"),)),)),
    )


def source() -> SummarySource:
    frozen = freeze_summary_source(checkpoint(), owner_id="foreground", source_version=3)
    assert frozen is not None
    return frozen


def payload(value: ConversationCheckpoint) -> dict[str, object]:
    """完整候选计量仍包括固定目录与每次动态尾部，不只计历史文字。"""
    return {
        "model": "explicit/model",
        "max_tokens": 100,
        "messages": [
            {"role": "system", "content": "fixed catalog " + "z" * 450},
            *(
                {"role": message.role.value, "content": message.text}
                for message in value.history.messages
            ),
            {"role": "user", "content": "dynamic tail"},
        ],
    }


def proposal() -> SummaryProposal:
    frozen = source()
    summary = Message.user("summary with original source references")
    candidate = candidate_checkpoint(frozen, summary)
    large = GenerationCapacity(100000, 100)
    candidate_size = assess_capacity(payload(candidate), large).estimate.input_units
    # 将实际候选设在约60%以证明0.5不是安装硬门槛，完整原请求仍更大。
    limits = GenerationCapacity(int(candidate_size / 0.6 / 0.9) + 100, 100)
    before = assess_capacity(payload(frozen.checkpoint), limits)
    after = assess_capacity(payload(candidate), limits)
    return build_summary_proposal(
        frozen, summary, summary_request_id="summary-request", before=before, after=after
    )


def test_recent_complete_turns_current_suffix_and_unresolved_material_are_protected() -> None:
    """保护两轮真实边界及当前输入，旧未决材料进一步限制可压缩前缀。"""
    original = checkpoint()
    frozen = source()
    assert frozen.end_turn == 4 and frozen.end_message == 8
    assert frozen.messages == original.history.messages[:8]
    pinned = freeze_summary_source(
        original, owner_id="foreground", source_version=3, protected_message_indices=(5,)
    )
    assert pinned is not None and pinned.end_turn == 2 and pinned.end_message == 4
    assert (
        freeze_summary_source(
            original, owner_id="foreground", source_version=3, protected_message_indices=(0,)
        )
        is None
    )


def test_summary_occupies_no_fake_turn_and_preserves_all_other_state() -> None:
    """摘要占位不多算完整交互，当前及最近两轮的整个过程原样保留。"""
    frozen = source()
    candidate = candidate_checkpoint(frozen, Message.user("summary"))
    assert candidate.history.messages[1:] == frozen.checkpoint.history.messages[8:]
    assert candidate.history.turn_index == 2 and candidate.history.turn_boundaries == (3, 5)
    assert candidate.history.continuation == frozen.checkpoint.history.continuation
    assert candidate.reminders == frozen.checkpoint.reminders
    next_source = freeze_summary_source(candidate, owner_id="foreground", source_version=4)
    assert next_source is None


@pytest.mark.parametrize("invalid_budget", (0, -1, True))
def test_manual_summary_cannot_forge_a_nonpositive_or_boolean_denominator(
    invalid_budget: int,
) -> None:
    """提交边界不信任手工 dataclass，非法分母不能绕过真实缩小校验。"""
    valid = proposal()
    forged = replace(
        valid,
        before=replace(valid.before, input_budget=invalid_budget),
        after=replace(valid.after, input_budget=invalid_budget),
    )
    with pytest.raises(SummaryError, match="正整数输入分母"):
        validate_summary_proposal(forged)


@pytest.mark.parametrize("invalid_usage", (-1, True))
def test_manual_summary_cannot_forge_negative_or_boolean_usage(invalid_usage: int) -> None:
    """非法使用量不能被当作一个已经缩小、在容量内的候选。"""
    valid = proposal()
    forged = replace(
        valid,
        after=replace(
            valid.after, estimate=replace(valid.after.estimate, input_units=invalid_usage)
        ),
    )
    with pytest.raises(SummaryError, match="非负整数用量"):
        validate_summary_proposal(forged)


def test_coverage_identity_does_not_reset_due_to_new_version_or_tail_reminder() -> None:
    """没有新增可压缩来源时，同覆盖的可靠已试记录仍能阻止重复摘要。"""
    first = source()
    changed = replace(
        first.checkpoint, reminders=ReminderState((SystemReminder((TextBlock("new tail"),)),))
    )
    second = freeze_summary_source(changed, owner_id="foreground", source_version=99)
    assert second is not None
    assert second.coverage_id == first.coverage_id


def test_valid_candidate_above_target_can_be_installed() -> None:
    """真实完整请求缩小且在预算内即可，约60%候选不因50%目标被拒绝。"""
    candidate = proposal()
    assert candidate.after.within_budget and not candidate.after.target_met
    assert 0.5 < candidate.after.ratio < 0.8
    assert candidate.before.estimate.input_units > candidate.after.estimate.input_units
    validate_summary_proposal(candidate)


def test_modified_source_coverage_cannot_bypass_recorded_protection() -> None:
    """Source也保存保护条件，手工扩展覆盖不能删除最近两轮或未决材料。"""
    frozen = source()
    forged = replace(frozen, end_turn=5, end_message=10)
    with pytest.raises(SummaryError, match="保护"):
        candidate_checkpoint(forged, Message.user("summary"))
    forged = replace(frozen, coverage_id="wrong-identity")
    with pytest.raises(SummaryError, match="身份"):
        candidate_checkpoint(forged, Message.user("summary"))


@pytest.mark.parametrize("change", ["protected", "growth", "overflow", "denominator"])
def test_invalid_proposal_keeps_protected_material_and_capacity_constraints(change: str) -> None:
    """来源之外的改写、没有缩小、仍超限或换分母不能安装为成功摘要。"""
    candidate = proposal()
    if change == "protected":
        candidate = replace(
            candidate, candidate=replace(candidate.candidate, reminders=ReminderState())
        )
    elif change == "growth":
        candidate = replace(candidate, after=candidate.before)
    elif change == "overflow":
        candidate = replace(
            candidate,
            after=replace(
                candidate.after,
                estimate=replace(
                    candidate.after.estimate, input_units=candidate.after.input_budget + 1
                ),
            ),
        )
    else:
        candidate = replace(
            candidate, after=replace(candidate.after, input_budget=candidate.after.input_budget + 1)
        )
    with pytest.raises(SummaryError):
        validate_summary_proposal(candidate)


async def test_install_port_returns_version_only_after_commit_and_preserves_failures() -> None:
    """版本/CAS/空闲事务由生产Store实现；本单元只验证委托与失败传播。"""

    class StorePort:
        def __init__(self) -> None:
            self.expected: SummaryProposal | None = None
            self.result: int | None = 4
            self.error: StorageError | None = None

        async def reserve_summary(self, source: SummarySource) -> Reservation:
            return "reserved"

        async def install_summary(self, proposal: SummaryProposal) -> int | None:
            self.expected = proposal
            if self.error is not None:
                raise self.error
            return self.result

    port = StorePort()
    candidate = proposal()
    assert await install_summary(port, candidate) == 4
    assert port.expected is candidate
    port.result = None
    assert await install_summary(port, candidate) is None
    failure = StorageError("synthetic atomic write fault")
    port.error = failure
    with pytest.raises(StorageError) as caught:
        await install_summary(port, candidate)
    assert caught.value is failure
    assert source().checkpoint == candidate.source.checkpoint
