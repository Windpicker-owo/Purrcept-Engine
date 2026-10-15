"""本机语音输入的纯插件入口，默认不加载设备或模型。"""

from purrcept_engine.plugins.voice_input.plugin import factory

__all__ = ["factory"]
