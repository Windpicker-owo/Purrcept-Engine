"""把一个分 P 的实际视频流解码成带时间标注的分镜图。

调用方提供已经选择好的流和半开时间区间。这里从前一个关键帧解码，按实际
显示时间取样，并关闭收到的流。返回时戳与 JPEG；稿件身份和媒体入库由插件处理。
"""

from __future__ import annotations

import io
import time
from math import ceil
from typing import BinaryIO

import av
from av.error import FFmpegError
from PIL import Image, ImageDraw

from .source import SourceError


def storyboard(
    media: BinaryIO, start: float, end: float, cells: int, tile_width: int, budget: float
) -> tuple[bytes, tuple[float, ...]]:
    """在有限窗口内均匀取样，保留选中帧的真实分 P 时间。"""
    deadline = time.monotonic() + budget
    columns = min(cells, 3)
    tile_height = tile_width * 9 // 16
    label_height = 24
    row_height = tile_height + label_height
    timestamps: list[float] = []

    try:
        with (
            media,
            av.open(media, mode="r", options={"protocol_whitelist": ""}) as container,
            Image.new("RGB", (columns * tile_width, ceil(cells / columns) * row_height)) as sheet,
        ):
            if not container.streams.video:
                raise SourceError("media_unavailable", "该分 P 的媒体没有视频轨。")
            stream = container.streams.video[0]
            time_base = stream.time_base
            if time_base is None:
                raise SourceError("media_unavailable", "该分 P 的视频轨缺少时间基准。")
            origin = (stream.start_time or 0) * time_base
            if start > 0:
                container.seek(int((start + origin) / time_base), stream=stream)

            # 解码可能从窗口之前开始。先以原始 PTS 过滤，再缩放入图；
            # 即便两个采样点落在同一帧，也只保留一次真实画面。
            step = (end - start) / cells
            sample = 0
            draw = ImageDraw.Draw(sheet)
            for frame in container.decode(stream):
                if time.monotonic() >= deadline:
                    raise SourceError("media_timeout", "视频解码超过本次观影的处理期限。")
                if frame.pts is None or frame.time_base is None:
                    continue
                timestamp = float(frame.pts * frame.time_base - origin)
                if timestamp >= end:
                    break
                if timestamp < start + sample * step:
                    continue
                scale = min(tile_width / frame.width, tile_height / frame.height)
                scaled = frame.reformat(
                    width=max(1, round(frame.width * scale)),
                    height=max(1, round(frame.height * scale)),
                    format="rgb24",
                )
                position = len(timestamps)
                left = position % columns * tile_width
                top = position // columns * row_height
                plane = scaled.planes[0]
                with Image.frombytes(
                    "RGB",
                    (scaled.width, scaled.height),
                    bytes(plane),
                    "raw",
                    "RGB",
                    plane.line_size,
                    1,
                ) as image:
                    sheet.paste(
                        image,
                        (
                            left + (tile_width - image.width) // 2,
                            top + (tile_height - image.height) // 2,
                        ),
                    )
                draw.text(
                    (left + 6, top + tile_height + 4),
                    f"#{position + 1}  {timestamp:.3f}s",
                    fill="white",
                )
                timestamps.append(timestamp)
                while sample < cells and start + sample * step <= timestamp:
                    sample += 1
                if sample == cells:
                    break
            if not timestamps:
                raise SourceError("no_frames", "该时间范围内没有取得可定位的画面帧。")
            with (
                sheet.crop(
                    (0, 0, columns * tile_width, ceil(len(timestamps) / columns) * row_height)
                ) as filled,
                io.BytesIO() as output,
            ):
                filled.save(output, format="JPEG", quality=85)
                return output.getvalue(), tuple(timestamps)
    except (FFmpegError, OSError) as error:
        raise SourceError("media_unavailable", f"视频解码失败：{error}") from error
