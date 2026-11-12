"""按模型保存表演偏好，供正式控制器和独立调节页读取同一份数据。

Preferences 只描述归一化姿态与运动响应，不修改 VTS 参数绑定。读取有界、
写入校验版本并备份原文件；调用方串行保存，后台帧循环在工作线程读取，
仅在完整校验成功后采用新版本。模型身份不符时拒绝交叉套用。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class Preferences(BaseModel):
    """一个模型的持久表演偏好；数值为控制通道比例，不是相机或人体角度。

    零姿态偏移保留原校准。mouth_gain/gamma 在口型平滑之前改变目标，
    保留闭口和短停顿惯性；mouth_form 只能影响已有嘴型绑定，不推断音素。
    空闲转移只在没有显式或语境注视时使用，间隔是随机停留的平均尺度。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)
    model_id: str = Field(min_length=1)
    idle_enabled: bool = True
    idle_interval_seconds: float = Field(default=3.2, ge=1, le=12)
    idle_gaze_scale: float = Field(default=1.0, ge=0, le=2)
    idle_posture_scale: float = Field(default=1.0, ge=0, le=2)
    head_x: float = Field(default=0.0, ge=-0.6, le=0.6)
    head_y: float = Field(default=0.0, ge=-0.6, le=0.6)
    head_z: float = Field(default=0.0, ge=-0.6, le=0.6)
    body_x: float = Field(default=0.0, ge=-0.5, le=0.5)
    body_y: float = Field(default=0.0, ge=-0.5, le=0.5)
    body_z: float = Field(default=0.0, ge=-0.5, le=0.5)
    gaze_x: float = Field(default=0.0, ge=-0.5, le=0.5)
    gaze_y: float = Field(default=0.0, ge=-0.5, le=0.5)
    eye_open: float = Field(default=1.0, ge=0.2, le=1)
    mouth_gain: float = Field(default=1.0, ge=0, le=4)
    mouth_gamma: float = Field(default=1.0, ge=0.4, le=2)
    mouth_form: float = Field(default=0.0, ge=-1, le=1)
    mouth_form_gain: float = Field(default=1.0, ge=0, le=3)
    mouth_viseme_form_scale: float = Field(default=0.65, ge=0, le=2)
    mouth_attack_seconds: float = Field(default=0.07, ge=0.02, le=0.3)
    mouth_release_seconds: float = Field(default=0.14, ge=0.03, le=0.4)
    mouth_gap_hold_seconds: float = Field(default=0.08, ge=0, le=0.2)


class PreferenceConflict(ValueError):
    """页面版本落后于磁盘，必须重新读取后再保存。"""


def load_preferences(path: Path, model_id: str) -> tuple[Preferences, str]:
    """读取指定模型的有界偏好；文件缺失时返回默认值和 missing 版本。

    Args:
        path: 插件配置决定的固定文件，不接受浏览器提供路径。
        model_id: 当前校准模型身份。

    Raises:
        ValueError: 文件过大、字段无效或模型身份不符。
        OSError: 文件存在但无法读取。
    """
    try:
        with path.open("rb") as stream:
            raw = stream.read(32769)
    except FileNotFoundError:
        return Preferences(model_id=model_id), "missing"
    if len(raw) > 32768:
        raise ValueError("Live2D 偏好超过 32 KiB。")
    value = Preferences.model_validate_json(raw)
    if value.model_id != model_id:
        raise ValueError("Live2D 偏好与当前校准模型不符。")
    return value, hashlib.sha256(raw).hexdigest()


def save_preferences(path: Path, value: Preferences, previous: str) -> str:
    """核对版本、备份旧字节并原子保存；同一服务须串行调用。

    Args:
        path: 模型专用的持久文件。
        value: 已通过严格校验的新偏好。
        previous: 页面读取的实际字节摘要。

    Returns:
        新文件的摘要。

    Raises:
        PreferenceConflict: 保存期间发现文件已被修改。
        OSError: 备份或替换失败，此时不宣称保存成功。

    Note:
        文件系统没有跨进程 CAS；保存前再次核对可检测外部编辑，
        多个调节服务不应同时拥有同一路径。
    """
    _, revision = load_preferences(path, value.model_id)
    if previous != revision:
        raise PreferenceConflict("偏好已被修改，请重新载入后保存。")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (value.model_dump_json(indent=2) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _, current = load_preferences(path, value.model_id)
        if current != revision:
            raise PreferenceConflict("保存前偏好已变化，请重新载入。")
        if revision != "missing":
            backup = path.parent / "backups" / (path.name + "." + revision + ".bak")
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                backup.write_bytes(path.read_bytes())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(raw).hexdigest()
