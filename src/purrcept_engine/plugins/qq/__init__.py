"""QQ 纯工厂入口；只在明确启用 build 时导入可选 WebSocket SDK。"""

from purrcept_engine.plugins.qq.plugin import factory

__all__ = ["factory"]
