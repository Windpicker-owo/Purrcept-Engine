"""将 QQ 语音的实际音轨转换为可交付的有界 WAV，不负责下载或接管。"""

import io
import wave

import av
from av.error import FFmpegError

MAX_VOICE_BYTES = 20 * 1024 * 1024
MAX_VOICE_SECONDS = 600
_RATE = 16000


class VoiceError(ValueError):
    """平台语音不完整、不受支持或超过输入预算。"""


def decode_voice(data: bytes) -> bytes:
    """按实际内容解码语音，转换成单声道 WAV，限制解压后的体积和时长。

    Args:
        data: 完整且已限制下载大小的平台音频字节。

    Returns:
        可作为原生音频材料交付的 16 kHz 单声道 WAV。

    Raises:
        VoiceError: 内容不可解码、为空或超过限制。

    Note:
        禁止解码器通过嵌套协议访问其他地址；转换不依赖文件名或 MIME 声明。
    """
    if not data or len(data) > MAX_VOICE_BYTES:
        raise VoiceError("QQ 语音为空或超过 20 MiB。")
    pcm = bytearray()
    try:
        with av.open(io.BytesIO(data), mode="r", options={"protocol_whitelist": ""}) as source:
            formats = set(source.format.name.split(","))
            if not formats.intersection({"amr", "wav", "mp3"}) or not source.streams.audio:
                raise VoiceError("QQ 语音格式不受支持或没有音轨。")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=_RATE)
            for frame in source.decode(audio=0):
                for converted in resampler.resample(frame):
                    pcm.extend(bytes(converted.planes[0])[: converted.samples * 2])
                    if len(pcm) > min(MAX_VOICE_BYTES - 44, MAX_VOICE_SECONDS * _RATE * 2):
                        raise VoiceError("QQ 语音解码后超过大小或时长限制。")
            for converted in resampler.resample(None):
                pcm.extend(bytes(converted.planes[0])[: converted.samples * 2])
                if len(pcm) > min(MAX_VOICE_BYTES - 44, MAX_VOICE_SECONDS * _RATE * 2):
                    raise VoiceError("QQ 语音解码后超过大小或时长限制。")
    except (FFmpegError, OSError, ValueError) as error:
        if isinstance(error, VoiceError):
            raise
        raise VoiceError("QQ 语音无法完整解码。") from error
    if not pcm:
        raise VoiceError("QQ 语音没有可解码的音频。")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(_RATE)
        wav.writeframes(pcm)
    return output.getvalue()
