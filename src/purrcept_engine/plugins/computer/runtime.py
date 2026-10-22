"""显式电脑连接、有限候选与按真实input_id确认的连续观察。

ComputerRuntime独占一个驱动和采样任务，正式输入/媒体归宿主端口。一个已
接管画面等待真实processed，另一个候选可被新画面替换；failed暂停，暂缓
不解锁。没有react、独立画面解释、自动声音或重做旧行动。查询只读缓存。
单次截图先保存同库证据，再写入配置目录的独立 JPEG；返回路径可用于显式
发送。连续观察不写文件，关闭不删除已交付文件，也不自行执行消息发送。
宿主命令委托给独立 CommandRunner；连接同时授予命令调用资格，断开回收其进程树。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from purrcept_engine.errors import HostStateError
from purrcept_engine.plugin_system import (
    CallOutcome,
    EvidencePort,
    EvidenceSnapshot,
    InputContext,
    InputPort,
    InputSource,
    InputSubmission,
    MediaAsset,
    MediaReference,
)
from purrcept_engine.plugins.computer.command import CommandParameters, CommandRunner
from purrcept_engine.plugins.computer.driver import ComputerDriver
from purrcept_engine.plugins.computer.models import (
    ChangeDetector,
    ComputerError,
    ComputerFrame,
    ObservationPolicy,
)
from purrcept_engine.values import JsonObject, encode_json

_LOG = logging.getLogger(__name__)


def _write_screenshot(directory: Path, data: bytes) -> Path:
    """以独占随机文件名保存完整 JPEG；失败删除本次残件，不覆盖现有文件。

    目录只来自宿主配置，模型不能指定任意写入路径。线程归运行时持有，只有
    完成写入、同步和关闭后才返回路径，避免后续发送读取半张图。
    """
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="screenshot-", suffix=".jpg", dir=directory)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return path


def load_policy(path: Path) -> ObservationPolicy:
    """只有不存在的业务文件采用初值。

    Args:
        path: 此操作定位的明确路径。

    Returns:
        经过完整边界校验的观察策略。

    Note:
        坏JSON或不可读文件明确失败。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ObservationPolicy()
    return ObservationPolicy.model_validate_json(text)


