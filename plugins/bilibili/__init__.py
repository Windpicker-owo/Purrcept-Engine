"""B 站本地插件入口，供配置中的 local/bilibili:factory 加载。"""

from .plugin import factory

__all__ = ["factory"]
