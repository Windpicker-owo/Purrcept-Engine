"""借用官方 LiteLLM 判断编译器，以宿主 HTTPX 客户端完成一次派发。

官方 1.105.0 的公开 adecisions 不转交请求级客户端，其默认 HTTP handler
还会隐式重发连接错误。本模块固定借用该版本的编译、签名、响应转换和错误
映射接缝，只替换网络派发所有者；不修改 SDK 源码、全局函数或客户端缓存。
DecisionResource 创建并关闭 HTTPX 客户端，本模块只借用，重试由请求服务决定。
SDK 回调不参与此路径；完整请求、真实尝试和结果由 Engine 自己的账本记录。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from importlib import import_module
from typing import Protocol, cast

import httpx


class _PreparedDecision(Protocol):
    """官方编译阶段交给传输阶段的只读字段，具体值及协议校验仍归 SDK。"""

    model: object
    custom_llm_provider: object
    logging_obj: object
    provider_config: object
    ir_request: object
    body: object
    api_base: object
    api_key: object
    headers: object
    timeout: object
    litellm_params: object


class _DecisionHandler(Protocol):
    """借用 SDK 的单次协议派发器，客户端及参数已由所属边界校验。"""

    async def adecisions(self, **kwargs: object) -> object:
        """使用调用方提供的 HTTP 接缝，返回提供商的中间响应。"""
        ...


class DecisionHttp:
    """资源所有者创建的单次 POST 适配器，不取得或关闭借用的连接池。

    LiteLLM 的 adecisions handler 在完成协议转换与签名后调用 post；它只需要
    此有限接口。并发调用各自构造请求，共享 HTTPX 连接池，不持有每请求状态。
    网络、状态码及取消错误原样交回 SDK 的错误映射，不自动重试或切换客户端。
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        """借用宿主创建的客户端。

        Args:
            client: 生命周期覆盖全部在途判断的资源，不由此适配器关闭。
        """
        self.client = client

    # Compatibility: timeout 是 SDK 传入的 HTTPX 参数名，必须保留原调用签名。
    async def post(
        self,
        url: str,
        *,
        content: bytes | str | None,
        json: object,
        headers: Mapping[str, str],
        timeout: httpx.Timeout | float | None,  # noqa: ASYNC109
        logging_obj: object,
    ) -> httpx.Response:
        """发送一次已签名判断请求，将 HTTP 错误交回 SDK 翻译。

        Args:
            url: SDK 根据显式提供商及端点生成的完整 URL。
            content: SDK 签名后的原始正文；缺省时使用 json。
            json: SDK 转换后的请求对象。
            headers: SDK 生成的显式认证及协议头。
            timeout: 本次尝试的剩余等待额度。
            logging_obj: SDK 日志接缝，本路径由 Engine 记录请求证据。

        Returns:
            已完整读取的成功 HTTP 响应，由 SDK 继续解释提供商协议。

        Raises:
            httpx.HTTPError: 实际传输或状态码失败，调用方负责分类。
        """
        del logging_obj
        response = await self.client.post(
            url, content=content, json=json, headers=headers, timeout=timeout
        )
        response.raise_for_status()
        return response


async def send_decision(http: DecisionHttp, payload: Mapping[str, object]) -> object:
    """借用固定版本的协议转换完成一次判断，不进入 SDK 默认重试与连接缓存。

    Args:
        http: 当前 DecisionResource 拥有的有限 HTTP 适配器。
        payload: Purrcept LiteLLM 已编译的完整判断参数，不修改调用者的映射。

    Returns:
        SDK 规范化的 Decisions 响应，继续由 Purrcept 适配层验证。

    Raises:
        Exception: SDK 原有的请求校验或提供商错误映射结果。

    Note:
        私有 SDK 接缝由 pyproject 精确固定版本，并由真实 HTTP 编码、错误次数及
        生命周期回归测试守护。升级 SDK 必须重新核验这些接缝，不能静默回退。
    """
    module = import_module("litellm.decisions.main")
    calls = import_module("litellm.decisions.call")
    prepare = cast(Callable[..., _PreparedDecision], module.__dict__["_prepare_decisions_call"])
    handler = cast(_DecisionHandler, calls.__dict__["_HANDLER"])
    format_response = cast(Callable[..., object], calls.__dict__["_format_response"])
    map_error = cast(Callable[..., Exception], calls.__dict__["_map_upstream_exception"])

    options = dict(payload)
    parameters = {
        name: options.pop(name, None)
        for name in (
            "model",
            "questions",
            "safety_identifier",
            "api_key",
            "api_base",
            "timeout",
            "custom_llm_provider",
            "extra_headers",
        )
    }
    parameters["decision_input"] = options.pop("input", None)
    call = prepare(**parameters, kwargs=options)

    try:
        response = await handler.adecisions(
            model=call.model,
            custom_llm_provider=call.custom_llm_provider,
            logging_obj=call.logging_obj,
            provider_config=call.provider_config,
            request=call.ir_request,
            body=call.body,
            api_base=call.api_base,
            api_key=call.api_key,
            headers=call.headers,
            timeout=call.timeout,
            litellm_params=call.litellm_params,
            client=http,
        )
    except Exception as error:
        # SDK 的分类决定请求服务能否重试；保留官方映射及原始异常链。
        raise map_error(error, call) from error
    return format_response(response, call)