def save_policy(path: Path, policy: ObservationPolicy) -> None:
    """先写同目录临时文件，再原子替换，失败不采用未保存的新偏好。

    Args:
        path: 此操作定位的明确路径。
        policy: 明确的观察间隔、变化门槛与帧节流规则。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="computer-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(policy.model_dump_json(indent=2))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class _Candidate:
    """冻结一帧观察候选及其连接和运行代次。"""

    frame: ComputerFrame
    blocks: tuple[float, ...]
    native_id: str


class ComputerRuntime:
    """插件拥有的电脑生命周期，驱动与命令只在显式连接后启用。

    inputs/evidence为有限宿主端口；preferences为独立业务偏好文件，不恢复
    连接。采集失败和主体失败都暂停交付，明确observe(start)创建新代次。
    stop_inputs撤新采样资格，但不取消实际接管提交；close等资源真正结束。
    """

    def __init__(
        self,
        instance_id: str,
        driver_factory: Callable[[], ComputerDriver],
        inputs: InputPort,
        evidence: EvidencePort,
        policy: ObservationPolicy,
        preferences: Path,
        *,
        screenshot_dir: Path,
        command_base_dir: Path,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """构造只登记所有权，不读取设备或创建后台任务。

        Args:
            instance_id: 本次装配中唯一的插件或宿主实例身份。
            driver_factory: 为当前插件实例创建独立设备驱动的工厂。
            inputs: 正式接管与查询输入的有限端口，不持有主脑或通用存储。
            evidence: 完整原文、媒体及不可变分页服务。
            policy: 明确的观察间隔、变化门槛与帧节流规则。
            preferences: 显式保存的观察偏好文件路径。
            screenshot_dir: 单次截图的文件保存目录，关闭后仍保留已交付文件。
            command_base_dir: 宿主命令默认工作目录，也是相对路径的解析基准。
            monotonic: 本实例使用的单调时钟，不与持久化墙钟混用。
        """
        self.instance_id, self._factory = instance_id, driver_factory
        self._inputs, self._evidence = inputs, evidence
        self.policy, self._preferences = policy, preferences
        self._screenshot_dir = screenshot_dir.absolute()
        self._commands = CommandRunner(command_base_dir)
        self._clock = monotonic
        self._driver: ComputerDriver | None = None
        self._connection: str | None = None
        self._generation = 0
        self._connect_lock, self._capture_lock = asyncio.Lock(), asyncio.Lock()
        self._policy_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._sampler: asyncio.Task[None] | None = None
        self._disconnect_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._owned: set[asyncio.Future[object]] = set()
        self._stopping = False
        self._observing = False
        self._latest: ComputerFrame | None = None
        self._candidate: _Candidate | None = None
        self._inflight: str | None = None
        self._last_analysis = -float("inf")
        self._detector = ChangeDetector()
        self._error: JsonObject | None = None

    async def _own[Result](self, operation: Awaitable[Result]) -> Result:
        """实际接管/文件写入不随等待取消消失，关闭仍持有受理任务。"""
        task = asyncio.ensure_future(operation)
        owned = cast(asyncio.Future[object], task)
        self._owned.add(owned)
        owned.add_done_callback(self._completed)
        return await asyncio.shield(task)

    def _completed(self, task: asyncio.Future[object]) -> None:
        self._owned.discard(task)
        if not task.cancelled():
            task.exception()

    def _require_driver(self) -> ComputerDriver:
        if self._close_task is not None or not self._connection or self._driver is None:
            raise ComputerError("宿主实体机未连接；请先显式调用me.computer.connect()。")
        if not self._driver.alive:
            raise ComputerError("宿主电脑采集进程已断开。")
        return self._driver

    async def command(self, parameters: CommandParameters, deadline: float) -> CallOutcome:
        """在已连接电脑上运行一次宿主 PowerShell 命令。

        Args:
            parameters: 脚本、工作目录、超时和输出预算。
            deadline: 执行服务提供的绝对单调截止时间。

        Returns:
            命令的实际退出、输出或部分未知事实；不会重试或回滚外部效果。

        Raises:
            ComputerError: 电脑尚未连接或已经断开。
            HostStateError: 插件已停止接管新命令。
        """
        self._require_driver()
        if self._stopping:
            raise HostStateError("电脑已停止接管新命令。")
        return await self._commands.run(parameters, deadline)

    async def connect(self) -> JsonObject:
        """并发连接只创建一个驱动，未完成断开不构造第二份资源。

        Raises:
            HostStateError: 电脑已停止新的连接；上次电脑资源尚未确认释放，不能创建另一驱动。
            ComputerError: 连接在启动期间已撤销。
            BaseExceptionGroup: 电脑启动及资源清理失败。
        """
        async with self._connect_lock:
            if self._stopping or self._close_task is not None:
                raise HostStateError("电脑已停止新的连接。")
            if self._disconnect_task is not None:
                await asyncio.shield(self._disconnect_task)
                self._disconnect_task = None
            if self._connection is not None:
                self._require_driver()
                return self.snapshot()
            if self._driver is not None:
                raise HostStateError("上次电脑资源尚未确认释放，不能创建另一驱动。")
            self._generation += 1
            generation = self._generation
            driver = self._factory()
            self._driver = driver
            try:
                await driver.start()
                if generation != self._generation or self._stopping:
                    raise ComputerError("连接在启动期间已撤销。")
            except BaseException as failure:
                try:
                    await driver.close()
                except BaseException as cleanup:
                    raise BaseExceptionGroup(
                        "电脑启动及资源清理失败。", [failure, cleanup]
                    ) from None
                if self._driver is driver:
                    self._driver = None
                raise
            self._connection = uuid4().hex
            self._observing = self.policy.enabled
            self._error = None
            self._sampler = asyncio.create_task(self._run(), name="computer:" + self.instance_id)
            self._sampler.add_done_callback(self._sampler_done)
            return self.snapshot()

    def _sampler_done(self, task: asyncio.Task[None]) -> None:
        """后台失败已暂停交付；保留实际异常供关闭边界报告。"""
        if not task.cancelled() and (error := task.exception()) is not None:
            _LOG.error(
                "电脑持续观察失败。",
                exc_info=(type(error), error, error.__traceback__),
                extra={
                    "plugin_id": self.instance_id,
                    "input_id": self._inflight,
                    "generation": self._generation,
                },
            )

    async def configure(self, policy: ObservationPolicy) -> JsonObject:
        """宿主保存完整偏好，不建立连接。

        Args:
            policy: 明确的观察间隔、变化门槛与帧节流规则。

        Note:
            写入成功后才采用。
        """
        async with self._policy_lock:
            return await self._configure(policy)

    async def _configure(self, policy: ObservationPolicy) -> JsonObject:
        """锁覆盖保存和采用，多个控制请求不能按线程完成顺序覆盖新偏好。"""
        await self._own(asyncio.to_thread(save_policy, self._preferences, policy))
        self.policy = policy
        self._generation += 1
        self._observing = bool(self._connection) and policy.enabled and not self._stopping
        self._candidate = None
        self._inflight = None
        self._latest = None
        self._detector = ChangeDetector()
        self._last_analysis = -float("inf")
        self._error = None
        self._wake.set()
        if self._observing and (self._sampler is None or self._sampler.done()):
            self._sampler = asyncio.create_task(self._run(), name="computer:" + self.instance_id)
            self._sampler.add_done_callback(self._sampler_done)
        return self.snapshot()

    async def observe(self, operation: str) -> JsonObject:
        """开始、暂停或查询当前电脑持续观察。

        Args:
            operation: start、pause 或 status；开始建立新观察代次，查询不读取屏幕。

        Raises:
            ComputerError: operation必须为start、pause或status。
            HostStateError: 电脑已停止新的观察。

        Note:
            明确start建立新观察代次，pause撤采样，status不读屏幕。
        """
        if operation == "status":
            return self.snapshot()
        if operation not in ("start", "pause"):
            raise ComputerError("operation必须为start、pause或status。")
        if operation == "start":
            if self._stopping:
                raise HostStateError("电脑已停止新的观察。")
            self._require_driver()
        policy = self.policy.model_copy(update={"enabled": operation == "start"})
        return await self.configure(policy)

    async def foreground(self) -> JsonObject:
        """返回当次窗口，撤销连接后的迟到身份不能采用。

        Raises:
            ComputerError: 前台读取属于已撤销连接。
        """
        driver, generation = self._require_driver(), self._generation
        foreground = await driver.foreground()
        if generation != self._generation:
            raise ComputerError("前台读取属于已撤销连接。")
        return cast(JsonObject, foreground.model_dump(mode="json"))

    async def capture(
        self, policy: ObservationPolicy | None = None, *, native: bool = False
    ) -> ComputerFrame:
        """采集互斥但断开不取采集锁。

        Args:
            policy: 此次观察显式覆盖的策略；None 使用已保存偏好。
            native: 是否接管完整原生媒体，区别于文字投影。 默认 False。

        Returns:
            当前连接真实取得的完整画面、采集时刻与比较特征。

        Raises:
            ComputerError: 排队截图资格已撤销；截图属于已撤销连接或观察代次。

        Note:
            迟到帧不覆盖当前预览。
        """
        driver, generation = self._require_driver(), self._generation
        async with self._capture_lock:
            if generation != self._generation:
                raise ComputerError("排队截图资格已撤销。")
            frame = await driver.capture(policy or self.policy, native=native)
        if generation != self._generation:
            raise ComputerError("截图属于已撤销连接或观察代次。")
        self._latest = frame
        return frame

    def _source(self, frame: ComputerFrame, kind: str) -> JsonObject:
        return {
            "plugin": self.instance_id,
            "kind": kind,
            "source": "capture://host/" + (self._connection or "") + "/" + frame.target,
            "connection_id": self._connection,
            "generation": self._generation,
            "captured_at": datetime.fromtimestamp(frame.captured_at, UTC).isoformat(),
            "foreground": cast(JsonObject, frame.foreground.model_dump(mode="json")),
            "original_size": list(frame.original_size),
            "delivered_size": list(frame.delivered_size),
        }

    async def screenshot(self, target: str | None, monitor_id: str | None) -> CallOutcome:
        """采集一次原始图像，保存同库媒体及可供显式发送的 JPEG 文件。

        Args:
            target: 可选的明确截图目标，具体支持范围由驱动核验。
            monitor_id: 明确选择的显示器身份；None 使用驱动的默认显示器。

        Returns:
            图像媒体及 evidence_ref；文件保存完成时另有绝对 path、media_type、
            sha256 和 mirror_state。文件失败返回 failed，仍交付已入库图像。

        Raises:
            ComputerError: 未连接、驱动失效或采集期间连接已撤销。
            ValidationError: 截图目标与显示器参数不满足观察策略。
            StorageError: 原图证据接管失败，此时不写入本地文件。

        Note:
            单次原始分辨率截图先接管 BLOB，再保存独立文件，不另投输入。
            已开始的保存不随调用等待取消而撤回，关闭等待文件工作实际结束；
            文件不自动清理，避免后续发送或其他插件仍在引用时被删除。
        """
        policy = ObservationPolicy.model_validate(
            {
                **self.policy.model_dump(),
                "target": self.policy.target if target is None else target,
                "monitor_id": self.policy.monitor_id if monitor_id is None else monitor_id,
            }
        )
        frame = await self.capture(policy, native=True)
        # 采集已经复查连接资格；开始异步保存前固定归因，后续断开不能改写此帧来源。
        source = self._source(frame, "computer.screenshot")
        return await self._own(self._persist_screenshot(frame, source))

    async def _persist_screenshot(self, frame: ComputerFrame, source: JsonObject) -> CallOutcome:
        """同一所有者先固定证据再写文件，取消观察不能留下无人持有的写入。"""
        snapshot = EvidenceSnapshot(
            evidence_id="computer-" + uuid4().hex,
            version=1,
            kind="computer.screenshot",
            text="宿主实体机截图；不是用户发言。",
            source_json=encode_json(source).decode(),
            occurred_at=frame.captured_at,
            media=(
                MediaReference(
                    hashlib.sha256(frame.data).hexdigest(), "image/jpeg", len(frame.data)
                ),
            ),
        )
        committed = await self._evidence.capture(snapshot, (MediaAsset(frame.data, "image/jpeg"),))
        value: JsonObject = {
            **source,
            "evidence_ref": committed.evidence_id,
            "media_type": "image/jpeg",
            "sha256": committed.media[0].media_id,
        }
        # 权威媒体先完成接管；磁盘失败不能抹掉已取得的图像或伪造可发送路径。
        try:
            path = await asyncio.to_thread(_write_screenshot, self._screenshot_dir, frame.data)
        except OSError as error:
            value["mirror_state"] = "failed"
            return CallOutcome(
                "failed", value, type(error).__name__, str(error), media=committed.media
            )
        value.update({"path": str(path), "mirror_state": "saved"})
        return CallOutcome("confirmed", value, media=committed.media)

    def _pause_failure(self, error: BaseException, *, input_id: str | None = None) -> None:
        """故障是暂停资格，不伪造成功确认或丢弃已入账原文。"""
        self._observing = False
        self._generation += 1
        self._error = {
            "error_type": type(error).__name__,
            "message": str(error),
            "input_id": input_id,
        }

    async def step(self) -> bool:
        """采样任务的一次有限步骤。

        Raises:
            RuntimeError: 已接管电脑输入缺少实际处理状态。

        Note:
            先核实际处理状态，再替换唯一候选。
        """
        if not self._observing or self._stopping:
            return False
        if self._inflight is not None:
            inflight, generation = self._inflight, self._generation
            processing = await self._inputs.query_processing(inflight)
            if generation != self._generation or inflight != self._inflight:
                return False
            if processing is None:
                raise RuntimeError("已接管电脑输入缺少实际处理状态。")
            if processing.stage == "failed":
                self._pause_failure(ComputerError(processing.reason), input_id=self._inflight)
                return False
            if processing.stage == "processed":
                self._inflight = None
                self._last_analysis = self._clock()

        generation = self._generation
        frame = await self.capture()
        changed, blocks = await asyncio.to_thread(self._detector.sample, frame, self.policy)
        if generation != self._generation or not self._observing or self._stopping:
            return False
        if changed:
            self._candidate = _Candidate(frame, blocks, str(uuid4()))
        candidate = self._candidate
        if (
            candidate is None
            or self._inflight is not None
            or self._clock() - self._last_analysis < self.policy.analysis_interval
        ):
            return changed
        source = self._source(candidate.frame, "computer.observation")
        submission = InputSubmission(
            InputSource(
                self.instance_id,
                "host",
                self._connection or "",
                "computer.observation",
                candidate.native_id,
            ),
            "宿主实体机屏幕观察；不是用户发言。结合正常历史记录变化，无需回应时保持安静。\n"
            + encode_json(source).decode(),
            datetime.fromtimestamp(candidate.frame.captured_at, UTC),
            datetime.now(UTC),
            media=(MediaAsset(candidate.frame.data, "image/jpeg"),),
            # 实时观察只让通用缓冲到期；不取消生成，也不撤销已返回的行动。
            context=InputContext(minimum_wake=True),
        )
        admitted = await self._own(self._inputs.submit(submission))
        if generation != self._generation or not self._observing or self._stopping:
            return changed
        self._inflight = admitted.input_id
        self._last_analysis = self._clock()
        self._detector.committed(candidate.blocks)
        if self._candidate is candidate:
            self._candidate = None
        return changed

    async def _run(self) -> None:
        """取得/接管失败暂停且原样保留，不周期制造新的失败输入。"""
        interval = self.policy.min_interval
        while self._connection is not None and not self._stopping:
            self._wake.clear()
            generation = self._generation
            try:
                if self._observing:
                    changed = await self.step()
                    interval = (
                        self.policy.min_interval
                        if changed
                        else min(self.policy.max_interval, interval * 1.4)
                    )
                else:
                    interval = self.policy.max_interval
            except BaseException as error:
                if isinstance(error, ComputerError) and generation != self._generation:
                    # 明确控制撤销的迟到采集不能覆盖新代次，也不是新采集故障。
                    continue
                self._pause_failure(error, input_id=self._inflight)
                raise
            try:
                await asyncio.wait_for(self._wake.wait(), interval)
            except TimeoutError:
                pass

    def preview(self) -> ComputerFrame | None:
        """只读当前连接真实缓存。

        Returns:
            当前连接真实取得的完整画面、采集时刻与比较特征。 未取得对应记录或素材时返回 None；
            读取故障仍抛出。

        Note:
            UI读取此值不触发采样或保存候选。
        """
        return self._latest

    def snapshot(self) -> JsonObject:
        """展示真实预览身份、接管/处理等待和错误，不带编码像素或解释字段。

        Returns:
            业务对象，按实际结果提供 connected、connection_id、observing、generation、policy、
            foreground、captured_at、original_size、delivered_size、input_id 等 字段。
        """
        frame = self._latest
        return {
            "connected": bool(self._connection) and self._driver is not None and self._driver.alive,
            "connection_id": self._connection,
            "observing": self._observing,
            "generation": self._generation,
            "policy": cast(JsonObject, self.policy.model_dump(mode="json")),
            "foreground": None
            if frame is None
            else cast(JsonObject, frame.foreground.model_dump(mode="json")),
            "captured_at": None if frame is None else frame.captured_at,
            "original_size": None if frame is None else list(frame.original_size),
            "delivered_size": None if frame is None else list(frame.delivered_size),
            "input_id": self._inflight,
            "candidate": self._candidate is not None,
            "error": self._error,
        }

    async def prepare_request(self) -> JsonObject:
        """给主体连接、观察及最新窗口语境，不把驱动参数和进程句柄当作认知材料。

        Returns:
            业务对象，按实际结果提供 connected、observing、last_window_title、captured_at、
            pending_input_id、error 字段。

        Note:
            截图仍由正式输入交付；标题和采集时间只说明最后观察到什么窗口，不
            冒充当前画面或重新采集。完整尺寸、策略和连接代次留在 snapshot。
        """
        state = self.snapshot()
        frame = self._latest
        return {
            "connected": state["connected"],
            "observing": self._observing,
            "last_window_title": None if frame is None else frame.foreground.title,
            "captured_at": None if frame is None else frame.captured_at,
            "pending_input_id": self._inflight,
            "error": self._error,
        }

    async def stop_inputs(self) -> None:
        """撤新采样，不撤销实际接管提交，观察者取消仍等待同一收尾。"""
        self._stopping, self._observing = True, False
        self._generation += 1
        self._wake.set()
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop(), name="computer-stop")
        await asyncio.shield(self._stop_task)

    async def _stop(self) -> None:
        if self._sampler is not None:
            await self._sampler

    async def disconnect(self) -> JsonObject:
        """先撤连接代次，再让驱动结束阻塞采集。

        Note:
            终止本插件在途命令的进程树；不关闭其他电脑应用。
        """
        self._connection, self._observing = None, False
        self._generation += 1
        self._latest, self._candidate = None, None
        self._wake.set()
        if self._disconnect_task is None:
            self._disconnect_task = asyncio.create_task(
                self._disconnect(), name="computer-disconnect"
            )
        await asyncio.shield(self._disconnect_task)
        return self.snapshot()

    async def _disconnect(self) -> None:
        """命令和采集都必须收回；一方失败仍释放另一方，失败不清空占用引用。"""
        failures: list[BaseException] = []
        try:
            await self._commands.stop()
        except BaseException as error:
            failures.append(error)
        driver = self._driver
        try:
            if driver is not None:
                await driver.close()
        except BaseException as error:
            failures.append(error)
        else:
            if self._driver is driver:
                self._driver = None
        if self._sampler is not None:
            await asyncio.gather(self._sampler, return_exceptions=True)
        if failures:
            raise BaseExceptionGroup("电脑命令或采集资源收尾失败。", failures)

    async def close(self) -> None:
        """实际借用结束后的总资源清理，不关闭共享宿主Store。"""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="computer-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        """清理边界聚合原失败，仍继续释放驱动及所有已受理提交。"""
        failures: list[BaseException] = []
        try:
            await self.stop_inputs()
        except BaseException as error:
            failures.append(error)
        try:
            await self.disconnect()
        except BaseException as error:
            failures.append(error)
        if self._owned:
            results = await asyncio.gather(*tuple(self._owned), return_exceptions=True)
            failures.extend(result for result in results if isinstance(result, BaseException))
        if failures:
            raise BaseExceptionGroup("电脑资源收尾失败。", failures)
