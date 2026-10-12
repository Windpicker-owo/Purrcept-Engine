"""接管一次受限行动，拥有 Worker、有限派发和独立真实回执的完整寿命。

ExecutionService 在接管前预留 Worker 名额，先保存源码和目录版本，再通过
隔离进程调用冻结能力。普通等待按行动轮转，控制调用使用独立名额；实际
Provider 返回前不释放名额或目标占用。调用者取消只结束观察，关闭则撤销新
派发并终止本服务的 Worker，等待已经进入 Provider 的调用保存真实结果。

本模块不在宿主执行行动代码，不重放未知效果，也不拥有插件和数据库的关闭。
"""

from __future__ import annotations

import asyncio
import hashlib
import msvcrt
import os
import sys
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import uuid4

from purrcept_engine.app.config import ExecutionSettings
from purrcept_engine.errors import ConfigError, EngineError
from purrcept_engine.storage.execution import ActionRecord

from .catalog import Binding, Catalog
from .ports import ExecutionJournal
from .protocol import ActionProtocolError, FrameChannel, JsonObject, encode_json
from .providers import (
    CallContext,
    CallOutcome,
    CapabilityImplementation,
    CapabilityInputError,
    bind_implementations,
)
from .windows import WindowsProcess


class ExecutionBusy(EngineError):
    """Worker 名额已满；本次源码未接管，也没有隐式等待队列。"""


class ExecutionClosed(EngineError):
    """服务已停止接管新行动。"""


class ExecutionLimitError(EngineError):
    """行动源码不能完整放入配置授予的额度。"""


@dataclass(slots=True)
class _Worker:
    """宿主持有的短期资源；reader 结束后才允许关闭 FrameChannel。"""

    process: WindowsProcess
    channel: FrameChannel
    directory: TemporaryDirectory[str]
    reader: threading.Thread | None = None


@dataclass(slots=True)
class _Action:
    """一次接管的内存资格；持久状态与实际效果始终以 Store 为权威。"""

    action_id: str
    source: str
    input_id: str | None
    deadline: float
    can_start: Callable[[], bool] | None
    messages: asyncio.Queue[JsonObject | BaseException]
    worker: _Worker | None = None
    eligible: bool = True
    sequence: int = 0
    stop_error: str | None = None
    failure: BaseException | None = None
    terminal: _Terminal | None = None


@dataclass(frozen=True, slots=True)
class _Terminal:
    """已校验的脚本结果，资源监督结束后才保存整体终态。"""

    state: Literal["completed", "failed"]
    result_json: str | None
    output: str
    error: str | None


@dataclass(slots=True)
class _Call:
    """已持久准备且尚未释放资源的调用，deadline 从收到帧时固定。"""

    action: _Action
    sequence: int
    binding: Binding
    arguments: JsonObject
    context: CallContext
    ordinary: bool
    target_reserved: bool = False


