"""QQ 的纯配置快照，连接与媒体资源只在插件 build/start 后取得。

入站范围仅控制自动接收，不是人员权限；没有 OWNER/GUEST 或跨通道身份映射。
路径统一相对主配置目录解析，重连不修改这份冻结配置或自动重发旧调用。
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
ListMode = Literal["blacklist", "whitelist"]
Mode = Literal["reverse", "direct"]


def _array(value: object) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("配置字段必须是数组。")
    return tuple(cast(Sequence[object], value))


def _ids(value: object) -> tuple[str, ...]:
    """TOML 列表在配置边界冻结；数字账号按明确语义统一为字符串。"""
    result: list[str] = []
    for item in _array(value):
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise ValueError("范围条目必须是数字 QQ 号。")
        text = str(item).strip()
        if not text.isascii() or not text.isdigit() or int(text) <= 0:
            raise ValueError("范围条目必须是正数字 QQ 号。")
        result.append(text)
    return tuple(dict.fromkeys(result))


class QQSettings(BaseModel):
    """一项 QQ 装配的静态资源与范围；重连次数来自配置，模型不能另填预算。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    phone_id: str = "phone"
    segment: str = "qq"
    label: str = "QQ"
    mode: Mode = "reverse"
    host: str = "127.0.0.1"
    port: int = Field(default=8095, ge=1, le=65535)
    access_token: SecretStr = SecretStr("")
    group_list_type: ListMode = "blacklist"
    group_list: tuple[str, ...] = ()
    private_list_type: ListMode = "blacklist"
    private_list: tuple[str, ...] = ()
    ban_user_id: tuple[str, ...] = ()
    inbox_size: int = Field(default=50, gt=0)
    cache_conversations: int = Field(default=128, gt=0)
    cache_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    frame_bytes: int = Field(default=1024 * 1024, gt=0)
    pending_calls: int = Field(default=16, gt=0)
    call_timeout_seconds: Positive = 15.0
    connect_attempts: int = Field(default=5, ge=1, le=32)
    connect_timeout_seconds: Positive = 10.0
    backoff_seconds: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)
    stable_seconds: Positive = 60.0
    typing_delay_base_seconds: NonNegative = 0.35
    typing_delay_per_char_seconds: NonNegative = 0.04
    typing_delay_max_seconds: NonNegative = 1.8
    media_dir: str = ".purrcept/qq-media"
    extra_media_roots: tuple[str, ...] = ()
    image_max_bytes: int = Field(default=2 * 1024 * 1024, gt=0)
    file_max_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    sticker_dir: str = ".purrcept/qq-stickers"

    @field_validator("group_list", "private_list", "ban_user_id", mode="before")
    @classmethod
    def freeze_ids(cls, value: object) -> tuple[str, ...]:
        """仅在真实配置边界规范账号值，运行期不重复转换来源身份。

        Args:
            value: 配置边界收到的账号身份序列，不把单个字符串拆成字符列表。
        """
        return _ids(value)

    @field_validator("extra_media_roots", mode="before")
    @classmethod
    def freeze_paths(cls, value: object) -> tuple[str, ...]:
        """保留显式目录声明，拒绝把单条字符串解释成字符列表。

        Args:
            value: 显式媒体目录声明序列，拒绝以单条字符串代替目录列表。

        Raises:
            ValueError: extra_media_roots 必须是非空路径数组。
        """
        values = _array(value)
        if any(not isinstance(item, str) or not item.strip() for item in values):
            raise ValueError("extra_media_roots 必须是非空路径数组。")
        return tuple(str(item) for item in values)

    @field_validator("backoff_seconds", mode="before")
    @classmethod
    def freeze_backoff(cls, value: object) -> tuple[float, ...]:
        """有限退避只按已配置失败序号选择，不使用 SDK 的无限恢复迭代器。

        Args:
            value: 有限重连退避秒数序列，采用顺序与失败次数一致。

        Raises:
            ValueError: 退避时长必须是有限非负数。
        """
        result: list[float] = []
        for item in _array(value):
            if isinstance(item, bool) or not isinstance(item, (float, int)):
                raise ValueError("退避时长必须是有限非负数。")
            if not math.isfinite(item) or item < 0:
                raise ValueError("退避时长必须是有限非负数。")
            result.append(float(item))
        return tuple(result)

    @model_validator(mode="after")
    def coherent(self) -> QQSettings:
        """目录标识与恢复次数在纯声明阶段完整核验，不能启动后悄悄补默认值。

        Returns:
            已校验并固定基准路径的 QQ 配置。

        Raises:
            ValueError: 参数值或组合不满足本入口的明确约束，错误正文指出具体字段。
        """
        if len(self.backoff_seconds) != self.connect_attempts - 1:
            raise ValueError("backoff_seconds 数量须等于 connect_attempts - 1。")
        for name in (self.phone_id, self.segment):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("phone_id/segment 必须是明确对象标识。")
        if not self.label.strip() or not self.media_dir.strip() or not self.sticker_dir.strip():
            raise ValueError("标签及资源目录不能留空。")
        if not self.host or any(char in self.host for char in "/?#@ \r\n"):
            raise ValueError("host 只能填写主机名或 IP，不能携带路径、凭据或查询。")
        return self

    def typing_pause_seconds(self, text: str) -> float:
        """保留既有打字间隔，零上限明确关闭延迟。

        Args:
            text: 本次明确传输、输入或呈现的完整文本。

        Note:
            不借此重置调用期限。
        """
        return min(
            self.typing_delay_max_seconds,
            self.typing_delay_base_seconds + self.typing_delay_per_char_seconds * len(text),
        )


def resolve_path(value: str, base_dir: Path) -> Path:
    """配置路径采用固定基准，构造时不探测文件系统或依赖进程 cwd。

    Args:
        value: 显式媒体路径，沿当前基准目录解析，不探测文件系统。
        base_dir: 解析相对配置路径的基准目录。

    Returns:
        沿显式基准目录解析的绝对路径；不在此验证文件存在性。
    """
    path = Path(value).expanduser()
    return Path(os.path.abspath(path if path.is_absolute() else base_dir / path))
