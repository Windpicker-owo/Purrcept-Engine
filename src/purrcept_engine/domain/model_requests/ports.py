"""模型公开材料的纯编译端口，由应用选取匹配传输的实现。"""

from typing import Protocol

from purrcept_core.models import DecisionRequest, ModelRequest


class MaterialCompiler(Protocol):
    """按真实 SDK 顺序冻结公开材料与编译器身份；不联网或读取凭据。"""

    def __call__(self, request: ModelRequest | DecisionRequest, *, model: str) -> tuple[str, str]:
        """返回完整材料 JSON 与可核验编译器身份，不裁剪或改变请求。"""
        ...
