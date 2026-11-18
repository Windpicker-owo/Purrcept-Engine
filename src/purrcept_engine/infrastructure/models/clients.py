"""拥有 LiteLLM 生成及 TypeSafe/OpenRouter 判断的实例客户端与关闭进度。

应用按已校验模型配置创建资源，借出 ``backend`` 给 Core，并在在途借用结束后
关闭资源。模型材料、重试预算和请求证据属于主体会话；这里仅固定传输来源、
关闭 SDK 隐式重试并收回本实例的 HTTP 客户端，不修改 SDK 全局配置。
生成和判断连接只使用模型显式配置的 proxy，不受宿主环境代理影响。
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
from purrcept_engine.infrastructure.models.decision_transport import DecisionHttp, send_decision

if TYPE_CHECKING:
    from purrcept_litellm import LiteLLMBackend, LiteLLMDecisionBackend

__all__ = ["DecisionResource", "GenerationResource"]

# LiteLLM 1.109.1 的兼容目录描述线上协议；其中这些服务商在生成时有自己的
# HTTP 派发分支，仍需 AsyncHTTPHandler。其余兼容服务商复用 OpenAI SDK。
_NATIVE_COMPATIBLE_PROVIDERS = frozenset(
    {
        "deepseek",
        "azure_ai",
        "fireworks_ai",
        "together_ai",
        "ragflow",
        "xai",
        "groq",
        "opencode_go",
        "cometapi",
        "hosted_vllm",
        "edenai",
    }
)


def _openai_endpoint(settings: ModelConnection) -> str | None:
    """按固定版本的生成路由选客户端，兼容服务商的默认端点取自 SDK。"""
    from litellm import get_llm_provider
    from litellm.constants import openai_compatible_providers
    from litellm.llms.openai_like.json_loader import (  # pyright: ignore[reportMissingTypeStubs]
        JSONProviderRegistry,
    )

    _, provider, _, resolved_base = get_llm_provider(
        settings.model, api_base=settings.api_base, api_key=settings.api_key.get_secret_value()
    )
    if provider == "openai":
        return settings.api_base or "https://api.openai.com/v1"
    if provider in _NATIVE_COMPATIBLE_PROVIDERS:
        return None
    if (
        provider in openai_compatible_providers
        or provider in {"custom_openai", "bigmodel"}
        or JSONProviderRegistry.exists(provider)
    ):
        endpoint = settings.api_base or resolved_base
        if not endpoint:
            raise ConfigError(f"{provider} 未提供默认服务地址，请填写 api_base。")
        return endpoint
    return None


class GenerationResource:
    """应用创建的生成资源，按模型前缀借给 LiteLLM 对应的实例客户端。

    ``create`` 只保存已校验的模型配置，不探测模型或启动生成；调用方将
    ``backend`` 交给 Core。同一宿主事件循环内的并发生成各用独立请求，
    共用客户端连接池。首次生成时按服务商派发协议选择 OpenAI 客户端或
    LiteLLM 原生 HTTP handler；未使用资源的关闭不导入 LiteLLM。
    应用应先终止新借用并等待在途调用，再调用 ``close``。重复或被取消的
    关闭等待加入同一清理任务，不关闭 LiteLLM 全局缓存或其他宿主客户端。
    """

    def __init__(self, settings: ModelConnection) -> None:
        """固定凭据、端点与客户端所有权。

        Args:
            settings: 当前组件已校验的配置或纯声明。

        Note:
            不建立真实连接。
        """
        self._http: httpx.AsyncClient | None = None
        self._client: AsyncOpenAI | None = None
        self._native_handler: object | None = None
        self._settings = settings
        self._backend: LiteLLMBackend | None = None
        self._close_task: asyncio.Task[None] | None = None

    @classmethod
    def create(cls, settings: ModelConnection) -> GenerationResource:
        """从已校验配置建立一个独立所有者，不读取环境凭据或探测服务。

        Args:
            settings: 当前组件已校验的配置或纯声明。

        Returns:
            宿主新取得的生成资源；注入的外部资源不由此重复取得。
        """
        return cls(settings)

    @property
    def backend(self) -> GenerationResource:
        """借出本资源的 Core 生成边界，读取此属性不导入或调用 SDK。"""
        return self

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        """首次调用才初始化 SDK，关闭开始后拒绝新生成。

        Args:
            request: 已经按 Core 协议组装的完整生成请求。
            model: 本次调用使用的显式模型名，不在领域层推断替代模型。
            emit: 流事件观察回调；None 表示不向外部观察者投递。

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
            self._close_task = asyncio.create_task(self._close_client(), name="generation-close")
        await asyncio.shield(self._close_task)

    async def _close_client(self) -> None:
        """关闭已取得的实例连接池；未使用原生资源不为清理创建 handler。"""
        if self._client is not None:
            await self._client.close()
        elif self._http is not None:
            await self._http.aclose()

    async def _complete(self, **kwargs: object) -> object:
        """借用实例客户端进入真实 LiteLLM，不进入其全局客户端缓存。"""
        # 首次选择路由位于适配器的异常映射边界内，配置错误也能留下正常的请求回执。
        # 同一事件循环在第一个 await 前安装客户端，并发生成共享同一连接池。
        if self._client is None and self._native_handler is None:
            endpoint = _openai_endpoint(self._settings)
            if endpoint is not None:
                http = httpx.AsyncClient(
                    trust_env=False,
                    proxy=self._settings.proxy,
                    event_hooks={"request": [self._remove_implicit_account_headers]},
                )
                try:
                    self._client = AsyncOpenAI(
                        api_key=self._settings.api_key.get_secret_value(),
                        base_url=endpoint,
                        organization="",
                        project="",
                        max_retries=0,
                        http_client=http,
                    )
                except Exception:
                    await http.aclose()
                    raise
            else:
                # Lifecycle: 原生协议仍借用本实例独占的 HTTP 池，不能退入 SDK 全局
                # 客户端缓存。底层重试关闭，整轮重试继续由 Engine 的请求服务拥有。
                transport_module = import_module("litellm.llms.custom_httpx.http_handler")
                create_handler = cast(
                    Callable[..., object], transport_module.__dict__["AsyncHTTPHandler"]
                )
                handler = create_handler(
                    # 缓存查询、创建与生成必须共用同一代理传输，不能只代理最终生成。
                    transport=httpx.AsyncHTTPTransport(
                        proxy=self._settings.proxy, retries=0, trust_env=False
                    ),
                )
                self._native_handler = handler
                # SDK handler 没有类型桩，在此读取实例池，不向其它模块扩散动态类型。
                self._http = cast(httpx.AsyncClient, getattr(handler, "client"))  # noqa: B009
        module = import_module("litellm")
        function = cast(Callable[..., Awaitable[object]], module.__dict__["acompletion"])
        client = self._client if self._client is not None else self._native_handler
        return await function(client=client, **kwargs)

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
    使用官方 LiteLLM 的固定版本编译接缝，经 DecisionHttp 借用本实例 HTTPX
    客户端。协议编译、签名、Provider 映射和结果规范化仍由 SDK 完成；
    本资源固定客户端和实际尝试数，不修改 SDK 或重新实现判断协议。
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
        self._handler: DecisionHttp | None = None
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
        """
        if self._close_task is not None:
            raise ModelRequestError("判断模型资源已开始关闭", provider="engine")
        if self._backend is None:
            from purrcept_litellm import LiteLLMDecisionBackend

            # Ownership: HTTPX 客户端归当前资源，SDK 仅借用单次 POST 接缝。
            # 显式传输关闭环境代理及底层重试，不进入 SDK 默认 handler 的隐式重发。
            self._http = httpx.AsyncClient(
                trust_env=False,
                transport=httpx.AsyncHTTPTransport(
                    proxy=self._settings.proxy, retries=0, trust_env=False
                ),
            )
            self._handler = DecisionHttp(self._http)
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
        if self._http is not None:
            await self._http.aclose()

    async def _decide(self, **kwargs: object) -> object:
        """用本实例 handler 调用真实 SDK，保留其协议及错误翻译。"""
        assert self._handler is not None
        return await send_decision(self._handler, kwargs)
