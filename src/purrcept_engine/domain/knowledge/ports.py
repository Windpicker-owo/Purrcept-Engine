"""定义原文与认识账本、语义召回和候选重排的有限端口。

KnowledgeService 与独立反思借用这些契约；应用注入同库实现和可选后端。
原文读取、实际呈现与认识写入分开，协议不取得通用连接或模型客户端。
"""

from __future__ import annotations

from typing import Literal, Protocol

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
        """保存原文及可靠待复核关系。

        Args:
            snapshot: 完整不可变原文快照，含来源、确切版本及媒体引用。
            needs_reflection: 是否为新原文登记可靠的待反思关系。 默认 True。

        Returns:
            完整保存的原文快照，包含确切来源、版本与媒体关系。

        Raises:
            StorageError: 权威提交或读取失败，不能以空结果替代。
            KnowledgeInputError: 来源、版本、范围或业务字段不满足本次操作的要求。

        Note:
            重复接管不改写原文。
        """
        ...

    async def get_evidence(
        self, evidence_id: str, *, version: int | None = None, include_inactive: bool = False
    ) -> EvidenceSnapshot | None:
        """读取原文资格。

        Args:
            evidence_id: 已保存原文快照的稳定身份。
            version: 目标记录的确切版本，区别于可变的当前版本。 None 首读当前版本，
                续读仍绑定游标的原版本。
            include_inactive: 是否同时返回已失效的历史记录，并保留失效原因。 默认 False。

        Returns:
            完整保存的原文快照，包含确切来源、版本与媒体关系。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。

        Raises:
            StorageError: 权威提交或读取失败，不能以空结果替代。

        Note:
            历史查询不等于允许引用撤销来源。
        """
        ...

    async def get_slice(self, slice_ref: str) -> EvidenceSlice | None:
        """读取不可变片段身份及确切范围。

        Args:
            slice_ref: 已保存原文片段的不可变身份。

        Returns:
            同一原文版本中已保存的精确范围及不可变片段身份。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
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
        """创建/续读相同快照片段，取得不登记模型已读。

        Args:
            evidence_id: 已保存原文快照的稳定身份。
            level: evidence 展开原文，summary 或 overview 读取概要；与目标材料种类一并核验。
                默认 'evidence'。
            cursor: 宿主签发并保存的不透明续读游标，续读同一快照版本。 None 表示首次读取。
            max_chars: 本页正文的字符上限；分页游标继续绑定原快照。 默认 8000。
            version: 目标记录的确切版本，区别于可变的当前版本。 None 首读当前版本，
                续读仍绑定游标的原版本。

        Returns:
            当前版本的一页正文、确切范围和下一游标；取得页面不等于已呈现给模型。

        Raises:
            KnowledgeInputError: 来源、版本、范围或业务字段不满足本次操作的要求。
            StorageError: 权威提交或读取失败，不能以空结果替代。
        """
        ...

    async def revoke_evidence(self, evidence_id: str, version: int) -> None:
        """撤销引用资格，保留原文与全部认识版本。

        Args:
            evidence_id: 已保存原文快照的稳定身份。
            version: 目标记录的确切版本，区别于可变的当前版本。
        """
        ...

    async def read_evidence_cursor(self, cursor: str, *, max_chars: int = 8000) -> EvidencePage:
        """由已保存的不透明游标恢复固定版本，插件无需解析或缓存来源身份。

        Args:
            cursor: 宿主签发并保存的不透明续读游标，续读同一快照版本。
            max_chars: 本页正文的字符上限；分页游标继续绑定原快照。 默认 8000。

        Returns:
            当前版本的一页正文、确切范围和下一游标；取得页面不等于已呈现给模型。

        Raises:
            StorageError: 权威提交或读取失败，不能以空结果替代。
            KnowledgeInputError: 来源、版本、范围或业务字段不满足本次操作的要求。
        """
        ...

    async def pending_reflection(self, *, limit: int = 32) -> tuple[PendingEvidence, ...]:
        """读取有限可靠来源及已有范围结论，不确认交付。

        Args:
            limit: 本次读取或更新的最大条目数。 默认 32。

        Returns:
            本次有限读取的可靠待反思来源及已有范围结论。
        """
        ...

    async def begin_reflection(
        self,
        batch_id: str,
        required_slice_refs: tuple[str, ...],
        findings: tuple[OperationalFinding, ...] = (),
        *,
        purpose: Literal["memory", "behavior"] = "memory",
    ) -> ReflectionBatch:
        """冻结范围和异常，恢复代次不能借用旧资格。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            required_slice_refs: 本批必须完成复核的冻结原文范围。
            findings: 本批需要核验的实际异常或运行观察。 默认不附加此类条目。

        Returns:
            冻结的反思范围与批次版本，后续修订须携带同一资格。

        Raises:
            StorageError: 权威提交或读取失败，不能以空结果替代。
            KnowledgeInputError: 来源、版本、范围或业务字段不满足本次操作的要求。
            KnowledgeConflict: 批次、来源或认识版本已变化，调用方须重读当前事实。
        """
        ...

    async def get_reflection(self, batch_id: str, version: int) -> ReflectionBatch:
        """取得权威批次/完成回执，不从最终文字推断完成。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            version: 当前反思批次的确切版本，旧代次不再拥有写入资格。

        Returns:
            冻结的反思范围与批次版本，后续修订须携带同一资格。

        Raises:
            StorageError: 权威提交或读取失败，不能以空结果替代。
        """
        ...

    async def mark_presented(
        self, batch_id: str, version: int, slice_refs: tuple[str, ...]
    ) -> None:
        """实际请求成功门槛记录已呈现范围，工具创建片段不能调用它。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            version: 目标记录的确切版本，区别于可变的当前版本。
            slice_refs: 当前材料实际覆盖的原文片段身份。

        Raises:
            StorageError: 权威提交或读取失败，不能以空结果替代。
            KnowledgeConflict: 批次、来源或认识版本已变化，调用方须重读当前事实。
        """
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
        """登记本批真实完整交付及媒体。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            version: 目标记录的确切版本，区别于可变的当前版本。
            tool_call_id: Core 分配的工具调用身份，用于匹配完整工具结果。
            material: 真正交付的完整消息或工具内容，供后续请求逐项匹配。
            slice_refs: 当前材料实际覆盖的原文片段身份。

        Returns:
            本次完整材料与精确原文范围的持久交付登记。

        Raises:
            StorageError: 权威提交或读取失败，不能以空结果替代。

        Note:
            保存不等于实际请求已呈现。
        """
        ...

    async def deliveries_for_history(
        self, messages: tuple[Message, ...]
    ) -> tuple[EvidenceDelivery, ...]:
        """只恢复与当前真实历史完整匹配的原交付，不扫描正文中的身份标记。

        Args:
            messages: 按实际先后排列的完整 Core 消息，不从正文猜测交付身份。

        Returns:
            条目集合：本次完整材料与精确原文范围的持久交付登记。
        """
        ...

    async def abandon_reflection(self, batch_id: str, version: int, reason: str) -> ReflectionBatch:
        """撤销失败代次写资格，保留已提交认识和未覆盖待办。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            version: 当前反思批次的确切版本，旧代次不再拥有写入资格。
            reason: 供后续核对的明确处置原因。

        Returns:
            冻结的反思范围与批次版本，后续修订须携带同一资格。

        Note:
            不自行安排重试。
        """
        ...

    async def revise_memory(self, batch_id: str, version: int, patch: MemoryPatch) -> MemoryRecord:
        """逐次原子修订，批次后续失败不回滚已写入认识。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            version: 目标记录的确切版本，区别于可变的当前版本。
            patch: 含原始证据、版本、关系与有效期的认识修订提案。

        Returns:
            包含原始依据、关系、有效期与确切版本的认识记录。

        Raises:
            StorageError: 权威提交或读取失败，不能以空结果替代。
            KnowledgeInputError: 来源、版本、范围或业务字段不满足本次操作的要求。
            KnowledgeConflict: 批次、来源或认识版本已变化，调用方须重读当前事实。
        """
        ...

    async def get_memory(
        self,
        memory_id: str,
        *,
        version: int | None = None,
        include_inactive: bool = False,
        at: float | None = None,
    ) -> MemoryRecord | None:
        """默认读取当前有效认识，显式历史保留失效原因。

        Args:
            memory_id: 已保存认识的稳定身份。
            version: 目标记录的确切版本，区别于可变的当前版本。 None 首读当前版本，
                续读仍绑定游标的原版本。
            include_inactive: 是否同时返回已失效的历史记录，并保留失效原因。 默认 False。
            at: 核验记录有效性的 Unix 时间戳；None 使用当前时间。

        Returns:
            包含原始依据、关系、有效期与确切版本的认识记录。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。

        Raises:
            StorageError: 权威提交或读取失败，不能以空结果替代。
        """
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
        """取得权威核验后的关键词、关系、时间候选。

        Args:
            query: 用于关键词或语义检索的意图文本。
            entities: 需要共同检索或关联的实体名称集合。 默认不附加此类条目。
            start: 按发生时间筛选的起点；None 表示不限制此端。
            end: 按发生时间筛选的终点；None 表示不限制此端。
            at: 核验记录有效性的 Unix 时间戳；None 使用当前时间。
            include_inactive: 是否同时返回已失效的历史记录，并保留失效原因。 默认 False。
            limit: 本次读取或更新的最大条目数。 默认 40。

        Returns:
            已通过来源及版本核验的候选集合，保留各路线排名。
        """
        ...

    async def get_candidate(
        self,
        reference: KnowledgeReference,
        *,
        include_inactive: bool = False,
        at: float | None = None,
    ) -> EvidenceSnapshot | MemoryRecord | None:
        """异步检索返回后再核验确切版本及来源。

        Args:
            reference: 包含身份与确切版本的认识或原文引用。
            include_inactive: 是否同时返回已失效的历史记录，并保留失效原因。 默认 False。
            at: 核验记录有效性的 Unix 时间戳；None 使用当前时间。

        Returns:
            经权威版本核验的原文或认识记录。 未取得对应记录或素材时返回 None；读取故障仍抛出。

        Raises:
            StorageError: 权威提交或读取失败，不能以空结果替代。
        """
        ...

    async def pending_index(
        self, *, route: str | None = None, limit: int = 32
    ) -> tuple[IndexJob, ...]:
        """分页读取显式路线的可重建版本任务。

        Args:
            route: 派生索引路线名称，用于区分关键词与语义任务。 None
                按此账本的全部可用路线读取。
            limit: 本次读取或更新的最大条目数。 默认 32。

        Returns:
            本次有限读取的可重建索引任务。
        """
        ...

    async def apply_index(
        self,
        reference: KnowledgeReference,
        *,
        terms: str,
        vector: tuple[float, ...] | None = None,
        model: str | None = None,
    ) -> bool:
        """只安装仍有效的同版本派生值，迟到任务不能复活认识。

        Args:
            reference: 包含身份与确切版本的认识或原文引用。
            terms: 从同版本完整正文生成的关键词索引文本。
            vector: 对应明确模型空间的有限数值向量。
            model: 本次调用使用的显式模型名，不在领域层推断替代模型。
        """
        ...

    async def refresh_keyword_index(self, *, limit: int = 100) -> int:
        """提交有限本地关键词更新，不消费独立的语义计算任务。

        Args:
            limit: 本次读取或更新的最大条目数。 默认 100。
        """
        ...

    async def semantic_search(
        self, vector: tuple[float, ...], model: str, *, limit: int = 40
    ) -> tuple[KnowledgeReference, ...]:
        """以实际同模型向量召回，不从旧版本索引恢复失效认识。

        Args:
            vector: 对应明确模型空间的有限数值向量。
            model: 本次调用使用的显式模型名，不在领域层推断替代模型。
            limit: 本次读取或更新的最大条目数。 默认 40。

        Returns:
            按相关性排列的确切版本引用，使用前仍须复查权威资格。
        """
        ...

    async def supervise(
        self, batch_id: str, version: int, notice: SupervisionNotice
    ) -> SupervisionRecord:
        """立即保存有据监管，urgent 由宿主请求重思考。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            version: 目标记录的确切版本，区别于可变的当前版本。
            notice: 有原文依据的监管意见，紧急意见仍须显式交接前台。

        Returns:
            包含原始依据、活动版本约束和当前回应的监管记录。

        Raises:
            StorageError: 权威提交或读取失败，不能以空结果替代。
            KnowledgeInputError: 来源、版本、范围或业务字段不满足本次操作的要求。
        """
        ...

    async def pending_supervision(self, *, limit: int = 32) -> tuple[SupervisionRecord, ...]:
        """交付前核验来源、期限及活动版本。

        Args:
            limit: 本次读取或更新的最大条目数。 默认 32。

        Returns:
            条目集合：包含原始依据、活动版本约束和当前回应的监管记录。
        """
        ...

    async def finish_reflection(
        self, batch_id: str, version: int, submission: FinishSubmission
    ) -> ReflectionReceipt:
        """原子完成范围及邮箱确认，重复完成返回已有回执。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            version: 当前反思批次的确切版本，旧代次不再拥有写入资格。
            submission: 本批已复核与明确延期的范围及完成结论。

        Returns:
            实际已提交的结案回执，重复完成复用原结果。

        Raises:
            StorageError: 权威提交或读取失败，不能以空结果替代。
            KnowledgeInputError: 来源、版本、范围或业务字段不满足本次操作的要求。
            KnowledgeConflict: 批次、来源或认识版本已变化，调用方须重读当前事实。
        """
        ...

    async def plan_wakeup(self, plan: WakeupPlan) -> WakeupPlan:
        """保存有据未来计划，不与注意 defer 共用到期语义。

        Args:
            plan: 带来源与到期时刻的唤醒计划，不直接执行外部行动。

        Returns:
            已保存的计划及其当前结论，不直接代表外部行动完成。

        Raises:
            StorageError: 权威提交或读取失败，不能以空结果替代。
            KnowledgeInputError: 来源、版本、范围或业务字段不满足本次操作的要求。
        """
        ...

    async def resolve_plan(self, identity: str, outcome: str, evidence: str) -> WakeupPlan:
        """用实际证据结束前台计划。

        Args:
            identity: 需要明确结案的未完成计划身份。
            outcome: 本次计划的明确结案结论。
            evidence: 结束计划或确认事实所依据的实际证据文本。

        Returns:
            已保存的计划及其当前结论，不直接代表外部行动完成。

        Raises:
            StorageError: 权威提交或读取失败，不能以空结果替代。
            KnowledgeInputError: 来源、版本、范围或业务字段不满足本次操作的要求。
        """
        ...

    async def pending_plans(
        self, *, due_before: float | None = None, limit: int = 32
    ) -> tuple[WakeupPlan, ...]:
        """查询有限未完成计划，不重复执行外部行动。

        Args:
            due_before: 仅返回此 Unix 时刻前到期的计划；None 不限制到期时间。
            limit: 本次读取或更新的最大条目数。 默认 32。

        Returns:
            条目集合：已保存的计划及其当前结论，不直接代表外部行动完成。
        """
        ...

    async def respond_supervision(
        self, identity: str, decision: str, reason: str
    ) -> SupervisionRecord:
        """记录接受、部分接受或拒绝，回应不证明意见正确。

        Args:
            identity: 待查询或回应的监管意见身份。
            decision: accept、partial 或 reject，分别表示接受、部分接受或拒绝。
            reason: 供后续核对的明确处置原因。

        Returns:
            包含原始依据、活动版本约束和当前回应的监管记录。

        Raises:
            StorageError: 权威提交或读取失败，不能以空结果替代。
            KnowledgeInputError: 来源、版本、范围或业务字段不满足本次操作的要求。
        """
        ...


class SemanticRetriever(Protocol):
    """显式装配的可选语义路线，返回排名中的确切来源/认识版本。

    实现借用自己的已配置 embedding 资源和派生向量；不能改写权威认识或
    返回无版本身份。关闭属于 app，错误原样传出，未配置时不创建调用。
    """

    async def retrieve(self, query: str) -> tuple[KnowledgeReference, ...]:
        """按语义相关性排序，主体在使用前复查来源和版本。

        Args:
            query: 用于关键词或语义检索的意图文本。

        Returns:
            按相关性排列的确切版本引用，使用前仍须复查权威资格。
        """
        ...


class Reranker(Protocol):
    """显式配置的可选重排，只能排列给出的冻结候选，不添加未知来源。"""

    async def rank(
        self, query: str, candidates: tuple[KnowledgeCandidate, ...]
    ) -> tuple[KnowledgeReference, ...]:
        """返回唯一候选身份序列，可省略候选而不能制造另一版本。

        Args:
            query: 用于关键词或语义检索的意图文本。
            candidates: 已通过权威核验的冻结候选，重排不得新增来源。

        Returns:
            按相关性排列的确切版本引用，使用前仍须复查权威资格。
        """
        ...
