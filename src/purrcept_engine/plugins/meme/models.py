"""定义梗百科公开 API、插件配置和本地收藏的校验边界。

远端正文只作为资料交给调用者，不成为宿主指令。配置决定 computer 父节点、
每日检查时刻和扫描窗口；运行资源与刺激交接由 runtime 拥有。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator

from purrcept_engine.plugin_system import TimeError, resolve_timezone
from purrcept_engine.values import JsonObject, decode_object

BASE_URL = "https://cnmeme.wiki"


class MemeSettings(BaseModel):
    """由应用一次校验的实例配置；相对 state_dir 由工厂沿配置目录解析。

    Note:
        daily_enabled 控制宿主存活期间的每日拉取，不启动操作系统计划任务。
        同一个状态目录只供一个实例使用，收藏不写回梗百科账号。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, validate_default=True)
    computer_id: str = Field(default="computer", pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    meme_id: str = Field(default="meme", pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    state_dir: Path = Field(default=Path(".purrcept/meme"), strict=False)
    daily_enabled: bool = True
    daily_at: str = Field(default="09:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str = "Asia/Shanghai"
    max_pages: int = Field(default=5, ge=1, le=20)
    reminder_limit: int = Field(default=10, ge=1, le=30)
    request_timeout_seconds: float = Field(default=10, gt=0, le=30, allow_inf_nan=False)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        """拒绝缺失的时区数据，避免每日任务随机器默认时区漂移。

        Args:
            value: IANA 时区名称；UTC/GMT 不需要额外数据。

        Raises:
            ValueError: 时区不存在或缺少 tzdata。
        """
        try:
            resolve_timezone(value)
        except TimeError as error:
            raise ValueError(str(error)) from error
        return value


class Meme(BaseModel):
    """公开词条的一次资料快照，收藏保留此版本而非承诺实时内容。

    Attributes:
        slug: 远端详情使用的原生身份，不接受完整 URL 代替。
        content_html: 仅详情提供的资料正文，不执行其中内容。
    """

    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)
    slug: str = Field(min_length=1, max_length=500)
    title: str
    summary: str
    tags: list[str]
    updated_at: str | None
    cover_url: str | None = None
    source_video: JsonObject | None = None
    content_html: str | None = None

    @field_validator("updated_at")
    @classmethod
    def aware_timestamp(cls, value: str | None) -> str | None:
        """保留原生时间文本，但拒绝无法解释的无时区更新时间。

        Args:
            value: API 的 ISO 时间文本，缺失时允许为 None。

        Raises:
            ValueError: 时间文本无效或没有时区。
        """
        if value is not None and datetime.fromisoformat(value).utcoffset() is None:
            raise ValueError("词条更新时间必须带时区。")
        return value

    def as_json(self) -> JsonObject:
        """返回资料及可核验的公开详情 API 链接。

        Returns:
            纯 JSON 快照；source_url 固定指向来源站点，不猜测网页路由。
        """
        value = decode_object(self.model_dump_json().encode())
        value["source_url"] = BASE_URL + "/api/v1/public/memes/" + quote(self.slug, safe="")
        return value


class MemePage(BaseModel):
    """公开列表信封；拒绝未知正文协议版本，不把坏响应当作空列表。"""

    model_config = ConfigDict(extra="ignore", strict=True)
    schema_version: Literal[1]
    items: list[Meme]
    limit: int = Field(ge=1, le=30)
    offset: int = Field(ge=0)
    has_more: bool


class MemeDetail(BaseModel):
    """公开详情信封；404 由 HTTP 边界明确报告。"""

    model_config = ConfigDict(extra="ignore", strict=True)
    schema_version: Literal[1]
    item: Meme


class PendingDigest(BaseModel):
    """先落盘再提交宿主的提醒，重启沿用同一原文与原生事件身份。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    event_id: str
    day: str
    occurred_at: str
    text: str
    fingerprints: dict[str, str]
    changed_count: int = Field(ge=1)
    truncated: bool


class MemeState(BaseModel):
    """插件独占的持久状态，完整替换成功后才发布给运行时读者。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    favorites: dict[str, Meme] = Field(default_factory=dict)
    seen: dict[str, str] = Field(default_factory=dict)
    last_day: str | None = None
    pending: PendingDigest | None = None
