"""用真实临时Store验证反思补读来源的结案与分页责任。

模型可以复核初始批次以外的已呈现材料；这些范围必须同步同用途队列，
否则完整末页会再次被调度。测试同时保留其他用途及在途批次的所有权。
"""

import sqlite3
from pathlib import Path
from typing import Literal

import pytest

from purrcept_engine.domain.evidence.models import (
    BehaviorConclusion,
    EvidenceSnapshot,
    FinishSubmission,
    Review,
)
from purrcept_engine.infrastructure.sqlite.store import Store


@pytest.mark.parametrize("purpose", ["behavior"])
@pytest.mark.parametrize("page_chars", [4, 8000])
@pytest.mark.parametrize("extra_claimed", [False, True])
async def test_extra_review_settles_only_covered_source_and_purpose(
    tmp_path: Path, purpose: Literal["behavior"], page_chars: int, extra_claimed: bool
) -> None:
    """补读完整页离开待办，补读前缀从确切下一页继续，另一用途仍从头读取。"""
    store = await Store.open(tmp_path / "engine.sqlite3")
    try:
        for identity in ("assigned", "extra"):
            await store.evidence.put_evidence(
                EvidenceSnapshot(
                    evidence_id=identity,
                    version=1,
                    kind="test",
                    text="abcdefgh",
                    source_json="{}",
                    occurred_at=1.0,
                )
            )
        assigned = await store.evidence.read_evidence("assigned")
        extra = await store.evidence.read_evidence("extra", max_chars=page_chars)
        assert assigned.slice is not None and extra.slice is not None
        state = await store.feedback.reflection_state(purpose=purpose)
        if extra_claimed:
            assert await store.feedback.reserve_reflection(
                batch_id="other",
                epoch=state.epoch,
                pages=(extra,),
                successful_turns=0,
                purpose=purpose,
            )
        assert await store.feedback.reserve_reflection(
            batch_id="review",
            epoch=state.epoch,
            pages=(assigned,),
            successful_turns=0,
            purpose=purpose,
        )
        batch = await store.evidence.begin_reflection(
            "review",
            (assigned.slice.slice_ref,),
            purpose=purpose,
        )
        references = (assigned.slice.slice_ref, extra.slice.slice_ref)
        await store.evidence.mark_presented(batch.batch_id, batch.version, references)
        await store.evidence.finish_reflection(
            batch.batch_id,
            batch.version,
            FinishSubmission(
                reviews=(
                    Review(
                        issue="补读复核",
                        evidence=references,
                        conclusion="已核对实际材料。",
                        reason="无需新增认识。",
                    ),
                ),
                deferred=(),
                findings=(),
                behavior=BehaviorConclusion(
                    decision="no_intervention", conclusion="无需干预。", evidence=references
                )
                if purpose == "behavior"
                else None,
            ),
        )
        sources = await store.feedback.reflection_sources(epoch=state.epoch, purpose=purpose)
        if extra_claimed:
            assert sources == ()
            with sqlite3.connect(tmp_path / "engine.sqlite3") as connection:
                assert connection.execute(
                    "SELECT state,batch_id FROM analysis_work WHERE evidence_id='extra' "
                    "AND purpose=?",
                    (purpose,),
                ).fetchone() == ("processing", "other")
            assert not await store.feedback.reserve_reflection(
                batch_id="steal",
                epoch=state.epoch,
                pages=(extra,),
                successful_turns=0,
                purpose=purpose,
            )
        elif page_chars == 4:
            assert len(sources) == 1 and sources[0].evidence_id == "extra"
            assert sources[0].cursor == extra.next_cursor
            next_page = await store.evidence.read_evidence("extra", cursor=sources[0].cursor)
            assert next_page.text == "efgh"
        else:
            assert extra.next_cursor is None and sources == ()
        assert store.authority_available
    finally:
        await store.close()
