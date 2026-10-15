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
        """不可因后续读取失败把已点击/输入改成未开始。"""
        super().__init__(message)
        self.facts = facts


@dataclass(frozen=True, slots=True)
class PageView:
    """当次页面身份，HTTP状态/拦截原因只有真实取得时才存在。"""

    url: str
    title: str
    status: int | None = None
    blocked: str | None = None

    def payload(self) -> JsonObject:
        """保留旧url/title以及真实可用的status/blocked字段。"""
        result: JsonObject = {"url": self.url, "title": self.title}
        if self.status is not None:
            result["status"] = self.status
        if self.blocked is not None:
            result["blocked"] = self.blocked
        return result


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """受支持角色的有限节点树及ref映射；真实驱动拒绝页面改变后的旧句柄。"""

    text: str
    refs: Mapping[str, str]

    def __post_init__(self) -> None:
        """冻结扩展返回的映射，外界不能修改同一snapshot的绑定。"""
        object.__setattr__(self, "refs", MappingProxyType(dict(self.refs)))


@dataclass(frozen=True, slots=True)
class ImageCapture:
    """完整实际图像及取得方式；原图未取得而截元素时保留明确原因。"""

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
        """取得页面/上下文，不自动重试另一个会话。"""
        ...

    async def set_navigation_policy(self, policy: NavigationPolicy) -> None:
        """首个动作前安装实际请求检查；安装失败不声明ready。"""
        ...

    async def close(self) -> None:
        """逆序释放实际资源，失败保留仍占用的引用。"""
        ...

    async def open(self, url: str, *, timeout_seconds: float) -> PageView:
        """实际导航，失败不自动重发。"""
        ...

    async def view(self) -> PageView:
        """取得当次页面身份，不能用旧标题填补失败。"""
        ...

    async def html(self) -> str:
        """取得完整渲染HTML，不提前按模型页长裁剪。"""
        ...

    async def inner_text(self) -> str:
        """取得完整实际可见文本，仅在HTML抽取没有内容时使用。"""
        ...

    async def snapshot(self, *, interactive: bool) -> SnapshotResult:
        """取得本页受支持角色树和有效ref，内置每角色最多40个节点。"""
        ...

    async def click(self, handle: str) -> PageView:
        """保留click handler；实际交互后的读取故障携带确认事实。"""
        ...

    async def type_text(self, handle: str, text: str, *, delay_ms: float, submit: bool) -> PageView:
        """清空并输入，按明确参数提交Enter，不自动重放。"""
        ...

    async def scroll(self, *, direction: str, handle: str | None, amount: int) -> PageView:
        """滚视口或现有ref，不能猜不存在的ref。"""
        ...

    async def screenshot(self, *, full_page: bool) -> ImageCapture:
        """完整当次JPEG，大小和原生提交由Session处理。"""
        ...

    async def save(self, handle: str) -> ImageCapture:
        """读取实际原图或截取已知元素，明确区分取得方式和原图失败。"""
        ...
