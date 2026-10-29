"""借用冻结 SDK 的纯编译接缝；不拥有请求预算、重试或客户端。"""

from __future__ import annotations

from purrcept_core.models import (
    DecisionRequest,
    ModelRequest,
    ModelRequestError,
)
from purrcept_litellm.compilation import compile_decision, compile_request, compiler_identity
from purrcept_litellm.config import LiteLLMConfig
from purrcept_litellm.decision import LiteLLMDecisionConfig

from purrcept_engine.serialization import encode

_TRANSPORT_FIELDS = frozenset(
    {
        "api_key",
        "api_base",
        "api_version",
        "timeout",
        "client",
        "http_client",
        "headers",
        "extra_headers",
        "authorization",
        "organization",
        "project",
        "success_callback",
        "failure_callback",
        "callbacks",
        "callback",
        "metadata",
        "access_token",
        "refresh_token",
        "password",
        "credentials",
    }
)


def compile_materials(request: ModelRequest | DecisionRequest, *, model: str) -> tuple[str, str]:
    """借用冻结 SDK 的纯编译器，记录真实顺序且不给编译器凭据。

    Args:
        request: 已按 Core 协议组装的完整生成或判断请求。
        model: 本次调用使用的显式模型名，不在领域层推断替代模型。

    Returns:
        公开请求材料的完整 JSON 与实际 SDK 编译器源码身份；传输秘密只保留排除字段名。

    Raises:
        ModelRequestError: SDK 自动重试必须显式关闭。
        TypeError: SDK 编译器身份无法核验。

    Note:
        SDK 的公共纯编译入口与实际后端共用同一实现，身份涵盖共享转换依赖。
        本模块不实现消息或 TypeSafe 协议转换。公开生成参数保持原值；
        明确传输字段仅记录排除名称，不能将秘密写入原始请求证据。
    """
    if isinstance(request, ModelRequest):
        config = LiteLLMConfig(
            default_options={"num_retries": 0, "max_retries": 0, "drop_params": False}
        )
        payload = compile_request(request, model=model, config=config)
    else:
        config = LiteLLMDecisionConfig(default_options={"num_retries": 0, "max_retries": 0})
        payload = compile_decision(request, model=model, config=config)

    for field in ("num_retries", "max_retries"):
        if payload.get(field, 0) != 0:
            raise ModelRequestError("SDK 自动重试必须显式关闭", provider="engine", model=model)
    public = {key: value for key, value in payload.items() if key not in _TRANSPORT_FIELDS}
    excluded = sorted(key for key in payload if key in _TRANSPORT_FIELDS)
    identity = compiler_identity("generation" if isinstance(request, ModelRequest) else "decision")
    return encode({"payload": public, "excluded_transport_fields": excluded}), identity
