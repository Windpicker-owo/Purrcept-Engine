"""只在受限子进程内执行行动，并以有界 JSON 代理调用冻结能力。

直接脚本入口先安装标准库可加载的包命名空间，不执行 Engine 的应用装配。
启动握手核验来源、目录分块与身份；收到 execute 后再次验证 AST，再创建唯一
接收线程并运行异步行动。能力对象只包含路径，真实执行和回执始终属于宿主。

stdout/stderr 共用字节预算。协议错误立即终止进程，不能被行动捕获后继续派发；
MemoryError 在异常处理之前触发固定退出码，避免脚本吞掉内存违规。父进程负责
Job、在途真实结果及强退后的清理，本模块不持有设备、SDK、凭据或数据库。
"""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import io
import itertools
import json
import math
import msvcrt
import os
import re
import statistics
import sys
import threading
from collections.abc import Coroutine, Iterator, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import CodeType, ModuleType
from typing import Protocol, TypeGuard, cast


def _install_package_namespace() -> None:
    """脚本启动仅暴露已安装代码路径，不执行父包 __init__ 的 SDK 导入。"""
    package_directory = Path(__file__).resolve().parent.parent
    package = ModuleType("purrcept_engine")
    package.__dict__["__path__"] = [str(package_directory)]
    execution = ModuleType("purrcept_engine.execution")
    execution.__dict__["__path__"] = [str(package_directory / "execution")]
    sys.modules["purrcept_engine"] = package
    sys.modules["purrcept_engine.execution"] = execution


try:
    if __name__ == "__main__":
        _install_package_namespace()
    from purrcept_engine.execution.protocol import (
        ActionProtocolError,
        FrameChannel,
        JsonObject,
        JsonValue,
        decode_object,
        encode_json,
    )
    from purrcept_engine.execution.validation import validate_source
except ImportError as error:
    # 包内标准库模块缺失属于安装产物错误，不加载 SDK 或另一实现代替。
    raise RuntimeError("Worker 所需的包内协议或校验模块不可用。") from error

MEMORY_EXIT_CODE = 71
PROTOCOL_EXIT_CODE = 72


class RemoteCapabilityError(Exception):
    """宿主返回的能力失败，只携带错误名称与文本，不提供宿主异常对象。"""

    def __init__(self, error_type: str, message: str) -> None:
        """保留模型可在脚本中判断的远端类别，真实回执仍由宿主保存。"""
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type


class WorkerLimitError(RuntimeError):
    """行动的调用、输出或结果超过宿主额度，没有额外的自动截断或重试。"""


@dataclass(frozen=True, slots=True)
class _Limits:
    """宿主明确授予的单次额度；Worker 不能用脚本参数增大这些值。"""

    max_calls: int
    source_bytes: int
    output_bytes: int
    result_bytes: int
    call_arguments_bytes: int
    call_result_bytes: int


@dataclass(frozen=True, slots=True)
class _Bootstrap:
    """第一帧的行动身份、源码和完整目录验证材料。"""

    action_id: str
    catalog_version: str
    source: str
    directory: str
    limits: _Limits
    binding_bytes: int
    binding_digest: str
    binding_chunks: int


@dataclass(frozen=True, slots=True)
class _Binding:
    """代理只知道冻结路径与目标身份，不持有真实 Provider。"""

    path: tuple[str, ...]
    capability_id: str
    target_id: str


def _fields(message: JsonObject, expected: set[str]) -> None:
    if set(message) != expected:
        raise ActionProtocolError("行动消息字段缺失或包含未声明字段。")


def _text(message: JsonObject, field: str) -> str:
    value = message.get(field)
    if not isinstance(value, str) or not value:
        raise ActionProtocolError(f"{field} 必须是非空字符串。")
    return value


def _positive(message: JsonObject, field: str) -> int:
    value = message.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ActionProtocolError(f"{field} 必须是正整数。")
    return value


