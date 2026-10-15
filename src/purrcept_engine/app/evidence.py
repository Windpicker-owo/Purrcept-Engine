"""向插件借出原文与共享分页，隔开主体认识权限和权威队列内部实现。"""

from purrcept_engine.domain.inputs.models import MediaAsset
from purrcept_engine.domain.knowledge.models import EvidencePage, EvidenceSnapshot
from purrcept_engine.infrastructure.sqlite.knowledge_port import KnowledgeLedger


class EvidenceAccess:
    """app 创建的有限原文门面，与宿主 Store 同寿命，不独占新的资源。"""

    def __init__(self, ledger: KnowledgeLedger, page_chars: int) -> None:
        """固定已校验统一页长。

        Args:
            ledger: 当前业务专用账本，不提供任意数据库操作。
            page_chars: 本实例统一的正文分页字符上限。

        Note:
            借用账本而不把其认识写入方法交给插件。
        """
        self._ledger = ledger
        self._page_chars = page_chars

    @property
    def page_chars(self) -> int:
        """读取本实例统一分页配置，不改变已有快照和游标归属。"""
        return self._page_chars

    async def capture(
        self, snapshot: EvidenceSnapshot, assets: tuple[MediaAsset, ...]
    ) -> EvidenceSnapshot:
        """同事务保存工具明确取得的完整媒体与原文快照。

        Args:
            snapshot: 完整不可变原文快照，含来源、确切版本及媒体引用。
            assets: 与快照关联的完整媒体字节，随原文在同一权威边界保存。

        Returns:
            完整保存的原文快照，包含确切来源、版本与媒体关系。

        Note:
            完整保存工具取得媒体，不将同一截图重新投成外界刺激。
        """
        return await self._ledger.capture(snapshot, assets)

    async def put_evidence(
        self, snapshot: EvidenceSnapshot, *, needs_reflection: bool = False
    ) -> EvidenceSnapshot:
        """保存完整取得原文，媒体和待复核关系在同一权威事务确认。

        Args:
            snapshot: 完整不可变原文快照，含来源、确切版本及媒体引用。
            needs_reflection: 是否为新原文登记可靠的待反思关系。 默认 False。

        Returns:
            完整保存的原文快照，包含确切来源、版本与媒体关系。
        """
        return await self._ledger.put_evidence(snapshot, needs_reflection=needs_reflection)

    async def read_evidence(
        self,
        evidence_id: str,
        *,
        level: str = "evidence",
        cursor: str | None = None,
        max_chars: int = 8000,
    ) -> EvidencePage:
        """读取该版本的一页，原文件后续编辑不会改写已经保存正文。

        Args:
            evidence_id: 已保存原文快照的稳定身份。
            level: evidence 展开原文，summary 或 overview 读取概要；与目标材料种类一并核验。
                默认 'evidence'。
            cursor: 宿主签发并保存的不透明续读游标，续读同一快照版本。 None 表示首次读取。
            max_chars: 本页正文的字符上限；分页游标继续绑定原快照。 默认 8000。

        Returns:
            当前版本的一页正文、确切范围和下一游标；取得页面不等于已呈现给模型。
        """
        return await self._ledger.read_evidence(
            evidence_id, level=level, cursor=cursor, max_chars=max_chars
        )

    async def read_evidence_cursor(self, cursor: str, *, max_chars: int = 8000) -> EvidencePage:
        """从权威游标关系查回旧版本，不依赖插件自己缓存页身份。

        Args:
            cursor: 宿主签发并保存的不透明续读游标，续读同一快照版本。
            max_chars: 本页正文的字符上限；分页游标继续绑定原快照。 默认 8000。

        Returns:
            当前版本的一页正文、确切范围和下一游标；取得页面不等于已呈现给模型。
        """
        return await self._ledger.read_evidence_cursor(cursor, max_chars=max_chars)
