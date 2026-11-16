"""共享原文阅读门面；长期记忆检索仅由 Notebook 提供。"""

from purrcept_engine.domain.evidence.models import EvidencePage
from purrcept_engine.domain.evidence.ports import EvidenceStore


class EvidenceService:
    """应用向主体借出的只读原文服务，不拥有检索工作器或文件。"""

    def __init__(self, store: EvidenceStore, *, page_chars: int = 8000) -> None:
        """绑定同库证据端口及正数页长。"""
        if isinstance(page_chars, bool) or page_chars < 1:
            raise ValueError("原文页长必须是正整数。")
        self.store, self.page_chars = store, page_chars

    async def read_evidence(
        self,
        identity: str,
        level: str = "evidence",
        cursor: str | None = None,
        max_chars: int | None = None,
        version: int | None = None,
    ) -> EvidencePage:
        """展开确切原文版本；游标只能继续原版本，缺失和撤销原样失败。"""
        return await self.store.read_evidence(
            identity,
            level=level,
            cursor=cursor,
            max_chars=self.page_chars if max_chars is None else max_chars,
            version=version,
        )
