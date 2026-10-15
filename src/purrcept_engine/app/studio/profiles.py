"""调音室预设的有界读取与乐观版本写入，与现场输出共用 VoiceProfile。

版本摘要使用实际文件字节，页面旧版本不能覆盖已经保存的新预设。写入
临时文件后原子替换；预设只在下一次合成/试听采用，不修改正在播放的实例。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

from purrcept_engine.plugins.audio.dsp import VoiceProfile


class ProfileConflict(ValueError):
    """实际预设字节已改变，调用方须读取当前版本后重新决定保存。"""


def load_profile(path: Path) -> tuple[VoiceProfile, str]:
    """缺失文件表示明确旁通。

    Args:
        path: 此操作定位的明确路径。

    Returns:
        本次读取的完整内容及其版本标记，供后续修改时核验。

    Raises:
        ValueError: 声线预设超过 16 KiB。

    Note:
        已存在的坏文件和 I/O 失败原样报告。
    """
    try:
        with path.open("rb") as stream:
            data = stream.read(16385)
    except FileNotFoundError:
        return VoiceProfile(), "missing"
    if len(data) > 16384:
        raise ValueError("声线预设超过 16 KiB。")
    return VoiceProfile.model_validate_json(data), hashlib.sha256(data).hexdigest()


def save_profile(path: Path, profile: VoiceProfile, previous: str) -> str:
    """核对实际版本再原子替换。

    Args:
        path: 此操作定位的明确路径。
        profile: 当前操作采用的已校验模型或声线预设。
        previous: 调用方持有的先前预设版本，用于拒绝覆盖并发修改。

    Raises:
        ProfileConflict: 声线预设已被修改，请重新载入后保存；声线预设保存前已被修改，
            请重新载入。

    Note:
        同实例调用由 Studio 串行，外部修改下次读取可见。
    """
    _, revision = load_profile(path)
    if previous != revision:
        raise ProfileConflict("声线预设已被修改，请重新载入后保存。")
    data = (profile.model_dump_json(indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # 覆盖前再次核对外部编辑；文件系统不提供跨进程 compare-and-swap，
        # 同一服务的保存串行，磁盘版本始终按下一读取的实际字节计算。
        _, current = load_profile(path)
        if current != revision:
            raise ProfileConflict("声线预设保存前已被修改，请重新载入。")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(data).hexdigest()
