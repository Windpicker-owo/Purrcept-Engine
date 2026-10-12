"""QQ 纯工厂入口；只在明确启用 build 时导入可选 WebSocket SDK。"""

from .plugin import factory

__all__ = ["factory"]
