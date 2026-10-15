"""音频输入与处理的稳定业务错误，不包含播放器或 SDK 实现。"""

from __future__ import annotations

from purrcept_engine.errors import EngineError


class AudioError(EngineError):
    """外部合成、设备协议或现场额度失败，不能据此自动重播。"""
