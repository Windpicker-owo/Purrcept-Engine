"""定义共享原文、行为评审和监管计划的有限端口。

应用借出同库实现；原文读取、模型实际呈现与评审结案分开确认。
端口不取得通用连接，长期记忆写入和检索属于 Notebook 插件。
"""

from __future__ import annotations

from typing import Literal, Protocol

from purrcept_core.models import ContentBlock, Message

from purrcept_engine.domain.evidence.models import (
    EvidenceDelivery,
    EvidencePage,
    EvidenceSlice,
    EvidenceSnapshot,
    FinishSubmission,
    OperationalFinding,
    ReflectionBatch,
    ReflectionReceipt,
    SupervisionNotice,
    SupervisionRecord,
    WakeupPlan,
)


class EvidenceStore(Protocol):
    """Store 专属线程提供的共享证据与行为评审接口，写操作确认实际事务完成。

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
            EvidenceInputError: 来源、版本、范围或业务字段不满足本次操作的要求。

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
            EvidenceInputError: 来源、版本、范围或业务字段不满足本次操作的要求。
            StorageError: 权威提交或读取失败，不能以空结果替代。
        """
        ...

    async def revoke_evidence(self, evidence_id: str, version: int) -> None:
        """撤销指定版本的引用资格，保留全部原文快照和历史引用。

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
            EvidenceInputError: 来源、版本、范围或业务字段不满足本次操作的要求。
        """
        ...

    async def begin_reflection(
        self,
        batch_id: str,
        required_slice_refs: tuple[str, ...],
        findings: tuple[OperationalFinding, ...] = (),
        *,
        purpose: Literal["behavior"] = "behavior",
    ) -> ReflectionBatch:
        """冻结范围和异常，恢复代次不能借用旧资格。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            required_slice_refs: 本批必须完成复核的冻结原文范围。
            findings: 本批需要核验的实际异常或运行观察。 默认不附加此类条目。

        Returns:
            冻结的行为复核范围与批次版本，后续监管和结案须携带同一资格。

        Raises:
            StorageError: 权威提交或读取失败，不能以空结果替代。
            EvidenceInputError: 来源、版本、范围或业务字段不满足本次操作的要求。
            EvidenceConflict: 批次资格或原文来源状态已变化，调用方须重读当前事实。
        """
        ...

    async def get_reflection(self, batch_id: str, version: int) -> ReflectionBatch:
        """取得权威批次/完成回执，不从最终文字推断完成。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            version: 当前反思批次的确切版本，旧代次不再拥有写入资格。

        Returns:
            冻结的行为复核范围与批次版本，后续监管和结案须携带同一资格。

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
            EvidenceConflict: 批次资格或原文来源状态已变化，调用方须重读当前事实。
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
        """撤销失败代次的评审写资格，保留已提交监管和未覆盖待办。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            version: 当前反思批次的确切版本，旧代次不再拥有写入资格。
            reason: 供后续核对的明确处置原因。

        Returns:
            冻结的行为复核范围与批次版本，后续监管和结案须携带同一资格。

        Note:
            不自行安排重试。
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
            EvidenceInputError: 来源、版本、范围或业务字段不满足本次操作的要求。
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
            EvidenceInputError: 来源、版本、范围或业务字段不满足本次操作的要求。
            EvidenceConflict: 批次资格或原文来源状态已变化，调用方须重读当前事实。
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
            EvidenceInputError: 来源、版本、范围或业务字段不满足本次操作的要求。
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
            EvidenceInputError: 来源、版本、范围或业务字段不满足本次操作的要求。
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
            EvidenceInputError: 来源、版本、范围或业务字段不满足本次操作的要求。
        """
        ...
