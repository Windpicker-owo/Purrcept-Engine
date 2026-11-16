"""插件向本机面板显式借出的有限控制与缓存预览。

面板声明只绑定已有能力身份，不按模型目录自动生成任意操作按钮。app 将身份
解析到同一执行器；设备状态和资源仍归插件，读取预览绝不启动采集。配置页和
浏览器断开不改变设备寿命，也不增加人员身份或角色授权。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from purrcept_engine.values import JsonObject, decode_object


@dataclass(frozen=True, slots=True)
class PanelControl:
    """一个明确快捷操作，固定参数不能被页面传值覆盖。

    Attributes:
        key: 当前设备或控制字段的稳定键，不使用展示文本充当身份。
        title: 当前业务内容面向调用者的展示标题。
        capability_id: 声明中与实现绑定的稳定能力身份。
        arguments_json: 已编码为严格 JSON 对象的原始能力参数。
        fields: 此快捷控制允许填写的参数字段集合。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        capability_id 对应当前插件已经声明的能力；fields 仅允许页面填写该操作
        真正需要的设备或模式参数。全部参数随后经过原能力的 Pydantic 边界。
        key 是页面操作身份，不是模型路径；title 是面向人的具体动作名称。
    """

    key: str
    title: str
    capability_id: str
    arguments_json: str = "{}"
    fields: tuple[str, ...] = ()

    def arguments(self, supplied: JsonObject) -> JsonObject:
        """合并明确可填写字段。

        Args:
            supplied: 设备当前返回的原始业务 JSON，仍须在投影边界核验。

        Raises:
            ValueError: 快捷控制不接受这些参数。

        Note:
            拒绝额外值或覆盖插件固定的控制参数。
        """
        fixed = decode_object(self.arguments_json.encode("utf-8"))
        if any(name not in self.fields or name in fixed for name in supplied):
            raise ValueError("快捷控制不接受这些参数。")
        return fixed | supplied


@dataclass(frozen=True, slots=True)
class PreviewFrame:
    """从当前设备缓存借出的完整媒体，不拥有采集器或增加正式输入。

    Attributes:
        data: 当前边界接收的原始协议或媒体内容。
        media_type: 完整媒体字节实际使用的 MIME 类型。
        captured_at: 当前画面实际取得的 Unix 时间戳（秒）。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    data: bytes
    media_type: str
    captured_at: float


@dataclass(frozen=True, slots=True)
class DevicePanel:
    """插件在 build 交回的设备面板端口，寿命与 PluginRuntime 相同。

    Attributes:
        kind: 此业务值所属的稳定种类。
        controls: 此设备明确允许的有限快捷控制声明。
        revision: 已观察的状态修订号。
        preview: 只读已取得画面的同步入口，不额外采集。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        revision 只表达连接/控制代次等资格，不能因每帧采集时间改变而使点击失效。
        app 在接管和实际开始前核对页面看到的版本。preview 可缺省；有值时只能
        返回已经取得的缓存，不能调用截图、网络或设备读取。所有调用在宿主循环上。
    """

    kind: Literal["computer", "qq", "voice_input", "audio", "live2d", "phone"]
    controls: tuple[PanelControl, ...]
    revision: Callable[[], str]
    preview: Callable[[], PreviewFrame | None] | None = None


@dataclass(frozen=True, slots=True)
class PhonePairing:
    """Phone 插件交给本机 WebUI 的配对端口，不进入能力目录或普通快照。

    read 只读取当前窗口和已配对事实；open 由本机用户明确点击后开启或刷新
    120 秒窗口。replace 撤销旧设备凭据与连接，再开放新窗口，保留历史记录。
    replace 必须传入 read 观察到的 device_id；目标变化时拒绝，避免旧确认撤销新配对。
    三个入口在宿主循环上调用，可能抛出存储或生命周期错误。返回值
    可含短时邀请，调用方只能在同源、禁止缓存的配对响应内使用。关闭监听器
    后不得借用此端口；主体和远程手机均不能通过模型能力调用它。
    """

    read: Callable[[], Awaitable[JsonObject]]
    open: Callable[[], Awaitable[JsonObject]]
    replace: Callable[[str], Awaitable[JsonObject]]


type PairingOperation = Literal["read", "open", "replace"]