def _identity(message: JsonObject, kind: str, action_id: str) -> None:
    if message.get("kind") != kind or message.get("action_id") != action_id:
        raise ActionProtocolError("消息种类或行动身份与当前阶段不符。")


def _bootstrap(message: JsonObject) -> _Bootstrap:
    """严格核验启动形状，未知额度或身份不能被默认值掩盖。"""
    _fields(
        message,
        {
            "kind",
            "action_id",
            "catalog_version",
            "source",
            "directory",
            "limits",
            "binding_bytes",
            "binding_digest",
            "binding_chunks",
        },
    )
    if message.get("kind") != "bootstrap":
        raise ActionProtocolError("首帧必须是 bootstrap。")
    raw_limits = message.get("limits")
    if not isinstance(raw_limits, dict):
        raise ActionProtocolError("启动额度必须是 JSON 对象。")
    _fields(
        raw_limits,
        {
            "max_calls",
            "source_bytes",
            "output_bytes",
            "result_bytes",
            "call_arguments_bytes",
            "call_result_bytes",
        },
    )
    limits = _Limits(
        _positive(raw_limits, "max_calls"),
        _positive(raw_limits, "source_bytes"),
        _positive(raw_limits, "output_bytes"),
        _positive(raw_limits, "result_bytes"),
        _positive(raw_limits, "call_arguments_bytes"),
        _positive(raw_limits, "call_result_bytes"),
    )
    source = message.get("source")
    if not isinstance(source, str):
        raise ActionProtocolError("行动源码必须是字符串。")
    if len(source.encode("utf-8")) > limits.source_bytes:
        raise WorkerLimitError("行动源码超过宿主字节额度。")
    directory = _text(message, "directory")
    if not os.path.isabs(directory):
        raise ActionProtocolError("Worker 工作目录必须是宿主持有的绝对路径。")
    binding_bytes = _positive(message, "binding_bytes")
    binding_chunks = _positive(message, "binding_chunks")
    if binding_chunks > binding_bytes:
        raise ActionProtocolError("非空目录分块数不能超过声明字节数。")
    digest = _text(message, "binding_digest")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ActionProtocolError("目录摘要必须是完整的 SHA-256 十六进制值。")
    return _Bootstrap(
        _text(message, "action_id"),
        _text(message, "catalog_version"),
        source,
        directory,
        limits,
        binding_bytes,
        digest,
        binding_chunks,
    )


def _read_bindings(channel: FrameChannel, bootstrap: _Bootstrap) -> tuple[_Binding, ...]:
    """每块先核验身份与总量，全部到齐且摘要一致后才采用冻结目录。"""
    chunks: list[bytes] = []
    received_bytes = 0
    digest = hashlib.sha256()
    for index in range(bootstrap.binding_chunks):
        message = channel.read()
        _fields(message, {"kind", "action_id", "index", "data"})
        _identity(message, "binding_chunk", bootstrap.action_id)
        sequence = message.get("index")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence != index:
            raise ActionProtocolError("目录分块序号必须连续且不能重复。")
        data = _text(message, "data").encode("utf-8")
        received_bytes += len(data)
        if received_bytes > bootstrap.binding_bytes:
            raise ActionProtocolError("目录分块超过声明的完整字节数。")
        digest.update(data)
        chunks.append(data)
    if received_bytes != bootstrap.binding_bytes or digest.hexdigest() != bootstrap.binding_digest:
        raise ActionProtocolError("目录分块不完整或摘要不一致。")

    catalog = decode_object(b"".join(chunks))
    _fields(catalog, {"bindings"})
    raw_bindings = catalog.get("bindings")
    if not isinstance(raw_bindings, list):
        raise ActionProtocolError("目录 bindings 必须是数组。")
    paths: set[tuple[str, ...]] = set()
    bindings: list[_Binding] = []
    for item in raw_bindings:
        if not isinstance(item, dict):
            raise ActionProtocolError("能力绑定必须是 JSON 对象。")
        _fields(item, {"path", "capability_id", "target_id"})
        path = tuple(_text(item, "path").split("."))
        if (
            path[0] not in {"me", "world"}
            or len(path) < 2
            or any(not part.isidentifier() or part.startswith("_") for part in path)
            or path in paths
        ):
            raise ActionProtocolError("能力路径无效或重复。")
        paths.add(path)
        bindings.append(_Binding(path, _text(item, "capability_id"), _text(item, "target_id")))
    if any(path[:index] in paths for path in paths for index in range(1, len(path))):
        raise ActionProtocolError("能力调用路径不能同时作为另一个路径的命名空间。")
    return tuple(bindings)


