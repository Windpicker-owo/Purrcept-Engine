"""向插件借出原文与共享分页，隔开主体认识权限和权威队列内部实现。"""

from purrcept_engine.storage.knowledge import EvidencePage, EvidenceSnapshot
from purrcept_engine.storage.knowledge_port import KnowledgeLedger
from purrcept_engine.storage.models import MediaAsset


class EvidenceAccess:
    """app 创建的有限原文门面，与宿主 Store 同寿命，不独占新的资源。"""

    def __init__(self, ledger: KnowledgeLedger, page_chars: int) -> None:
        """固定已校验统一页长；借用账本而不把其认识写入方法交给插件。"""
        self._ledger = ledger
        self._page_chars = page_chars

    @property
    def page_chars(self) -> int:
        """读取本实例统一分页配置，不改变已有快照和游标归属。"""
        return self._page_chars

    async def capture(
        self, snapshot: EvidenceSnapshot, assets: tuple[MediaAsset, ...]
    ) -> EvidenceSnapshot:
        """完整保存工具取得媒体，不将同一截图重新投成外界刺激。"""
        return await self._ledger.capture(snapshot, assets)

    async def put_evidence(
        self, snapshot: EvidenceSnapshot, *, needs_reflection: bool = False
    ) -> EvidenceSnapshot:
        """保存完整取得原文，媒体和待复核关系在同一权威事务确认。"""
        return await self._ledger.put_evidence(snapshot, needs_reflection=needs_reflection)

    async def read_evidence(
        self,
        evidence_id: str,
        *,
        level: str = "evidence",
        cursor: str | None = None,
        max_chars: int = 8000,
    ) -> EvidencePage:
        """读取该版本的一页，原文件后续编辑不会改写已经保存正文。"""
        return await self._ledger.read_evidence(
            evidence_id, level=level, cursor=cursor, max_chars=max_chars
        )

    async def read_evidence_cursor(self, cursor: str, *, max_chars: int = 8000) -> EvidencePage:
        """从权威游标关系查回旧版本，不依赖插件自己缓存页身份。"""
        return await self._ledger.read_evidence_cursor(cursor, max_chars=max_chars)
