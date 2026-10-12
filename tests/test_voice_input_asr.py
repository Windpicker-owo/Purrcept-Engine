"""用有限 WebSocket 替身验证生产 Fun-ASR 编码、最终确认及取消资源。

连接替身保留实际 run-task/PCM/finish-task 顺序，所有供应商帧经真实解析。
不会连接服务商，不读取凭据环境，也不请求真实识别模型。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

import pytest
from pydantic import SecretStr

from purrcept_engine.execution.protocol import JsonObject, decode_object, encode_json
from purrcept_engine.plugins.voice_input.asr import ASRSocket, FunASRTranscriber
from purrcept_engine.plugins.voice_input.models import VoiceInputError, VoiceInputSettings


class Socket:
    """受控供应商任务，发送结束确认后才发最终句子与任务完成。"""

    def __init__(self, *, wrong_identity: bool = False, failed: bool = False) -> None:
        self.identity = ""
        self.received: list[str | bytes] = []
        self.responses: asyncio.Queue[str] = asyncio.Queue(maxsize=8)
        self.closed = False
        self.wrong_identity, self.failed = wrong_identity, failed

    def event(self, kind: str, payload: JsonObject | None = None) -> None:
        """序列化供应商原帧，不跳过协议校验。"""
        value: JsonObject = {
            "header": {"event": kind, "task_id": "wrong" if self.wrong_identity else self.identity},
            "payload": payload or {},
        }
        self.responses.put_nowait(encode_json(value).decode("utf-8"))

    async def send(self, data: str | bytes) -> None:
        """按实际控制帧决定回复，不从测试直接伪造 TranscriptUpdate。"""
        self.received.append(data)
        if isinstance(data, str):
            message = decode_object(data.encode("utf-8"))
            header = message["header"]
            assert isinstance(header, dict)
            identity = header["task_id"]
            assert isinstance(identity, str)
            self.identity = identity
            if header["action"] == "run-task":
                self.event("task-started")
            else:
                assert header["action"] == "finish-task"
                if self.failed:
                    self.event("task-failed")
                else:
                    self.event(
                        "result-generated",
                        {
                            "output": {
                                "sentence": {
                                    "sentence_id": 1,
                                    "text": "最终文字",
                                    "sentence_end": True,
                                }
                            }
                        },
                    )
                    self.event("task-finished")

    async def recv(self) -> str | bytes:
        """保留真实等待，取消必须由连接上下文收回。"""
        return await self.responses.get()


def transcriber(socket: Socket) -> FunASRTranscriber:
    """用明确替身替换唯一连接点，其余供应商协议走生产代码。"""

    @asynccontextmanager
    async def connector(settings: VoiceInputSettings) -> AsyncGenerator[ASRSocket]:
        assert settings.asr_api_key.get_secret_value() == "test"
        try:
            yield socket
        finally:
            socket.closed = True

    return FunASRTranscriber(VoiceInputSettings(asr_api_key=SecretStr("test")), connector)


async def frames() -> AsyncIterator[bytes]:
    """生成两段完整 PCM。"""
    yield b"\x01\0" * 512
    yield b"\x02\0" * 512


async def test_production_asr_final_confirmation_and_exact_binary_order() -> None:
    socket = Socket()
    updates = [update async for update in transcriber(socket).transcribe(frames())]
    assert len(updates) == 1 and updates[0].final and updates[0].text == "最终文字"
    assert socket.received[1:3] == [b"\x01\0" * 512, b"\x02\0" * 512]
    initial = socket.received[0]
    assert isinstance(initial, str)
    payload = decode_object(initial.encode())["payload"]
    assert isinstance(payload, dict)
    assert payload["parameters"] == {
        "format": "pcm",
        "sample_rate": 16000,
        "semantic_punctuation_enabled": False,
        "max_sentence_silence": 500,
    }
    assert "test" not in initial and socket.closed


@pytest.mark.parametrize("wrong,failed", [(True, False), (False, True)])
async def test_provider_task_mismatch_or_failure_does_not_yield_final(
    wrong: bool, failed: bool
) -> None:
    socket = Socket(wrong_identity=wrong, failed=failed)
    with pytest.raises(VoiceInputError):
        _ = [update async for update in transcriber(socket).transcribe(frames())]
    assert socket.closed


async def test_cancellation_closes_socket_and_sender_without_finish_task() -> None:
    socket = Socket()
    entered = asyncio.Event()

    async def pending() -> AsyncIterator[bytes]:
        entered.set()
        await asyncio.Event().wait()
        yield b"\0" * 1024

    async def run() -> None:
        async for _ in transcriber(socket).transcribe(pending()):
            pass

    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert socket.closed and len(socket.received) == 1
