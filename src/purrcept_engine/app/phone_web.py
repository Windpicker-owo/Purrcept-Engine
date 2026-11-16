"""本机 WebUI 的 Phone 设置与配对路由，不启动第二个 Engine。

CLI 固定配置路径与当前数据目录，浏览器不能指定本机文件。预览计划只在当前
WebServer 内存保存；保存沿用宿主已经持有的运行锁，只影响下次启动。配对查询
直接借用当前插件，邀请不进入普通快照、模型能力或浏览器存储。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from purrcept_engine.errors import ConfigError, HostStateError
from purrcept_engine.plugin_system.panels import PairingOperation
from purrcept_engine.values import JsonObject, decode_object, encode_json


class PhoneWebHost(Protocol):
    """WebServer 借用的当前宿主；插件拥有配对窗口，WebUI 只做显式操作。"""

    def query_health(self) -> JsonObject:
        """读取生命周期，只有运行中的宿主能借出自己的配置写入资格。"""
        ...

    async def phone_pairing(
        self,
        target: str,
        *,
        operation: PairingOperation = "read",
        expected_device_id: str | None = None,
    ) -> JsonObject:
        """读取目标窗口，或处理已验证本机令牌的明确开启/重新配对。

        replace 必须核对用户确认的旧设备身份，不能撤销另一页面刚建立的新配对。
        """
        ...


def phone_routes(
    host: PhoneWebHost, config_path: Path | None, owner_data_dir: Path | None
) -> list[Route]:
    """为同一个本机来源边界创建路由；不自行监听或开启配对。

    Args:
        host: 当前 WebServer 借用的宿主。
        config_path: CLI 实际读取的配置；嵌入者不提供时只允许已有插件配对。
        owner_data_dir: 当前宿主拥有运行锁的数据目录，不能取自浏览器参数。

    Returns:
        由主 WebUI 的 LocalHttpBoundary 统一保护的有限路由。
    """
    # 可选 Web extra 提供编辑和 QR 依赖，普通宿主模块不隐式导入它们。
    from purrcept_engine.app.config import load_config
    from purrcept_engine.app.phone_setup import PhoneSetupPlan, apply, candidate_addresses, prepare
    from purrcept_engine.plugins.phone.plugin import PhoneSettings

    plan: PhoneSetupPlan | None = None
    preview_id: str | None = None
    saving = asyncio.Lock()

    async def setup(request: Request) -> JSONResponse:
        """读取公开设置或预览/提交下次启动的 Phone 配置，禁止任意文件路径。"""
        nonlocal plan, preview_id
        if config_path is None or owner_data_dir is None:
            return _json(
                {"available": False, "message": "此嵌入式宿主未提供配置文件；已有 Phone 仍可配对。"}
            )
        try:
            if request.method == "GET":
                from urllib.parse import urlsplit

                config = await asyncio.to_thread(load_config, config_path)
                plugin = next((item for item in config.plugins if item.entry == "phone"), None)
                settings = (
                    None if plugin is None else PhoneSettings.model_validate(plugin.configuration())
                )
                port = 8766 if settings is None else settings.port
                if config.web.enabled and port == config.web.port:
                    port = 8767 if port == 8766 else 8766
                addresses = await asyncio.to_thread(candidate_addresses)
                return _json(
                    {
                        "available": True,
                        "config_name": config_path.name,
                        "address": ""
                        if settings is None
                        else urlsplit(settings.public_url).hostname,
                        "port": port,
                        "addresses": list(addresses),
                        "configured": plugin is not None,
                    }
                )
            if host.query_health().get("phase") != "running":
                raise HostStateError("Engine 尚未运行，暂时不能保存手机设置。")
            content = bytearray()
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content) > 4096:
                    return _json({"message": "设置请求过大。"}, 413)
            value = decode_object(bytes(content))
            async with saving:
                if value.get("action") == "preview":
                    address, port = value.get("address"), value.get("port")
                    if not isinstance(address, str) or type(port) is not int:
                        return _json({"message": "请填写电脑地址和端口。"}, 400)
                    plan = None
                    preview_id = None
                    prepared = await asyncio.to_thread(prepare, config_path, address, port)
                    plan, preview_id = prepared, uuid4().hex
                    return _json(
                        {
                            "preview_id": preview_id,
                            "url": prepared.public_url,
                            "create_identity": prepared.create_identity,
                        }
                    )
                if (
                    value.get("action") != "save"
                    or plan is None
                    or value.get("preview_id") != preview_id
                ):
                    return _json({"message": "预览已失效，请重新查看改动。"}, 409)
                current = plan
                plan, preview_id = None, None
                backup = await asyncio.to_thread(apply, current, owner_data_dir=owner_data_dir)
                return _json({"saved": True, "restart_required": True, "backup_name": backup.name})
        except ConfigError as error:
            return _json({"message": str(error)}, 409)
        except OSError:
            return _json({"message": "无法读写手机设置，请检查配置目录权限和可用空间。"}, 409)
        except ValueError:
            # Pydantic 和 JSON 错误可能包含原始配置，不得回显模型凭据。
            return _json({"message": "配置或设置字段无效，原文件保持不变。"}, 400)

    async def pairing(request: Request) -> JSONResponse:
        """借用当前监听器并编码真实邀请；查询不续期，写请求才开启新窗口。"""
        operation: PairingOperation = "read"
        expected_device_id: str | None = None
        if request.method == "POST":
            operation = "open"
            if request.url.path.endswith("/replace"):
                operation = "replace"
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 4096:
                        return _json({"message": "重新配对请求过大。"}, 413)
                try:
                    value = decode_object(bytes(body))
                except ValueError:
                    return _json({"message": "请先读取当前配对，再确认重新配对。"}, 400)
                identity = value.get("device_id")
                if not isinstance(identity, str) or not 1 <= len(identity) <= 128:
                    return _json({"message": "请先读取当前配对，再确认重新配对。"}, 400)
                expected_device_id = identity
        value = await host.phone_pairing(
            request.path_params["target"],
            operation=operation,
            expected_device_id=expected_device_id,
        )
        if value.get("status") != "ready":
            return _json(value)
        from qrcode import QRCode
        from qrcode.constants import ERROR_CORRECT_M

        payload = {key: item for key, item in value.items() if key != "status"}
        qr = QRCode(error_correction=ERROR_CORRECT_M, border=4)
        qr.add_data(encode_json(payload))
        qr.make(fit=True)
        matrix = qr.get_matrix()
        # 只交付整数格路径，React 画 SVG，不注入服务端 HTML 或在 DOM 回显邀请原文。
        path = "".join(
            f"M{x} {y}h1v1h-1z"
            for y, row in enumerate(matrix)
            for x, filled in enumerate(row)
            if filled
        )
        return _json(
            {
                "status": "ready",
                "url": value["url"],
                "certificate_sha256": value["certificate_sha256"],
                "expires_at": value["expires_at"],
                "qr_size": len(matrix),
                "qr_path": path,
            }
        )

    return [
        Route("/api/phone/setup", setup, methods=["GET", "POST"]),
        Route("/api/phone/{target}/pairing", pairing, methods=["GET", "POST"]),
        Route("/api/phone/{target}/pairing/replace", pairing, methods=["POST"]),
    ]


def _json(value: JsonObject, status: int = 200) -> JSONResponse:
    """配对专用响应禁止缓存，避免后退或刷新复现已失效的短时邀请。"""
    return JSONResponse(
        value, status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    )