def _mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


class _ActionMap(Mapping[str, object]):
    """宿主 JSON 对象的只读映射；点访问仅作为普通数据字段的便利表示。"""

    def __init__(self, data: dict[str, object]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> object:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __getattr__(self, name: str) -> object:
        if name.startswith("_") or name not in self._data:
            raise AttributeError(name)
        return self._data[name]

    def __repr__(self) -> str:
        return repr(self._data)


def _wrap(value: JsonValue) -> object:
    """JSON 结果只保留值，不将宿主结构还原成有行为的真实对象。"""
    if isinstance(value, dict):
        return _ActionMap({key: _wrap(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_wrap(item) for item in value)
    return value


class _WorkerClient:
    """唯一接收线程分派有限调用 Future，取消局部等待不重发宿主行动。"""

    def __init__(
        self, channel: FrameChannel, action_id: str, bindings: tuple[_Binding, ...], limits: _Limits
    ) -> None:
        self._channel = channel
        self._action_id = action_id
        self._bindings = {binding.path: binding for binding in bindings}
        self._limits = limits
        self._used_calls = 0
        self._pending: dict[int, Future[JsonValue]] = {}
        self._state_lock = threading.Lock()
        self._terminal_sent = False
        self._receiver = threading.Thread(
            target=self._receive, name="purrcept-worker-receiver", daemon=False
        )

    @property
    def paths(self) -> frozenset[tuple[str, ...]]:
        """只暴露本次冻结路径；代理无法登记新对象或改写目标身份。"""
        return frozenset(self._bindings)

    def start(self) -> None:
        """握手与源码复检完成后启动唯一接收者，不在初始化中隐式借用 FD。"""
        self._receiver.start()

    async def call_path(
        self, path: tuple[str, ...], arguments: Mapping[object, object] | Mapping[str, object]
    ) -> object:
        """先核验参数数据再分配调用身份；只有成功交接的请求等待宿主结果。"""
        if path not in self._bindings:
            raise AttributeError(".".join(path))
        try:
            encoded = encode_json(arguments)
        except ActionProtocolError as error:
            raise WorkerLimitError("能力参数不是受支持的 JSON 数据。") from error
        if len(encoded) > self._limits.call_arguments_bytes:
            raise WorkerLimitError("能力参数超过宿主字节额度。")
        normalized = decode_object(encoded)
        with self._state_lock:
            if self._used_calls >= self._limits.max_calls:
                raise WorkerLimitError("行动能力调用次数已耗尽。")
            if self._terminal_sent:
                raise ActionProtocolError("行动已经终止，不能派发下一项调用。")
            self._used_calls += 1
            sequence = self._used_calls
            future: Future[JsonValue] = Future()
            self._pending[sequence] = future
        try:
            self._channel.send(
                {
                    "kind": "call",
                    "action_id": self._action_id,
                    "sequence": sequence,
                    "path": ".".join(path),
                    "arguments": normalized,
                }
            )
        except ActionProtocolError:
            # 协议损坏不是可捕获后继续执行的普通能力拒绝；宿主持有真实回执。
            os._exit(PROTOCOL_EXIT_CODE)
        result = await asyncio.wrap_future(future)
        return _wrap(result)

    async def call_capability(
        self, capability_id: str, *, target: str | None = None, **arguments: object
    ) -> object:
        """按冻结 capability/target 选择唯一代理，不在 Worker 重新发现能力。"""
        if not isinstance(capability_id, str) or not capability_id:
            raise ValueError("capability_id 必须是非空字符串。")
        if target is not None and (not isinstance(target, str) or not target):
            raise ValueError("target 必须是非空字符串或 None。")
        matches = [
            binding
            for binding in self._bindings.values()
            if binding.capability_id == capability_id
            and (target is None or binding.target_id == target)
        ]
        if len(matches) != 1:
            raise ValueError("能力没有唯一可用绑定；多目标时须明确 target。")
        return await self.call_path(matches[0].path, arguments)

    def send_terminal(self, payload: JsonObject) -> None:
        """终止只交付一次；发送后等待宿主关闭写端，实际 EOF 后才能回收线程。"""
        with self._state_lock:
            if self._terminal_sent:
                raise ActionProtocolError("行动重复发送终止消息。")
            self._terminal_sent = True
        self._channel.send(payload)

    def join(self) -> None:
        """宿主关闭写端产生 EOF 后，确认唯一实际接收者已经退出。"""
        self._receiver.join()

    def _receive(self) -> None:
        """所有响应均严格关联；错身份、重复序号或未知帧直接终止 Worker。"""
        try:
            while True:
                message = self._channel.read()
                _identity(message, "call_result", self._action_id)
                sequence = _positive(message, "sequence")
                ok = message.get("ok")
                if not isinstance(ok, bool):
                    raise ActionProtocolError("能力响应 ok 必须是布尔值。")
                expected = {"kind", "action_id", "sequence", "ok"}
                _fields(message, expected | ({"value"} if ok else {"error_type", "error"}))
                response_error: RemoteCapabilityError | None = None
                if ok:
                    value = message["value"]
                    if len(encode_json(value)) > self._limits.call_result_bytes:
                        raise ActionProtocolError("宿主能力响应超过声明的字节额度。")
                else:
                    error_type = _text(message, "error_type")
                    error_message = message.get("error")
                    if not isinstance(error_message, str):
                        raise ActionProtocolError("能力错误正文必须是字符串。")
                    if (
                        len(
                            encode_json(
                                {
                                    "error_type": error_type,
                                    "error": error_message,
                                }
                            )
                        )
                        > self._limits.call_result_bytes
                    ):
                        raise ActionProtocolError("宿主能力错误超过声明的字节额度。")
                    response_error = RemoteCapabilityError(error_type, error_message)
                with self._state_lock:
                    future = self._pending.pop(sequence, None)
                if future is None:
                    raise ActionProtocolError("能力响应没有唯一的在途调用。")
                # 局部 wait_for 可能已取消 Future。身份仍消耗一次，合法迟到响应
                # 不能让一个 InvalidStateError 破坏其余并发调用。
                if future.set_running_or_notify_cancel():
                    if response_error is None:
                        future.set_result(message["value"])
                    else:
                        future.set_exception(response_error)
        except EOFError:
            with self._state_lock:
                terminal_sent = self._terminal_sent
            if not terminal_sent:
                os._exit(PROTOCOL_EXIT_CODE)
        except ActionProtocolError:
            os._exit(PROTOCOL_EXIT_CODE)


class _NamespaceProxy:
    """沿冻结路径解析属性，不按名称导入任何真实模块或插件。"""

    def __init__(self, client: _WorkerClient, path: tuple[str, ...]) -> None:
        self._client = client
        self._path = path

    def __getattr__(self, name: str) -> object:
        if name.startswith("_"):
            raise AttributeError(name)
        candidate = (*self._path, name)
        if candidate in self._client.paths:
            return _BoundCapability(self._client, candidate)
        if any(path[: len(candidate)] == candidate for path in self._client.paths):
            return _NamespaceProxy(self._client, candidate)
        raise AttributeError(".".join(candidate))


class _BoundCapability:
    """可 await 的纯代理，仅接受关键词或一个映射参数。"""

    def __init__(self, client: _WorkerClient, path: tuple[str, ...]) -> None:
        self._client = client
        self._path = path

    async def __call__(self, *args: object, **kwargs: object) -> object:
        if args:
            if len(args) != 1 or kwargs or not _mapping(args[0]):
                raise TypeError("能力调用只接受关键词或一个映射参数。")
            arguments = args[0]
        else:
            arguments = kwargs
        return await self._client.call_path(self._path, arguments)


class _Output(io.TextIOBase):
    """stdout/stderr 合计按 UTF-8 字节保留，超限拒绝整个写入而非成功截断。"""

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._used_bytes = 0
        self._chunks: list[str] = []
        self._failure: WorkerLimitError | None = None

    def write(self, text: str) -> int:
        """标准流只保存能完整交付的片段，超限抛出可定位的额度错误。"""
        self.raise_if_failed()
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            self._failure = WorkerLimitError("行动输出包含无效 UTF-8 字符。")
            raise self._failure from None
        if self._used_bytes + size > self._max_bytes:
            self._failure = WorkerLimitError("stdout/stderr 合计超过宿主字节额度。")
            raise self._failure
        self._used_bytes += size
        self._chunks.append(text)
        return len(text)

    def getvalue(self) -> str:
        """取得已成功保存的输出；额度失败时它只表示失败之前的完整输出。"""
        return "".join(self._chunks)

    def raise_if_failed(self) -> None:
        """额度失败锁存为行动失败，捕获写入异常不能把未保存输出冒充完整成功。"""
        if self._failure is not None:
            raise self._failure


class _ActionProcedure(Protocol):
    """受校验包装器唯一生成的异步入口，局部 return 只返回普通数据。"""

    def __call__(self) -> Coroutine[object, object, object]: ...


def _module(source: ModuleType, names: tuple[str, ...]) -> ModuleType:
    """仅复制已审查的导出，不把 json.codecs 等传递模块扩为行动 API。"""
    facade = ModuleType(source.__name__)
    facade.__dict__.update({name: getattr(source, name) for name in names})
    return facade


def _safe_modules(client: _WorkerClient) -> dict[str, ModuleType]:
    """保留既有八模块的有限表面；新标准库成员不会自动进入行动环境。"""
    world_module = ModuleType("world")
    world_module.__dict__.update(
        {
            "me": _NamespaceProxy(client, ("me",)),
            "world": _NamespaceProxy(client, ("world",)),
            "capability": client.call_capability,
            "RemoteCapabilityError": RemoteCapabilityError,
        }
    )
    asyncio_module = ModuleType("asyncio")
    asyncio_module.__dict__.update(
        {
            "gather": asyncio.gather,
            "sleep": asyncio.sleep,
            "wait_for": asyncio.wait_for,
            "as_completed": asyncio.as_completed,
            "TimeoutError": TimeoutError,
        }
    )
    datetime_module = ModuleType("datetime")
    datetime_module.__dict__.update(
        {
            "date": date,
            "datetime": datetime,
            "time": time,
            "timedelta": timedelta,
            "timezone": timezone,
        }
    )
    re_module = _module(
        re,
        (
            "compile",
            "escape",
            "findall",
            "finditer",
            "fullmatch",
            "match",
            "search",
            "split",
            "sub",
            "subn",
            "error",
        ),
    )
    re_module.__dict__.update(
        {
            name: int(getattr(re, name))
            for name in (
                "ASCII",
                "IGNORECASE",
                "LOCALE",
                "MULTILINE",
                "DOTALL",
                "UNICODE",
                "VERBOSE",
            )
        }
    )
    return {
        "world": world_module,
        "asyncio": asyncio_module,
        "datetime": datetime_module,
        "json": _module(json, ("dumps", "loads", "JSONDecodeError")),
        "itertools": _module(
            itertools,
            (
                "accumulate",
                "batched",
                "chain",
                "combinations",
                "combinations_with_replacement",
                "compress",
                "count",
                "cycle",
                "dropwhile",
                "filterfalse",
                "groupby",
                "islice",
                "pairwise",
                "permutations",
                "product",
                "repeat",
                "starmap",
                "takewhile",
                "tee",
                "zip_longest",
            ),
        ),
        "math": _module(
            math,
            (
                "acos",
                "acosh",
                "asin",
                "asinh",
                "atan",
                "atan2",
                "atanh",
                "ceil",
                "comb",
                "copysign",
                "cos",
                "cosh",
                "degrees",
                "dist",
                "e",
                "erf",
                "erfc",
                "exp",
                "exp2",
                "expm1",
                "fabs",
                "factorial",
                "floor",
                "fmod",
                "frexp",
                "fsum",
                "gamma",
                "gcd",
                "hypot",
                "inf",
                "isclose",
                "isfinite",
                "isinf",
                "isnan",
                "isqrt",
                "lcm",
                "ldexp",
                "lgamma",
                "log",
                "log10",
                "log1p",
                "log2",
                "modf",
                "nan",
                "nextafter",
                "perm",
                "pi",
                "pow",
                "prod",
                "radians",
                "remainder",
                "sin",
                "sinh",
                "sqrt",
                "tan",
                "tanh",
                "tau",
                "trunc",
                "ulp",
            ),
        ),
        "re": re_module,
        "statistics": _module(
            statistics,
            (
                "mean",
                "fmean",
                "geometric_mean",
                "harmonic_mean",
                "median",
                "median_low",
                "median_high",
                "median_grouped",
                "mode",
                "multimode",
                "quantiles",
                "pstdev",
                "pvariance",
                "stdev",
                "variance",
                "correlation",
                "covariance",
                "linear_regression",
                "NormalDist",
                "StatisticsError",
            ),
        ),
    }


def _safe_builtins(modules: dict[str, ModuleType]) -> dict[str, object]:
    """提供固定 builtin 与顶层导入表，不允许进入真实 sys.modules 或子模块。"""

    def safe_import(
        name: str,
        globals: object = None,
        locals: object = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> ModuleType:
        del globals, locals, fromlist
        if level or name not in modules:
            raise ImportError("只允许受限表面中的顶层模块。")
        return modules[name]

    names = (
        "Exception",
        "RuntimeError",
        "TypeError",
        "ValueError",
        "KeyError",
        "IndexError",
        "TimeoutError",
        "ArithmeticError",
        "AssertionError",
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "filter",
        "float",
        "format",
        "frozenset",
        "int",
        "isinstance",
        "iter",
        "len",
        "list",
        "map",
        "max",
        "min",
        "next",
        "print",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "slice",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
    )
    result = {name: getattr(builtins, name) for name in names}
    result["__import__"] = safe_import
    return result


def _run_code(wrapped: str, client: _WorkerClient, output: _Output) -> object:
    """唯一 exec 所在处；校验包装器与受限导入共同约束运行环境。"""
    modules = _safe_modules(client)
    namespace: dict[str, object] = {
        "__builtins__": _safe_builtins(modules),
        "__name__": "purrcept_action",
    }
    compiled = compile(wrapped, "<purrcept-action>", "exec", dont_inherit=True)
    previous_stdout, previous_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout = output
        sys.stderr = output
        exec(compiled, namespace, namespace)
        # validate_source 固定创建此异步包装器，且拒绝 global 和动态代码；
        # 动态编译接口没有函数签名类型，转换仅恢复已由包装器保证的调用契约。
        action = cast(_ActionProcedure, namespace["purrcept_action"])
        return asyncio.run(action())
    finally:
        sys.stdout, sys.stderr = previous_stdout, previous_stderr


def _kill_on_memory_error(code: CodeType, offset: int, error: BaseException) -> None:
    """在 Python except/finally 之前终止，不给内存违规后的代码再次派发资格。"""
    del code, offset
    if isinstance(error, MemoryError):
        os._exit(MEMORY_EXIT_CODE)


def _install_memory_guard() -> None:
    """仅观察异常事件，避免全量行跟踪；监控接口从不暴露给行动。"""
    sys.monitoring.use_tool_id(5, "purrcept-worker-memory")
    sys.monitoring.register_callback(5, sys.monitoring.events.RAISE, _kill_on_memory_error)
    sys.monitoring.set_events(5, sys.monitoring.events.RAISE)


def main() -> int:
    """接管两个明确继承的句柄，完成一次行动后等待宿主 EOF 并关闭 FD。

    main 只供独立 Worker 调用。协议失败返回专用退出码；普通行动失败仍交付
    failed 终止帧。任何真实能力结果都由宿主持有，不靠本进程 finally 保存。
    """
    if len(sys.argv) != 4:
        raise ValueError("Worker 需要 read_handle、write_handle 和 max_frame_bytes。")
    read_handle, write_handle, max_frame_bytes = (int(value) for value in sys.argv[1:])
    read_fd = msvcrt.open_osfhandle(read_handle, os.O_RDONLY | os.O_BINARY)
    try:
        write_fd = msvcrt.open_osfhandle(write_handle, os.O_WRONLY | os.O_BINARY)
    except OSError:
        os.close(read_fd)
        raise
    try:
        channel = FrameChannel(read_fd, write_fd, max_frame_bytes)
    except ActionProtocolError:
        os.close(read_fd)
        os.close(write_fd)
        raise

    client: _WorkerClient | None = None
    bootstrap: _Bootstrap | None = None
    output: _Output | None = None
    try:
        _install_memory_guard()
        bootstrap = _bootstrap(channel.read())
        bindings = _read_bindings(channel, bootstrap)
        # 目录由宿主建立并在整个 Job 退出后回收，Worker 不创建临时目录，
        # 也不依赖强退时不会运行的 finally 完成文件清理。
        os.chdir(bootstrap.directory)
        channel.send(
            {
                "kind": "ready",
                "action_id": bootstrap.action_id,
                "catalog_version": bootstrap.catalog_version,
            }
        )
        execute = channel.read()
        _fields(execute, {"kind", "action_id"})
        _identity(execute, "execute", bootstrap.action_id)

        wrapped = validate_source(bootstrap.source, max_bytes=bootstrap.limits.source_bytes)
        output = _Output(bootstrap.limits.output_bytes)
        client = _WorkerClient(channel, bootstrap.action_id, bindings, bootstrap.limits)
        client.start()
        value = _run_code(wrapped, client, output)
        output.raise_if_failed()
        try:
            encoded = encode_json(value)
        except ActionProtocolError as error:
            raise WorkerLimitError("行动最终结果不是受支持的 JSON 数据。") from error
        if len(encoded) > bootstrap.limits.result_bytes:
            raise WorkerLimitError("行动最终结果超过宿主字节额度。")
        client.send_terminal(
            {
                "kind": "completed",
                "action_id": bootstrap.action_id,
                "value": decode_object(b'{"value":' + encoded + b"}")["value"],
                "stdout": output.getvalue(),
            }
        )
    except (ActionProtocolError, EOFError):
        if client is not None:
            os._exit(PROTOCOL_EXIT_CODE)
        return PROTOCOL_EXIT_CODE
    except Exception as error:
        # 这是独立进程的结果边界；不重试、不改变错误类型，也不让展示失败
        # 覆盖宿主已经保存的效果。受支持 JSON 与错误材料过大均明确失败。
        if bootstrap is None:
            return PROTOCOL_EXIT_CODE
        payload: JsonObject = {
            "kind": "failed",
            "action_id": bootstrap.action_id,
            "error_type": type(error).__name__,
            "error": str(error).encode("utf-8", errors="backslashreplace").decode("utf-8"),
            "stdout": "" if output is None else output.getvalue(),
        }
        try:
            if client is None:
                channel.send(payload)
            else:
                client.send_terminal(payload)
        except ActionProtocolError:
            if client is not None:
                os._exit(PROTOCOL_EXIT_CODE)
            return PROTOCOL_EXIT_CODE
    finally:
        if client is not None:
            client.join()
        channel.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
