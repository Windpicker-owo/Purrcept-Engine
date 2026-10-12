"""声明可选向量配置并拥有认识检索所借用的 HTTP 资源与固定索引工作器。

首批只实现与既有 openai/ embedding 路径一致的明确 HTTP 协议，不进入
LiteLLM 全局客户端、环境凭据或代理。配置校验和资源构造不发请求；关闭先
结束索引工作器和 HTTP 借用，再收回连接池。Reranker 仍是显式注入的扩展，
旧 Runtime 没有内置重排 Provider，本模块不虚构另一种模型或端点。
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable, Mapping
from typing import Annotated, cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator

from purrcept_engine.errors import HostStateError
from purrcept_engine.subject.knowledge import KnowledgeService, Reranker
from purrcept_engine.subject.semantic import (
    EmbeddingProtocolError,
    EmbeddingProvider,
    IndexChanges,
    KnowledgeIndexWorker,
    SemanticStore,
    VectorRetriever,
    validate_vectors,
)

Nonempty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class EmbeddingSettings(BaseModel):
    """纯启动配置；model_id 必须声明决定向量空间的明确版本。

    与旧 embedding 配置保留 model/model_id/api_key/api_base/timeout_seconds。
    api_base 必须显式提供；api_key=None 明确表示请求不使用凭据，不从环境
    或 SDK 账户默认值补入。模型、空间和端点变更通过重启生效。
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid", hide_input_in_errors=True)

    model: Nonempty
    model_id: Nonempty
    api_base: Nonempty
    api_key: SecretStr | None = None
    timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 30.0

    @field_validator("model")
    @classmethod
    def _openai_protocol(cls, value: str) -> str:
        """首批只保留明确 OpenAI 兼容路径，不根据名称猜另一 Provider。"""
        prefix, separator, model = value.partition("/")
        if prefix != "openai" or not separator or not model.strip():
            raise ValueError("Embedding model 必须使用非空 openai/ 协议标识。")
        return value

    @field_validator("api_base")
    @classmethod
    def _explicit_endpoint(cls, value: str) -> str:
        """只核验完整端点，拒绝内嵌凭据、查询串和片段，不探测外部服务。"""
        try:
            url = urlsplit(value)
            valid = (
                url.scheme in ("http", "https")
                and url.hostname is not None
                and url.username is None
                and url.password is None
                and not url.query
                and not url.fragment
            )
            _ = url.port
        except ValueError:
            raise ValueError("Embedding api_base 必须是无内嵌凭据的完整 HTTP(S) URL。") from None
        if not valid:
            raise ValueError("Embedding api_base 必须是无内嵌凭据的完整 HTTP(S) URL。")
        return value

    @field_validator("api_key")
    @classmethod
    def _explicit_credential(cls, value: SecretStr | None) -> SecretStr | None:
        """省略表示不使用凭据，空字符串不能假装已声明一个可用凭据。"""
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("Embedding api_key 不能是空字符串。")
        return value


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EmbeddingProtocolError("Embedding 响应对象形状无效。")
    return cast(Mapping[str, object], value)


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """响应序号和内容不能因重复 JSON 键而被解析器静默覆盖。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise EmbeddingProtocolError("Embedding 响应包含重复 JSON 字段。")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise EmbeddingProtocolError("Embedding 响应包含非有限 JSON 数值。")


def _vectors(response: httpx.Response, expected: int) -> tuple[tuple[float, ...], ...]:
    """按真实 index 恢复请求顺序，重复/缺失索引和异常坐标在 Provider 边界失败。"""
    try:
        raw: object = json.loads(
            response.content.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_fields,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise EmbeddingProtocolError("Embedding 响应不是完整 UTF-8 JSON。") from error
    rows = _object(raw).get("data")
    if not isinstance(rows, list):
        raise EmbeddingProtocolError("Embedding 响应 data 不是列表。")
    raw_rows = cast(list[object], rows)
    if len(raw_rows) != expected:
        raise EmbeddingProtocolError("Embedding 响应数量与请求不一致。")
    indexed: dict[int, tuple[float, ...]] = {}
    for raw_row in raw_rows:
        row = _object(raw_row)
        index, embedding = row.get("index"), row.get("embedding")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < expected:
            raise EmbeddingProtocolError("Embedding 响应 index 无效。")
        if index in indexed or not isinstance(embedding, list):
            raise EmbeddingProtocolError("Embedding 响应有重复 index 或无效向量。")
        vector: list[float] = []
        for value in cast(list[object], embedding):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise EmbeddingProtocolError("Embedding 坐标必须是有限真实数值。")
            vector.append(float(value))
        indexed[index] = tuple(vector)
    try:
        return validate_vectors(tuple(indexed[index] for index in range(expected)), expected)
    except ValueError as error:
        raise EmbeddingProtocolError(str(error)) from error


class OpenAIEmbeddingResource:
    """app 独占的明确 HTTP embedding 资源，查询和索引共享连接池。

    不使用 SDK 的账号、代理或重试默认值。每个请求独立冻结完整 texts；
    取消直接结束对应传输等待，不提交向量。close 撤销新借用，取消并等待
    本实例拥有的在途只读请求，随后关闭同一个连接池。
    """

    def __init__(
        self, settings: EmbeddingSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """构造不连接网络；提供 transport 时将其关闭责任交给此资源。"""
        self.settings = settings
        headers: dict[str, str] = {}
        if settings.api_key is not None:
            headers["Authorization"] = "Bearer " + settings.api_key.get_secret_value()
        self._http = httpx.AsyncClient(
            trust_env=False,
            transport=transport or httpx.AsyncHTTPTransport(retries=0, trust_env=False),
            headers=headers,
            timeout=settings.timeout_seconds,
            follow_redirects=False,
        )
        self._requests: set[asyncio.Task[tuple[tuple[float, ...], ...]]] = set()
        self._close_task: asyncio.Task[None] | None = None

    @property
    def model_id(self) -> str:
        """返回显式向量空间版本，不借模型响应改写它。"""
        return self.settings.model_id

    @property
    def pending_resources(self) -> int:
        """返回仍由此资源拥有的 HTTP 请求数，关闭等待不提前丢引用。"""
        return len(self._requests)

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """仅提交一份完整有限文本批；HTTP、协议错误和取消保留原型。"""
        if self._close_task is not None:
            raise HostStateError("Embedding 资源已开始关闭。")
        if not texts:
            return ()
        if len(texts) > 32 or any(not isinstance(text, str) for text in texts):
            raise ValueError("Embedding 一次需要至多 32 个完整字符串。")
        task = asyncio.create_task(self._request(texts), name="embedding-request")
        self._requests.add(task)
        try:
            return await task
        finally:
            self._requests.discard(task)

    async def _request(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """明确 POST 一次，响应失败不补参数、重发或进入另一个 Provider。"""
        async with asyncio.timeout(self.settings.timeout_seconds):
            response = await self._http.post(
                self.settings.api_base.rstrip("/") + "/embeddings",
                json={"model": self.settings.model.removeprefix("openai/"), "input": list(texts)},
            )
        response.raise_for_status()
        return _vectors(response, len(texts))

    async def close(self) -> None:
        """等待同一次资源归还；一个关闭观察者取消不会撤销连接池清理。"""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="embedding-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        """先终止并收回在途只读计算，再释放资源拥有的连接池与传输。"""
        requests = tuple(self._requests)
        for request in requests:
            request.cancel()
        if requests:
            await asyncio.gather(*requests, return_exceptions=True)
            self._requests.difference_update(requests)
        await self._http.aclose()


class KnowledgeResources:
    """Host 创建的认识资源所有者，借出同一 KnowledgeService 给前台和反思。

    embedding 配置缺省时不创建 HTTP 资源，只保留本地三路召回。注入的
    embedding/reranker 归调用者关闭；本对象只关闭自己创建的向量资源。
    Host 应先结束两条会话，再关闭本对象，最后关闭母 Store。
    """

    def __init__(
        self,
        store: SemanticStore,
        changes: IndexChanges,
        settings: EmbeddingSettings | None,
        *,
        page_chars: int,
        can_start: Callable[[], None],
        embedding: EmbeddingProvider | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        """取得明确所有权但不发请求；没有配置时不接受隐含的向量后端。"""
        if settings is None and embedding is not None:
            raise ValueError("借入 Embedding 后端仍须显式声明向量配置。")
        if (
            settings is not None
            and embedding is not None
            and embedding.model_id != settings.model_id
        ):
            raise ValueError("借入 Embedding 后端的向量空间必须与显式配置一致。")
        # Ordering: 先完成纯服务参数校验，避免页长错误发生在自有 HTTP 池取得之后。
        self.knowledge = KnowledgeService(store, page_chars=page_chars, reranker=reranker)
        self._owned_embedding = (
            OpenAIEmbeddingResource(settings)
            if settings is not None and embedding is None
            else None
        )
        provider = embedding if embedding is not None else self._owned_embedding
        if provider is not None:
            self.knowledge.semantic = VectorRetriever(store, provider)
        self.indexer = KnowledgeIndexWorker(store, changes, provider, can_start=can_start)
        self._close_task: asyncio.Task[None] | None = None
        self._resources_released = False

    @property
    def failure(self) -> BaseException | None:
        """返回索引工作器真实故障；检索调用的故障直接返回其调用者。"""
        return self.indexer.failure

    @property
    def pending_resources(self) -> int:
        """汇总固定工作器、提交和自有向量传输的真实借用。"""
        return self.indexer.pending_resources + (
            0 if self._owned_embedding is None else self._owned_embedding.pending_resources
        )

    @property
    def resources_released(self) -> bool:
        """确认索引借用及自有 HTTP 池已归还，原提交故障仍由 close 报告。

        pending_resources 为零只说明没有在途请求，不能证明连接池已经关闭。
        Host 仅在此事实成立后释放本所有者及母 Store，不根据错误类型推测
        传输寿命，也不把借入的调用者资源纳入关闭责任。
        """
        return self._resources_released

    async def start(self) -> None:
        """在 Host 允许新派发后启动一个固定工作器。"""
        if self._close_task is not None:
            raise HostStateError("认识资源已开始关闭。")
        await self.indexer.start()

    async def close(self) -> None:
        """等待唯一关闭任务，不把调用者借入的资源当作自身连接池关闭。"""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="knowledge-resources-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        """先结束索引网络/提交借用，再释放自身创建的向量传输。"""
        errors: list[BaseException] = []
        try:
            await self.indexer.close()
        except BaseException as error:
            # 此处只收集资源清理原型。indexer.close 已等全部接管提交真正结束，
            # 即使它们报告失败，HTTP 池仍属于 app 必须归还的独立资源。
            errors.append(error)
        transport_released = self._owned_embedding is None
        if self._owned_embedding is not None:
            try:
                await self._owned_embedding.close()
            except BaseException as error:
                errors.append(error)
            else:
                transport_released = True
        # 索引提交失败在工作器全部归还后报告；HTTP 关闭失败则仍不能证明
        # 连接池释放。把资源事实登记在抛错之前，供 Host 继续独立清理。
        self._resources_released = self.indexer.pending_resources == 0 and transport_released
        if errors:
            raise BaseExceptionGroup("认识资源收尾失败。", errors)
