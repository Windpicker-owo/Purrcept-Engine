"""经完整 Host/Store/Worker 链路播放本地语音，演示语境判断的接入。

注意与主脑均使用显式脚本后端，不调用在线模型；这是接线验收，不是 JEV
判断质量验收。只装配声音和 Live2D，数据写入独立目录。实际播放器/VTS
可由离线测试替换；生产所有者的提交、动作执行和媒体交接不被绕过。
以 python -m examples.live2d_dialogue_preview 运行，停止文件或总期限负责收尾。
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

from purrcept_core.models import (
    ChoiceAnswer,
    ChoiceProbability,
    ChoiceQuestion,
    DecisionRequest,
    DecisionResponse,
    Message,
    MessageRole,
    ModelEventSink,
    ModelRequest,
    ModelResponse,
    ToolCallBlock,
)

from examples.live2d_speech_preview import load_pcm
from purrcept_engine import EngineConfig, EngineHost, InputSource, InputSubmission
from purrcept_engine.plugin_system import PluginFactory
from purrcept_engine.plugins.audio.models import RecordParameters, SpeechAudio, SpeechParameters
from purrcept_engine.plugins.audio.player import AudioPlayer, FFplayPlayer
from purrcept_engine.plugins.audio.plugin import make_factory as audio_factory
from purrcept_engine.plugins.live2d.plugin import make_factory as body_factory
from purrcept_engine.plugins.live2d.vts import VTSDriver

# 每轮指定低频判断与说话方式。音频复用本地样本，不声称台词与这些文字匹配。
SCENES = (
    ("阅读聊天：友好解释", "warm", "chat", "explain"),
    ("查看工作：好奇疑问", "curious", "work", "question"),
    ("倾听反馈：关切回应", "concerned", "speaker", "neutral"),
    ("关注游戏：兴奋强调", "excited", "game", "emphasize"),
)


class PreviewAttention:
    """每轮只回答实际提交的三个问题，置信值为明确的测试输入。"""

    def __init__(self) -> None:
        """创建单次有界演示的计数，不保存历史请求正文。"""
        self.rounds = 0

    async def decide(self, request: DecisionRequest, *, model: str) -> DecisionResponse:
        """逐轮给出情绪和注视语义，额外请求或缺失问题均使验收失败。"""
        if self.rounds >= len(SCENES):
            raise RuntimeError("演示收到超出场景数量的注意请求。")
        _, emotion, focus, _ = SCENES[self.rounds]
        self.rounds += 1
        choices = {"attention": "wake", "emotion": emotion, "focus": focus}
        assert tuple(q.name for q in request.questions) == tuple(choices)
        answers: list[ChoiceAnswer] = []
        for question in request.questions:
            assert isinstance(question, ChoiceQuestion)
            choice = choices[question.name]
            answers.append(
                ChoiceAnswer(
                    question.name,
                    choice,
                    tuple(
                        ChoiceProbability(option.value, float(option.value == choice))
                        for option in question.choices
                    ),
                    0.9,
                )
            )
        return DecisionResponse(tuple(answers))


class PreviewBrain:
    """通过真实 play_action/Worker 提交 speak，不直接调用身体或音频运行时。"""

    def __init__(self) -> None:
        """每轮恰好一个行动提案和一次工具结果后的内部收尾。"""
        self.calls = 0

    async def generate(
        self, request: ModelRequest, *, model: str, emit: ModelEventSink | None = None
    ) -> ModelResponse:
        """依场景产生正式行动；连续会话与回执仍由 Host/Core 维护。"""
        index, finishing = divmod(self.calls, 2)
        self.calls += 1
        if finishing:
            return ModelResponse(Message.assistant("本轮本地演示结束。"))
        if index >= len(SCENES):
            raise RuntimeError("演示收到超出场景数量的前台请求。")
        delivery = SCENES[index][3]
        code = (
            'from world import me\nreturn await me.microphone.speak(text="本地测试语音", '
            f'body={{"delivery":"{delivery}","effort":0.15}})'
        )
        return ModelResponse(
            Message(
                MessageRole.ASSISTANT,
                (
                    ToolCallBlock(
                        f"preview-{index}", "play_action", arguments={"action_code": code}
                    ),
                ),
            )
        )


class LocalSpeech:
    """只提供预先解码的本地测试音频，替代付费 TTS，不采集真人动作。"""

    def __init__(self, pcm: bytes) -> None:
        """借用最长三十秒的 mono s16le 32 kHz PCM，调用间不复制整段。"""
        self._pcm = pcm
        self.plays = 0

    async def synthesize(
        self, parameters: SpeechParameters | RecordParameters, identity: str
    ) -> SpeechAudio:
        """演示只允许流式播放，误入文件合成路径必须明确失败。"""
        raise RuntimeError("本地演示仅支持流式播放。")

    async def stream(
        self, parameters: SpeechParameters, identity: str
    ) -> AsyncGenerator[bytes, None]:
        """交给正式音频所有者，再由其分发 PCM、开始和实际媒体进度。"""
        self.plays += 1
        for offset in range(0, len(self._pcm), 1280):
            yield self._pcm[offset : offset + 1280]

    async def close(self) -> None:
        """内存素材没有外部借用，真实播放器由宿主独立关闭。"""


def build_host(
    profile: Path,
    token: Path,
    data_dir: Path,
    source: LocalSpeech,
    player: AudioPlayer,
    body: PluginFactory,
) -> tuple[EngineHost, PreviewAttention, PreviewBrain]:
    """构造隔离宿主，所有模型资源均显式注入脚本后端。

    Args:
        profile: 当前 VTS 模型的校准文件。
        token: 本机令牌路径，不输出内容。
        data_dir: 本次独立 Store/Worker 目录，不使用日常会话数据。
        source: 已解码的本地音频源。
        player: 实际播放器或离线协议替身。
        body: 实际 VTS 或离线传输工厂，其余生命周期不替换。

    Returns:
        未启动的宿主和可核验请求次数的两个脚本后端。
    """
    config = EngineConfig.validate(
        {
            "host": {"data_dir": data_dir.resolve()},
            "console": False,
            "models": {
                "main": {
                    "kind": "generation",
                    "model": "openai/offline",
                    "api_key": "synthetic",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "context_window": 100000,
                    "max_output_tokens": 1000,
                },
                "attention": {
                    "kind": "decision",
                    "model": "offline",
                    "api_key": "synthetic",
                    "api_base": "http://127.0.0.1:9999/v1",
                    "input_limit": 100000,
                },
            },
            "subject": {
                "foreground": "main",
                "reflection": "main",
                "attention": "attention",
                "attention_batch_seconds": 0.0,
            },
            "plugins": [
                {
                    "instance_id": "audio",
                    "entry": "local-audio",
                    "config": {"playback_backend": "streaming"},
                },
                {
                    "instance_id": "body",
                    "entry": "local-body",
                    "config": {
                        "profile_path": str(profile.resolve()),
                        "token_path": str(token.resolve()),
                    },
                },
            ],
        },
        base_dir=data_dir.resolve(),
    )
    attention, brain = PreviewAttention(), PreviewBrain()
    host = EngineHost(
        config,
        plugin_factories={
            "local-audio": audio_factory(lambda _: source, lambda _: player, decode=False),
            "local-body": body,
        },
        generation_backends={"main": brain},
        decision_backends={"attention": attention},
    )
    return host, attention, brain


async def run_round(host: EngineHost, index: int) -> None:
    """接管一条真实输入并等待结案，最长四十五秒，异常保留在本次独立库。

    Args:
        host: 已启动且仅包含本地演示插件的宿主。
        index: SCENES 内的本次场景索引，不并发提交多轮。
    """
    now = datetime.now(UTC)
    admission = await host.submit(
        InputSubmission(
            InputSource("preview", "local", "dialogue", "text", str(index)),
            SCENES[index][0],
            now,
            now,
        )
    )
    async with asyncio.timeout(45):
        while True:
            record = await host.query_processing(admission.input_id)
            if record is not None and record.stage == "failed":
                raise RuntimeError("本地演示输入处理失败：" + record.reason)
            if record is not None and record.stage == "processed":
                return
            await asyncio.sleep(0.05)


async def preview(args: argparse.Namespace) -> None:
    """有限运行四轮完整对话，停止文件和总期限均会归还 Host 持有的设备。"""
    if not 0 < args.seconds <= 300:
        raise ValueError("演示期限须在0到300秒之间。")
    pcm = await load_pcm(args.audio, args.ffmpeg)
    player = FFplayPlayer(args.ffplay, sample_rate=32000)
    source = LocalSpeech(pcm)
    connected = asyncio.Event()

    class PreviewVTS(VTSDriver):
        """首帧真实确认后才允许演示输入进入注意判断。"""

        async def send(self, channels: dict[str, float]) -> None:
            """保留生产传输和协议验证，只通知演示观察者连接已可用。"""
            await super().send(channels)
            connected.set()

    host, attention, brain = build_host(
        args.profile,
        args.token,
        args.data_dir,
        source,
        player,
        body_factory(lambda settings, token, profile: PreviewVTS(settings.vts_url, token, profile)),
    )
    await host.start()
    try:
        await asyncio.wait_for(connected.wait(), 10)

        async def scenes() -> None:
            for index, scene in enumerate(SCENES):
                print(f"阶段{index + 1}：{scene[0]}；模型判断为脚本，本地语音复用。", flush=True)
                await run_round(host, index)
                await asyncio.sleep(3)
            print(
                f"完整链路结束：注意{attention.rounds}轮，主脑{brain.calls}次，播放{source.plays}次。",
                flush=True,
            )

        async def stop() -> None:
            """有界检查本地停止文件；期限到达属于正常停止，不伪报任务故障。"""
            for _ in range(int(args.seconds * 10)):
                if await asyncio.to_thread(args.stop_file.exists):
                    return
                await asyncio.sleep(0.1)

        async with asyncio.TaskGroup() as group:
            run = group.create_task(scenes())
            watch = group.create_task(stop())
            await asyncio.wait((run, watch), return_when=asyncio.FIRST_COMPLETED)
            if not run.done():
                run.cancel()
            else:
                watch.cancel()
    finally:
        await host.close()
        print("隔离宿主、声音和 VTS 连接已关闭。", flush=True)


def main() -> None:
    """只接受明确本地文件，不读取日常主配置或装配消息插件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("profile", "token", "audio", "stop-file", "data-dir"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffplay", default="ffplay")
    asyncio.run(preview(parser.parse_args()))


if __name__ == "__main__":
    main()
