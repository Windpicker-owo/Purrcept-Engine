"""独立原生OVRLipSync进程，只接受有界S16采样，不打开声音/VTS/网络。

一个进程拥有一个SDK初始化与上下文；stdin EOF先销毁上下文再Shutdown。
标准输出只传JSON事实，原生/协议错误返回明确error并终止，不把失败
回退成音量口型。宿主使用固定20ms块与实际播放器时钟完成视觉采用。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes as ct
import sys
from pathlib import Path
from typing import Protocol, cast

from purrcept_engine.values import JsonObject, decode_object, encode_json


class _Function(Protocol):
    """已经按OVR公开C ABI登记的函数指针，整数状态码须在边界校验。"""

    argtypes: list[object]
    restype: object

    def __call__(self, *arguments: object) -> object:
        """按已绑定的 C ABI 执行一次原生文件操作。

        Args:
            arguments: 按已装配参数模型校验的能力关键字参数。

        Note:
            传入C ABI对象并交回待核验原生值。
        """
        ...


def _function(library: ct.CDLL, name: str, arguments: list[object], result: object) -> _Function:
    """显式C ABI是此cast依据，不把未登记的动态DLL返回值扩散进实现。"""
    function = cast(_Function, getattr(library, name))
    function.argtypes, function.restype = arguments, result
    return function


def _check(result: object) -> None:
    if not isinstance(result, int) or isinstance(result, bool) or result != 0:
        raise RuntimeError("OVRLipSync原生调用失败。")


class OVRLipSync:
    """仅本进程创建一次的OVR上下文，Original provider沿旧C ABI。"""

    def __init__(self, library_path: Path, sample_rate: int) -> None:
        """初始化失败不泄漏进程级SDK状态，DLL必须是明确存在的路径。

        Args:
            library_path: 分析子进程中允许加载的明确动态库路径。
            sample_rate: PCM 的明确采样率（Hz）。
        """
        library = ct.CDLL(str(library_path.resolve(strict=True)))
        self._library = library
        self._rate, self._context = sample_rate, ct.c_uint()
        self._closed = False
        initialize = _function(library, "ovrLipSyncDll_Initialize", [ct.c_int, ct.c_int], ct.c_int)
        create = _function(
            library,
            "ovrLipSyncDll_CreateContextEx",
            [ct.POINTER(ct.c_uint), ct.c_int, ct.c_int, ct.c_int],
            ct.c_int,
        )
        self._process = _function(
            library,
            "ovrLipSyncDll_ProcessFrameEx",
            [
                ct.c_uint,
                ct.c_void_p,
                ct.c_uint,
                ct.c_int,
                ct.POINTER(ct.c_int),
                ct.POINTER(ct.c_int),
                ct.POINTER(ct.c_float),
                ct.c_int,
                ct.POINTER(ct.c_float),
                ct.c_void_p,
                ct.c_int,
            ],
            ct.c_int,
        )
        self._reset = _function(library, "ovrLipSyncDll_ResetContext", [ct.c_uint], ct.c_int)
        self._destroy = _function(library, "ovrLipSyncDll_DestroyContext", [ct.c_uint], ct.c_int)
        self._shutdown = _function(library, "ovrLipSyncDll_Shutdown", [], None)
        _check(initialize(sample_rate, sample_rate // 50))
        try:
            _check(create(ct.byref(self._context), 0, sample_rate, 0))
        except BaseException:
            self._shutdown()
            raise

    def reset(self) -> None:
        """新媒体识别前清除原生循环状态。"""
        _check(self._reset(self._context))

    def analyze(self, pcm: bytes, sample: int) -> JsonObject:
        """按SDK frameDelay毫秒修正样本位置，权重限幅沿固定十五项。

        Args:
            pcm: 已解码的 PCM 字节或按播放顺序产生的字节流。
            sample: 当前输入的原始采样值。

        Returns:
            业务对象，按实际结果提供 sample、weights 字段。

        Raises:
            ValueError: OVR采样块无效。
        """
        if len(pcm) != self._rate // 50 * 2 or sample < 0:
            raise ValueError("OVR采样块无效。")
        audio = ct.create_string_buffer(pcm)
        weights = (ct.c_float * 15)()
        frame, delay, laughter = ct.c_int(), ct.c_int(), ct.c_float()
        _check(
            self._process(
                self._context,
                audio,
                len(pcm) // 2,
                0,
                ct.byref(frame),
                ct.byref(delay),
                weights,
                15,
                ct.byref(laughter),
                None,
                0,
            )
        )
        return {
            "sample": max(0, sample - round(delay.value * self._rate / 1000)),
            "weights": [max(0.0, min(1.0, float(weight))) for weight in weights],
        }

    def close(self) -> None:
        """上下文先销毁再全局关闭，失败仍保证Shutdown执行一次。"""
        if not self._closed:
            self._closed = True
            try:
                _check(self._destroy(self._context))
            finally:
                self._shutdown()


def main() -> None:
    """只消费stdin有限帧。

    Raises:
        ValueError: 参数值或组合不满足本入口的明确约束，错误正文指出具体字段。

    Note:
        原生崩溃由宿主进程EOF识别为视觉故障。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--library", required=True)
    parser.add_argument("--rate", type=int, required=True)
    arguments = parser.parse_args()
    source: OVRLipSync | None = None
    try:
        source = OVRLipSync(Path(arguments.library), arguments.rate)
        _reply({"ready": True})
        while raw := sys.stdin.buffer.readline(16385):
            if len(raw) > 16384:
                raise ValueError("OVR请求帧超过16 KiB。")
            request = decode_object(raw)
            operation = request.get("operation")
            if operation == "reset":
                source.reset()
                _reply({"reset": True})
            elif operation == "analyze":
                encoded, sample = request.get("pcm"), request.get("sample")
                if (
                    not isinstance(encoded, str)
                    or not isinstance(sample, int)
                    or isinstance(sample, bool)
                ):
                    raise ValueError("OVR请求字段无效。")
                _reply(source.analyze(base64.b64decode(encoded, validate=True), sample))
            else:
                raise ValueError("OVR请求操作未登记。")
    except (OSError, RuntimeError, ValueError, binascii.Error) as error:
        _reply({"error_type": type(error).__name__, "error": str(error)})
        raise
    finally:
        if source is not None:
            source.close()


def _reply(value: JsonObject) -> None:
    sys.stdout.buffer.write(encode_json(value) + b"\n")
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
