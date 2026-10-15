"""在内存中封装 mono S16LE WAV；不创建解码器、设备或后台任务。"""

from __future__ import annotations

import io
import wave

from purrcept_engine.domain.audio.errors import AudioError


def encode_wav(pcm: bytes, sample_rate: int) -> bytes:
    """将非空mono S16LE采样封装为WAV，不解码、不变速或访问设备。

    Args:
        pcm: 已解码的 PCM 字节或按播放顺序产生的字节流。
        sample_rate: PCM 的明确采样率（Hz）。

    Raises:
        AudioError: WAV封装需要非空完整S16样本与8000..48000Hz采样率。

    Note:
        调音、ASR输入和原生音频共享此容器边界，字节数必须落在完整样本。
        采样率由已取得的实际PCM格式提供，不能从文件长度猜测。
    """
    if not pcm or len(pcm) % 2 or not 8000 <= sample_rate <= 48000:
        raise AudioError("WAV封装需要非空完整S16样本与8000..48000Hz采样率。")
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm)
    return stream.getvalue()
