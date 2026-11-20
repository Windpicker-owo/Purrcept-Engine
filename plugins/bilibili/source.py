"""定义 B 站只读数据源和可离线使用的本地清单适配器。

适配器负责页面游标、稿件与分 P 身份、字幕及媒体的取得。插件负责按调用窗口
组合材料和交付模型。清单由宿主操作者提供，启动时校验一次，页间顺序保持稳定。
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, BinaryIO, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from purrcept_engine.domain.bilibili import BVID_PATTERN as BVID_PATTERN
from purrcept_engine.errors import ConfigError

type Bvid = Annotated[str, Field(pattern=rf"^{BVID_PATTERN}$")]
type Rpid = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
type Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
type FeedKind = Literal["recommend", "hot", "related"]
type CommentOrder = Literal["hot", "new"]


class _SourceValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class VideoPart(_SourceValue):
    """用稿件内分 P 序号和 CID 标识独立的播放时间轴。"""

    page: int = Field(ge=1)
    cid: int = Field(ge=1)
    title: str = Field(max_length=200)
    duration: Seconds = Field(gt=0)


class Video(_SourceValue):
    """推荐、搜索和观影共用的稿件信息，分 P 保持原顺序。"""

    bvid: Bvid
    title: str = Field(min_length=1, max_length=200)
    uploader: str = Field(max_length=100)
    description: str = Field(default="", max_length=2000)
    parts: tuple[VideoPart, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _part_identities(self) -> Self:
        """校验序号能唯一定位分 P，避免素材跨时间轴拼接。"""
        if any(part.page != index for index, part in enumerate(self.parts, 1)):
            raise ValueError("分 P 必须从 1 连续编号。")
        if len({part.cid for part in self.parts}) != len(self.parts):
            raise ValueError("稿件内的 CID 不得重复。")
        return self


class VideoSummary(_SourceValue):
    """列表接口实际返回的视频卡片。分 P 在打开稿件时单独解析。"""

    bvid: Bvid
    title: str = Field(min_length=1, max_length=200)
    uploader: str = Field(max_length=100)
    duration: Seconds | None = None


class TranscriptLine(_SourceValue):
    """保留字幕或转写片段的实际起止时间。"""

    start: Seconds
    end: Seconds
    text: str = Field(min_length=1, max_length=4000, pattern=r"\S")

    @model_validator(mode="after")
    def _range(self) -> Self:
        """拒绝没有正时长的台词区间。"""
        if self.end <= self.start:
            raise ValueError("台词结束时间必须晚于开始时间。")
        return self


class Transcript(_SourceValue):
    """携带出处和 CID 的字幕轨或语音转写，可为空轨。"""

    cid: int = Field(ge=1)
    source: Literal["subtitle", "asr"]
    lines: tuple[TranscriptLine, ...]


class Comment(_SourceValue):
    """一条稿件评论，顶层评论和楼中楼共享稳定评论 ID。"""

    rpid: Rpid
    author: str = Field(max_length=100)
    text: str = Field(max_length=4000)
    likes: int = Field(default=0, ge=0)
    published_at: int = Field(default=0, ge=0)
    pinned: bool = False
    root_rpid: Rpid | None = None
    reply_count: int | None = Field(default=None, ge=0)


@dataclass(frozen=True, slots=True)
class Page[Item]:
    """按实际来源排序的一页数据，后续页由该来源的游标定位。"""

    items: tuple[Item, ...]
    next_cursor: str | None = None


class SourceError(Exception):
    """把数据源的已知失败及稳定错误类别交给能力回执。"""

    def __init__(self, code: str, message: str) -> None:
        """记录适合展示给调用者的原因。"""
        super().__init__(message)
        self.code = code


class BilibiliSource(Protocol):
    """由插件 build 创建、由 Engine 能力调用的只读数据源。

    内置 ManifestSource 和 WebSource，也可通过配置指定已安装模块的异步工厂。单个插件实例的
    调用由 Engine 串行调度，全部在途调用结束后才调用 close。来源自行管理访问权限、
    客户端和超时，用 SourceError 报告已知故障，返回 None 表示对应素材缺失。
    分页按来源顺序交付，游标与查询条件绑定使用。字幕和媒体必须对应所传稿件和 CID，
    时间使用分 P 内的秒数，媒体流所有权在 open_video 返回后移交给插件。
    """

    async def feed(
        self, kind: FeedKind, bvid: str | None, cursor: str | None, limit: int
    ) -> Page[Video | VideoSummary]:
        """返回指定推荐来源的一页。缺少来源时报告 SourceError。"""
        ...

    async def search(
        self, query: str, cursor: str | None, limit: int
    ) -> Page[Video | VideoSummary]:
        """返回一页视频搜索结果。"""
        ...

    async def video(self, bvid: str) -> Video:
        """读取指定稿件及分 P。稿件不可见时报告 SourceError。"""
        ...

    async def comments(
        self,
        bvid: str,
        root_rpid: str | None,
        order: CommentOrder,
        cursor: str | None,
        limit: int,
    ) -> Page[Comment]:
        """按条件返回顶层评论或同一楼里的回复。"""
        ...

    async def transcript(
        self, bvid: str, part: VideoPart, start: float, end: float
    ) -> Transcript | None:
        """读取覆盖该时间范围的字幕或转写，允许返回整轨供插件筛选。"""
        ...

    async def open_video(self, bvid: str, part: VideoPart) -> BinaryIO | None:
        """交出从分 P 起点开始的可 seek 二进制流，由插件解码并关闭。"""
        ...

    async def close(self) -> None:
        """宿主收回全部调用后释放适配器持有的资源。"""
        ...


class _PartMaterial(_SourceValue):
    file: str | None = None
    transcript: Transcript | None = None


class _Manifest(_SourceValue):
    videos: tuple[Video, ...]
    recommend: tuple[Bvid, ...] | None = None
    hot: tuple[Bvid, ...] | None = None
    related: dict[Bvid, tuple[Bvid, ...]] = Field(default_factory=dict)
    comments: dict[Bvid, tuple[Comment, ...]] = Field(default_factory=dict)
    materials: dict[Bvid, dict[str, _PartMaterial]] = Field(default_factory=dict)


def _page[Item](items: Sequence[Item], cursor: str | None, limit: int) -> Page[Item]:
    """本地只读快照的游标使用行偏移，外部来源可以采用自己的游标。"""
    if cursor is not None and re.fullmatch(r"[0-9]{1,9}", cursor) is None:
        raise SourceError("invalid_cursor", "游标无效，请用上一页的 next_cursor。")
    offset = int(cursor) if cursor is not None else 0
    end = offset + limit
    return Page(tuple(items[offset:end]), str(end) if end < len(items) else None)


class ManifestSource:
    """用本地清单复现已取得的 B 站推荐、评论和分 P 媒体。"""

    def __init__(self, path: Path) -> None:
        """加载一次清单并建立稿件索引，拒绝悬空推荐和跨稿件材料。"""
        self._root = path.parent.resolve()
        try:
            self._data = _Manifest.model_validate_json(path.read_bytes())
        except (OSError, ValidationError) as error:
            raise ConfigError(f"无法读取 B 站清单 {path}：{error}") from error
        self._videos = {video.bvid: video for video in self._data.videos}
        if len(self._videos) != len(self._data.videos):
            raise ConfigError("B 站清单包含重复的 BV 号。")
        referenced = {
            *self._data.comments,
            *self._data.materials,
            *self._data.related,
            *(self._data.recommend or ()),
            *(self._data.hot or ()),
        }
        referenced.update(bvid for items in self._data.related.values() for bvid in items)
        if missing := referenced.difference(self._videos):
            raise ConfigError("B 站清单引用了未定义的稿件：" + "、".join(sorted(missing)))
        for bvid, materials in self._data.materials.items():
            valid = {str(part.cid) for part in self._videos[bvid].parts}
            for cid, material in materials.items():
                if cid not in valid or (
                    material.transcript is not None and str(material.transcript.cid) != cid
                ):
                    raise ConfigError(f"B 站清单的素材 CID 与分 P 不一致：{bvid}/{cid}。")
        for bvid, comments in self._data.comments.items():
            roots = {item.rpid for item in comments if item.root_rpid is None}
            if len({item.rpid for item in comments}) != len(comments) or any(
                item.root_rpid not in roots for item in comments if item.root_rpid is not None
            ):
                raise ConfigError(f"B 站清单包含重复的评论或找不到根评论：{bvid}。")

    async def feed(
        self, kind: FeedKind, bvid: str | None, cursor: str | None, limit: int
    ) -> Page[Video]:
        """保留记录的推荐顺序，重复 BV 只交付首次出现的位置。"""
        if kind == "related":
            entries = self._data.related.get(bvid) if bvid is not None else None
        else:
            entries = self._data.recommend if kind == "recommend" else self._data.hot
        if entries is None:
            raise SourceError("unavailable", "清单没有这个推荐来源。")
        return _page([self._videos[key] for key in dict.fromkeys(entries)], cursor, limit)

    async def search(self, query: str, cursor: str | None, limit: int) -> Page[Video]:
        """按标题、UP 主、简介搜索清单，匹配词之间使用 AND。"""
        words = query.casefold().split()
        matches: list[Video] = []
        for video in self._videos.values():
            searchable = f"{video.title} {video.uploader} {video.description}".casefold()
            if all(word in searchable for word in words):
                matches.append(video)
        return _page(matches, cursor, limit)

    async def video(self, bvid: str) -> Video:
        """按 BV 号返回清单中的稿件。"""
        if bvid not in self._videos:
            raise SourceError("not_found", f"清单没有稿件 {bvid}。")
        return self._videos[bvid]

    async def comments(
        self,
        bvid: str,
        root_rpid: str | None,
        order: CommentOrder,
        cursor: str | None,
        limit: int,
    ) -> Page[Comment]:
        """按选定的根评论和排序分页，区分未采集与空列表。"""
        await self.video(bvid)
        comments = self._data.comments.get(bvid)
        if comments is None:
            raise SourceError("unavailable", "清单没有采集这条稿件的评论。")
        if root_rpid is not None and not any(
            item.rpid == root_rpid and item.root_rpid is None for item in comments
        ):
            raise SourceError("not_found", "未找到该稿件下指定的根评论。")
        matches = [item for item in comments if item.root_rpid == root_rpid]
        if root_rpid is None:
            replies = Counter(item.root_rpid for item in comments if item.root_rpid is not None)
            # 清单可能只保留一部分回复，原始总数存在时优先保留。
            matches = [
                item.model_copy(update={"reply_count": replies[item.rpid]})
                if item.reply_count is None
                else item
                for item in matches
            ]
        if order == "hot":
            matches.sort(key=lambda item: (not item.pinned, -item.likes, -item.published_at))
        else:
            matches.sort(key=lambda item: (not item.pinned, -item.published_at))
        return _page(matches, cursor, limit)

    async def transcript(
        self, bvid: str, part: VideoPart, start: float, end: float
    ) -> Transcript | None:
        """用 BV 和 CID 取本地轨道，插件负责统一的区间筛选。"""
        material = self._data.materials.get(bvid, {}).get(str(part.cid))
        return material.transcript if material is not None else None

    async def open_video(self, bvid: str, part: VideoPart) -> BinaryIO | None:
        """在清单目录内打开对应媒体，确保路径解析仍在数据集内。"""
        material = self._data.materials.get(bvid, {}).get(str(part.cid))
        if material is None or material.file is None:
            return None
        return await asyncio.to_thread(self._open, material.file)

    def _open(self, file: str) -> BinaryIO:
        """只在宿主配置提供的样本目录内读取普通文件。"""
        try:
            path = (self._root / file).resolve()
            if not path.is_relative_to(self._root) or not path.is_file():
                raise SourceError("media_unavailable", "清单媒体必须是目录内的可用文件。")
            return path.open("rb")
        except OSError as error:
            raise SourceError("media_unavailable", str(error)) from error

    async def close(self) -> None:
        """清单只有内存索引，每次打开的流归读取方管理。"""
