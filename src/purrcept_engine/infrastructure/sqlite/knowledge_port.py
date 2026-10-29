"""把认识账本事务交给 Store 的唯一有界队列，不创建另一连接或线程。

KnowledgeLedger 是认识子系统的异步端口。SQL 和业务校验在 knowledge 模块，
这里只拥有异步交接、实际材料字节计量和读写队列选择；取消/故障/连接寿命
沿 Store 契约传播。原文分页会保存不可变片段/游标，因此也是权威写入。
"""

import hashlib
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

from purrcept_core.models import ContentBlock, Message

import purrcept_engine.domain.knowledge.models as knowledge_models
from purrcept_engine.domain.inputs.models import MediaAsset, MediaReference
from purrcept_engine.domain.model_requests.service import prepare_off_loop
from purrcept_engine.infrastructure.sqlite import knowledge as sql
from purrcept_engine.serialization import encode


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
        """接管有限事务并返回实际结果，等待取消不撤销已排队写入。

        Args:
            operation: 在专属数据库线程执行的同步短事务，返回值交回原事件循环。
            logical_bytes: 本次操作占用的逻辑载荷字节数，用于接管前背压。 默认 0。
            control: 是否使用独立控制名额，避免普通输入阻塞真实回执收尾。 默认 False。
            writing: 是否属于写操作，以便关闭或权威故障时撤销派发资格。 默认 False。

        Returns:
            当前所有者持有的工作观察入口，等待取消不等于实际资源已归还。
        """
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
        """借入唯一队列，不打开库或创建消费者任务。

        Args:
            submit: 借用同一 Store 有界队列执行同步短事务的入口。
        """
        self._submit = submit

    async def capture(
        self,
        snapshot: knowledge_models.EvidenceSnapshot,
        assets: tuple[MediaAsset, ...],
    ) -> knowledge_models.EvidenceSnapshot:
        """原子接管工具取得的原生媒体及原文，不创建第二条外界输入批次。

        Args:
            snapshot: 完整不可变原文快照，含来源、确切版本及媒体引用。
            assets: 与快照关联的完整媒体字节，随原文在同一权威边界保存。

        Returns:
            完整保存的原文快照，包含确切来源、版本与媒体关系。

        Raises:
            knowledge_models.KnowledgeInputError: 捕获原文的媒体引用与完整字节不一致。
        """
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
            raise knowledge_models.KnowledgeInputError("捕获原文的媒体引用与完整字节不一致。")

        def commit(connection: sqlite3.Connection) -> knowledge_models.EvidenceSnapshot:
            """在既有权威边界完成已经接管的事实提交。"""
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
        self, snapshot: knowledge_models.EvidenceSnapshot, *, needs_reflection: bool = True
    ) -> knowledge_models.EvidenceSnapshot:
        """原文/媒体资格及可靠复核身份一并提交。

        Args:
            snapshot: 完整不可变原文快照，含来源、确切版本及媒体引用。
            needs_reflection: 是否为新原文登记可靠的待反思关系。 默认 True。

        Returns:
            完整保存的原文快照，包含确切来源、版本与媒体关系。
        """
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
    ) -> knowledge_models.EvidenceSnapshot | None:
        """读取确切原文资格，历史查询不授予当前引用权限。

        Args:
            evidence_id: 已保存原文快照的稳定身份。
            version: 目标记录的确切版本，区别于可变的当前版本。 None 首读当前版本，
                续读仍绑定游标的原版本。
            include_inactive: 是否同时返回已失效的历史记录，并保留失效原因。 默认 False。

        Returns:
            完整保存的原文快照，包含确切来源、版本与媒体关系。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
        return await self._submit(
            lambda connection: sql.get_evidence(
                connection, evidence_id, version=version, include_inactive=include_inactive
            ),
            logical_bytes=_bytes(evidence_id),
        )

    async def get_slice(self, slice_ref: str) -> knowledge_models.EvidenceSlice | None:
        """读取宿主保存的范围身份，不从文本标记推断阅读资格。

        Args:
            slice_ref: 已保存原文片段的不可变身份。

        Returns:
            同一原文版本中已保存的精确范围及不可变片段身份。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
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
    ) -> knowledge_models.EvidencePage:
        """同快照范围读取并保存游标。

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

        Note:
            取得正文不等于已向模型呈现。
        """
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

    async def read_evidence_cursor(
        self, cursor: str, *, max_chars: int = 8000
    ) -> knowledge_models.EvidencePage:
        """用已保存的 opaque 游标查回原始快照，不建立插件分页缓存。

        Args:
            cursor: 宿主签发并保存的不透明续读游标，续读同一快照版本。
            max_chars: 本页正文的字符上限；分页游标继续绑定原快照。 默认 8000。

        Returns:
            当前版本的一页正文、确切范围和下一游标；取得页面不等于已呈现给模型。
        """
        return await self._submit(
            lambda connection: sql.read_evidence_cursor(connection, cursor, max_chars=max_chars),
            logical_bytes=_bytes(cursor),
            control=True,
            writing=True,
        )

    async def revoke_evidence(self, evidence_id: str, version: int) -> None:
        """撤销来源资格且保留原始材料和所有认识历史。

        Args:
            evidence_id: 已保存原文快照的稳定身份。
            version: 目标记录的确切版本，区别于可变的当前版本。
        """
        await self._submit(
            lambda connection: sql.revoke_evidence(connection, evidence_id, version),
            logical_bytes=_bytes(evidence_id),
            control=True,
            writing=True,
        )

    async def pending_reflection(
        self, *, limit: int = 32
    ) -> tuple[knowledge_models.PendingEvidence, ...]:
        """读取有限可靠批次，查询不会确认已读或理解。

        Args:
            limit: 本次读取或更新的最大条目数。 默认 32。

        Returns:
            本次有限读取的可靠待反思来源及已有范围结论。
        """
        return await self._submit(
            lambda connection: sql.pending_reflection(connection, limit=limit)
        )

    async def begin_reflection(
        self,
        batch_id: str,
        required_slice_refs: tuple[str, ...],
        findings: tuple[knowledge_models.OperationalFinding, ...] = (),
        *,
        purpose: Literal["memory", "behavior"] = "memory",
    ) -> knowledge_models.ReflectionBatch:
        """冻结原文范围及程序异常，重复等待沿同一批次身份核验。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            required_slice_refs: 本批必须完成复核的冻结原文范围。
            findings: 本批需要核验的实际异常或运行观察。 默认不附加此类条目。

        Returns:
            冻结的反思范围与批次版本，后续修订须携带同一资格。
        """
        return await self._submit(
            lambda connection: sql.begin_reflection(
                connection, batch_id, required_slice_refs, findings, purpose=purpose
            ),
            logical_bytes=_bytes(
                batch_id,
                required_slice_refs,
                tuple(item.model_dump(mode="json") for item in findings),
            ),
            control=True,
            writing=True,
        )

    async def get_reflection(self, batch_id: str, version: int) -> knowledge_models.ReflectionBatch:
        """读取当前资格或真实完成回执，不解析最终模型文字。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            version: 当前反思批次的确切版本，旧代次不再拥有写入资格。

        Returns:
            冻结的反思范围与批次版本，后续修订须携带同一资格。
        """
        return await self._submit(
            lambda connection: sql.get_reflection(connection, batch_id, version),
            logical_bytes=_bytes(batch_id),
        )

    async def abandon_reflection(
        self, batch_id: str, version: int, reason: str
    ) -> knowledge_models.ReflectionBatch:
        """结束本批写入资格，保留真实逐项修订与未确认范围，不伪造完成。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            version: 当前反思批次的确切版本，旧代次不再拥有写入资格。
            reason: 供后续核对的明确处置原因。

        Returns:
            冻结的反思范围与批次版本，后续修订须携带同一资格。
        """
        return await self._submit(
            lambda connection: sql.abandon_reflection(connection, batch_id, version, reason),
            logical_bytes=_bytes(batch_id, reason),
            control=True,
            writing=True,
        )

    async def mark_presented(
        self, batch_id: str, version: int, slice_refs: tuple[str, ...]
    ) -> None:
        """实际成功模型请求的范围资格先提交，下一工具才能据此修订。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            version: 目标记录的确切版本，区别于可变的当前版本。
            slice_refs: 当前材料实际覆盖的原文片段身份。
        """
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
    ) -> knowledge_models.EvidenceDelivery:
        """原子登记原批次、完整材料、媒体和切片，不能提前取得已读资格。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            version: 目标记录的确切版本，区别于可变的当前版本。
            tool_call_id: Core 分配的工具调用身份，用于匹配完整工具结果。
            material: 真正交付的完整消息或工具内容，供后续请求逐项匹配。
            slice_refs: 当前材料实际覆盖的原文片段身份。

        Returns:
            本次完整材料与精确原文范围的持久交付登记。
        """
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
    ) -> tuple[knowledge_models.EvidenceDelivery, ...]:
        """只读核验当前完整历史中的原交付，返回原批次身份和确切切片关系。

        Args:
            messages: 按实际先后排列的完整 Core 消息，不从正文猜测交付身份。

        Returns:
            条目集合：本次完整材料与精确原文范围的持久交付登记。
        """
        return await self._submit(
            lambda connection: sql.deliveries_for_history(connection, messages),
            logical_bytes=_bytes(messages),
        )

    async def revise_memory(
        self, batch_id: str, version: int, patch: knowledge_models.MemoryPatch
    ) -> knowledge_models.MemoryRecord:
        """逐项修订原子生效，后续批次失败不撤销已经确认的认识。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            version: 目标记录的确切版本，区别于可变的当前版本。
            patch: 含原始证据、版本、关系与有效期的认识修订提案。

        Returns:
            包含原始依据、关系、有效期与确切版本的认识记录。
        """
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
    ) -> knowledge_models.MemoryRecord | None:
        """默认读取当前有效版本，显式历史显示失效原因及发生时间。

        Args:
            memory_id: 已保存认识的稳定身份。
            version: 目标记录的确切版本，区别于可变的当前版本。 None 首读当前版本，
                续读仍绑定游标的原版本。
            include_inactive: 是否同时返回已失效的历史记录，并保留失效原因。 默认 False。
            at: 核验记录有效性的 Unix 时间戳；None 使用当前时间。

        Returns:
            包含原始依据、关系、有效期与确切版本的认识记录。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。
        """
        return await self._submit(
            lambda connection: sql.get_memory(
                connection, memory_id, version=version, include_inactive=include_inactive, at=at
            ),
            logical_bytes=_bytes(memory_id),
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
    ) -> tuple[knowledge_models.KnowledgeCandidate, ...]:
        """融合本地权威核验后的关键词、关系及时间候选。

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
        reference: knowledge_models.KnowledgeReference,
        *,
        include_inactive: bool = False,
        at: float | None = None,
    ) -> knowledge_models.EvidenceSnapshot | knowledge_models.MemoryRecord | None:
        """异步排名结束后复核实际版本，拒绝过时派生数据复活来源。

        Args:
            reference: 包含身份与确切版本的认识或原文引用。
            include_inactive: 是否同时返回已失效的历史记录，并保留失效原因。 默认 False。
            at: 核验记录有效性的 Unix 时间戳；None 使用当前时间。

        Returns:
            经权威版本核验的原文或认识记录。 未取得对应记录或素材时返回 None；读取故障仍抛出。
        """
        return await self._submit(
            lambda connection: sql.get_candidate(
                connection, reference, include_inactive=include_inactive, at=at
            ),
            logical_bytes=_bytes(reference.model_dump(mode="json")),
        )

    async def pending_index(
        self, *, route: str | None = None, limit: int = 32
    ) -> tuple[knowledge_models.IndexJob, ...]:
        """读取有限可重建任务，关键词和语义路线各自确认。

        Args:
            route: 派生索引路线名称，用于区分关键词与语义任务。 None
                按此账本的全部可用路线读取。
            limit: 本次读取或更新的最大条目数。 默认 32。

        Returns:
            本次有限读取的可重建索引任务。
        """
        return await self._submit(
            lambda connection: sql.pending_index(connection, route=route, limit=limit)
        )

    async def queue_semantic_rebuild(self, model_id: str, *, limit: int = 32) -> int:
        """先只读检查空间缺口，真正有候选才申请有界写入。

        Args:
            model_id: 标识实际向量空间及其版本的模型身份。
            limit: 本次读取或更新的最大条目数。 默认 32。

        Raises:
            ValueError: 向量重建一次最多扫描 32 个版本。

        Note:
            空扫描不发布通知。
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 32:
            raise ValueError("向量重建一次最多扫描 32 个版本。")
        references = await self._submit(
            lambda connection: sql.semantic_rebuild_candidates(connection, model_id, limit=limit),
            logical_bytes=_bytes(model_id),
        )
        if not references:
            return 0
        return await self._submit(
            lambda connection: sql.queue_semantic_rebuild(connection, model_id, references),
            logical_bytes=_bytes(
                model_id, tuple(item.model_dump(mode="json") for item in references)
            ),
            control=True,
            writing=True,
        )

    async def discard_stale_index(
        self, reference: knowledge_models.KnowledgeReference, *, route: str
    ) -> bool:
        """在同事务复查后只确认已经失效的确切版本任务，不安装任何猜测向量。

        Args:
            reference: 包含身份与确切版本的认识或原文引用。
            route: 派生索引路线名称，用于区分关键词与语义任务。
        """
        return await self._submit(
            lambda connection: sql.discard_stale_index(connection, reference, route=route),
            logical_bytes=_bytes(reference.model_dump(mode="json"), route),
            control=True,
            writing=True,
        )

    async def apply_index(
        self,
        reference: knowledge_models.KnowledgeReference,
        *,
        terms: str,
        vector: tuple[float, ...] | None = None,
        model: str | None = None,
    ) -> bool:
        """仅安装仍有资格的确切版本。

        Args:
            reference: 包含身份与确切版本的认识或原文引用。
            terms: 从同版本完整正文生成的关键词索引文本。
            vector: 对应明确模型空间的有限数值向量。
            model: 本次调用使用的显式模型名，不在领域层推断替代模型。

        Note:
            向量不成为原文权威。
        """
        return await self._submit(
            lambda connection: sql.apply_index(
                connection, reference, terms=terms, vector=vector, model=model
            ),
            logical_bytes=_bytes(reference.model_dump(mode="json"), terms, vector, model),
            control=True,
            writing=True,
        )

    async def refresh_keyword_index(self, *, limit: int = 100) -> int:
        """在单事务队列维护一批本地索引，不等待模型或网络。

        Args:
            limit: 本次读取或更新的最大条目数。 默认 100。
        """
        return await self._submit(
            lambda connection: sql.refresh_keyword_index(connection, limit=limit),
            control=True,
            writing=True,
        )

    async def semantic_search(
        self, vector: tuple[float, ...], model: str, *, limit: int = 40
    ) -> tuple[knowledge_models.KnowledgeReference, ...]:
        """查询实际同模型派生向量，返回确切来源版本供复核。

        Args:
            vector: 对应明确模型空间的有限数值向量。
            model: 本次调用使用的显式模型名，不在领域层推断替代模型。
            limit: 本次读取或更新的最大条目数。 默认 40。

        Returns:
            按相关性排列的确切版本引用，使用前仍须复查权威资格。
        """
        if not 1 <= limit <= 100:
            raise ValueError("语义候选上限必须在1到100之间。")
        ranked: tuple[tuple[float, str, str, int, knowledge_models.KnowledgeReference], ...] = ()
        after: tuple[str, str, int] | None = None
        while True:
            cursor = after
            rows = await self._submit(
                lambda connection, cursor=cursor: sql.vector_candidates(connection, model, cursor)
            )
            if not rows:
                break
            last = rows[-1][0]
            after = (last.kind, last.id, last.version)
            qualified = await self._submit(
                lambda connection, rows=rows: tuple(
                    item for item in rows if sql.get_candidate(connection, item[0]) is not None
                )
            )
            previous = ranked
            ranked = await prepare_off_loop(
                lambda qualified=qualified, previous=previous: sql.rank_vectors(
                    vector, qualified, previous, limit
                )
            )
        references = tuple(item[-1] for item in sorted(ranked, reverse=True))
        return await self._submit(
            lambda connection: tuple(
                reference
                for reference in references
                if sql.get_candidate(connection, reference) is not None
            )
        )

    async def supervise(
        self, batch_id: str, version: int, notice: knowledge_models.SupervisionNotice
    ) -> knowledge_models.SupervisionRecord:
        """有据监管立即入账，后续前台回应另有自己的实际记录。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            version: 目标记录的确切版本，区别于可变的当前版本。
            notice: 有原文依据的监管意见，紧急意见仍须显式交接前台。

        Returns:
            包含原始依据、活动版本约束和当前回应的监管记录。
        """
        return await self._submit(
            lambda connection: sql.supervise(connection, batch_id, version, notice),
            logical_bytes=_bytes(batch_id, notice.model_dump(mode="json")),
            control=True,
            writing=True,
        )

    async def pending_supervision(
        self, *, limit: int = 32
    ) -> tuple[knowledge_models.SupervisionRecord, ...]:
        """读取仍符合来源/期限/活动版本的待回应意见。

        Args:
            limit: 本次读取或更新的最大条目数。 默认 32。

        Returns:
            条目集合：包含原始依据、活动版本约束和当前回应的监管记录。
        """
        return await self._submit(
            lambda connection: sql.pending_supervision(connection, limit=limit)
        )

    async def finish_reflection(
        self, batch_id: str, version: int, submission: knowledge_models.FinishSubmission
    ) -> knowledge_models.ReflectionReceipt:
        """原子确认具体覆盖、真实认识修订与邮箱交接，普通文字无此权限。

        Args:
            batch_id: 独立反思批次的身份，须与批次版本配对使用。
            version: 当前反思批次的确切版本，旧代次不再拥有写入资格。
            submission: 本批已复核与明确延期的范围及完成结论。

        Returns:
            实际已提交的结案回执，重复完成复用原结果。
        """
        return await self._submit(
            lambda connection: sql.finish_reflection(connection, batch_id, version, submission),
            logical_bytes=_bytes(batch_id, submission.model_dump(mode="json")),
            control=True,
            writing=True,
        )

    async def plan_wakeup(self, plan: knowledge_models.WakeupPlan) -> knowledge_models.WakeupPlan:
        """保存明确未来计划，注意 defer 的默认期限不用于它。

        Args:
            plan: 带来源与到期时刻的唤醒计划，不直接执行外部行动。

        Returns:
            已保存的计划及其当前结论，不直接代表外部行动完成。
        """
        return await self._submit(
            lambda connection: sql.plan_wakeup(connection, plan),
            logical_bytes=_bytes(plan.model_dump(mode="json")),
            control=True,
            writing=True,
        )

    async def resolve_plan(
        self, identity: str, outcome: str, evidence: str
    ) -> knowledge_models.WakeupPlan:
        """以真实来源结案未完成计划，不能自动填入完成结论。

        Args:
            identity: 需要明确结案的未完成计划身份。
            outcome: 本次计划的明确结案结论。
            evidence: 结束计划或确认事实所依据的实际证据文本。

        Returns:
            已保存的计划及其当前结论，不直接代表外部行动完成。
        """
        return await self._submit(
            lambda connection: sql.resolve_plan(connection, identity, outcome, evidence),
            logical_bytes=_bytes(identity, outcome, evidence),
            control=True,
            writing=True,
        )

    async def pending_plans(
        self, *, due_before: float | None = None, limit: int = 32
    ) -> tuple[knowledge_models.WakeupPlan, ...]:
        """读取有限未完成计划，到期交接仍由主体调度事务确认。

        Args:
            due_before: 仅返回此 Unix 时刻前到期的计划；None 不限制到期时间。
            limit: 本次读取或更新的最大条目数。 默认 32。

        Returns:
            条目集合：已保存的计划及其当前结论，不直接代表外部行动完成。
        """
        return await self._submit(
            lambda connection: sql.pending_plans(connection, due_before=due_before, limit=limit)
        )

    async def respond_supervision(
        self, identity: str, decision: str, reason: str
    ) -> knowledge_models.SupervisionRecord:
        """保存前台回应及理由，回应不证明该监管意见正确。

        Args:
            identity: 待查询或回应的监管意见身份。
            decision: accept、partial 或 reject，分别表示接受、部分接受或拒绝。
            reason: 供后续核对的明确处置原因。

        Returns:
            包含原始依据、活动版本约束和当前回应的监管记录。
        """
        return await self._submit(
            lambda connection: sql.respond_supervision(connection, identity, decision, reason),
            logical_bytes=_bytes(identity, decision, reason),
            control=True,
            writing=True,
        )
