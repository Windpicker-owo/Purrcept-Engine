"""向展示插件提供正在实际播放的台词投影，不包含模型思考或工具结果。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpeechCaption:
    """声音所有者发布的一次真实播放快照。

    Attributes:
        playback_id: 播放器发生身份，不以相同文本代替。
        text: 此次 speak 明确交给合成器的台词。
        elapsed_seconds: 播放器已确认的媒体时间。

    Note:
        仅播放期间返回；排队、合成、撤销、失败及结束均返回 None。
        消费者不得将此投影写回音频业务或推断物理听到的效果。
    """

    playback_id: str
    text: str
    elapsed_seconds: float
