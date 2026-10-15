"""VTube Studio 的单连接请求/认证，不拥有音频或身体运动状态。

一个连接只有一个在途请求，以 requestID 匹配真实响应。认证拒绝需要
明确重新连接，不自动反复弹窗；暂时传输失败交给身体所有者有限恢复，
本驱动不重发旧请求。令牌只在本机显式路径读取/保存，不能进入模型
目录或状态快照；取消等待仍保留线程所有权，close 等待真实 IO 归还。
"""

from __future__ import annotations

import asyncio
import ssl
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from purrcept_engine.plugins.live2d.models import BodyError, BodyUnavailable, ModelProfile
from purrcept_engine.values import ActionProtocolError, JsonObject, decode_object, encode_json


class BodyDriver(Protocol):
    """Live2DRuntime 创建的独立视觉连接，内置 VTSDriver。

    connect 获取认证并核验当前模型；send 对一帧取得真实协议响应，不能
    把本地接受帧当确认。close 不释放音频设备。接口串行使用，异常由
    身体所有者决定离线并报告。BodyUnavailable 只表示可有限恢复的传输
    故障，其他 BodyError 终止恢复；内部错误和取消原样传播。没有缓存
    旧帧或自动效果重试，close 成功意味着 socket 与令牌 IO 均已归还。
    实现可提供只读 resources_released 明证：旧 IO 错误仍需报告，但若
    所有实际资源已归还，身体所有者可据此释放引用；缺失明证的失败保持未知。
    """

    async def connect(self) -> None:
        """连接、认证和校准成功后才返回。"""
        ...

    async def send(self, channels: dict[str, float]) -> None:
        """提交当下最新帧并取得匹配回执。"""
        ...

    async def close(self) -> None:
        """释放视觉连接，不关闭 VTS 应用或声音。"""
        ...


