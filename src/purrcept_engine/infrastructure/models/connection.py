"""模型传输的最小只读配置契约；不依赖应用 TOML 模式。"""

from typing import Protocol

from pydantic import SecretStr


class ModelConnection(Protocol):
    """应用借出已校验的端点和凭据，客户端不得补入环境账号。"""

    @property
    def model(self) -> str:
        """返回显式协议与模型标识。"""
        ...

    @property
    def api_base(self) -> str:
        """返回已解析端点。"""
        ...

    @property
    def api_key(self) -> SecretStr:
        """返回配置明确提供的凭据。"""
        ...
