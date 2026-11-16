"""声明实体手机与聊天能力，借用同库端口后构建唯一 Phone Runtime。

目录在 prepare 固定；离线和权限变化通过动态状态表达，不改变工具 schema。
所有模型设备调用通过现有执行器取得 CallContext，再跨网络派发给 Android。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from purrcept_engine.errors import ConfigError
from purrcept_engine.plugin_system import (
    CallContext,
    CallOutcome,
    CapabilityDeclaration,
    FunctionCapability,
    ObjectDeclaration,
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
    typed_factory,
)
from purrcept_engine.plugin_system.panels import DevicePanel, PhonePairing
from purrcept_engine.plugins.phone.protocol import validate_text
from purrcept_engine.plugins.phone.runtime import PhoneRuntime
from purrcept_engine.values import JsonObject, decode_object


class PhoneSettings(BaseModel):
    """显式设备监听和本地邀请路径；网络入口始终使用 TLS。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    listen_host: str = "127.0.0.1"
    port: int = Field(default=8766, ge=1, le=65535)
    public_url: str
    certificate: Path = Field(strict=False)
    private_key: Path = Field(strict=False)
    pairing_enabled: bool = Field(
        default=False, description="启动时自动开启两分钟邀请；本机 WebUI 仍可显式开启。"
    )
    invitation_file: Path = Field(default=Path(".purrcept/phone-invitation.json"), strict=False)

    @model_validator(mode="after")
    def validate_url(self) -> PhoneSettings:
        """邀请只包含明确 HTTPS 地址，不夹带凭据、查询或路径。"""
        url = urlsplit(self.public_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ValueError("public_url 必须是无路径和凭据的 HTTPS 地址。")
        return self


class _Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Empty(_Parameters):
    pass


class _Send(_Parameters):
    text: str = Field(min_length=1, max_length=32768)

    @field_validator("text")
    @classmethod
    def bounded_text(cls, value: str) -> str:
        """入队前验证出站信封能够交付，不能留下永久阻塞补送的超大消息。"""
        return validate_text(value)


class _History(_Parameters):
    after: int = Field(default=0, ge=0)


class _Package(_Parameters):
    package: str = Field(min_length=1, max_length=255)


class _Target(_Parameters):
    frame_id: str = Field(min_length=1, max_length=128)
    element_id: int = Field(ge=1, le=300)


class _TextInput(_Target):
    text: str = Field(max_length=32768)


class _Swipe(_Parameters):
    frame_id: str = Field(min_length=1, max_length=128)
    direction: Literal["up", "down"]


class _Key(_Parameters):
    key: Literal["back", "home", "recents"]


def _declare(instance_id: str, settings: PhoneSettings, base_dir: Path) -> PluginBlueprint:
    """解析资源位置并固定能力，不在声明阶段读取证书或连接手机。"""
    settings = settings.model_copy(
        update={
            name: path if path.is_absolute() else base_dir / path
            for name, path in (
                ("certificate", settings.certificate),
                ("private_key", settings.private_key),
                ("invitation_file", settings.invitation_file),
            )
        }
    )
    declarations = (
        CapabilityDeclaration(
            instance_id + ":status",
            "phone",
            "status",
            "查询手机网络、操作会话和权限状态，不采集。",
            _Empty,
            5.0,
            "await me.phone.status()",
        ),
        CapabilityDeclaration(
            instance_id + ":connect",
            "phone",
            "connect",
            "在手机用户授权范围内显式建立操作会话，不开启连续观察。",
            _Empty,
            20.0,
            "await me.phone.connect()",
        ),
        CapabilityDeclaration(
            instance_id + ":disconnect",
            "phone",
            "disconnect",
            "撤销手机操作会话，保留聊天连接。",
            _Empty,
            10.0,
            "await me.phone.disconnect()",
            control=True,
        ),
        CapabilityDeclaration(
            instance_id + ":send",
            instance_id + ":chat",
            "send",
            "持久接管向手机发送的文字；queued 表示已保存待投递，不表示手机已收到。",
            _Send,
            10.0,
            'await me.phone.chat.send(text="你好")',
        ),
        CapabilityDeclaration(
            instance_id + ":history",
            instance_id + ":chat",
            "history",
            "读取手机聊天的一页实际记录及序号。",
            _History,
            10.0,
            "await me.phone.chat.history()",
        ),
    )

    command_specs: tuple[tuple[str, type[BaseModel], str, str], ...] = (
        ("apps", _Empty, "读取手机可启动应用。", "await me.phone.apps()"),
        (
            "open_app",
            _Package,
            "请求打开指定手机应用，不保证应用已加载完成。",
            'await me.phone.open_app(package="org.example.app")',
        ),
        ("foreground", _Empty, "读取当前前台包名，不截图。", "await me.phone.foreground()"),
        (
            "screen",
            _Empty,
            "读取界面结构和新的 frame_id；窗口、内容变化或已接受的操作会使旧快照失效。",
            "await me.phone.screen()",
        ),
        (
            "screenshot",
            _Empty,
            "取得完整手机截图并作为原生图片证据交付。",
            "await me.phone.screenshot()",
        ),
        (
            "tap",
            _Target,
            "点击有效快照的元素，手势完成不代表应用业务成功。",
            'await me.phone.tap(frame_id="快照身份", element_id=1)',
        ),
        (
            "input_text",
            _TextInput,
            "设置有效快照中可编辑元素的文字，不读取密码。",
            'await me.phone.input_text(frame_id="快照身份", element_id=1, text="你好")',
        ),
        (
            "swipe",
            _Swipe,
            "在有效快照对应窗口上下滑动。",
            'await me.phone.swipe(frame_id="快照身份", direction="up")',
        ),
        (
            "key",
            _Key,
            "发送一次 Android 返回、主页或最近任务键。",
            'await me.phone.key(key="back")',
        ),
    )
    device_declarations = tuple(
        CapabilityDeclaration(
            instance_id + ":" + name,
            "phone",
            name,
            description,
            parameters,
            30.0,
            example,
            "未知结果禁止重放；每次已接受的界面操作后重新 screen。"
            "stale_frame 时按新元素重新决定，禁止原样重试。",
            concurrency="target_serial",
        )
        for name, parameters, description, example in command_specs
    )

    async def build(ports: PluginPorts) -> PluginRuntime:
        """资源只在 build/start 取得，部分启动失败由同一所有者回收。"""
        from purrcept_engine.plugins.phone.transport import PhoneServer

        if ports.phone is None:
            raise ConfigError("phone 插件需要宿主手机业务端口。")
        runtime = PhoneRuntime(
            instance_id,
            ports.inputs,
            ports.phone,
            ports.late_results,
            ports.evidence,
            subject_name=ports.subject_name,
            host_snapshot=ports.snapshot,
        )
        server = PhoneServer(runtime, settings)

        async def status(parameters: _Empty, context: CallContext) -> CallOutcome:
            return CallOutcome("confirmed", runtime.snapshot())

        async def connect(parameters: _Empty, context: CallContext) -> CallOutcome:
            return await runtime.command("connect", {}, context)

        async def disconnect(parameters: _Empty, context: CallContext) -> CallOutcome:
            try:
                return await runtime.command("disconnect", {}, context)
            finally:
                runtime.control_session = None

        def bind_device(
            declaration: CapabilityDeclaration[BaseModel],
        ) -> FunctionCapability[BaseModel]:
            """将冻结 schema 绑定到一次网络操作，模型不接收任意参数字典。"""

            async def invoke(parameters: BaseModel, context: CallContext) -> CallOutcome:
                return await runtime.command(
                    declaration.name, decode_object(parameters.model_dump_json().encode()), context
                )

            return FunctionCapability(declaration, invoke)

        async def send(parameters: _Send, context: CallContext) -> CallOutcome:
            return await runtime.send_chat(parameters.text, context)

        async def history(parameters: _History, context: CallContext) -> CallOutcome:
            return CallOutcome("confirmed", await runtime.history(parameters.after))

        async def prepare_request() -> JsonObject:
            return {"phone": runtime.snapshot()}

        return PluginRuntime(
            (
                FunctionCapability(declarations[0], status),
                FunctionCapability(declarations[1], connect),
                FunctionCapability(declarations[2], disconnect),
                FunctionCapability(declarations[3], send),
                FunctionCapability(declarations[4], history),
                *(bind_device(item) for item in device_declarations),
            ),
            start=server.start,
            stop_inputs=runtime.stop_inputs,
            close=server.close,
            snapshot=runtime.snapshot,
            panel=DevicePanel("phone", (), lambda: "1"),
            phone_pairing=PhonePairing(server.pairing, server.open_pairing, server.replace_pairing),
            prepare_request=prepare_request,
        )

    return PluginBlueprint(
        (
            ObjectDeclaration("phone", "phone", "远程实体手机", "subject"),
            ObjectDeclaration(instance_id + ":chat", "chat", "手机聊天", "phone"),
        ),
        (*declarations, *device_declarations),
        build,
        required_modules=("starlette", "uvicorn"),
    )


factory: PluginFactory = typed_factory(PhoneSettings, _declare)
