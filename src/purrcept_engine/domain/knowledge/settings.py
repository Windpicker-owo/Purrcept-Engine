"""声明向量空间、端点及凭据；校验不建立连接。"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator

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
