"""WorkerSession 的执行资源协作；不创建另一份行动资格或权威记录。"""

from __future__ import annotations

import asyncio
import hashlib
import msvcrt
import os
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from purrcept_engine.domain.execution.providers import (
    CallContext,
)
from purrcept_engine.infrastructure.execution.protocol import FrameChannel
from purrcept_engine.infrastructure.execution.state import (
    CapabilityCall,
    ScriptAction,
    WorkerHandle,
    WorkerTerminal,
)
from purrcept_engine.infrastructure.processes.windows import WindowsProcess
from purrcept_engine.values import ActionProtocolError, JsonObject, encode_json

if TYPE_CHECKING:
    from purrcept_engine.infrastructure.execution.service import ExecutionService


class WorkerSession:
    """由执行器构造并独占，借用同一运行状态；资源关闭仍归执行器。"""

    def __init__(self, runtime: ExecutionService) -> None:
        """固定唯一执行器借用，不取得额外 Provider、账本或后台任务。"""
        self.runtime = runtime

    async def release_worker(
        self,
        worker: WorkerHandle,
        action: ScriptAction,
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

    def launch(self) -> WorkerHandle:
        """在文件工作线程建立仅本次拥有的目录和管道，Job 创建原子交接句柄。"""
        root = self.runtime.scratch_root.resolve()
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
            channel = FrameChannel(parent_read, parent_write, self.runtime.settings.frame_bytes)
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
                    str(self.runtime.settings.frame_bytes),
                ),
                cpu_seconds=self.runtime.settings.worker_cpu_seconds,
                memory_bytes=self.runtime.settings.worker_memory_bytes,
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
        return WorkerHandle(process, channel, directory)

    def start_reader(self, action: ScriptAction) -> None:
        """每个 Worker 只有一个真实接收线程，队列容量来自单行动帧数上界。"""
        worker = action.worker
        if worker is None:
            raise RuntimeError("Worker 尚未接管。")
        loop = asyncio.get_running_loop()

        def publish(message: JsonObject | BaseException) -> None:
            try:
                action.messages.put_nowait(message)
            except asyncio.QueueFull:
                self.runtime.stop(action, "ProtocolQueueLimit")

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

    async def bootstrap(self, action: ScriptAction) -> None:
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
                    for path, binding in self.runtime.catalog.bindings.items()
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
                if len(encode_json(frame)) <= self.runtime.settings.frame_bytes:
                    low = midpoint
                else:
                    high = midpoint - 1
            chunks.append(text[:low])
            text = text[low:]
        limits = {
            "max_calls": self.runtime.settings.max_calls,
            "source_bytes": self.runtime.settings.source_bytes,
            "output_bytes": self.runtime.settings.output_bytes,
            "result_bytes": self.runtime.settings.result_bytes,
            "call_arguments_bytes": self.runtime.settings.call_arguments_bytes,
            "call_result_bytes": self.runtime.settings.call_result_bytes,
        }
        await asyncio.to_thread(
            worker.channel.send,
            {
                "kind": "bootstrap",
                "action_id": action.action_id,
                "catalog_version": self.runtime.catalog.version,
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

    async def monitor(self, action: ScriptAction) -> None:
        """监督整个 Job 的总 CPU 与接管起算墙钟；结束资格先于终止请求。"""
        worker = action.worker
        if worker is None:
            raise RuntimeError("Worker 尚未接管。")
        while worker.process.poll() is None:
            if asyncio.get_running_loop().time() >= action.deadline:
                self.runtime.stop(action, "ActionWallLimit")
            elif worker.process.total_cpu_seconds >= self.runtime.settings.worker_cpu_seconds:
                self.runtime.stop(action, "WorkerCpuLimit")
            elif self.runtime.closed:
                self.runtime.stop(action, "ServiceClosed")
            # Windows 默认单调时钟分辨率可为 15.6ms，10ms 定时器可能提前
            # 唤醒而热轮询。此等待只限制监督频率，资格仍核验绝对期限和总 CPU。
            await asyncio.sleep(0.02)
        code = worker.process.poll()
        if code == 71:
            action.stop_error = "WorkerMemoryLimit"
        elif code == 72:
            action.stop_error = action.stop_error or "WorkerProtocolViolation"
        elif worker.process.total_cpu_seconds >= self.runtime.settings.worker_cpu_seconds:
            action.stop_error = action.stop_error or "WorkerCpuLimit"

    async def frames(self, action: ScriptAction) -> None:
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
                self.runtime.call_dispatcher.dispatch()
                action.terminal = self.terminal(message)
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
                or sequence > self.runtime.settings.max_calls
            ):
                raise ActionProtocolError("Worker 调用序号不连续或超过额度。")
            path = message.get("path")
            arguments = message.get("arguments")
            if not isinstance(path, str) or not isinstance(arguments, dict):
                raise ActionProtocolError("Worker 路径和参数类型错误。")
            binding = self.runtime.catalog.bindings.get(path)
            if binding is None:
                raise ActionProtocolError("Worker 调用与冻结目录不一致。")
            encoded = encode_json(arguments)
            if len(encoded) > self.runtime.settings.call_arguments_bytes:
                raise ActionProtocolError("Worker 能力参数超过字节额度。")
            action.sequence = sequence
            call_id = f"{action.action_id}:{sequence}"
            deadline = min(
                action.deadline,
                asyncio.get_running_loop().time() + binding.declaration.timeout_seconds,
            )
            call = CapabilityCall(
                action,
                sequence,
                binding,
                arguments,
                CallContext(
                    call_id,
                    action.action_id,
                    deadline,
                    action.input_id,
                    action.presented_call_ids,
                ),
                not binding.declaration.control,
            )
            # 已退出 Worker 的迟到 Provider 仍占用本次全局额度；名额在数据库
            # 等待前取得，不让多个接收者同时穿过最后一个空位。
            accepted = self.runtime.call_dispatcher.reserve_call(call)
            try:
                await self.runtime.store.begin_call(
                    call_id,
                    action_id=action.action_id,
                    sequence=sequence,
                    path=path,
                    target_id=binding.declaration.object_id,
                    arguments_json=encoded.decode("utf-8"),
                )
            except BaseException:
                if accepted:
                    self.runtime.call_dispatcher.release_call(call)
                raise
            if not accepted:
                await self.runtime.call_dispatcher.not_started(
                    call, error_type="CallQueueFull", reason="调用等待额度已满。"
                )
                continue
            self.runtime.call_dispatcher.enqueue(call)
            self.runtime.call_dispatcher.dispatch()

    def terminal(self, message: JsonObject) -> WorkerTerminal:
        """只保存脚本整体结果；真实能力效果只能来自独立 Provider 回执。"""
        output = message.get("stdout")
        if (
            not isinstance(output, str)
            or len(output.encode("utf-8")) > self.runtime.settings.output_bytes
        ):
            raise ActionProtocolError("Worker 输出字段错误或超过额度。")
        if message["kind"] == "completed":
            if set(message) != {"kind", "action_id", "value", "stdout"}:
                raise ActionProtocolError("Worker 完成帧形状错误。")
            result = encode_json(message["value"])
            if len(result) > self.runtime.settings.result_bytes:
                raise ActionProtocolError("Worker 最终结果超过额度。")
            return WorkerTerminal("completed", result.decode("utf-8"), output, None)
        else:
            if set(message) != {"kind", "action_id", "error_type", "error", "stdout"}:
                raise ActionProtocolError("Worker 失败帧形状错误。")
            error_type, error = message.get("error_type"), message.get("error")
            if not isinstance(error_type, str) or not error_type or not isinstance(error, str):
                raise ActionProtocolError("Worker 失败字段类型错误。")
            return WorkerTerminal("failed", None, output, f"{error_type}: {error}")

    async def deliver(self, call: CapabilityCall, response: JsonObject) -> None:
        """只向仍接收结果的 Worker 交付；管道失败不覆盖已入账的真实效果。"""
        action = call.action
        if not isinstance(action, ScriptAction):
            raise RuntimeError("Worker 交付只能使用真实脚本所有者。")
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
            self.runtime.stop(action, f"ResultDeliveryFailure: {error}")
