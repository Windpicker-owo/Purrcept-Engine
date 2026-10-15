"""单会话 VAD 与停顿分段，持续人声独立于起音和最终文字接管。

Segmenter 不拥有任务或权威输入。前置缓存有界，连续发言最多 60 秒；
超过界限或丢样须中止并报告缺口，不能提交截断片段。
"""

from __future__ import annotations

import math
import struct
from collections import deque
from importlib import import_module
from pathlib import Path
from typing import Literal, Protocol, cast

from purrcept_engine.plugins.voice_input.models import VoiceInputError, VoiceInputSettings

type SegmentEvent = tuple[Literal["begin", "pcm", "speech_started", "end", "discard"], bytes]


class _Numpy(Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    float32: object
    int64: object

    def zeros(self, shape: tuple[int, ...], *, dtype: object) -> object:
        """分配指定形状和元素格式的全零推理数组。

        Args:
            shape: 待分配推理数组的明确维度。
            dtype: 原生采样数组的元素格式。
        """
        ...

    def array(self, values: object, *, dtype: object) -> object:
        """将原始值转换为指定元素格式的推理数组。

        Args:
            values: 待校验、编码或构造数组的原始值集合。
            dtype: 原生采样数组的元素格式。
        """
        ...


class _Array(Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def tolist(self) -> object:
        """将当前原生数组转换为可校验的 Python 值。"""
        ...


class _SessionOptions(Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    intra_op_num_threads: int
    inter_op_num_threads: int


class _InferenceSession(Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def run(self, names: None, inputs: dict[str, object]) -> list[object]:
        """用给定输入张量执行一次 ONNX 推理，返回声明的全部输出。

        Args:
            names: 传 None 请求 ONNX 会话返回全部声明输出。
            inputs: 按固定 ONNX 输入名绑定的采样与状态张量。

        Returns:
            ONNX 会话按输出声明顺序返回的原生张量。
        """
        ...


class _OnnxRuntime(Protocol):
    """限定原生库在此适配层使用的调用形状。

    Note:
        具体资源由外层驱动创建和关闭；该协议不负责加载库或扩大原生 API 范围。
    """

    def SessionOptions(self) -> _SessionOptions:
        """创建当前检测实例独立使用的 ONNX 会话选项。

        Returns:
            当前检测实例独立持有的 ONNX 会话选项。
        """
        ...

    def InferenceSession(
        self, path: str, options: _SessionOptions, *, providers: list[str]
    ) -> _InferenceSession:
        """按固定模型文件和明确 Provider 创建本次推理会话。

        Args:
            path: 此操作定位的明确路径。
            options: 本实例 ONNX 会话的线程与执行选项。
            providers: 显式允许的 ONNX 执行 Provider 列表。

        Returns:
            仅使用明确模型路径和 Provider 的推理会话。
        """
        ...


class SileroVad:
    """运行时按收音会话创建的 ONNX 循环状态；不在声卡回调中执行推理。"""

    def __init__(self, path: Path) -> None:
        """显式模型文件缺失直接失败。

        Args:
            path: 此操作定位的明确路径。

        Raises:
            VoiceInputError: Silero VAD 模型不存在，请配置本机 ONNX 文件。

        Note:
            原生库仅在开始收音时加载。
        """
        if not path.is_file():
            raise VoiceInputError("Silero VAD 模型不存在，请配置本机 ONNX 文件。")
        # 原生可选库的有限接口由这里适配；object 表示 ONNX 张量而非通用服务。
        self.numpy = cast(_Numpy, import_module("numpy"))
        ort = cast(_OnnxRuntime, import_module("onnxruntime"))
        options = ort.SessionOptions()
        options.intra_op_num_threads = options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        self.state = self.numpy.zeros((2, 1, 128), dtype=self.numpy.float32)
        self.context: list[float] = [0.0] * 64

    def probability(self, pcm: bytes) -> float:
        """保留官方模型的 64 样本上下文及循环状态，拒绝无效概率输出。

        Args:
            pcm: 已解码的 PCM 字节或按播放顺序产生的字节流。

        Raises:
            VoiceInputError: 设备、捕获格式、VAD/ASR 协议或有界帧队列不满足收音要求。
        """
        if len(pcm) != 1024:
            raise VoiceInputError("VAD 需要恰好 512 个 mono S16 样本。")
        samples = [sample / 32768 for sample in struct.unpack("<512h", pcm)]
        wave = self.numpy.array([self.context + samples], dtype=self.numpy.float32)
        outputs = self.session.run(
            None,
            {
                "input": wave,
                "state": self.state,
                "sr": self.numpy.array(16000, dtype=self.numpy.int64),
            },
        )
        if len(outputs) != 2:
            raise VoiceInputError("VAD 模型输出形状不匹配。")
        values = cast(_Array, outputs[0]).tolist()
        if not isinstance(values, list) or not values or not isinstance(values[0], list):
            raise VoiceInputError("VAD 模型概率形状不匹配。")
        row = cast(list[object], values[0])
        if len(row) != 1 or not isinstance(row[0], (float, int)):
            raise VoiceInputError("VAD 模型概率无效。")
        probability = float(row[0])
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise VoiceInputError("VAD 模型概率超出范围。")
        self.state, self.context = outputs[1], samples[-64:]
        return probability


class Segmenter:
    """runtime 顺序调用的单会话状态机，事件顺序就是发言缓存交接顺序。"""

    def __init__(self, settings: VoiceInputSettings) -> None:
        """用毫秒门槛建立有界前置缓存，不保存整场收音。

        Args:
            settings: 当前组件已校验的配置或纯声明。
        """
        self.settings = settings
        self.pre: deque[bytes] = deque(maxlen=(settings.pre_roll_ms + 31) // 32)
        self.active = False
        self.rise = self.speech = self.silence = self.frames = 0
        self.interrupt_speech = 0
        self.interrupted = False

    def _confirm_speech(self) -> list[SegmentEvent]:
        """每次发言仅一次确认；短间隙可保持窗口，但间隙不增加人声时长。"""
        if not self.interrupted and self.interrupt_speech >= self.settings.interrupt_ms:
            self.interrupted = True
            return [("speech_started", b"")]
        return []

    def feed(self, pcm: bytes, probability: float) -> list[SegmentEvent]:
        """起音后一次性交前置缓存，正常停顿才结束有效发言。

        Args:
            pcm: 已解码的 PCM 字节或按播放顺序产生的字节流。
            probability: 当前 VAD 帧的语音概率。

        Returns:
            当前 VAD 帧触发的发言开始、PCM 与结束事件，保持实际顺序。

        Raises:
            VoiceInputError: 收音帧或 VAD 概率不连续；连续发言超过 60 秒，当前未完成录音已释放，
                请暂停后重新开始。
        """
        if len(pcm) != 1024 or not math.isfinite(probability) or not 0 <= probability <= 1:
            raise VoiceInputError("收音帧或 VAD 概率不连续。")
        voiced = probability >= self.settings.speech_threshold
        if not self.active:
            self.pre.append(pcm)
            self.rise = self.rise + 32 if voiced else 0
            if self.rise < self.settings.start_ms:
                return []
            self.active = True
            self.speech, self.silence = self.rise, 0
            self.interrupt_speech, self.interrupted = self.rise, False
            self.frames = len(self.pre)
            prefix = b"".join(self.pre)
            self.pre.clear()
            return [("begin", b""), ("pcm", prefix), *self._confirm_speech()]

        self.frames += 1
        if self.frames * 512 > 16000 * 60:
            raise VoiceInputError("连续发言超过 60 秒，当前未完成录音已释放，请暂停后重新开始。")
        self.speech += 32 if voiced else 0
        self.silence = 0 if voiced else self.silence + 32
        if voiced:
            self.interrupt_speech += 32
        elif self.silence > self.settings.interrupt_gap_ms:
            self.interrupt_speech = 0
        result: list[SegmentEvent] = [("pcm", pcm), *self._confirm_speech()]
        if self.silence >= self.settings.end_ms:
            result.append(("end" if self.speech >= self.settings.min_speech_ms else "discard", b""))
            self.active = False
            self.rise = self.speech = self.silence = self.frames = 0
        return result
