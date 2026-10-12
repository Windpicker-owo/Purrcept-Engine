"""音频借出的有限视觉事实接缝，不授予设备客户端或资源所有权。

app 在全部插件构造后绑定同实例 BodyPort。调用只改变身体本地最新状态，
不得等待网络或恢复；返回值分开说明 accepted 和 VTS adopted。离线事实
不会排队等待连接，后续恢复只运行基线。
"""

from typing import Protocol

from purrcept_engine.execution.protocol import JsonObject

from .models import BodyIntent


class BodyPort(Protocol):
    """Live2D 所有者提供、音频在实际开始/结束调用的限定借用契约。

    所有调用在宿主循环，不能启动音频或阻塞传输。playback_id 来自宿主
    声音运行时分配，并记录原CallContext关联，不是模型参数；进度只更新
    当前关联。过期和离线不补演。
    """

    def playback_started(
        self, playback_id: str, intent: BodyIntent | None, deadline: float
    ) -> JsonObject:
        """采用当前播放资格，未在线则报告未受理，不保留待恢复姿态。"""
        ...

    def playback_progress(self, playback_id: str, elapsed_seconds: float, energy: float) -> None:
        """只接收实际播放器进度；energy 是可用时的音频采样强度。"""
        ...

    def playback_pcm(
        self, playback_id: str, sample_rate: int, sample_offset: int, pcm: bytes
    ) -> None:
        """交付调音后20ms采样块；只供视觉分析，不能阻塞或回压声音。"""
        ...

    def playback_finished(self, playback_id: str) -> JsonObject:
        """撤销同身份联动，返回采用确认；旧结束不能清除新播放。"""
        ...
