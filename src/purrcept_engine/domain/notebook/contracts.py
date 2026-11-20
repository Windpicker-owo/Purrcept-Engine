"""定义 Notebook 的派生存储和后台模型边界。

Markdown 内容归插件文件所有者；这里的值只定位文件版本、已处理范围和发布意图。
应用按插件实例借出端口，SQLite 不决定整理策略，模型端口不取得文件写权限。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Protocol

from purrcept_core.models import Message, ModelResponse, SystemInstruction
from pydantic import BaseModel, ConfigDict, Field, field_validator

from purrcept_engine.domain.evidence.models import EvidenceSnapshot
from purrcept_engine.domain.model_requests.service import RequestBudget
from purrcept_engine.domain.notebook.settings import EmbeddingSettings
from purrcept_engine.values import JsonObject

MEMORY_CONTENT_CHARS = 1800


class Document(BaseModel):
    """文件当前版本的可重建登记；正文仍从文件或确切证据快照读取。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    path: str
    file_version: str
    evidence_id: str
    version: int
    summary: str
    updated_at: float
    generated: bool
    skill: bool


class Work(BaseModel):
    """一个源文件的未完成范围；换版本重置游标，旧范围仍可作为历史证据。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    document: Document
    cursor: str | None = None
    queued_at: float
    changes: int
    next_attempt_at: float


class Revision(BaseModel):
    """模型提出的单页修订；插件核验目标、来源和预期版本后才允许发布。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    path: str
    expected_version: str | None
    summary: Annotated[str, Field(min_length=1, max_length=140)]
    content: Annotated[str, Field(min_length=1, max_length=MEMORY_CONTENT_CHARS, pattern=r"\S")]
    sources: tuple[str, ...] = Field(min_length=1, strict=False)

    @field_validator("summary")
    @classmethod
    def summary_line(cls, value: str) -> str:
        """摘要写入单行元数据，Unicode 换行也会改变后续文件解析。"""
        if value.splitlines() != [value] or not value.strip():
            raise ValueError("记忆摘要必须是非空的单行文本。")
        return value.strip()

    @field_validator("content")
    @classmethod
    def memory_points(cls, value: str) -> str:
        """正文只承载事实；文件外壳和来源区由发布者统一写入。"""
        text = value.strip()
        if not text:
            raise ValueError("记忆正文不能为空。")
        if re.search(
            r"^\s*(?:#{1,6}\s*(?:摘要|概览|要点|细节|来源|summary|overview|sources)\s*#*\s*$"
            r"|(?:summary|overview)\s*[:：])",
            text,
            re.M | re.I,
        ):
            raise ValueError("记忆正文不能包含摘要、概览或来源包装。")
        if text.splitlines()[0].strip() == "---":
            raise ValueError("记忆正文不能包含文件元数据。")
        return text.replace("\r\n", "\n")


class Proposal(BaseModel):
    """单次实际阅读范围的整理结果；defer 保留待办，不以普通文字结案。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    revisions: tuple[Revision, ...] = Field(default=(), max_length=32, strict=False)
    defer: bool = False


class Publication(BaseModel):
    """文件发布前持久保存的意图；恢复只核对结果，不自动重放文件操作。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    identity: str
    path: str
    expected_version: str | None
    result_version: str
    sources: tuple[str, ...] = Field(default=(), strict=False)
    retained_sources: tuple[str, ...] = Field(default=(), strict=False)


class NotebookStore(Protocol):
    """应用为一个插件实例借出的持久端口，所有方法等待真实事务完成。

    插件决定何时整理及发布，端口仅保存派生状态。取消等待不撤销接管事务。
    实现由应用按实例绑定，不能读写其他实例；内置实现借用 Store 唯一提交线程。
    """

    async def documents(self) -> tuple[Document, ...]:
        """读取本实例已登记的当前文件，供扫描对账和版本核验。"""
        ...

    async def register(self, document: Document, *, needs_work: bool) -> None:
        """登记已发布版本；相同哈希幂等，新源版本合并原待办。"""
        ...

    async def remove(self, paths: tuple[str, ...]) -> None:
        """移除已确认不存在的当前路径及待办，保留不可变证据。"""
        ...

    async def pending(self, *, limit: int = 32) -> tuple[Work, ...]:
        """按最早入队时间读取有限任务，包含持久冷却期限。"""
        ...

    async def advance(self, work: Work, cursor: str | None) -> None:
        """仅为仍匹配的文件版本推进范围，末页结案，不覆盖新版本待办。"""
        ...

    async def postpone(self, works: tuple[Work, ...], reason: str) -> None:
        """为指定版本登记有界指数冷却，保留故障原因和未完成范围。"""
        ...

    async def publications(self) -> tuple[Publication, ...]:
        """读取上次未确认的发布意图，恢复时必须核对实际文件哈希。"""
        ...

    async def intend(self, publication: Publication) -> None:
        """在磁盘发布前保存意图，不据此声称文件已发生变化。"""
        ...

    async def settle(self, identity: str, state: str) -> None:
        """记录发布的实际确认、冲突或未知状态，不自动重试文件写入。"""
        ...

    async def search(
        self, query: str, *, path: str = "", limit: int = 40
    ) -> tuple[EvidenceSnapshot, ...]:
        """检索本实例当前笔记版本，保留可选向量/重排失败原型。"""
        ...

    async def maintain_index(self) -> None:
        """维护至多32个当前笔记向量；模型调用在数据库事务之外。"""
        ...

    async def status(self) -> JsonObject:
        """返回有限任务、索引和失败状态，不向模型注入队列正文。"""
        ...


class BackgroundModel(Protocol):
    """应用借出的单用途模型调用资源，Notebook 持有调用任务和整批预算。

    实现执行完整容量检查、请求证据记录及唯一传输重试。模型没有世界工具；
    输入和响应只使用 Core 值。调用结束前插件必须保留所有权，应用最后关闭客户端。
    """

    async def generate(
        self,
        instructions: tuple[SystemInstruction, ...],
        messages: tuple[Message, ...],
        *,
        batch_id: str,
        sources: tuple[str, ...],
        budget: RequestBudget,
    ) -> ModelResponse:
        """按同一批次截止生成结构化提案，超容或证据故障时不派发。"""
        ...


class BackgroundRetrySettings(BaseModel):
    """插件模型自己的传输重试预算，不从行为评审配置取得默认值。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    attempts: int = Field(default=2, ge=1)
    attempt_seconds: float = Field(default=120.0, gt=0, allow_inf_nan=False)
    total_seconds: float = Field(default=180.0, gt=0, allow_inf_nan=False)


class ModelRequirement(BaseModel):
    """插件纯声明的命名模型需求，由应用在取得任何资源前验证。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    retry: BackgroundRetrySettings = Field(default_factory=BackgroundRetrySettings)
    alias: str
    model: str


class NotebookRequirement(BaseModel):
    """插件声明需要隔离的笔记索引资源，不在声明阶段建立连接。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    embedding: EmbeddingSettings | None = None


type BackgroundModels = Mapping[str, BackgroundModel]
