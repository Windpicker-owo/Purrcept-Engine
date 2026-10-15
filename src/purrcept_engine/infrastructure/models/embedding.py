"""独占显式 embedding HTTP 资源；不调度认识或修改全局 SDK。"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from typing import cast

import httpx

from purrcept_engine.domain.knowledge.semantic import (
    EmbeddingProtocolError,
    validate_vectors,
)
from purrcept_engine.domain.knowledge.settings import EmbeddingSettings
from purrcept_engine.errors import HostStateError


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
        """构造不连接网络。

        Args:
            settings: 当前组件已校验的配置或纯声明。
            transport: 显式注入的 HTTP 传输；None 使用标准传输实现。

        Note:
            提供 transport 时将其关闭责任交给此资源。
        """
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
        """仅提交一份完整有限文本批。

        Args:
            texts: 按原顺序交给 embedding 的非空文本批次。

        Returns:
            按文本输入顺序取得、数量和维度已核验的有限向量。

        Raises:
            HostStateError: Embedding 资源已开始关闭。
            ValueError: Embedding 一次需要至多 32 个完整字符串。

        Note:
            HTTP、协议错误和取消保留原型。
        """
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
        """等待同一次资源归还。

        Note:
            一个关闭观察者取消不会撤销连接池清理。
        """
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
