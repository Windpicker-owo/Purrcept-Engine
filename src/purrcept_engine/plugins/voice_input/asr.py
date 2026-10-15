"""Fun-ASR 显式 WebSocket 传输，单发言单任务，不重试或提交临时文本。

供应商 task-started、句子修订与 task-finished 均校验任务身份。发送与接收
并行但由本次迭代器收回；网络失败按原发言报告，不用新身份重发音频。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect

from purrcept_engine.plugins.voice_input.models import (
    TranscriptUpdate,
    VoiceInputError,
    VoiceInputSettings,
)
from purrcept_engine.values import JsonObject, decode_object, encode_json


class ASRSocket(Protocol):
    """识别适配器借用的有限帧传输，测试可替换而不连接付费端点。"""

    async def send(self, data: str | bytes) -> None:
        """有序发送控制帧或原始 PCM。

        Args:
            data: 当前边界接收的原始协议或媒体内容。
        """
        ...

    async def recv(self) -> str | bytes:
        """接收供应商原始帧，协议校验由转写器负责。

        Returns:
            当前原生连接实际交付的文本或二进制消息。
        """
        ...


type ASRConnect = Callable[[VoiceInputSettings], AbstractAsyncContextManager[ASRSocket]]


class _ConnectedSocket:
    """将 websockets 的通用分片接口收敛到 ASR 单帧传输契约。"""

    def __init__(self, connection: ClientConnection) -> None:
        """借用当前 ASR 原生连接，不改变外层连接资源所有权。

        Args:
            connection: 当前所有者实际取得的协议连接。
        """
        self.connection = connection

    async def send(self, data: str | bytes) -> None:
        """按输入顺序发送当前 ASR 会话的控制消息与 PCM 帧。

        Args:
            data: 当前边界接收的原始协议或媒体内容。
        """
        await self.connection.send(data)

    async def recv(self) -> str | bytes:
        """等待当前原生连接交付下一条文本或二进制消息。

        Returns:
            当前原生连接实际交付的文本或二进制消息。
        """
        return await self.connection.recv()


@asynccontextmanager
async def connect_asr(settings: VoiceInputSettings) -> AsyncGenerator[ASRSocket]:
    """生产连接只采用显式凭据及端点，不读取 SDK 环境默认值。

    Args:
        settings: 当前组件已校验的配置或纯声明。

    Yields:
        本次连接实际取得的 ASR 套接字，退出上下文后释放。
    """
    headers = {"Authorization": "Bearer " + settings.asr_api_key.get_secret_value()}
    if settings.workspace_id:
        headers["X-DashScope-WorkSpace"] = settings.workspace_id
    async with connect(
        settings.asr_url,
        additional_headers=headers,
        open_timeout=settings.asr_timeout_seconds,
        max_size=1024 * 1024,
        max_queue=8,
        proxy=None,
    ) as connection:
        yield _ConnectedSocket(connection)


def _object(parent: JsonObject, name: str) -> JsonObject:
    value = parent.get(name)
    if not isinstance(value, dict):
        raise VoiceInputError("ASR 协议缺少对象字段：" + name + "。")
    return value


class FunASRTranscriber:
    """runtime 创建的可复用适配器，每次 transcribe 私有网络与任务身份。"""

    def __init__(self, settings: VoiceInputSettings, connector: ASRConnect = connect_asr) -> None:
        """注入有限传输接缝，构造不启动网络。

        Args:
            settings: 当前组件已校验的配置或纯声明。
            connector: 创建实际协议连接的显式工厂。
        """
        self.settings, self.connector = settings, connector

    async def transcribe(self, frames: AsyncIterator[bytes]) -> AsyncIterator[TranscriptUpdate]:
        """EOF 发送 finish-task，供应商最终确认后才允许正常退出。

        Args:
            frames: 当前捕获块的帧数或顺序产生的 PCM 帧流。

        Yields:
            按真实接收顺序产生的转写更新，保留临时与最终文本区别。

        Raises:
            VoiceInputError: 设备、捕获格式、VAD/ASR 协议或有界帧队列不满足收音要求。
        """
        settings = self.settings
        if not settings.asr_api_key.get_secret_value():
            raise VoiceInputError("ASR 未配置显式密钥。")
        identity = uuid4().hex
        header: JsonObject = {"action": "run-task", "task_id": identity, "streaming": "duplex"}
        payload: JsonObject = {
            "task_group": "audio",
            "task": "asr",
            "function": "recognition",
            "model": settings.asr_model,
            "parameters": {
                "format": "pcm",
                "sample_rate": 16000,
                "semantic_punctuation_enabled": False,
                "max_sentence_silence": settings.end_ms,
            },
            "input": {},
        }
        async with self.connector(settings) as socket:
            await socket.send(encode_json({"header": header, "payload": payload}).decode("utf-8"))
            first = await asyncio.wait_for(socket.recv(), settings.asr_timeout_seconds)
            message = decode_object(first.encode("utf-8") if isinstance(first, str) else first)
            started = _object(message, "header")
            if started.get("event") != "task-started" or started.get("task_id") != identity:
                raise VoiceInputError("ASR 未确认当前识别任务开始。")

            async def send() -> None:
                async for pcm in frames:
                    if not pcm or len(pcm) % 2:
                        raise VoiceInputError("ASR 收到不完整 PCM。")
                    await socket.send(pcm)
                await socket.send(
                    encode_json(
                        {
                            "header": {
                                "action": "finish-task",
                                "task_id": identity,
                                "streaming": "duplex",
                            },
                            "payload": {"input": {}},
                        }
                    ).decode("utf-8")
                )

            sender = asyncio.create_task(send(), name="voice-asr-send")
            try:
                async with asyncio.timeout(60 + settings.asr_timeout_seconds * 2):
                    while True:
                        receiver = asyncio.create_task(socket.recv(), name="voice-asr-receive")
                        try:
                            if not sender.done():
                                await asyncio.wait(
                                    (sender, receiver), return_when=asyncio.FIRST_COMPLETED
                                )
                            if sender.done():
                                sender.result()
                                raw = await asyncio.wait_for(receiver, settings.asr_timeout_seconds)
                            else:
                                raw = await receiver
                        finally:
                            if not receiver.done():
                                receiver.cancel()
                            await asyncio.gather(receiver, return_exceptions=True)
                        event = decode_object(raw.encode("utf-8") if isinstance(raw, str) else raw)
                        event_header = _object(event, "header")
                        if event_header.get("task_id") != identity:
                            raise VoiceInputError("ASR 返回了其他任务的结果。")
                        kind = event_header.get("event")
                        if kind == "task-failed":
                            raise VoiceInputError("ASR 供应商确认识别失败。")
                        if kind == "task-finished":
                            await sender
                            return
                        if kind == "result-generated":
                            sentence = _object(
                                _object(_object(event, "payload"), "output"), "sentence"
                            )
                            text, final = sentence.get("text"), sentence.get("sentence_end")
                            sentence_id = sentence.get("sentence_id", sentence.get("begin_time"))
                            if (
                                not isinstance(text, str)
                                or type(final) is not bool
                                or isinstance(sentence_id, bool)
                                or not isinstance(sentence_id, (str, int))
                            ):
                                raise VoiceInputError("ASR 句子字段无效。")
                            yield TranscriptUpdate(str(sentence_id), text, final)
                        elif kind != "task-started":
                            raise VoiceInputError("ASR 返回了未识别的任务事件。")
            finally:
                if not sender.done():
                    sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
