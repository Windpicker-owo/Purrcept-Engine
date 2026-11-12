"""冻结超时当刻的安全进度，区分网络等待与宿主事件循环长时间停顿。

使用真实 HTTPX 响应流和有限本机阻塞替身，不访问远端服务、设备或模型。
验证业务回执与结构化日志都能定位同一调用，而非只留下笼统的超时类型。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import cast

import httpx
import pytest
from pydantic import SecretStr
from test_audio import install_transport, metadata

from purrcept_engine.plugin_system import AudioError
from purrcept_engine.plugins.audio.models import AudioSettings, RecordParameters, SpeechParameters
from purrcept_engine.plugins.audio.tts import FishSource


@pytest.mark.parametrize("block_loop", [False, True])
async def test_partial_response_timeout_keeps_network_and_loop_progress(
    block_loop: bool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """首块后等待和本机阻塞留下不同的循环延迟，响应均在超时后归还。"""
    caplog.set_level(logging.INFO, logger="purrcept_engine.plugins.audio.tts")
    closed = asyncio.Event()

    def stall() -> None:
        # 故意在同步回调中阻塞一次，覆盖未轮到一秒探针就先处理超时的顺序。
        time.sleep(1.15)

    class PartialBody(httpx.AsyncByteStream):
        def __init__(self, trace: Callable[[str, Mapping[str, object]], Awaitable[None]]) -> None:
            self.trace = trace

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"abc"
            if block_loop:
                asyncio.get_running_loop().call_soon(stall)
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            closed.set()
            await self.trace("http11.response_closed.complete", {})

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return metadata(request)
        trace = cast(
            Callable[[str, Mapping[str, object]], Awaitable[None]], request.extensions["trace"]
        )
        await trace("http11.receive_response_body.started", {"headers": "secret-provider-data"})
        return httpx.Response(
            200, headers={"Content-Type": "audio/mpeg"}, stream=PartialBody(trace)
        )

    transport = httpx.MockTransport(respond)
    install_transport(monkeypatch, transport)
    source = FishSource(
        AudioSettings(
            api_key=SecretStr("secret-key"), model="model", voice_id="voice", timeout_seconds=0.05
        ),
        transport=transport,
    )
    try:
        with pytest.raises(AudioError, match=r"文件合成.*TimeoutError") as failure:
            await source.synthesize(RecordParameters(text="secret-text"), "diagnostic-call")
        assert "已收到响应头" in str(failure.value)
        assert "已接收 3 字节" in str(failure.value)
        assert "http11.receive_response_body.started" in str(failure.value)
        assert "secret" not in str(failure.value)
        assert closed.is_set()
        records = [r.__dict__ for r in caplog.records if r.__dict__.get("outcome") == "failed"]
        assert len(records) == 1
        record = records[0]
        assert record["call_id"] == "diagnostic-call"
        assert record["stage"] == "文件合成"
        assert record["status_code"] == 200
        assert record["body_bytes"] == 3
        assert isinstance(record["first_byte_ms"], float)
        assert isinstance(record["event_loop_lag_ms"], float)
        assert (record["event_loop_lag_ms"] > 100) == block_loop
        assert "secret" not in caplog.text
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    finally:
        await source.close()


async def test_connect_timeout_does_not_claim_headers_or_body_received(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TLS 阶段失败可与上游生成等待区分，不回显底层异常中的秘密。"""
    caplog.set_level(logging.INFO, logger="purrcept_engine.plugins.audio.tts")

    async def respond(request: httpx.Request) -> httpx.Response:
        trace = cast(
            Callable[[str, Mapping[str, object]], Awaitable[None]], request.extensions["trace"]
        )
        await trace("connection.start_tls.started", {"secret": "secret-credential"})
        raise httpx.ConnectTimeout("secret-upstream-error", request=request)

    transport = httpx.MockTransport(respond)
    install_transport(monkeypatch, transport)
    source = FishSource(AudioSettings(api_key=SecretStr("secret-key")), transport=transport)
    try:
        with pytest.raises(AudioError, match=r"能力目录查询.*ConnectTimeout") as failure:
            await source.synthesize(RecordParameters(text="secret-text"), "tls-call")
        assert "未收到响应头" in str(failure.value)
        assert "connection.start_tls.started" in str(failure.value)
        assert "secret" not in str(failure.value)
        assert "secret" not in caplog.text
    finally:
        await source.close()


async def test_stopping_stream_is_cancelled_without_a_false_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """下游关闭生成器时仍归还流，不把主动停音记成传输失败。"""
    caplog.set_level(logging.INFO, logger="purrcept_engine.plugins.audio.tts")
    closed = asyncio.Event()

    class StreamBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'event: audio\ndata: {"audioBase64":"YWJj"}\n\n'
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            closed.set()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return metadata(request)
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=StreamBody()
        )

    transport = httpx.MockTransport(respond)
    install_transport(monkeypatch, transport)
    source = FishSource(
        AudioSettings(api_key=SecretStr("test"), model="model", voice_id="voice"),
        transport=transport,
    )
    try:
        stream = source.stream(SpeechParameters(text="测试"), "cancel-stream")
        assert await anext(stream) == b"abc"
        await stream.aclose()
        assert closed.is_set()
        records = [r.__dict__ for r in caplog.records if r.__dict__.get("stage") == "流式合成"]
        assert [r["outcome"] for r in records] == ["started", "cancelled"]
    finally:
        await source.close()


async def test_shared_client_keeps_concurrent_request_diagnostics_separate(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """复用一个长期客户端时，并发 record 的身份和响应长度不能互相覆盖。"""
    caplog.set_level(logging.INFO, logger="purrcept_engine.plugins.audio.tts")

    class Body(httpx.AsyncByteStream):
        def __init__(self, data: bytes) -> None:
            self.data = data

        async def __aiter__(self) -> AsyncIterator[bytes]:
            await asyncio.sleep(0)
            yield self.data

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return metadata(request)
        data = b"abc" if request.headers["x-request-id"] == "first" else b"defgh"
        return httpx.Response(200, headers={"Content-Type": "audio/mpeg"}, stream=Body(data))

    transport = httpx.MockTransport(respond)
    install_transport(monkeypatch, transport)
    source = FishSource(
        AudioSettings(api_key=SecretStr("test"), model="model", voice_id="voice"),
        transport=transport,
    )
    try:
        results = await asyncio.gather(
            source.synthesize(RecordParameters(text="一"), "first"),
            source.synthesize(RecordParameters(text="二"), "second"),
        )
        assert [r.data for r in results] == [b"abc", b"defgh"]
        records = [
            r.__dict__
            for r in caplog.records
            if r.__dict__.get("stage") == "文件合成" and r.__dict__.get("outcome") == "completed"
        ]
        assert {r["call_id"]: r["body_bytes"] for r in records} == {"first": 3, "second": 5}
        assert len({r["transport_id"] for r in records}) == 1
    finally:
        await source.close()
