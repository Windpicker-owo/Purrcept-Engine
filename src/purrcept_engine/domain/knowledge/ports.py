"""定义领域协作契约与不可变设置；调用与所有权由应用显式连接。"""

from __future__ import annotations

from typing import Protocol

from purrcept_core.models import ContentBlock, Message

from purrcept_engine.domain.knowledge.models import (
    EvidenceDelivery,
    EvidencePage,
    EvidenceSlice,
    EvidenceSnapshot,
    FinishSubmission,
    IndexJob,
    KnowledgeCandidate,
    KnowledgeReference,
    MemoryPatch,
    MemoryRecord,
    OperationalFinding,
    PendingEvidence,
    ReflectionBatch,
    ReflectionReceipt,
    SupervisionNotice,
    SupervisionRecord,
    WakeupPlan,
)


class KnowledgeStore(Protocol):
    """Store 专属线程提供的认识账本接口，所有写操作确认实际事务完成。

    取消等待不撤销提交，模型、网络和媒体解码不进入事务。校验来源/版本的
    业务冲突可纠正；权威存储及内部故障原样传出，不能翻译为空结果或输入错。
    """

    async def put_evidence(
        self, snapshot: EvidenceSnapshot, *, needs_reflection: bool = True
    ) -> EvidenceSnapshot:
        """保存原文及可靠待复核关系；重复接管不改写原文。"""
        ...

    async def get_evidence(
        self, evidence_id: str, *, version: int | None = None, include_inactive: bool = False
    ) -> EvidenceSnapshot | None:
        """读取原文资格；历史查询不等于允许引用撤销来源。"""
        ...

    async def get_slice(self, slice_ref: str) -> EvidenceSlice | None:
        """读取不可变片段身份及确切范围。"""
        ...

    async def read_evidence(
        self,
        evidence_id: str,
        *,
        level: str = "evidence",
        cursor: str | None = None,
        max_chars: int = 8000,
        version: int | None = None,
    ) -> EvidencePage:
        """创建/续读相同快照片段，取得不登记模型已读。"""
        ...

    async def revoke_evidence(self, evidence_id: str, version: int) -> None:
        """撤销引用资格，保留原文与全部认识版本。"""
        ...

    async def read_evidence_cursor(self, cursor: str, *, max_chars: int = 8000) -> EvidencePage:
        """由已保存的不透明游标恢复固定版本，插件无需解析或缓存来源身份。"""
        ...

    async def pending_reflection(self, *, limit: int = 32) -> tuple[PendingEvidence, ...]:
        """读取有限可靠来源及已有范围结论，不确认交付。"""
        ...

    async def begin_reflection(
        self,
        batch_id: str,
        required_slice_refs: tuple[str, ...],
        findings: tuple[OperationalFinding, ...] = (),
    ) -> ReflectionBatch:
        """冻结范围和异常，恢复代次不能借用旧资格。"""
        ...

    async def get_reflection(self, batch_id: str, version: int) -> ReflectionBatch:
        """取得权威批次/完成回执，不从最终文字推断完成。"""
        ...

    async def mark_presented(
        self, batch_id: str, version: int, slice_refs: tuple[str, ...]
    ) -> None:
        """实际请求成功门槛记录已呈现范围，工具创建片段不能调用它。"""
        ...

    async def record_delivery(
        self,
        batch_id: str,
        version: int,
        *,
        tool_call_id: str | None,
        material: Message | tuple[ContentBlock, ...],
        slice_refs: tuple[str, ...],
    ) -> EvidenceDelivery:
        """登记本批真实完整交付及媒体；保存不等于实际请求已呈现。"""
        ...

    async def deliveries_for_history(
        self, messages: tuple[Message, ...]
    ) -> tuple[EvidenceDelivery, ...]:
        """只恢复与当前真实历史完整匹配的原交付，不扫描正文中的身份标记。"""
        ...

    async def abandon_reflection(self, batch_id: str, version: int, reason: str) -> ReflectionBatch:
        """撤销失败代次写资格，保留已提交认识和未覆盖待办；不自行安排重试。"""
        ...

    async def revise_memory(self, batch_id: str, version: int, patch: MemoryPatch) -> MemoryRecord:
        """逐次原子修订，批次后续失败不回滚已写入认识。"""
        ...

    async def get_memory(
        self,
        memory_id: str,
        *,
        version: int | None = None,
        include_inactive: bool = False,
        at: float | None = None,
    ) -> MemoryRecord | None:
        """默认读取当前有效认识，显式历史保留失效原因。"""
        ...

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
    ) -> tuple[KnowledgeCandidate, ...]:
        """取得权威核验后的关键词、关系、时间候选。"""
        ...

    async def get_candidate(
        self,
        reference: KnowledgeReference,
        *,
        include_inactive: bool = False,
        at: float | None = None,
    ) -> EvidenceSnapshot | MemoryRecord | None:
        """异步检索返回后再核验确切版本及来源。"""
        ...

    async def pending_index(
        self, *, route: str | None = None, limit: int = 32
    ) -> tuple[IndexJob, ...]:
        """分页读取显式路线的可重建版本任务。"""
        ...

    async def apply_index(
        self,
        reference: KnowledgeReference,
        *,
        terms: str,
        vector: tuple[float, ...] | None = None,
        model: str | None = None,
    ) -> bool:
        """只安装仍有效的同版本派生值，迟到任务不能复活认识。"""
        ...

    async def refresh_keyword_index(self, *, limit: int = 100) -> int:
        """提交有限本地关键词更新，不消费独立的语义计算任务。"""
        ...

    async def semantic_search(
        self, vector: tuple[float, ...], model: str, *, limit: int = 40
    ) -> tuple[KnowledgeReference, ...]:
        """以实际同模型向量召回，不从旧版本索引恢复失效认识。"""
        ...

    async def supervise(
        self, batch_id: str, version: int, notice: SupervisionNotice
    ) -> SupervisionRecord:
        """立即保存有据监管，urgent 由宿主请求重思考。"""
        ...

    async def pending_supervision(self, *, limit: int = 32) -> tuple[SupervisionRecord, ...]:
        """交付前核验来源、期限及活动版本。"""
        ...

    async def finish_reflection(
        self, batch_id: str, version: int, submission: FinishSubmission
    ) -> ReflectionReceipt:
        """原子完成范围及邮箱确认，重复完成返回已有回执。"""
        ...

    async def plan_wakeup(self, plan: WakeupPlan) -> WakeupPlan:
        """保存有据未来计划，不与注意 defer 共用到期语义。"""
        ...

    async def resolve_plan(self, identity: str, outcome: str, evidence: str) -> WakeupPlan:
        """用实际证据结束前台计划。"""
        ...

    async def pending_plans(
        self, *, due_before: float | None = None, limit: int = 32
    ) -> tuple[WakeupPlan, ...]:
        """查询有限未完成计划，不重复执行外部行动。"""
        ...

    async def respond_supervision(
        self, identity: str, decision: str, reason: str
    ) -> SupervisionRecord:
        """记录接受、部分接受或拒绝，回应不证明意见正确。"""
        ...


class SemanticRetriever(Protocol):
    """显式装配的可选语义路线，返回排名中的确切来源/认识版本。

    实现借用自己的已配置 embedding 资源和派生向量；不能改写权威认识或
    返回无版本身份。关闭属于 app，错误原样传出，未配置时不创建调用。
    """

    async def retrieve(self, query: str) -> tuple[KnowledgeReference, ...]:
        """按语义相关性排序，主体在使用前复查来源和版本。"""
        ...


class Reranker(Protocol):
    """显式配置的可选重排，只能排列给出的冻结候选，不添加未知来源。"""

    async def rank(
        self, query: str, candidates: tuple[KnowledgeCandidate, ...]
    ) -> tuple[KnowledgeReference, ...]:
        """返回唯一候选身份序列，可省略候选而不能制造另一版本。"""
        ...
