"""用本地清单和真实视频验证 B 站插件的浏览、分 P 和模型材料交付。"""

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import av
import pytest
from bilibili.source import BilibiliSource, ManifestSource
from PIL import Image
from purrcept_core.models import (
    ImageBlock,
    ImageBytes,
    Message,
    ModelResponse,
    TextBlock,
    ToolResultBlock,
)
from test_host_subject import Backend, proposal, wait_processed

from purrcept_engine import EngineConfig, EngineHost, InputSource, InputSubmission
from purrcept_engine.values import JsonObject, decode_object, encode_json

BVID = "BV1xx411c7m9"
OTHER = "BV1xx411c7m8"


def encode_demo_video(path: Path, color: tuple[int, int, int]) -> None:
    """用画面颜色区分分 P 和时间，能识别串 P 或总从头抽帧的回归。"""
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("mpeg4", rate=2)  # pyright: ignore[reportUnknownMemberType]
        stream.width, stream.height, stream.pix_fmt = 64, 36, "yuv420p"
        for frame_number in range(12):
            frame = av.VideoFrame(64, 36, "rgb24")
            plane = frame.planes[0]
            rgb = bytes((0, 128, 200) if frame_number < 4 else color)
            row = rgb * frame.width
            plane.update((row + bytes(plane.line_size - len(row))) * frame.height)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def _write_manifest(tmp_path: Path) -> None:
    """准备时间轴独立的两分 P、推荐和评论样本。"""
    encode_demo_video(tmp_path / "p1.mp4", (20, 190, 30))
    encode_demo_video(tmp_path / "p2.mp4", (210, 30, 20))
    document: JsonObject = {
        "videos": [
            {
                "bvid": BVID,
                "title": "相机入门",
                "uploader": "摄影样本",
                "parts": [
                    {"page": 1, "cid": 11, "title": "讲解", "duration": 6},
                    {"page": 2, "cid": 22, "title": "演示", "duration": 6},
                ],
            },
            {
                "bvid": OTHER,
                "title": "镜头笔记",
                "uploader": "摄影样本",
                "parts": [{"page": 1, "cid": 33, "title": "笔记", "duration": 6}],
            },
        ],
        "recommend": [BVID, BVID, OTHER],
        "related": {BVID: [OTHER]},
        "comments": {
            BVID: [
                {"rpid": "1", "author": "小甲", "text": "热评", "likes": 12, "published_at": 1},
                {"rpid": "2", "author": "小乙", "text": "新评", "likes": 2, "published_at": 2},
                {"rpid": "3", "author": "小丙", "text": "补充", "root_rpid": "1"},
                {"rpid": "4", "author": "小丁", "text": "再补充", "root_rpid": "1"},
            ],
        },
        "materials": {
            BVID: {
                "11": {"file": "p1.mp4"},
                "22": {
                    "file": "p2.mp4",
                    "transcript": {
                        "cid": 22,
                        "source": "asr",
                        "lines": [
                            {"start": 0, "end": 1, "text": "窗口前的台词"},
                            {"start": 2, "end": 3, "text": "现在按下红色按钮"},
                            {"start": 4, "end": 5, "text": "窗口后的台词"},
                        ],
                    },
                },
            },
            OTHER: {"33": {"file": "missing.mp4"}},
        },
    }
    (tmp_path / "manifest.json").write_bytes(encode_json(document))


async def adapter_factory(base_dir: Path, config: JsonObject) -> BilibiliSource:
    """从单独的模块入口装配，验证真实配置能切换数据源工厂。"""
    assert config == {"offline": True}
    return ManifestSource(base_dir / "manifest.json")


def _config(tmp_path: Path, *, custom: bool = False) -> EngineConfig:
    """装配正常的 CLI/Host 插件别名并启用模型原生图像。"""
    adapter = (
        {"adapter": "test_bilibili:adapter_factory", "adapter_config": {"offline": True}}
        if custom
        else {"adapter_config": {"path": "manifest.json"}}
    )
    return EngineConfig.validate(
        {
            "host": {"data_dir": tmp_path / "engine"},
            "console": False,
            "models": {
                "main": {
                    "model": "openai/offline",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "api_key": "synthetic",
                    "supports_contents": ["text", "image"],
                    "context_window": 100_000,
                    "max_output_tokens": 1000,
                }
            },
            "subject": {"foreground": "main", "reflection": "main"},
            "plugins": [{"instance_id": "bili", "entry": "bilibili:factory", "config": adapter}],
        },
        base_dir=tmp_path,
    )


