"""拥有生成网关及 TypeSafe/OpenRouter 判断的显式客户端与关闭进度。

应用按已校验模型配置创建资源，借出 ``backend`` 给 Core，并在在途借用结束后
关闭资源。模型材料、重试预算和请求证据属于主体会话；这里仅固定传输来源、
关闭 SDK 隐式重试并收回本实例的 HTTP 客户端，不修改 SDK 全局配置。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from importlib import import_module
from typing import TYPE_CHECKING, cast

import httpx
from openai import AsyncOpenAI
from purrcept_core.models import (
    DecisionRequest,
    DecisionResponse,
    ModelEventSink,
    ModelRequest,
    ModelRequestError,
    ModelResponse,
)

from purrcept_engine.errors import ConfigError
from purrcept_engine.infrastructure.models.connection import ModelConnection

if TYPE_CHECKING:
    from purrcept_litellm import LiteLLMBackend, LiteLLMDecisionBackend

__all__ = ["DecisionResource", "OpenAIGenerationResource"]


class OpenAIGenerationResource:
    """应用创建并拥有的 OpenAI 兼容生成路径，可被会话重复借用。

    ``create`` 只构造已校验端点的客户端，不探测模型或启动生成；调用方将
    ``backend`` 交给 Core。同一宿主事件循环内的并发生成各用独立请求，
    共用客户端连接池；首次 SDK 初始化在第一个等待点前完成。
    应用应先终止新借用并等待在途调用，再调用 ``close``。重复或被取消的
    关闭等待加入同一清理任务，不关闭 LiteLLM 全局缓存或其他宿主客户端。
    """

    def __init__(self, settings: ModelConnection) -> None:
        """固定凭据、端点与客户端所有权。

        Args:
            settings: 当前组件已校验的配置或纯声明。

        Raises:
            ConfigError: 首批生成路径需要显式 openai/ 模型协议标识。

        Note:
            不建立真实连接。
        """
        if not settings.model.startswith("openai/"):
            raise ConfigError("首批生成路径需要显式 openai/ 模型协议标识")

        self._http = httpx.AsyncClient(
            trust_env=False,
            event_hooks={"request": [self._remove_implicit_account_headers]},
        )
        self._client = AsyncOpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url=settings.api_base,
            organization="",
            project="",
            max_retries=0,
            http_client=self._http,
        )
        self._settings = settings
        self._backend: LiteLLMBackend | None = None
        self._close_task: asyncio.Task[None] | None = None

    @classmethod
    def create(cls, settings: ModelConnection) -> OpenAIGenerationResource:
        """从已校验配置建立一个独立所有者，不读取环境凭据或探测服务。

        Args:
            settings: 当前组件已校验的配置或纯声明。

        Returns:
            宿主新取得的生成资源；注入的外部资源不由此重复取得。
        """
        return cls(settings)

    @property
    def backend(self) -> OpenAIGenerationResource:
        """借出本资源的 Core 生成边界，读取此属性不导入或调用 SDK。"""
        return self

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        """首次调用才初始化 SDK，关闭开始后拒绝新生成。

        Args:
            request: 已经按 Core 协议组装的完整生成请求。
            model: 本次调用使用的显式模型名，不在领域层推断替代模型。
            emit: 流事件观察回调；None 表示不向外部观察者投递。 None
                表示不向外部观察者发送流事件。

        Returns:
            通过协议与证据提交门槛的真实模型响应。

        Raises:
            ModelRequestError: 生成模型资源已开始关闭。
        """
        if self._close_task is not None:
            raise ModelRequestError("生成模型资源已开始关闭", provider="engine")
        if self._backend is None:
            # Lifecycle: SDK 导入可能初始化自己的价格目录；放在真实调用边界，
            # 使配置、资源装配和未使用资源的关闭不触发此第三方外部副作用。
            from purrcept_litellm import LiteLLMBackend

            self._backend = LiteLLMBackend(
                api_key=self._settings.api_key.get_secret_value(),
                api_base=self._settings.api_base,
                default_options={"num_retries": 0, "max_retries": 0, "drop_params": False},
                completion=self._complete,
            )
        return await self._backend.generate(request, model=model, emit=emit)

    async def close(self) -> None:
        """等待同一次客户端收回。

        Note:
            等待者取消不取消资源清理。
        """
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._client.close(), name="generation-close")
        await asyncio.shield(self._close_task)

    async def _complete(self, **kwargs: object) -> object:
        """借用实例客户端进入真实 LiteLLM，不进入其全局客户端缓存。"""
        module = import_module("litellm")
        function = cast(Callable[..., Awaitable[object]], module.__dict__["acompletion"])
        return await function(client=self._client, **kwargs)

    async def _remove_implicit_account_headers(self, request: httpx.Request) -> None:
        """在实际派发边界移除配置未声明的组织及项目账号控制头。"""
        # Compatibility: 首批冻结的 LiteLLM 会用环境组织值覆盖借用客户端属性；
        # OpenAI SDK 也可继承项目。请求钩子晚于这些默认值的应用，且仅改变
        # 本实例的实际请求，不清空进程环境、不制造替代凭据或改写其他请求。
        request.headers.pop("OpenAI-Organization", None)
        request.headers.pop("OpenAI-Project", None)


class DecisionResource:
    """应用拥有的专用判断传输，借出 Core Decisions 后端。

    模型前缀显式选择 TypeSafe 或 OpenRouter，端点与凭据来自同一配置。
    TypeSafe 使用 /systemone；OpenRouter 使用 /alpha/decisions，并保留
    上游模型的命名空间。两条路径都由 LiteLLM 编译，不回退到聊天生成。
    依赖已冻结的 LiteLLM 本地补丁提供公开 ``http_client`` 借用接缝及
    ``retry_connection_errors`` 开关。协议编译、Provider 映射和结果规范化
    继续由 SDK 完成；本资源固定客户端和实际尝试数，不重新实现判断协议。
    应用先结束在途借用，再关闭同一个所有者，不操作 SDK 全局客户端缓存。
    """

    def __init__(self, settings: ModelConnection) -> None:
        """记录已校验配置，首次调用前不导入 SDK 或取得 HTTP 传输。

        Args:
            settings: 当前组件已校验的配置或纯声明。

        Raises:
            ConfigError: 专用判断路径需要显式 typesafe/ 或 openrouter/ 模型协议标识。
        """
        # 只接入已核验传输契约的提供商；端点地址不能代替模型前缀选择协议。
        if not settings.model.startswith(("typesafe/", "openrouter/")):
            raise ConfigError("专用判断路径需要显式 typesafe/ 或 openrouter/ 模型协议标识")

        self._settings = settings
        self._handler: object | None = None
        self._http: httpx.AsyncClient | None = None
        self._backend: LiteLLMDecisionBackend | None = None
        self._close_task: asyncio.Task[None] | None = None

    @classmethod
    def create(cls, settings: ModelConnection) -> DecisionResource:
        """建立显式判断所有者，不探测端点或修改全局 SDK 配置。

        Args:
            settings: 当前组件已校验的配置或纯声明。

        Returns:
            宿主新取得的判断资源，隐式传输重试已经关闭。
        """
        return cls(settings)

    @property
    def backend(self) -> DecisionResource:
        """借出本资源的 Core 判断边界。

        Note:
            读取不初始化 SDK 或传输。
        """
        return self

    async def decide(self, request: DecisionRequest, *, model: str) -> DecisionResponse:
        """首次请求才取得 SDK handler，关闭后不重建资源或启动判断。

        Args:
            request: 已经按 Core 协议组装的专用判断请求。
            model: 本次调用使用的显式模型名，不在领域层推断替代模型。

        Returns:
            真实判断响应，概率和拒答仍由所属判断流程解释。

        Raises:
            ModelRequestError: 判断模型资源已开始关闭。
            TypeError: LiteLLM 判断 handler 未提供 HTTPX AsyncClient。
        """
        if self._close_task is not None:
            raise ModelRequestError("判断模型资源已开始关闭", provider="engine")
        if self._backend is None:
            from purrcept_litellm import LiteLLMDecisionBackend

            # Ownership: 此实例取得并登记 handler，不进入全局客户端缓存。
            # 首调用前无需打开 SDK；显式 transport 则关闭环境代理来源和连接重发。
            module = import_module("litellm.llms.custom_httpx.http_handler")
            handler_factory = cast(Callable[..., object], module.__dict__["AsyncHTTPHandler"])
            self._handler = handler_factory(
                transport=httpx.AsyncHTTPTransport(retries=0, trust_env=False),
                retry_connection_errors=False,
            )
            # Boundary: 此 SDK 子模块没有公开类型标记。只验证实际借用的
            # HTTPX 资源契约，不用猜测的类型或 repr 代替真实客户端。
            client: object = getattr(self._handler, "client", None)
            if not isinstance(client, httpx.AsyncClient):
                raise TypeError("LiteLLM 判断 handler 未提供 HTTPX AsyncClient")
            self._http = client
            self._backend = LiteLLMDecisionBackend(
                api_key=self._settings.api_key.get_secret_value(),
                api_base=self._settings.api_base,
                default_options={"num_retries": 0, "max_retries": 0},
                decision=self._decide,
            )
        return await self._backend.decide(request, model=model)

    async def close(self) -> None:
        """收回此 handler 的 HTTP 资源，重复等待同一次关闭任务。"""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_http(), name="decision-close")
        await asyncio.shield(self._close_task)

    async def _close_http(self) -> None:
        """未使用资源只提交关闭状态，不为了清理而初始化 SDK。"""
        if self._handler is not None and self._http is None:
            # 初始化已取得 handler 却拒绝其客户端类型时，不能把未知资源
            # 当作未使用状态并报告释放成功；保留失败供宿主关闭结果呈现。
            raise TypeError("无法确认 LiteLLM 判断 handler 的 HTTP 资源所有权")
        if self._http is not None:
            await self._http.aclose()

    async def _decide(self, **kwargs: object) -> object:
        """用本实例 handler 调用真实 SDK，保留其协议及错误翻译。"""
        assert self._handler is not None
        module = import_module("litellm")
        function = cast(Callable[..., Awaitable[object]], module.__dict__["adecisions"])
        return await function(http_client=self._handler, **kwargs)
