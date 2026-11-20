"""验证 B 站网页适配器的接口形状、失败分类及观影媒体交付。"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import httpx
import pytest
from bilibili import web_source
from bilibili.source import SourceError, TranscriptLine
from bilibili.web_source import WebSettings, WebSource
from PIL import Image
from purrcept_core.models import (
    ImageBlock,
    ImageBytes,
    Message,
    ModelResponse,
    TextBlock,
    ToolResultBlock,
)
from test_bilibili import BVID, OTHER, encode_demo_video
from test_host_subject import Backend, proposal, wait_processed

from purrcept_engine import EngineConfig, EngineHost, InputSource, InputSubmission
from purrcept_engine.values import JsonObject, JsonValue

_COOKIE = "SESSDATA=synthetic"
_VIDEO: JsonObject = {
    "bvid": BVID,
    "aid": 121,
    "title": "分 P 测试",
    "owner": {"name": "样本作者"},
    "pages": [
        {"page": 1, "cid": 221, "part": "第一段", "duration": 6},
        {"page": 2, "cid": 222, "part": "第二段", "duration": 6},
    ],
}


def _response(data: JsonValue, *, code: int = 0) -> httpx.Response:
    return httpx.Response(200, json={"code": code, "data": data})


def _nav() -> httpx.Response:
    """用匿名响应的公开 WBI 键覆盖无 Cookie 的签名流程。"""
    return _response(
        {
            "wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/" + "1" * 32 + ".png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "2" * 32 + ".png",
            }
        },
        code=-101,
    )


def _reply(rpid: int, *, root: int = 0) -> JsonObject:
    return {
        "rpid_str": str(rpid),
        "root": root,
        "member": {"uname": "样本用户"},
        "content": {"message": "评论正文"},
        "rcount": 2,
    }


def _transport(
    monkeypatch: pytest.MonkeyPatch,
    transport: httpx.MockTransport,
) -> None:
    """只给本用例的固定接口返回，不允许真实网络流量。"""

    def route(client: httpx.AsyncClient, url: httpx.URL) -> httpx.MockTransport:
        return transport

    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", route)


async def test_web_reads_home_search_comments_and_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    """校验推荐分页、评论游标、站点排序和楼中楼参数。"""
    seen: list[str] = []
    root = 9007199254740993
    offset = '{"type":1,"direction":1,"data":{"pn":2}}'

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.host == "api.bilibili.com"
        path, query = request.url.path, request.url.params
        seen.append(path)
        if path == "/x/web-interface/nav":
            return _nav()
        if path == "/x/web-interface/view":
            return _response(_VIDEO)
        if path == "/x/web-interface/wbi/index/top/feed/rcmd":
            assert len(query["w_rid"]) == 32
            identifier = BVID if query["fresh_idx"] == "1" else OTHER
            return _response(
                {
                    "item": [
                        {"goto": "live", "title": "直播"},
                        {
                            "goto": "av",
                            "bvid": identifier,
                            "title": "推荐视频",
                            "owner": {"name": "UP"},
                            "duration": 41,
                        },
                    ]
                }
            )
        if path == "/x/web-interface/wbi/search/type":
            assert query["search_type"] == "video" and query["keyword"] == "摄影"
            return _response(
                {
                    "result": [
                        {
                            "bvid": BVID,
                            "title": '<em class="keyword">摄影</em> &amp; 取景',
                            "author": "UP",
                            "duration": "1:23",
                        },
                        {"bvid": OTHER, "title": "第二项"},
                    ],
                    "numPages": 1,
                }
            )
        if path == "/x/v2/reply/wbi/main":
            assert query["oid"] == "121"
            requested = json.loads(query["pagination_str"])["offset"]
            if query["mode"] == "2":
                return _response({"replies": [_reply(5)], "cursor": {"is_end": True}})
            assert query["mode"] == "3"
            if requested == offset:
                return _response({"replies": [_reply(3)], "cursor": {"is_end": True}})
            assert requested == ""
            return _response(
                {
                    "replies": [_reply(root), _reply(2)],
                    "top_replies": [_reply(root)],
                    "cursor": {"pagination_reply": {"next_offset": offset}},
                }
            )
        assert path == "/x/v2/reply/reply" and query["root"] == str(root)
        return _response(
            {
                "replies": [_reply(11, root=root), _reply(12, root=root)],
                "page": {"num": 1, "size": 20, "count": 2},
            }
        )

    _transport(monkeypatch, httpx.MockTransport(handle))
    source = WebSource(WebSettings())
    try:
        home = await source.feed("recommend", None, None, 1)
        assert [item.bvid for item in home.items] == [BVID] and home.next_cursor
        following = await source.feed("recommend", None, home.next_cursor, 1)
        assert [item.bvid for item in following.items] == [OTHER]
        search = await source.search("摄影", None, 1)
        assert search.items[0].title == "摄影 & 取景" and search.items[0].duration == 83
        search_two = await source.search("摄影", search.next_cursor, 1)
        assert search_two.items[0].bvid == OTHER
        assert seen.count("/x/web-interface/wbi/search/type") == 1
        with pytest.raises(SourceError, match="游标无效"):
            await source.search("其他关键词", search.next_cursor, 1)

        first = await source.comments(BVID, None, "hot", None, 1)
        assert first.items[0].rpid == str(root) and first.items[0].pinned
        second = await source.comments(BVID, None, "hot", first.next_cursor, 1)
        assert second.items[0].rpid == "2"
        third = await source.comments(BVID, None, "hot", second.next_cursor, 1)
        assert third.items[0].rpid == "3" and third.next_cursor is None
        newest = await source.comments(BVID, None, "new", None, 1)
        assert newest.items[0].rpid == "5"
        thread = await source.comments(BVID, str(root), "hot", None, 2)
        assert [(item.rpid, item.root_rpid) for item in thread.items] == [
            ("11", str(root)),
            ("12", str(root)),
        ]
        assert seen.count("/x/web-interface/nav") == 1
    finally:
        await source.close()


async def test_web_risk_response_is_explicit_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """平台 412 不会被解释为空推荐，也不发起备用请求。"""
    seen: list[httpx.URL] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(412, text="<html>request blocked</html>")

    _transport(monkeypatch, httpx.MockTransport(handle))
    source = WebSource(WebSettings())
    try:
        with pytest.raises(SourceError, match="HTTP 412") as raised:
            await source.feed("recommend", None, None, 2)
        assert raised.value.code == "blocked" and len(seen) == 1
    finally:
        await source.close()


async def test_web_transcribes_audio_when_no_subtitle_is_visible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """登录所限的字幕用本地转写补齐，同一 CID 和窗口贯穿音轨请求。"""
    model = tmp_path / "example.bin"
    model.touch()

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/x/web-interface/view":
            return _response(_VIDEO)
        if path == "/x/web-interface/nav":
            return _nav()
        if path == "/x/player/wbi/v2":
            assert request.url.params["cid"] == "222"
            return _response({"need_login_subtitle": True})
        if path == "/x/player/playurl":
            assert request.url.params["cid"] == "222"
            return _response(
                {
                    "dash": {
                        "audio": [
                            {
                                "baseUrl": "https://untrusted.example/second-part.m4s",
                                "backupUrl": ["//sample.bilivideo.com/second-part.m4s"],
                                "bandwidth": 32000,
                            },
                        ]
                    }
                }
            )
        assert request.url.host == "sample.bilivideo.com" and path == "/second-part.m4s"
        assert request.headers["Referer"].endswith("?p=2")
        return httpx.Response(200, content=b"selected audio")

    async def asr(
        audio: BinaryIO,
        model_path: Path,
        start: float,
        end: float,
    ) -> tuple[TranscriptLine, ...]:
        with audio:
            assert audio.read() == b"selected audio"
        assert model_path == model and (start, end) == (2, 4)
        return (TranscriptLine(start=2.25, end=3.5, text="本机识别"),)

    _transport(monkeypatch, httpx.MockTransport(handle))
    monkeypatch.setattr(web_source, "transcribe", asr)
    source = WebSource(WebSettings(asr_model=model.name), base_dir=tmp_path)
    try:
        video = await source.video(BVID)
        track = await source.transcript(BVID, video.parts[1], 2, 4)
        assert track is not None and track.source == "asr" and track.cid == 222
        assert track.lines == (TranscriptLine(start=2.25, end=3.5, text="本机识别"),)
    finally:
        await source.close()


async def test_web_selected_part_reaches_model_with_subtitles_and_pixels(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """通过正式 WebSource 和 Engine Worker 交付 P2 的台词与解码后图像。"""
    media = tmp_path / "part2.mp4"
    encode_demo_video(media, (210, 30, 20))
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if request.url.host == "api.bilibili.com":
            assert request.headers["Cookie"] == _COOKIE
        else:
            assert "Cookie" not in request.headers
        if path == "/x/web-interface/view":
            assert request.url.params["bvid"] == BVID
            return _response(_VIDEO)
        if path == "/x/web-interface/nav":
            return _nav()
        if path == "/x/player/wbi/v2":
            assert request.url.params["cid"] == "222"
            return _response(
                {
                    "bvid": BVID,
                    "cid": 222,
                    "subtitle": {
                        "subtitles": [
                            {"lan": "zh-CN", "subtitle_url": "//aisubtitle.hdslb.com/part2.json"}
                        ]
                    },
                }
            )
        if path == "/part2.json":
            return httpx.Response(
                200,
                json={
                    "body": [
                        {"from": 0, "to": 2, "content": "窗外台词"},
                        {"from": 2, "to": 4, "content": "这是第二段的台词"},
                    ]
                },
            )
        if path == "/x/player/playurl":
            assert request.url.params["cid"] == "222"
            return _response(
                {
                    "cid": 222,
                    "dash": {
                        "video": [
                            {
                                "baseUrl": "https://sample.bilivideo.com.untrusted.example/part2.mp4",
                                "backup_url": [
                                    "http://sample.bilivideo.com/part2.mp4",
                                    "https://sample.bilivideo.com/part2.mp4",
                                ],
                                "codecs": "avc1",
                            }
                        ]
                    },
                }
            )
        assert str(request.url) == "https://sample.bilivideo.com/part2.mp4"
        assert request.headers["Referer"].endswith("?p=2")
        return httpx.Response(200, content=media.read_bytes())

    _transport(monkeypatch, httpx.MockTransport(handle))
    backend = Backend(
        (
            proposal(
                "bili-live-part",
                "play_action",
                {
                    "action_code": "from world import me\n"
                    f'return await me.phone.bilibili.watch(video="{BVID}", page=2, start=2, end=4)'
                },
            ),
            ModelResponse(Message.assistant("完成。")),
        )
    )
    config = EngineConfig.validate(
        {
            "host": {"data_dir": tmp_path / "engine"},
            "console": False,
            "models": {
                "main": {
                    "model": "openai/offline",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "supports_contents": ["text", "image"],
                    "context_window": 100_000,
                }
            },
            "subject": {"foreground": "main"},
            "plugins": [
                {
                    "instance_id": "bili",
                    "entry": "bilibili:factory",
                    "config": {
                        "adapter": "web",
                        "adapter_config": {"cookie": _COOKIE},
                    },
                }
            ],
        },
        base_dir=tmp_path,
    )
    host = EngineHost(config, generation_backends={"main": backend})
    await host.start()
    try:
        now = datetime.now(UTC)
        admission = await host.submit(
            InputSubmission(
                InputSource("test", "actor", "chat", "text", "watch"),
                "看第二段",
                now,
                now,
            )
        )
        await wait_processed(host, admission.input_id)
        tool = next(
            block
            for message in backend.requests[1].messages
            for block in message.content
            if isinstance(block, ToolResultBlock)
        )
        receipt = next(block.text for block in tool.content if isinstance(block, TextBlock))
        assert "这是第二段的台词" in receipt and "窗外台词" not in receipt
        outcome = json.loads(receipt)["calls"][0]["result"]
        assert outcome["part"]["cid"] == 222 and outcome["window"]["start"] == 2
        assert outcome["transcript"]["lines"] == [
            {"start": 2, "end": 4, "text": "这是第二段的台词"}
        ]
        picture = next(block.source for block in tool.content if isinstance(block, ImageBlock))
        assert isinstance(picture, ImageBytes) and picture.media_type == "image/jpeg"
        with Image.open(io.BytesIO(picture.data)) as image:
            pixel = image.getpixel((100, 90))
            assert isinstance(pixel, tuple) and pixel[0] > 190 and pixel[1] < 55
        assert seen[-2:] == ["/x/player/playurl", "/part2.mp4"]
    finally:
        await host.close()