class VTSDriver:
    """一个 VTS socket 的所有者；纯构造不打开设备或建立连接。"""

    def __init__(self, url: str, token_path: Path, profile: ModelProfile) -> None:
        """冻结显式端点、令牌与人工校准，不猜测模型。"""
        from websockets.asyncio.client import ClientConnection

        self._socket: ClientConnection | None = None
        self._url, self._token_path, self._profile = url, token_path, profile
        self._lock = asyncio.Lock()
        self._token_read_task: asyncio.Task[str | None] | None = None
        self._token_write_task: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[None] | None = None

    async def _request(
        self, kind: str, data: JsonObject, *, deadline_seconds: float = 2.0
    ) -> JsonObject:
        """通知不充当响应；无限通知流仍受同一请求期限约束。"""
        from websockets.exceptions import ConnectionClosed, PayloadTooBig, ProtocolError

        socket = self._socket
        if socket is None:
            raise BodyUnavailable("VTS 尚未连接。")
        identity = uuid4().hex
        try:
            async with asyncio.timeout(deadline_seconds), self._lock:
                await socket.send(
                    encode_json(
                        {
                            "apiName": "VTubeStudioPublicAPI",
                            "apiVersion": "1.0",
                            "requestID": identity,
                            "messageType": kind,
                            "data": data,
                        }
                    ).decode()
                )
                while True:
                    raw = await socket.recv()
                    try:
                        response = decode_object(raw.encode() if isinstance(raw, str) else raw)
                    except ActionProtocolError as error:
                        raise BodyError("VTS响应不是合法JSON协议。") from error
                    if response.get("requestID") != identity:
                        continue
                    payload = response.get("data")
                    if not isinstance(payload, dict):
                        raise BodyError("VTS 响应缺少 data。")
                    if response.get("messageType") == "APIError":
                        raise BodyError("VTS API 明确拒绝请求。")
                    if response.get("messageType") != kind.removesuffix("Request") + "Response":
                        raise BodyError("VTS 响应类型与请求不符。")
                    return payload
        except ConnectionClosed as error:
            # Compatibility: websockets 将协议拒绝也报告为连接关闭，不能把
            # 明确的协议/权限关闭误判为临时断线并再次连接。
            rejected_codes = {1002, 1003, 1007, 1008, 1009, 1010}
            frames = (error.rcvd, error.sent)
            if any(frame is not None and frame.code in rejected_codes for frame in frames):
                raise BodyError("VTS WebSocket 协议或权限拒绝连接。") from error
            raise BodyUnavailable("VTS 连接已断开。") from error
        except (PayloadTooBig, ProtocolError, ssl.SSLError) as error:
            raise BodyError("VTS WebSocket 协议无效。") from error
        except (OSError, TimeoutError) as error:
            raise BodyUnavailable("VTS 请求传输暂时不可用。") from error

    async def connect(self) -> None:
        """取得令牌后真实认证，核验模型再登记本插件输入。

        首次认证可能显示 VTS 原生对话框，最多等待 120 秒；授权流程的
        断线或超时终止当前恢复轮次，不能自动重复弹窗。部分失败仍持有
        已取得的 socket 和令牌线程，由身体所有者调用 close 清理。
        """
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidHandshake, InvalidProxy, InvalidURI

        if self._socket is not None or self._closing is not None:
            raise RuntimeError("VTS 驱动只允许一次连接，恢复需要新的驱动。")
        try:
            # Security: VTS 使用配置中明确的本机端点，不借进程代理改变
            # 令牌及姿态请求的实际接收方。
            self._socket = await connect(
                self._url, open_timeout=3, close_timeout=1, max_size=1_048_576, proxy=None
            )
        except (InvalidHandshake, InvalidProxy, InvalidURI, ssl.SSLError) as error:
            raise BodyError("VTS WebSocket 端点或握手被拒绝。") from error
        except (OSError, TimeoutError) as error:
            raise BodyUnavailable("VTS 连接暂时不可用。") from error

        identity: JsonObject = {"pluginName": "Purrcept Live2D", "pluginDeveloper": "Purrcept"}
        self._token_read_task = asyncio.create_task(self._read_token())
        try:
            token = await asyncio.shield(self._token_read_task)
        except BodyError:
            self._token_read_task = None
            raise
        else:
            self._token_read_task = None

        if token is None:
            try:
                response = await self._request(
                    "AuthenticationTokenRequest", identity, deadline_seconds=120
                )
            except BodyUnavailable as error:
                raise BodyError("VTS 原生授权未完成，需要明确重新授权。") from error
            value = response.get("authenticationToken")
            if not isinstance(value, str) or not value.strip():
                raise BodyError("VTS 未授予认证令牌。")
            token = value
            self._token_write_task = asyncio.create_task(self._write_token(token))
            try:
                await asyncio.shield(self._token_write_task)
            except BodyError:
                self._token_write_task = None
                raise
            else:
                self._token_write_task = None

        if not token.strip():
            raise BodyError("VTS 本地认证令牌为空，需要明确重新授权。")
        response = await self._request(
            "AuthenticationRequest", {**identity, "authenticationToken": token.strip()}
        )
        if response.get("authenticated") is not True:
            raise BodyError("VTS 令牌被拒绝，需要明确重新授权。")
        model = await self._request("CurrentModelRequest", {})
        if model.get("modelLoaded") is not True or model.get("modelID") != self._profile.model_id:
            raise BodyError("VTS 当前模型与校准身份不符。")
        existing = await self._request("InputParameterListRequest", {})
        custom = existing.get("customParameters")
        if not isinstance(custom, list):
            raise BodyError("VTS 输入目录格式无效。")
        names: set[str] = set()
        for item in custom:
            if (
                not isinstance(item, dict)
                or not isinstance(name := item.get("name"), str)
                or not name
            ):
                raise BodyError("VTS 自定义输入目录项无效。")
            names.add(name)
        for binding in self._profile.bindings:
            if binding.parameter not in names:
                await self._request(
                    "ParameterCreationRequest",
                    {
                        "parameterName": binding.parameter,
                        "min": binding.minimum,
                        "max": binding.maximum,
                        "defaultValue": binding.neutral
                        if binding.default_value is None
                        else binding.default_value,
                        "explanation": "Purrcept calibrated body input",
                    },
                )

    async def _read_token(self) -> str | None:
        """不存在才进入首次授权；权限、编码或文件错误不能当作断线恢复。"""
        try:
            return await asyncio.to_thread(self._token_path.read_text, encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as error:
            raise BodyError("VTS 本地令牌无法读取。") from error

    async def _write_token(self, token: str) -> None:
        """令牌保存失败明确终止恢复，不循环原生授权。"""
        try:
            await asyncio.to_thread(self._save_token, token)
        except OSError as error:
            raise BodyError("VTS 本地令牌无法保存。") from error

    def _save_token(self, token: str) -> None:
        """新授权令牌只写入配置路径，文件失败由调用线程转换并报告。"""
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(token, encoding="utf-8")

    async def send(self, channels: dict[str, float]) -> None:
        """真实 VTS 注入响应确认视觉采用，不向另一模型静默输出。"""
        model = await self._request("CurrentModelRequest", {})
        if model.get("modelLoaded") is not True or model.get("modelID") != self._profile.model_id:
            raise BodyError("VTS 当前模型已变化。")
        await self._request(
            "InjectParameterDataRequest",
            {
                "faceFound": True,
                "mode": "set",
                "parameterValues": [
                    {"id": binding.parameter, "value": binding.map(channels)}
                    for binding in self._profile.bindings
                ],
            },
        )

    async def close(self) -> None:
        """独立收尾任务等待令牌 IO 与 socket；取消观察不撤销真实清理。"""
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        await asyncio.shield(self._closing)

    @property
    def resources_released(self) -> bool:
        """只证明真实归还；迟到的旧 IO 错误仍由 close 报告。

        清理任务未结束、socket 关闭失败或令牌线程未归还时都为 False。
        调用方不能只因存在异常就推断资源仍在使用，也不能提前释放引用。
        """
        return (
            self._closing is not None
            and self._closing.done()
            and self._socket is None
            and self._token_read_task is None
            and self._token_write_task is None
        )

    async def _close(self) -> None:
        """每项资源独立回收，某项失败不跳过另一个实际资源。"""
        failures: list[BaseException] = []
        tasks = (self._token_read_task, self._token_write_task)
        for task in tasks:
            if task is not None:
                result = await asyncio.gather(task, return_exceptions=True)
                if isinstance(result[0], BaseException):
                    failures.append(result[0])
        self._token_read_task = self._token_write_task = None

        socket = self._socket
        if socket is not None:
            try:
                await socket.close()
            except BaseException as error:
                failures.append(error)
            else:
                if self._socket is socket:
                    self._socket = None
        if failures:
            raise BaseExceptionGroup("VTS 资源收尾失败。", failures)
