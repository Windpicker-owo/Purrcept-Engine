"""把认识账本事务交给 Store 的唯一有界队列，不创建另一连接或线程。

KnowledgeLedger 是认识子系统的异步端口。SQL 和业务校验在 knowledge 模块，
这里只拥有异步交接、实际材料字节计量和读写队列选择；取消/故障/连接寿命
沿 Store 契约传播。原文分页会保存不可变片段/游标，因此也是权威写入。
"""

import hashlib
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Protocol

from purrcept_core.models import ContentBlock, Message

from . import knowledge as sql
from .model_requests import encode
from .models import MediaAsset, MediaReference


class _Submit(Protocol):
    """Store 内部的泛型队列接缝，连接只在其专属线程执行 operation 时出现。"""

    def __call__[Result](
        self,
        operation: Callable[[sqlite3.Connection], Result],
        *,
        logical_bytes: int = 0,
        control: bool = False,
        writing: bool = False,
    ) -> Awaitable[Result]:
        """接管有限事务并返回实际结果，等待取消不撤销已排队写入。"""
        ...


def _bytes(*values: object) -> int:
    """只有公开值进入交接额度，Pydantic 记录须先转为完整 JSON 表示。"""
    return len(encode(values).encode("utf-8"))


class KnowledgeLedger:
    """Store 创建的认识账本端口，与其母 Store 同事件循环和同资源寿命。

    主体借用本对象；设备插件只取得 app 提供的有限原文门面。接口从不
    将连接或任意 SQL 传给调用者。业务冲突保持可纠正原异常；内部/权威
    失败不会改为空结果，后续资格由 Store 撤销。
    """

    def __init__(self, submit: _Submit) -> None:
        """借入唯一队列，不打开库或创建消费者任务。"""
        self._submit = submit

    async def capture(
        self,
        snapshot: sql.EvidenceSnapshot,
        assets: tuple[MediaAsset, ...],
    ) -> sql.EvidenceSnapshot:
        """原子接管工具取得的原生媒体及原文，不创建第二条外界输入批次。"""
        expected = tuple(
            MediaReference(
                hashlib.sha256(asset.content).hexdigest(),
                asset.media_type,
                len(asset.content),
                asset.name,
            )
            for asset in assets
        )
        if expected != snapshot.media:
            raise sql.KnowledgeInputError("捕获原文的媒体引用与完整字节不一致。")

        def commit(connection: sqlite3.Connection) -> sql.EvidenceSnapshot:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                for reference, asset in zip(expected, assets, strict=True):
                    connection.execute(
                        "INSERT OR IGNORE INTO media VALUES(?,?)",
                        (reference.media_id, asset.content),
                    )
                return sql.put_evidence_in_transaction(connection, snapshot, needs_reflection=False)

        return await self._submit(
            commit,
            logical_bytes=_bytes(snapshot.model_dump(mode="json"))
            + sum(len(asset.content) for asset in assets),
            writing=True,
        )

    async def put_evidence(
        self, snapshot: sql.EvidenceSnapshot, *, needs_reflection: bool = True
    ) -> sql.EvidenceSnapshot:
        """原文/媒体资格及可靠复核身份一并提交。"""
        return await self._submit(
            lambda connection: sql.put_evidence(
                connection, snapshot, needs_reflection=needs_reflection
            ),
            logical_bytes=_bytes(snapshot.model_dump(mode="json")),
            control=True,
            writing=True,
        )

    async def get_evidence(
        self, evidence_id: str, *, version: int | None = None, include_inactive: bool = False
    ) -> sql.EvidenceSnapshot | None:
        """读取确切原文资格，历史查询不授予当前引用权限。"""
        return await self._submit(
            lambda connection: sql.get_evidence(
                connection, evidence_id, version=version, include_inactive=include_inactive
            ),
            logical_bytes=_bytes(evidence_id),
        )

    async def get_slice(self, slice_ref: str) -> sql.EvidenceSlice | None:
        """读取宿主保存的范围身份，不从文本标记推断阅读资格。"""
        return await self._submit(
            lambda connection: sql.get_slice(connection, slice_ref), logical_bytes=_bytes(slice_ref)
        )

    async def read_evidence(
        self,
        evidence_id: str,
        *,
        level: str = "evidence",
        cursor: str | None = None,
        max_chars: int = 8000,
        version: int | None = None,
    ) -> sql.EvidencePage:
        """同快照范围读取并保存游标；取得正文不等于已向模型呈现。"""
        return await self._submit(
            lambda connection: sql.read_evidence(
                connection,
                evidence_id,
                level=level,
                cursor=cursor,
                max_chars=max_chars,
                version=version,
            ),
            logical_bytes=_bytes(evidence_id, cursor),
            control=True,
            writing=True,
        )

    async def read_evidence_cursor(self, cursor: str, *, max_chars: int = 8000) -> sql.EvidencePage:
        """用已保存的 opaque 游标查回原始快照，不建立插件分页缓存。"""
        return await self._submit(
            lambda connection: sql.read_evidence_cursor(connection, cursor, max_chars=max_chars),
            logical_bytes=_bytes(cursor),
            control=True,
            writing=True,
        )

    async def revoke_evidence(self, evidence_id: str, version: int) -> None:
        """撤销来源资格且保留原始材料和所有认识历史。"""
        await self._submit(
            lambda connection: sql.revoke_evidence(connection, evidence_id, version),
            logical_bytes=_bytes(evidence_id),
            control=True,
            writing=True,
        )

    async def pending_reflection(self, *, limit: int = 32) -> tuple[sql.PendingEvidence, ...]:
        """读取有限可靠批次，查询不会确认已读或理解。"""
        return await self._submit(
            lambda connection: sql.pending_reflection(connection, limit=limit)
        )

    async def begin_reflection(
        self,
        batch_id: str,
        required_slice_refs: tuple[str, ...],
        findings: tuple[sql.OperationalFinding, ...] = (),
    ) -> sql.ReflectionBatch:
        """冻结原文范围及程序异常，重复等待沿同一批次身份核验。"""
        return await self._submit(
            lambda connection: sql.begin_reflection(
                connection, batch_id, required_slice_refs, findings
            ),
            logical_bytes=_bytes(
                batch_id,
                required_slice_refs,
                tuple(item.model_dump(mode="json") for item in findings),
            ),
            control=True,
            writing=True,
        )

    async def get_reflection(self, batch_id: str, version: int) -> sql.ReflectionBatch:
        """读取当前资格或真实完成回执，不解析最终模型文字。"""
        return await self._submit(
            lambda connection: sql.get_reflection(connection, batch_id, version),
            logical_bytes=_bytes(batch_id),
        )

    async def abandon_reflection(
        self, batch_id: str, version: int, reason: str
    ) -> sql.ReflectionBatch:
        """结束本批写入资格，保留真实逐项修订与未确认范围，不伪造完成。"""
        return await self._submit(
            lambda connection: sql.abandon_reflection(connection, batch_id, version, reason),
            logical_bytes=_bytes(batch_id, reason),
            control=True,
            writing=True,
        )

    async def mark_presented(
        self, batch_id: str, version: int, slice_refs: tuple[str, ...]
    ) -> None:
        """实际成功模型请求的范围资格先提交，下一工具才能据此修订。"""
        await self._submit(
            lambda connection: sql.mark_presented(connection, batch_id, version, slice_refs),
            logical_bytes=_bytes(batch_id, slice_refs),
            control=True,
            writing=True,
        )

    async def record_delivery(
        self,
        batch_id: str,
        version: int,
        *,
        tool_call_id: str | None,
        material: Message | tuple[ContentBlock, ...],
        slice_refs: tuple[str, ...],
    ) -> sql.EvidenceDelivery:
        """原子登记原批次、完整材料、媒体和切片，不能提前取得已读资格。"""
        return await self._submit(
            lambda connection: sql.record_delivery(
                connection,
                batch_id,
                version,
                tool_call_id=tool_call_id,
                material=material,
                slice_refs=slice_refs,
            ),
            logical_bytes=_bytes(batch_id, material, slice_refs),
            control=True,
            writing=True,
        )

    async def deliveries_for_history(
        self, messages: tuple[Message, ...]
    ) -> tuple[sql.EvidenceDelivery, ...]:
        """只读核验当前完整历史中的原交付，返回原批次身份和确切切片关系。"""
        return await self._submit(
            lambda connection: sql.deliveries_for_history(connection, messages),
            logical_bytes=_bytes(messages),
        )

    async def revise_memory(
        self, batch_id: str, version: int, patch: sql.MemoryPatch
    ) -> sql.MemoryRecord:
        """逐项修订原子生效，后续批次失败不撤销已经确认的认识。"""
        return await self._submit(
            lambda connection: sql.revise_memory(connection, batch_id, version, patch),
            logical_bytes=_bytes(batch_id, patch.model_dump(mode="json")),
            control=True,
            writing=True,
        )

    async def get_memory(
        self,
        memory_id: str,
        *,
        version: int | None = None,
        include_inactive: bool = False,
        at: float | None = None,
    ) -> sql.MemoryRecord | None:
        """默认读取当前有效版本，显式历史显示失效原因及发生时间。"""
        return await self._submit(
            lambda connection: sql.get_memory(
                connection, memory_id, version=version, include_inactive=include_inactive, at=at
            ),
            logical_bytes=_bytes(memory_id),
        )

    async def record_conflict(
        self, left: sql.KnowledgeReference, right: sql.KnowledgeReference
    ) -> None:
        """保存明确未决关系，不让相似度自动淘汰任一认识。"""
        await self._submit(
            lambda connection: sql.record_conflict(connection, left, right),
            logical_bytes=_bytes(left.model_dump(mode="json"), right.model_dump(mode="json")),
            control=True,
            writing=True,
        )

    async def search_knowledge(
        self,
        query: str,
        *,
        entities: tuple[str, ...] = (),
        start: float | None = None,
        end: float | None = None,
        at: float | None = None,
        include_inactive: bool = False,
        limit: int = 40,
    ) -> tuple[sql.KnowledgeCandidate, ...]:
        """融合本地权威核验后的关键词、关系及时间候选。"""
        return await self._submit(
            lambda connection: sql.search_knowledge(
                connection,
                query,
                entities=entities,
                start=start,
                end=end,
                at=at,
                include_inactive=include_inactive,
                limit=limit,
            ),
            logical_bytes=_bytes(query, entities),
        )

    async def get_candidate(
        self,
        reference: sql.KnowledgeReference,
        *,
        include_inactive: bool = False,
        at: float | None = None,
    ) -> sql.EvidenceSnapshot | sql.MemoryRecord | None:
        """异步排名结束后复核实际版本，拒绝过时派生数据复活来源。"""
        return await self._submit(
            lambda connection: sql.get_candidate(
                connection, reference, include_inactive=include_inactive, at=at
            ),
            logical_bytes=_bytes(reference.model_dump(mode="json")),
        )

    async def pending_index(
        self, *, route: str | None = None, limit: int = 32
    ) -> tuple[sql.IndexJob, ...]:
        """读取有限可重建任务，关键词和语义路线各自确认。"""
        return await self._submit(
            lambda connection: sql.pending_index(connection, route=route, limit=limit)
        )

    async def apply_index(
        self,
        reference: sql.KnowledgeReference,
        *,
        terms: str,
        vector: tuple[float, ...] | None = None,
        model: str | None = None,
    ) -> bool:
        """仅安装仍有资格的确切版本；向量不成为原文权威。"""
        return await self._submit(
            lambda connection: sql.apply_index(
                connection, reference, terms=terms, vector=vector, model=model
            ),
            logical_bytes=_bytes(reference.model_dump(mode="json"), terms, vector, model),
            control=True,
            writing=True,
        )

    async def refresh_keyword_index(self, *, limit: int = 100) -> int:
        """在单事务队列维护一批本地索引，不等待模型或网络。"""
        return await self._submit(
            lambda connection: sql.refresh_keyword_index(connection, limit=limit),
            control=True,
            writing=True,
        )

    async def semantic_search(
        self, vector: tuple[float, ...], model: str, *, limit: int = 40
    ) -> tuple[sql.KnowledgeReference, ...]:
        """查询实际同模型派生向量，返回确切来源版本供复核。"""
        return await self._submit(
            lambda connection: sql.semantic_search(connection, vector, model, limit=limit),
            logical_bytes=_bytes(vector, model),
        )

    async def supervise(
        self, batch_id: str, version: int, notice: sql.SupervisionNotice
    ) -> sql.SupervisionRecord:
        """有据监管立即入账，后续前台回应另有自己的实际记录。"""
        return await self._submit(
            lambda connection: sql.supervise(connection, batch_id, version, notice),
            logical_bytes=_bytes(batch_id, notice.model_dump(mode="json")),
            control=True,
            writing=True,
        )

    async def pending_supervision(self, *, limit: int = 32) -> tuple[sql.SupervisionRecord, ...]:
        """读取仍符合来源/期限/活动版本的待回应意见。"""
        return await self._submit(
            lambda connection: sql.pending_supervision(connection, limit=limit)
        )

    async def finish_reflection(
        self, batch_id: str, version: int, submission: sql.FinishSubmission
    ) -> sql.ReflectionReceipt:
        """原子确认具体覆盖、真实认识修订与邮箱交接，普通文字无此权限。"""
        return await self._submit(
            lambda connection: sql.finish_reflection(connection, batch_id, version, submission),
            logical_bytes=_bytes(batch_id, submission.model_dump(mode="json")),
            control=True,
            writing=True,
        )

    async def plan_wakeup(self, plan: sql.WakeupPlan) -> sql.WakeupPlan:
        """保存明确未来计划，注意 defer 的默认期限不用于它。"""
        return await self._submit(
            lambda connection: sql.plan_wakeup(connection, plan),
            logical_bytes=_bytes(plan.model_dump(mode="json")),
            control=True,
            writing=True,
        )

    async def resolve_plan(self, identity: str, outcome: str, evidence: str) -> sql.WakeupPlan:
        """以真实来源结案未完成计划，不能自动填入完成结论。"""
        return await self._submit(
            lambda connection: sql.resolve_plan(connection, identity, outcome, evidence),
            logical_bytes=_bytes(identity, outcome, evidence),
            control=True,
            writing=True,
        )

    async def pending_plans(
        self, *, due_before: float | None = None, limit: int = 32
    ) -> tuple[sql.WakeupPlan, ...]:
        """读取有限未完成计划，到期交接仍由主体调度事务确认。"""
        return await self._submit(
            lambda connection: sql.pending_plans(connection, due_before=due_before, limit=limit)
        )

    async def respond_supervision(
        self, identity: str, decision: str, reason: str
    ) -> sql.SupervisionRecord:
        """保存前台回应及理由，回应不证明该监管意见正确。"""
        return await self._submit(
            lambda connection: sql.respond_supervision(connection, identity, decision, reason),
            logical_bytes=_bytes(identity, decision, reason),
            control=True,
            writing=True,
        )