async def test_read_only_catalog_and_paging_through_worker(tmp_path: Path) -> None:
    """用实际 Worker 按推荐游标翻页，并区分热评、新评及楼中楼。"""
    _write_manifest(tmp_path)
    host = EngineHost(_config(tmp_path, custom=True), generation_backends={"main": Backend(())})
    await host.start()
    try:
        assert {
            path for path in host.catalog.bindings if path.startswith("me.phone.bilibili.")
        } == {
            "me.phone.bilibili.feed",
            "me.phone.bilibili.search",
            "me.phone.bilibili.watch",
            "me.phone.bilibili.inspect",
            "me.phone.bilibili.comments",
        }
        result = await host.execute(
            f"""from world import me
first = await me.phone.bilibili.feed(limit=1)
second = await me.phone.bilibili.feed(cursor=first["next_cursor"], limit=1)
related = await me.phone.bilibili.feed(kind="related", video="{BVID}")
search = await me.phone.bilibili.search(query="相机 摄影")
hot = await me.phone.bilibili.comments(video="{BVID}", limit=1)
newest = await me.phone.bilibili.comments(video="{BVID}", order="new", limit=1)
replies = await me.phone.bilibili.comments(video="{BVID}", root_rpid="1", limit=1)
more = await me.phone.bilibili.comments(
    video="{BVID}", root_rpid="1", cursor=replies["next_cursor"]
)
return [first, second, related, search, hot, newest, replies, more]"""
        )
        assert result.state == "completed", result.error
        first, second, related, search, hot, newest, replies, more = json.loads(
            result.result_json or "null"
        )
        assert first["kind"] == "recommend" and first["items"][0]["bvid"] == BVID
        assert second["items"][0]["bvid"] == OTHER and second["next_cursor"] is None
        assert related["kind"] == "related" and related["bvid"] == BVID
        assert [item["bvid"] for item in search["items"]] == [BVID]
        assert hot["items"][0]["rpid"] == "1"
        assert hot["items"][0]["reply_count"] == 2
        assert newest["items"][0]["rpid"] == "2"
        assert replies["items"][0]["rpid"] == "3" and replies["next_cursor"] is not None
        assert more["items"][0]["rpid"] == "4" and more["next_cursor"] is None

        missing = await host.execute(
            'from world import me\nawait me.phone.bilibili.feed(kind="hot")'
        )
        assert missing.calls[0].state == "failed"
        assert missing.calls[0].error_type == "unavailable"
    finally:
        await host.close()


async def test_requested_part_transcript_and_real_frames_reach_one_model_result(
    tmp_path: Path,
) -> None:
    """从分享 URL 的分 P 与时间定位，在同一原生工具块中交付 JPEG 和转写。"""
    _write_manifest(tmp_path)
    link = f"https://www.bilibili.com/video/{BVID}/?p=2&t=2"
    backend = Backend(
        (
            proposal(
                "bili-watch",
                "play_action",
                {
                    "action_code": "from world import me\n"
                    f'return await me.phone.bilibili.watch(video="{link}", end=4)'
                },
            ),
            ModelResponse(Message.assistant("内部完成。")),
        )
    )
    host = EngineHost(_config(tmp_path), generation_backends={"main": backend})
    await host.start()
    try:
        now = datetime.now(UTC)
        admission = await host.submit(
            InputSubmission(
                InputSource("test", "actor", "chat", "text", "watch"),
                "看看演示里发生了什么",
                now,
                now,
            )
        )
        await wait_processed(host, admission.input_id)
        assert len(backend.requests) == 2
        tool = next(
            block
            for message in backend.requests[1].messages
            for block in message.content
            if isinstance(block, ToolResultBlock)
        )
        text = next(block.text for block in tool.content if isinstance(block, TextBlock))
        assert "现在按下红色按钮" in text
        assert "窗口前的台词" not in text and "窗口后的台词" not in text
        receipt = decode_object(text.encode("utf-8"))
        calls = receipt["calls"]
        assert isinstance(calls, list) and isinstance(calls[0], dict)
        value = calls[0]["result"]
        assert isinstance(value, dict)
        assert value["part"] == {"page": 2, "cid": 22, "title": "演示", "duration": 6.0}
        assert value["transcript"] == {
            "state": "available",
            "source": "asr",
            "cid": 22,
            "lines": [{"start": 2.0, "end": 3.0, "text": "现在按下红色按钮"}],
        }
        assert isinstance(value["storyboard"], dict)
        assert value["storyboard"]["frames"] == [
            {"cell": index, "seconds": seconds} for index, seconds in enumerate((2, 2.5, 3, 3.5), 1)
        ]

        picture = next(block.source for block in tool.content if isinstance(block, ImageBlock))
        assert isinstance(picture, ImageBytes) and picture.media_type == "image/jpeg"
        with Image.open(io.BytesIO(picture.data)) as image:
            pixel = image.getpixel((100, 90))
            assert isinstance(pixel, tuple) and pixel[0] > 190 and pixel[1] < 55

        closeup = await host.execute(
            "from world import me\n"
            f'return await me.phone.bilibili.inspect(video="{BVID}", page=1, start=2, end=3)'
        )
        assert closeup.calls[0].state == "confirmed" and closeup.calls[0].media
        outcome = decode_object((closeup.calls[0].result_json or "{}").encode("utf-8"))
        assert outcome["transcript"] == {
            "state": "unavailable",
            "reason": "数据源未提供字幕或转写。",
        }

        missing = await host.execute(
            f'from world import me\nawait me.phone.bilibili.watch(video="{OTHER}")'
        )
        assert missing.calls[0].state == "failed" and not missing.calls[0].media
        assert missing.calls[0].error_type == "media_unavailable"
    finally:
        await host.close()


@pytest.mark.parametrize("target", ["https://example.com/video/" + BVID, "C:/test.mp4"])
async def test_watch_rejects_other_urls_and_model_local_paths(tmp_path: Path, target: str) -> None:
    """模型输入只选择清单里的稿件，无法交给播放器任意路径或链接。"""
    _write_manifest(tmp_path)
    host = EngineHost(_config(tmp_path), generation_backends={"main": Backend(())})
    await host.start()
    try:
        result = await host.execute(
            f'from world import me\nawait me.phone.bilibili.watch(video="{target}")'
        )
        assert result.calls[0].error_type == "CapabilityInputError" and not result.calls[0].media
    finally:
        await host.close()
