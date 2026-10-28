"""梗百科插件入口；纯工厂声明不会联网或启动日更任务。"""

from purrcept_engine.plugins.meme.plugin import factory

__all__ = ["factory"]
