"""借用冻结 SDK 的纯编译接缝；不拥有请求预算、重试或客户端。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from importlib import import_module
from importlib.metadata import version
from pathlib import Path
from typing import Protocol, cast

from purrcept_core.models import (
    DecisionRequest,
    ModelRequest,
    ModelRequestError,
)

from purrcept_engine.serialization import encode


class _GenerationCompiler(Protocol):
    """冻结 SDK 纯编译入口的精确借用签名，不持有传输资源。"""

    def __call__(self, request: ModelRequest, *, model: str, config: object) -> dict[str, object]:
        """按冻结 SDK 规则将完整生成请求编译为原生 payload。

        Args:
            request: 已经按 Core 协议组装的完整生成请求。
            model: 本次调用使用的显式模型名，不在领域层推断替代模型。
            config: 冻结 SDK 的请求编译配置，仅含明确调用选项，不借入真实凭据。
        """
        ...


class _DecisionCompiler(Protocol):
    """专用判断纯编译接缝，保持 SDK 候选与输入表示。"""

    def __call__(
        self, request: DecisionRequest, *, model: str, config: object
    ) -> dict[str, object]:
        """按冻结 SDK 规则将完整判断请求编译为原生 payload。

        Args:
            request: 已经按 Core 协议组装的专用判断请求。
            model: 本次调用使用的显式模型名，不在领域层推断替代模型。
            config: 冻结 SDK 的请求编译配置，仅含明确调用选项，不借入真实凭据。
        """
        ...


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
        TypeError: 冻结 SDK 编译器没有可核验源码。

    Note:
        当前 SDK 未公开编译入口，因此这处接缝精确绑定 0.2.x 的两个模块并记录
        源码摘要。它不再实现消息或 TypeSafe 协议转换。公开生成参数保持原值；
        明确传输字段仅记录排除名称，不能将秘密写入原始请求证据。
    """
    if isinstance(request, ModelRequest):
        module = import_module("purrcept_litellm._request")
        compiler = cast(_GenerationCompiler, module.__dict__["compile_request"])
        config_module = import_module("purrcept_litellm.config")
        factory = cast(Callable[..., object], config_module.__dict__["LiteLLMConfig"])
        config = factory(default_options={"num_retries": 0, "max_retries": 0, "drop_params": False})
        payload = compiler(request, model=model, config=config)
    else:
        module = import_module("purrcept_litellm._decision")
        decision_compiler = cast(_DecisionCompiler, module.__dict__["compile_decision"])
        config_module = import_module("purrcept_litellm.decision")
        factory = cast(Callable[..., object], config_module.__dict__["LiteLLMDecisionConfig"])
        config = factory(default_options={"num_retries": 0, "max_retries": 0})
        payload = decision_compiler(request, model=model, config=config)

    for field in ("num_retries", "max_retries"):
        if payload.get(field, 0) != 0:
            raise ModelRequestError("SDK 自动重试必须显式关闭", provider="engine", model=model)
    public = {key: value for key, value in payload.items() if key not in _TRANSPORT_FIELDS}
    excluded = sorted(key for key in payload if key in _TRANSPORT_FIELDS)
    filename: object = module.__dict__.get("__file__")
    if not isinstance(filename, str):
        raise TypeError("冻结 SDK 编译器没有可核验源码。")
    fingerprint = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
    identity = (
        f"purrcept_litellm/{version('purrcept_litellm')}:litellm/{version('litellm')}:"
        f"{module.__name__}:{fingerprint}"
    )
    return encode({"payload": public, "excluded_transport_fields": excluded}), identity