class ExecutionService:
    """由 app 创建的执行资源所有者，所有公开异步操作使用同一事件循环。

    构造检查声明与实际实现精确对应，不打开设备。execute 接管后独立运行，
    调用方可通过 query 查询回执；取消该等待不会取消已开始的行动。close 可
    重复等待同一收尾任务，取消 close 等待不撤销收尾，不重置宿主关闭期限。
    插件与 Store 必须保留到本服务真正关闭，内部故障原样报告。
    """

    def __init__(
        self,
        catalog: Catalog,
        implementations: tuple[CapabilityImplementation, ...],
        store: ExecutionJournal,
        settings: ExecutionSettings,
        *,
        scratch_root: Path,
    ) -> None:
        """冻结实现对应关系；临时目录必须位于显式给出的绝对根路径下。"""
        if not scratch_root.is_absolute():
            raise ValueError("行动临时根目录必须是绝对路径。")
        self._implementations = bind_implementations(implementations)
        declarations = {
            binding.declaration.capability_id: binding.declaration
            for binding in catalog.bindings.values()
        }
        if set(declarations) != set(self._implementations):
            raise ConfigError("目录能力与实际实现缺失或多余。")
        if any(
            self._implementations[identifier].declaration != declaration
            for identifier, declaration in declarations.items()
        ):
            raise ConfigError("能力实现与冻结声明不一致。")

        self._catalog = catalog
        self._store = store
        self._settings = settings
        self._scratch_root = scratch_root
        self._actions: dict[str, _Action] = {}
        self._owned_actions: set[asyncio.Task[ActionRecord]] = set()
        self._owned_calls: set[asyncio.Task[None]] = set()
        self._pending_calls = 0
        self._ordinary: dict[str, deque[_Call]] = {}
        self._rotation: deque[str] = deque()
        self._controls: deque[_Call] = deque()
        self._ordinary_active = 0
        self._control_active = 0
        self._targets: set[str] = set()
        self._dispatch_timer: asyncio.TimerHandle | None = None
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._failures: list[BaseException] = []

    @property
    def pending_resources(self) -> int:
        """返回实际 Worker 与未收尾调用数，供宿主关闭超时后报告资源状态。"""
        return len(self._actions) + self._pending_calls

    async def execute(
        self,
        source: str,
        *,
        input_id: str | None = None,
        can_start: Callable[[], bool] | None = None,
    ) -> ActionRecord:
        """接管完整源码后等待整体结果，等待取消不撤销该行动的所有权。

        can_start 只在实际 execute 帧之前核验生成资格。派发之后资格变化
        不终止已开始的独立行动；服务关闭与自身资源额度仍可以停止 Worker。
        """
        if self._closed:
            raise ExecutionClosed("执行服务已关闭接管入口。")
        if len(self._actions) >= self._settings.max_workers:
            raise ExecutionBusy("行动 Worker 名额已满。")
        if not isinstance(source, str):
            raise TypeError("行动源码必须是字符串。")
        try:
            source_bytes = len(source.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ExecutionLimitError("行动源码不是有效 UTF-8。") from error
        if source_bytes > self._settings.source_bytes:
            raise ExecutionLimitError("行动源码超过字节额度。")
        if input_id is not None and (not isinstance(input_id, str) or not input_id):
            raise ValueError("输入关联必须是非空身份。")

        loop = asyncio.get_running_loop()
        action = _Action(
            uuid4().hex,
            source,
            input_id,
            loop.time() + self._settings.action_seconds,
            can_start,
            asyncio.Queue(maxsize=self._settings.max_calls + 2),
        )
        # 名额在第一次 await 前取得；两项并发接管不能同时看到最后一个空位。
        self._actions[action.action_id] = action
        task = asyncio.create_task(self._run(action), name=f"action:{action.action_id}")
        self._owned_actions.add(task)
        task.add_done_callback(self._action_done)
        return await asyncio.shield(task)

    async def query(self, action_id: str) -> ActionRecord | None:
        """查询权威行动与调用回执，不从 Worker 的自报状态补造效果。"""
        return await self._store.get_action(action_id)

    async def close(self) -> None:
        """停止新入口与新调用，等待本服务持有的实际进程和 Provider 收尾。

        宿主拥有总体关闭期限。本方法不另开 30 秒预算，不关闭仍被调用使用
        的插件或 Store；等待取消后同一后台收尾继续，后续 close 可再次观察。
        """
        if self._close_task is None:
            self._closed = True
            for action in self._actions.values():
                self._stop(action, "ServiceClosed")
            self._dispatch()
            self._close_task = asyncio.create_task(self._finish_close())
        await asyncio.shield(self._close_task)

    async def _finish_close(self) -> None:
        """先收回 Worker，再等真实调用；任何原始失败在全部资源收尾后报告。"""
        while self._owned_actions or self._owned_calls:
            if self._owned_actions:
                await asyncio.gather(*self._owned_actions, return_exceptions=True)
            if self._owned_calls:
                await asyncio.gather(*self._owned_calls, return_exceptions=True)
        if self._dispatch_timer is not None:
            self._dispatch_timer.cancel()
            self._dispatch_timer = None
        if self._failures:
            raise BaseExceptionGroup("执行资源收尾期间的原始故障。", self._failures)

    def _action_done(self, task: asyncio.Task[ActionRecord]) -> None:
        """独立任务即使观察者已取消仍取回异常，关闭时保留同一真实失败。"""
        self._owned_actions.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self._failures.append(error)

    async def _run(self, action: _Action) -> ActionRecord:
        """保存意图、启动/核验、处理帧、保存整体结果并按真实资源顺序释放。"""
        monitor: asyncio.Task[None] | None = None
        admitted = False
        try:
            await self._store.begin_action(
                action.action_id, catalog_version=self._catalog.version, source=action.source
            )
            admitted = True
            if self._closed or asyncio.get_running_loop().time() >= action.deadline:
                action.stop_error = "ServiceClosed" if self._closed else "ActionWallLimit"
            else:
                action.worker = await asyncio.to_thread(self._launch)
                self._start_reader(action)
                monitor = asyncio.create_task(self._monitor(action))
                await self._bootstrap(action)
                ready = await action.messages.get()
                if isinstance(ready, BaseException):
                    raise ready
                if ready != {
                    "kind": "ready",
                    "action_id": action.action_id,
                    "catalog_version": self._catalog.version,
                }:
                    raise ActionProtocolError("Worker ready 身份或目录版本不一致。")

                if (
                    self._closed
                    or not action.eligible
                    or asyncio.get_running_loop().time() >= action.deadline
                    or (action.can_start is not None and not action.can_start())
                ):
                    self._stop(action, action.stop_error or "GenerationRevoked")
                else:
                    # 发送前最后核验之后不再读取生成资格；已派发行动独立收尾。
                    # ready 已证明子进程正在等这份短帧，管道没有其他写者。
                    # 此处不 await，防止关闭/生成撤销插入最终核验和实际派发之间。
                    action.worker.channel.send({"kind": "execute", "action_id": action.action_id})
                    await self._frames(action)
        except (ActionProtocolError, EOFError, OSError) as error:
            if action.stop_error is None:
                action.stop_error = f"{type(error).__name__}: {error}"
            if action.worker is not None:
                self._stop(action, action.stop_error)
        except BaseException as error:
            # 这是独立资源所有者的清理边界；原始故障在真实资源收回后重新抛出。
            action.failure = error
            self._stop(action, "ExecutionInternalFailure")
        finally:
            action.eligible = False
            self._dispatch()
            if action.worker is not None:
                cleanup_error = await self._release_worker(action.worker, action, monitor)
                if cleanup_error is not None:
                    action.failure = (
                        cleanup_error
                        if action.failure is None
                        else BaseExceptionGroup(
                            "行动及实际资源回收失败。", [action.failure, cleanup_error]
                        )
                    )
                else:
                    self._actions.pop(action.action_id, None)
            else:
                self._actions.pop(action.action_id, None)

        if action.failure is not None:
            if admitted:
                try:
                    await self._store.finish_action(
                        action.action_id,
                        state="failed",
                        result_json=None,
                        output="" if action.terminal is None else action.terminal.output,
                        error=f"{type(action.failure).__name__}: {action.failure}",
                    )
                except BaseException as record_error:
                    raise BaseExceptionGroup(
                        "行动原始故障与权威结束提交均失败。",
                        [action.failure, record_error],
                    ) from None
            raise action.failure
        if action.stop_error is not None and admitted:
            state: Literal["failed", "timed_out", "cancelled"] = "failed"
            if action.stop_error in {"ActionWallLimit", "WorkerCpuLimit"}:
                state = "timed_out"
            elif action.stop_error in {"ServiceClosed", "GenerationRevoked"}:
                state = "cancelled"
            await self._store.finish_action(
                action.action_id, state=state, result_json=None, output="", error=action.stop_error
            )
        elif action.terminal is not None:
            terminal = action.terminal
            await self._store.finish_action(
                action.action_id,
                state=terminal.state,
                result_json=terminal.result_json,
                output=terminal.output,
                error=terminal.error,
            )
        record = await self._store.get_action(action.action_id)
        if record is None:
            raise RuntimeError("已接管行动缺少权威记录。")
        return record

    async def _release_worker(
        self,
        worker: _Worker,
        action: _Action,
        monitor: asyncio.Task[None] | None,
    ) -> BaseException | None:
        """逐项收回实际资源，管道错误不能跳过 Job 收尾或抹掉原始失败。

        只有确认 Job 已退出才清理宿主持有的临时目录。任何收回失败都保留
        action 对资源的引用，供关闭状态报告；不重建另一 Worker 或重复关闭
        可能已经被操作系统复用的 FD。
        """
        errors: list[BaseException] = []
        try:
            if worker.process.poll() is None and action.stop_error is not None:
                worker.process.terminate()
        except OSError as error:
            errors.append(error)

        if worker.reader is not None:
            try:
                await asyncio.to_thread(worker.reader.join)
            except BaseException as error:
                errors.append(error)
        try:
            # terminal 后 reader 已返回；关闭写端的 EOF 让 Worker 接收者退出。
            await asyncio.to_thread(worker.channel.close)
        except BaseException as error:
            errors.append(error)
            try:
                worker.process.terminate()
            except OSError as termination_error:
                errors.append(termination_error)

        exited = False
        try:
            await worker.process.wait()
            exited = True
        except OSError as error:
            errors.append(error)
        if monitor is not None:
            try:
                await monitor
            except BaseException as error:
                errors.append(error)
        if exited:
            try:
                worker.process.close()
            except OSError as error:
                errors.append(error)
            try:
                await asyncio.to_thread(worker.directory.cleanup)
            except OSError as error:
                errors.append(error)
        if errors:
            return BaseExceptionGroup("Worker 资源回收失败。", errors)
        return None

    def _launch(self) -> _Worker:
        """在文件工作线程建立仅本次拥有的目录和管道，Job 创建原子交接句柄。"""
        root = self._scratch_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        directory = TemporaryDirectory(prefix="action-", dir=root)
        actual = Path(directory.name).resolve()
        if not actual.is_relative_to(root) or actual == root:
            directory.cleanup()
            raise ValueError("行动临时目录越过指定根路径。")
        descriptors: list[int] = []
        try:
            child_read, parent_write = os.pipe()
            descriptors.extend((child_read, parent_write))
            parent_read, child_write = os.pipe()
            descriptors.extend((parent_read, child_write))
            channel = FrameChannel(parent_read, parent_write, self._settings.frame_bytes)
            os.set_inheritable(child_read, True)
            os.set_inheritable(child_write, True)
            read_handle = msvcrt.get_osfhandle(child_read)
            write_handle = msvcrt.get_osfhandle(child_write)
            worker_path = Path(__file__).with_name("worker.py").resolve()
            executable: object = vars(sys)["_base_executable"]
            if not isinstance(executable, str) or not executable:
                raise RuntimeError("CPython 未提供真实基础解释器路径。")
            process = WindowsProcess.launch(
                (
                    executable,
                    "-I",
                    str(worker_path),
                    str(read_handle),
                    str(write_handle),
                    str(self._settings.frame_bytes),
                ),
                cpu_seconds=self._settings.worker_cpu_seconds,
                memory_bytes=self._settings.worker_memory_bytes,
                inherited_handles=(read_handle, write_handle),
            )
        except BaseException:
            # 资源取得边界仅清理本次所有权并原样传播，不把启动错误变为空成功。
            for descriptor in descriptors:
                os.close(descriptor)
            directory.cleanup()
            raise
        os.close(child_read)
        os.close(child_write)
        return _Worker(process, channel, directory)

    def _start_reader(self, action: _Action) -> None:
        """每个 Worker 只有一个真实接收线程，队列容量来自单行动帧数上界。"""
        worker = action.worker
        if worker is None:
            raise RuntimeError("Worker 尚未接管。")
        loop = asyncio.get_running_loop()

        def publish(message: JsonObject | BaseException) -> None:
            try:
                action.messages.put_nowait(message)
            except asyncio.QueueFull:
                self._stop(action, "ProtocolQueueLimit")

        def receive() -> None:
            try:
                while True:
                    message = worker.channel.read()
                    loop.call_soon_threadsafe(publish, message)
                    if message.get("kind") in {"completed", "failed"}:
                        return
            except (ActionProtocolError, EOFError) as error:
                loop.call_soon_threadsafe(publish, error)

        worker.reader = threading.Thread(target=receive, name=f"action-pipe:{action.action_id}")
        worker.reader.start()

    async def _bootstrap(self, action: _Action) -> None:
        """完整目录以摘要绑定的 UTF-8 分块交付，不因单帧上限删掉声明。"""
        worker = action.worker
        if worker is None:
            raise RuntimeError("Worker 尚未接管。")
        bindings = encode_json(
            {
                "bindings": [
                    {
                        "path": path,
                        "capability_id": binding.declaration.capability_id,
                        "target_id": binding.declaration.object_id,
                    }
                    for path, binding in self._catalog.bindings.items()
                ]
            }
        )
        # ASCII 转义后的信封仍可能比原 UTF-8 大；逐块按真实编码检查，绝不
        # 直接把 byte 切片当成可解码字符串，也不猜一份目录能装进一帧。
        text = bindings.decode("utf-8")
        chunks: list[str] = []
        while text:
            low, high = 1, len(text)
            while low < high:
                midpoint = (low + high + 1) // 2
                frame = {
                    "kind": "binding_chunk",
                    "action_id": action.action_id,
                    "index": len(chunks),
                    "data": text[:midpoint],
                }
                if len(encode_json(frame)) <= self._settings.frame_bytes:
                    low = midpoint
                else:
                    high = midpoint - 1
            chunks.append(text[:low])
            text = text[low:]
        limits = {
            "max_calls": self._settings.max_calls,
            "source_bytes": self._settings.source_bytes,
            "output_bytes": self._settings.output_bytes,
            "result_bytes": self._settings.result_bytes,
            "call_arguments_bytes": self._settings.call_arguments_bytes,
            "call_result_bytes": self._settings.call_result_bytes,
        }
        await asyncio.to_thread(
            worker.channel.send,
            {
                "kind": "bootstrap",
                "action_id": action.action_id,
                "catalog_version": self._catalog.version,
                "source": action.source,
                "directory": worker.directory.name,
                "limits": limits,
                "binding_bytes": len(bindings),
                "binding_digest": hashlib.sha256(bindings).hexdigest(),
                "binding_chunks": len(chunks),
            },
        )
        for index, chunk in enumerate(chunks):
            await asyncio.to_thread(
                worker.channel.send,
                {
                    "kind": "binding_chunk",
                    "action_id": action.action_id,
                    "index": index,
                    "data": chunk,
                },
            )

    async def _monitor(self, action: _Action) -> None:
        """监督整个 Job 的总 CPU 与接管起算墙钟；结束资格先于终止请求。"""
        worker = action.worker
        if worker is None:
            raise RuntimeError("Worker 尚未接管。")
        while worker.process.poll() is None:
            if asyncio.get_running_loop().time() >= action.deadline:
                self._stop(action, "ActionWallLimit")
            elif worker.process.total_cpu_seconds >= self._settings.worker_cpu_seconds:
                self._stop(action, "WorkerCpuLimit")
            elif self._closed:
                self._stop(action, "ServiceClosed")
            # Windows 默认单调时钟分辨率可为 15.6ms，10ms 定时器可能提前
            # 唤醒而热轮询。此等待只限制监督频率，资格仍核验绝对期限和总 CPU。
            await asyncio.sleep(0.02)
        code = worker.process.poll()
        if code == 71:
            action.stop_error = "WorkerMemoryLimit"
        elif code == 72:
            action.stop_error = action.stop_error or "WorkerProtocolViolation"
        elif worker.process.total_cpu_seconds >= self._settings.worker_cpu_seconds:
            action.stop_error = action.stop_error or "WorkerCpuLimit"

    def _stop(self, action: _Action, reason: str) -> None:
        """同步撤销尚未开始的调用资格；已进入 Provider 的效果继续独立收尾。"""
        action.eligible = False
        action.stop_error = action.stop_error or reason
        if action.worker is not None and action.worker.process.poll() is None:
            action.worker.process.terminate()

    async def _frames(self, action: _Action) -> None:
        """严格关联每次连续调用，先保存准备事实，再进入有界普通/控制队列。"""
        while True:
            message = await action.messages.get()
            if isinstance(message, BaseException):
                raise message
            if message.get("action_id") != action.action_id:
                raise ActionProtocolError("Worker 帧的行动身份错误。")
            kind = message.get("kind")
            if kind in {"completed", "failed"}:
                action.eligible = False
                self._dispatch()
                action.terminal = self._terminal(message)
                return
            if kind != "call" or set(message) != {
                "kind",
                "action_id",
                "sequence",
                "path",
                "arguments",
            }:
                raise ActionProtocolError("Worker 调用帧形状错误。")
            sequence = message.get("sequence")
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence != action.sequence + 1
                or sequence > self._settings.max_calls
            ):
                raise ActionProtocolError("Worker 调用序号不连续或超过额度。")
            path = message.get("path")
            arguments = message.get("arguments")
            if not isinstance(path, str) or not isinstance(arguments, dict):
                raise ActionProtocolError("Worker 路径和参数类型错误。")
            binding = self._catalog.bindings.get(path)
            if binding is None:
                raise ActionProtocolError("Worker 调用与冻结目录不一致。")
            encoded = encode_json(arguments)
            if len(encoded) > self._settings.call_arguments_bytes:
                raise ActionProtocolError("Worker 能力参数超过字节额度。")
            action.sequence = sequence
            call_id = f"{action.action_id}:{sequence}"
            deadline = min(
                action.deadline,
                asyncio.get_running_loop().time() + binding.declaration.timeout_seconds,
            )
            call = _Call(
                action,
                sequence,
                binding,
                arguments,
                CallContext(call_id, action.action_id, deadline, action.input_id),
                not binding.declaration.control,
            )
            # 已退出 Worker 的迟到 Provider 仍占用本次全局额度；名额在数据库
            # 等待前取得，不让多个接收者同时穿过最后一个空位。
            capacity = self._settings.max_workers * self._settings.max_calls
            accepted = self._pending_calls < capacity
            if accepted:
                self._pending_calls += 1
            try:
                await self._store.begin_call(
                    call_id,
                    action_id=action.action_id,
                    sequence=sequence,
                    path=path,
                    target_id=binding.declaration.object_id,
                    arguments_json=encoded.decode("utf-8"),
                )
            except BaseException:
                if accepted:
                    self._pending_calls -= 1
                raise
            if not accepted:
                await self._not_started(
                    call, error_type="CallQueueFull", reason="调用等待额度已满。"
                )
                continue
            if call.ordinary:
                if action.action_id not in self._ordinary:
                    self._ordinary[action.action_id] = deque()
                    self._rotation.append(action.action_id)
                self._ordinary[action.action_id].append(call)
            else:
                self._controls.append(call)
            self._dispatch()

    def _terminal(self, message: JsonObject) -> _Terminal:
        """只保存脚本整体结果；真实能力效果只能来自独立 Provider 回执。"""
        output = message.get("stdout")
        if not isinstance(output, str) or len(output.encode("utf-8")) > self._settings.output_bytes:
            raise ActionProtocolError("Worker 输出字段错误或超过额度。")
        if message["kind"] == "completed":
            if set(message) != {"kind", "action_id", "value", "stdout"}:
                raise ActionProtocolError("Worker 完成帧形状错误。")
            result = encode_json(message["value"])
            if len(result) > self._settings.result_bytes:
                raise ActionProtocolError("Worker 最终结果超过额度。")
            return _Terminal("completed", result.decode("utf-8"), output, None)
        else:
            if set(message) != {"kind", "action_id", "error_type", "error", "stdout"}:
                raise ActionProtocolError("Worker 失败帧形状错误。")
            error_type, error = message.get("error_type"), message.get("error")
            if not isinstance(error_type, str) or not error_type or not isinstance(error, str):
                raise ActionProtocolError("Worker 失败字段类型错误。")
            return _Terminal("failed", None, output, f"{error_type}: {error}")

    def _dispatch(self) -> None:
        """普通请求按来源轮转且保留来源内次序，控制请求不等待普通名额。"""
        if self._dispatch_timer is not None:
            self._dispatch_timer.cancel()
            self._dispatch_timer = None
        now = asyncio.get_running_loop().time()
        for _ in range(len(self._controls)):
            call = self._controls[0]
            expired = self._closed or not call.action.eligible or now >= call.context.deadline
            if not expired and self._control_active >= self._settings.max_provider_calls:
                break
            self._controls.popleft()
            if not expired:
                self._control_active += 1
            self._start_call(call, reserved=not expired)

        stalled = 0
        while self._rotation and stalled < len(self._rotation):
            action_id = self._rotation.popleft()
            queue = self._ordinary[action_id]
            call = queue[0]
            expired = self._closed or not call.action.eligible or now >= call.context.deadline
            serial = call.binding.declaration.concurrency == "target_serial"
            available = self._ordinary_active < self._settings.max_provider_calls and (
                not serial or call.binding.declaration.object_id not in self._targets
            )
            if expired or available:
                queue.popleft()
                if not expired:
                    self._ordinary_active += 1
                    if serial:
                        call.target_reserved = True
                        self._targets.add(call.binding.declaration.object_id)
                self._start_call(call, reserved=not expired)
                stalled = 0
            else:
                stalled += 1
            if queue:
                self._rotation.append(action_id)
            else:
                del self._ordinary[action_id]

        waiting = (*self._controls, *(call for queue in self._ordinary.values() for call in queue))
        if waiting:
            deadline = min(call.context.deadline for call in waiting)
            self._dispatch_timer = asyncio.get_running_loop().call_at(deadline, self._dispatch)

    def _start_call(self, call: _Call, *, reserved: bool) -> None:
        task = asyncio.create_task(self._invoke(call, reserved=reserved))
        self._owned_calls.add(task)
        task.add_done_callback(self._call_done)

    def _call_done(self, task: asyncio.Task[None]) -> None:
        """内部故障撤销服务派发而保留原始异常，不能让后台任务静默失踪。"""
        self._owned_calls.discard(task)
        self._pending_calls -= 1
        if not task.cancelled() and (error := task.exception()) is not None:
            self._failures.append(error)
            self._closed = True
            for action in self._actions.values():
                action.failure = error
                self._stop(action, "ExecutionInternalFailure")
            self._dispatch()

    async def _invoke(self, call: _Call, *, reserved: bool) -> None:
        """持久启动门槛之后再次核验；超时保留真实任务并独立澄清未知回执。"""
        try:
            if not reserved or not self._call_eligible(call):
                await self._not_started(call)
                return
            await self._store.mark_call_started(call.context.call_id)
            if not self._call_eligible(call):
                await self._not_started(call)
                return
            implementation = self._implementations[call.binding.declaration.capability_id]
            provider = asyncio.create_task(self._enter_provider(call, implementation))
            remaining = max(0.0, call.context.deadline - asyncio.get_running_loop().time())
            done, _ = await asyncio.wait((provider,), timeout=remaining)
            timed_out = not done
            if timed_out:
                await self._record(
                    call,
                    CallOutcome("unknown", None, "TimeoutError", "能力期限耗尽，效果尚未确认。"),
                )
            try:
                outcome = await provider
            except asyncio.CancelledError:
                # 插件自身取消不证明外界没有效果；服务从不取消此独立调用任务。
                outcome = CallOutcome(
                    "unknown", None, "CancelledError", "Provider 已取消，效果未确认。"
                )
            except CapabilityInputError as error:
                outcome = CallOutcome("failed", None, type(error).__name__, str(error))
            except Exception as error:
                # Provider 是真实扩展边界：保留原错误类型和失败事实，不提示重填
                # 参数、不撤销已发生效果；插件可用 unknown/部分结果表达无法确认。
                outcome = CallOutcome("failed", None, type(error).__name__, str(error))
            if outcome is None:
                await self._not_started(call)
                return
            await self._record(call, outcome, deliver=not timed_out)
        finally:
            if reserved:
                if call.ordinary:
                    self._ordinary_active -= 1
                else:
                    self._control_active -= 1
                if call.target_reserved:
                    self._targets.remove(call.binding.declaration.object_id)
            self._dispatch()

    async def _enter_provider(
        self, call: _Call, implementation: CapabilityImplementation
    ) -> CallOutcome | None:
        """任务真正获得运行时再次核验；核验到 invoke 之间不交还事件循环。"""
        if not self._call_eligible(call):
            return None
        return await implementation.invoke(call.arguments, call.context)

    def _call_eligible(self, call: _Call) -> bool:
        return (
            not self._closed
            and call.action.eligible
            and asyncio.get_running_loop().time() < call.context.deadline
        )

    async def _not_started(
        self,
        call: _Call,
        *,
        error_type: str = "CallNotStarted",
        reason: str = "调用在开始前失去资格或期限。",
    ) -> None:
        await self._store.record_call_result(
            call.context.call_id,
            state="not_started",
            result_json=None,
            error_type=error_type,
            error=reason,
        )
        await self._deliver(call, {"ok": False, "error_type": error_type, "error": reason})

    async def _record(self, call: _Call, outcome: CallOutcome, *, deliver: bool = True) -> None:
        """原始结果先完整入账；交付超限只改变交付结果，不改变效果或自动重做。"""
        if not isinstance(outcome, CallOutcome) or outcome.state not in {
            "confirmed",
            "failed",
            "unknown",
        }:
            raise TypeError("Provider 未返回声明的 CallOutcome。")
        encoded = encode_json(outcome.value)
        await self._store.record_call_result(
            call.context.call_id,
            state=outcome.state,
            result_json=encoded.decode("utf-8"),
            error_type=outcome.error_type,
            error=outcome.error,
        )
        if not deliver:
            return
        if outcome.state == "confirmed":
            response: JsonObject = {"ok": True, "value": outcome.value}
            size = len(encoded)
        else:
            response = {
                "ok": False,
                "error_type": outcome.error_type
                or ("CapabilityFailed" if outcome.state == "failed" else "UnknownResult"),
                "error": outcome.error
                or ("能力返回明确失败。" if outcome.state == "failed" else "能力效果无法确认。"),
            }
            size = len(
                encode_json({"error_type": response["error_type"], "error": response["error"]})
            )
        if size > self._settings.call_result_bytes:
            response = {
                "ok": False,
                "error_type": "ResultDeliveryLimit",
                "error": f"结果已完整保存，交付字节超限；call_id={call.context.call_id}",
            }
            error_bytes = len(
                encode_json({"error_type": response["error_type"], "error": response["error"]})
            )
            if error_bytes > self._settings.call_result_bytes:
                # 极小配置连错误及身份也不能交付；明确终止并保留原回执，
                # 不能为了报错再次发送超额度数据，让 Worker 误报协议损坏。
                self._stop(call.action, f"ResultDeliveryLimit: call_id={call.context.call_id}")
                return
        await self._deliver(call, response)

    async def _deliver(self, call: _Call, response: JsonObject) -> None:
        """只向仍接收结果的 Worker 交付；管道失败不覆盖已入账的真实效果。"""
        action = call.action
        worker = action.worker
        if worker is None or not action.eligible:
            return
        try:
            await asyncio.to_thread(
                worker.channel.send,
                {
                    "kind": "call_result",
                    "action_id": action.action_id,
                    "sequence": call.sequence,
                    **response,
                },
            )
        except ActionProtocolError as error:
            self._stop(action, f"ResultDeliveryFailure: {error}")


__all__ = ["ExecutionBusy", "ExecutionClosed", "ExecutionLimitError", "ExecutionService"]
