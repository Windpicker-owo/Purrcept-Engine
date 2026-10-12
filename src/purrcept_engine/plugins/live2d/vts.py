"""VTube Studio 的单连接请求/认证，不拥有音频或身体运动状态。

一个连接只有一个在途请求，以 requestID 匹配真实响应。认证拒绝需要
明确重新连接，不自动反复弹窗；传输失败不补发旧姿态。令牌只在本机
显式路径读取/保存，不能进入模型目录或状态快照。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from purrcept_engine.execution.protocol import (
    ActionProtocolError,
    JsonObject,
    decode_object,
    encode_json,
)

from .models import BodyError, ModelProfile


class BodyDriver(Protocol):
    """Live2DRuntime 创建的独立视觉连接，内置 VTSDriver。

    connect 获取认证并核验当前模型；send 对一帧取得真实协议响应，不能
    把本地接受帧当确认。close 不释放音频设备。接口串行使用，异常由
    身体所有者决定离线并报告；没有缓存旧帧或自动效果重试。
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
    """一个 VTS socket 的所有者；纯构造不加载 websockets 或打开设备。"""

    def __init__(self, url: str, token_path: Path, profile: ModelProfile) -> None:
        """冻结显式端点、令牌与人工校准，不猜测模型。"""
        from websockets.asyncio.client import ClientConnection

        self._socket: ClientConnection | None = None
        self._url, self._token_path, self._profile = url, token_path, profile
        self._lock = asyncio.Lock()

    async def _request(
        self, kind: str, data: JsonObject, *, deadline_seconds: float = 2.0
    ) -> JsonObject:
        """通知不充当响应；无限通知流仍受同一请求期限约束。"""
        socket = self._socket
        if socket is None:
            raise BodyError("VTS 尚未连接。")
        identity = uuid4().hex
        async with self._lock, asyncio.timeout(deadline_seconds):
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

    async def connect(self) -> None:
        """取得令牌后真实认证，核验模型再登记本插件输入。

        认证第一次可能显示 VTS 原生对话框，只在明确 connect 内执行。
        部分失败仍由本驱动清理 socket，不能失去已经取得的资源引用。
        """
        from websockets.asyncio.client import connect
        from websockets.exceptions import WebSocketException

        try:
            self._socket = await connect(
                self._url, open_timeout=3, close_timeout=1, max_size=1_048_576
            )
            identity: JsonObject = {"pluginName": "Purrcept Live2D", "pluginDeveloper": "Purrcept"}
            try:
                token = await asyncio.to_thread(self._token_path.read_text, encoding="utf-8")
            except FileNotFoundError:
                response = await self._request(
                    "AuthenticationTokenRequest", identity, deadline_seconds=120
                )
                value = response.get("authenticationToken")
                if not isinstance(value, str) or not value:
                    raise BodyError("VTS 未授予认证令牌。") from None
                token = value
                await asyncio.to_thread(self._save_token, token)
            response = await self._request(
                "AuthenticationRequest", {**identity, "authenticationToken": token.strip()}
            )
            if response.get("authenticated") is not True:
                raise BodyError("VTS 令牌被拒绝，需要明确重新授权。")
            model = await self._request("CurrentModelRequest", {})
            if (
                model.get("modelLoaded") is not True
                or model.get("modelID") != self._profile.model_id
            ):
                raise BodyError("VTS 当前模型与校准身份不符。")
            existing = await self._request("InputParameterListRequest", {})
            custom = existing.get("customParameters")
            if not isinstance(custom, list):
                raise BodyError("VTS 输入目录格式无效。")
            names: set[str] = set()
            for item in custom:
                if isinstance(item, dict) and isinstance(name := item.get("name"), str):
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
        except (OSError, TimeoutError, WebSocketException) as error:
            raise BodyError("VTS 连接/认证传输失败。") from error

    def _save_token(self, token: str) -> None:
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(token, encoding="utf-8")

    async def send(self, channels: dict[str, float]) -> None:
        """真实 VTS 注入响应确认视觉采用，不向另一模型静默输出。"""
        from websockets.exceptions import WebSocketException

        try:
            model = await self._request("CurrentModelRequest", {})
            if (
                model.get("modelLoaded") is not True
                or model.get("modelID") != self._profile.model_id
            ):
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
        except (OSError, TimeoutError, WebSocketException) as error:
            raise BodyError("VTS 帧提交传输失败。") from error

    async def close(self) -> None:
        """回收实际 socket 后清空引用；失败时仍保留所有权。"""
        socket = self._socket
        if socket is not None:
            await socket.close()
            if self._socket is socket:
                self._socket = None
