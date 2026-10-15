"""浏览器的宿主驱动契约，真实Patchright实现在patchright.py。

会话在build/start取得，关闭时释放全部资源；Worker只拿冻结ref和JSON，不
接触页面对象。站点策略必须覆盖实际文档请求、逐跳重定向及衍生弹窗，不能
只检查open参数。SDK故障与参数拒绝分开；部分实际效果须携带明确事实。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from purrcept_engine.errors import EngineError
from purrcept_engine.plugins.browser.navigation import NavigationPolicy
from purrcept_engine.values import JsonObject


class BrowserError(EngineError):
    """设备/协议故障，不能当作模型参数错误或静默换浏览器。"""


class BrowserInputError(ValueError):
    """URL/句柄等真实调用边界明确拒绝，可在原行动内纠正。"""


class BrowserActionError(BrowserError):
    """实际交互后取得页面状态失败，facts保留已确认的局部效果。"""

    def __init__(self, message: str, facts: JsonObject) -> None:
        """不可因后续读取失败把已点击/输入改成未开始。

        Args:
            message: 对调用方公开的原因或错误说明。
            facts: 已确认的局部效果事实，后续读取失败仍须保留。
        """
        super().__init__(message)
        self.facts = facts


@dataclass(frozen=True, slots=True)
class PageView:
    """当次页面身份，HTTP状态/拦截原因只有真实取得时才存在。

    Attributes:
        url: 本次明确访问的资源 URL，实际请求仍须通过对应策略。
        title: 当前业务内容面向调用者的展示标题。
        status: 实际取得的 HTTP 状态码；未取得时保留 None。
        blocked: 此次导航实际被拦截的原因，未拦截时为 None。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    url: str
    title: str
    status: int | None = None
    blocked: str | None = None

    def payload(self) -> JsonObject:
        """将当次页面身份与已取得的状态投影为业务 JSON。

        Returns:
            业务对象，按实际结果提供 url、title 字段。

        Note:
            保留 url/title，以及实际取得的 status/blocked 字段。
        """
        result: JsonObject = {"url": self.url, "title": self.title}
        if self.status is not None:
            result["status"] = self.status
        if self.blocked is not None:
            result["blocked"] = self.blocked
        return result


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """受支持角色的有限节点树及ref映射；真实驱动拒绝页面改变后的旧句柄。

    Attributes:
        text: 本次明确传输、输入或呈现的完整文本。
        refs: 当前节点快照签发的展示引用与原生句柄映射。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    text: str
    refs: Mapping[str, str]

    def __post_init__(self) -> None:
        """冻结扩展返回的映射，外界不能修改同一snapshot的绑定。"""
        object.__setattr__(self, "refs", MappingProxyType(dict(self.refs)))


@dataclass(frozen=True, slots=True)
class ImageCapture:
    """完整实际图像及取得方式；原图未取得而截元素时保留明确原因。

    Attributes:
        data: 当前边界接收的原始协议或媒体内容。
        media_type: 完整媒体字节实际使用的 MIME 类型。
        acquired_via: 原图或元素截图等实际图像取得方式。
        original_error: 原图读取失败的真实原因，成功时为 None。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    data: bytes
    media_type: str
    acquired_via: str
    original_error: str | None = None


class BrowserDriver(Protocol):
    """app装配创建、BrowserSession串行借用的一页浏览器所有者。

    start失败须可close实际已取得资源；未启动不读缓存。导航策略安装成功
    才可执行动作，所有文档跳转每次发送前核验，popup不能成为旁路。close
    重复加入同一资源收尾。内置PatchrightDriver，隔离测试注入同契约替身。
    """

    async def start(self) -> None:
        """取得当前浏览器页面与上下文资源。

        Note:
            取得页面/上下文，不自动重试另一个会话。
        """
        ...

    async def set_navigation_policy(self, policy: NavigationPolicy) -> None:
        """首个动作前安装实际请求检查。

        Args:
            policy: 覆盖文档请求、逐跳重定向及弹窗的站点访问规则。

        Note:
            安装失败不声明ready。
        """
        ...

    async def close(self) -> None:
        """逆序释放实际资源，失败保留仍占用的引用。"""
        ...

    async def open(self, url: str, *, timeout_seconds: float) -> PageView:
        """实际导航，失败不自动重发。

        Args:
            url: 本次明确访问的资源 URL，实际请求仍须通过对应策略。
            timeout_seconds: 本次操作的等待上限（秒），不得扩大所属行动或活动期限。

        Returns:
            此次操作实际取得的页面 URL、标题与可用的状态信息。
        """
        ...

    async def view(self) -> PageView:
        """取得当次页面身份，不能用旧标题填补失败。

        Returns:
            此次操作实际取得的页面 URL、标题与可用的状态信息。
        """
        ...

    async def html(self) -> str:
        """取得完整渲染HTML，不提前按模型页长裁剪。"""
        ...

    async def inner_text(self) -> str:
        """取得完整实际可见文本，仅在HTML抽取没有内容时使用。"""
        ...

    async def snapshot(self, *, interactive: bool) -> SnapshotResult:
        """取得本页受支持角色树和有效ref，内置每角色最多40个节点。

        Args:
            interactive: 是否仅投影可交互节点。

        Returns:
            当次节点树及有效 ref 映射，页面变化后旧 ref 不能继续使用。
        """
        ...

    async def click(self, handle: str) -> PageView:
        """点击当前会话的有效元素，返回实际页面状态。

        Args:
            handle: 当前所有者持有的原生句柄。

        Returns:
            此次操作实际取得的页面 URL、标题与可用的状态信息。

        Note:
            保留click handler；实际交互后的读取故障携带确认事实。
        """
        ...

    async def type_text(self, handle: str, text: str, *, delay_ms: float, submit: bool) -> PageView:
        """清空并输入，按明确参数提交Enter，不自动重放。

        Args:
            handle: 当前所有者持有的原生句柄。
            text: 本次明确传输、输入或呈现的完整文本。
            delay_ms: 逐字输入的延迟毫秒数。
            submit: 是否在完成文字输入后明确发送 Enter。

        Returns:
            此次操作实际取得的页面 URL、标题与可用的状态信息。
        """
        ...

    async def scroll(self, *, direction: str, handle: str | None, amount: int) -> PageView:
        """滚视口或现有ref，不能猜不存在的ref。

        Args:
            direction: 明确的滚动方向，由浏览器边界校验。
            handle: 当前所有者持有的原生句柄。
            amount: 本次滚动的像素数量。

        Returns:
            此次操作实际取得的页面 URL、标题与可用的状态信息。
        """
        ...

    async def screenshot(self, *, full_page: bool) -> ImageCapture:
        """完整当次JPEG，大小和原生提交由Session处理。

        Args:
            full_page: 是否截取完整页面，而非仅当前视口。

        Returns:
            实际取得的完整图像、编码与获取方式。
        """
        ...

    async def save(self, handle: str) -> ImageCapture:
        """读取实际原图或截取已知元素，明确区分取得方式和原图失败。

        Args:
            handle: 当前所有者持有的原生句柄。

        Returns:
            实际取得的完整图像、编码与获取方式。
        """
        ...
