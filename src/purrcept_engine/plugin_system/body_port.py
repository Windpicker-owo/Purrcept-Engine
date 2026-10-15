"""音频借出的有限视觉事实接缝，不授予设备客户端或资源所有权。

app 在全部插件构造后绑定同实例 BodyPort。调用只改变身体本地最新状态，
不得等待网络或恢复；返回值分开说明 accepted 和 VTS adopted。离线事实
不会排队等待连接，后续恢复只运行基线。
"""

from typing import Protocol

from purrcept_engine.plugin_system.body import BodyIntent
from purrcept_engine.values import JsonObject


class BodyPort(Protocol):
    """Live2D 所有者提供、音频在实际开始/结束调用的限定借用契约。

    所有调用在宿主循环，不能启动音频或阻塞传输。playback_id 来自宿主
    声音运行时分配，并记录原CallContext关联，不是模型参数；进度只更新
    当前关联。过期和离线不补演。
    """

    def playback_started(
        self, playback_id: str, intent: BodyIntent | None, deadline: float
    ) -> JsonObject:
        """采用当前播放资格，未在线则报告未受理，不保留待恢复姿态。

        Args:
            playback_id: 本次实际播放的稳定身份，用于拒绝迟到口型或停止确认。
            intent: 有明确期限的身体语义姿态。
            deadline: 本次姿态允许采用的绝对单调截止时刻。
        """
        ...

    def playback_progress(self, playback_id: str, elapsed_seconds: float, energy: float) -> None:
        """只接收实际播放器进度。

        Args:
            playback_id: 本次实际播放的稳定身份，用于拒绝迟到口型或停止确认。
            elapsed_seconds: 实际播放器报告的已播放秒数。
            energy: 实际 PCM 片段的音量能量，用于驱动视觉反馈。

        Note:
            energy 是可用时的音频采样强度。
        """
        ...

    def playback_pcm(
        self, playback_id: str, sample_rate: int, sample_offset: int, pcm: bytes
    ) -> None:
        """交付调音后20ms采样块。

        Args:
            playback_id: 本次实际播放的稳定身份，用于拒绝迟到口型或停止确认。
            sample_rate: PCM 的明确采样率（Hz）。
            sample_offset: 当前 PCM 片段在本次播放中的起始采样位置。
            pcm: 已解码的 PCM 字节或按播放顺序产生的字节流。

        Note:
            只供视觉分析，不能阻塞或回压声音。
        """
        ...

    def playback_finished(self, playback_id: str) -> JsonObject:
        """撤销同身份联动，返回采用确认。

        Args:
            playback_id: 本次实际播放的稳定身份，用于拒绝迟到口型或停止确认。

        Note:
            旧结束不能清除新播放。
        """
        ...
