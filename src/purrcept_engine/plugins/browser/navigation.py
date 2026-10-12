"""浏览器导航共用的 URL 与主机策略，不依赖 Session 或具体驱动。

Session 在显式入口预检；驱动在实际请求发出前使用同一策略检查点击、提交和重定向。
本策略约束文档导航，不把页面的图片、脚本等子资源等同于文档导航。
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse

type NavigationPolicy = Callable[[str], str]
"""导航前验证 URL 的宿主回调；返回规范 URL，拒绝时抛出 ValueError。"""


def normalize_and_authorize_url(
    url: str,
    *,
    allowed_hosts: tuple[str, ...],
    blocked_hosts: tuple[str, ...],
) -> str:
    """补全 https、限制 http(s)，并按黑名单优先规则验证完整主机名。"""

    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must not be empty.")
    stripped = url.strip()
    parsed = urlparse(stripped)
    if not parsed.scheme:
        stripped = "https://" + stripped
        parsed = urlparse(stripped)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("url must use http or https.")
    host = parsed.hostname
    if not host:
        raise ValueError("url must include a host.")
    lowered = host.lower().rstrip(".")
    if any(_host_matches(lowered, pattern) for pattern in blocked_hosts):
        raise ValueError(f"Host {host!r} is blocked.")
    if allowed_hosts and not any(_host_matches(lowered, pattern) for pattern in allowed_hosts):
        raise ValueError(f"Host {host!r} is not in allowed_hosts.")
    return stripped


def _host_matches(host: str, pattern: str) -> bool:
    needle = pattern.lower().rstrip(".")
    if needle.startswith("*."):
        return host == needle[2:] or host.endswith(needle[1:])
    return host == needle


__all__ = ["NavigationPolicy", "normalize_and_authorize_url"]
