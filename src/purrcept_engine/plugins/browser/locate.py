"""按显式配置或 Chrome、Edge、已安装 bundled Chromium 的顺序选择程序。

显式executable_path优先；文件/注册表检查仅在实际start运行，不安装或下载
软件。Windows为首版平台，具体已选择程序失败不再启动另一实例重试。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from purrcept_engine.plugins.browser.settings import BrowserSettings


@dataclass(frozen=True, slots=True)
class BrowserLaunch:
    """供SDK调用的互斥executable/channel，不携带另一个隐式配置来源。

    Attributes:
        executable_path: 显式浏览器程序路径；None 按当前解析规则确定。
        channel: 当前 Worker 唯一借用的有界 IPC 帧通道。
        kind: 此业务值所属的稳定种类。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    executable_path: str | None
    channel: str | None
    kind: str


def resolve_launch(
    settings: BrowserSettings, *, environ: Mapping[str, str] | None = None
) -> BrowserLaunch:
    """按原具体程序查找选择一次，操作在资源所有者的启动线程执行。

    Args:
        settings: 当前组件已校验的配置或纯声明。
        environ: 用于解析浏览器启动设置的显式环境快照。 None 在此解析入口读取当前进程环境。

    Returns:
        已解析的浏览器启动声明，不会因此启动浏览器。

    Raises:
        FileNotFoundError: 配置的浏览器executable_path不是文件。
    """
    configured = settings.executable_path
    if configured is not None:
        if not configured.is_file():
            raise FileNotFoundError("配置的浏览器executable_path不是文件。")
        return BrowserLaunch(str(configured), None, "custom")
    channel = settings.channel.strip().lower()
    if not channel:
        return BrowserLaunch(None, None, "bundled-chromium")
    env = os.environ if environ is None else environ
    families = (channel, "msedge") if channel == "chrome" else (channel,)
    for family in families:
        if family.startswith("chrome"):
            folder = {
                "chrome": "Chrome",
                "chrome-beta": "Chrome Beta",
                "chrome-dev": "Chrome Dev",
                "chrome-canary": "Chrome SxS",
            }.get(family)
            suffix = None if folder is None else ("Google", folder, "Application", "chrome.exe")
        elif family.startswith("msedge"):
            folder = {
                "msedge": "Edge",
                "msedge-beta": "Edge Beta",
                "msedge-dev": "Edge Dev",
                "msedge-canary": "Edge SxS",
            }.get(family)
            suffix = None if folder is None else ("Microsoft", folder, "Application", "msedge.exe")
        else:
            return BrowserLaunch(None, channel, channel)
        if suffix is None:
            return BrowserLaunch(None, channel, channel)
        candidates = tuple(
            Path(env[key]).joinpath(*suffix)
            for key in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432")
            if env.get(key)
        )
        if environ is None:
            candidates += _registry_paths(suffix[-1])
        if family.startswith("chrome"):
            candidates += _cache_paths(env)
        for path in dict.fromkeys(candidates):
            if path.is_file():
                return BrowserLaunch(str(path), None, family)
    return BrowserLaunch(None, None, "bundled-chromium")


def _registry_paths(executable: str) -> tuple[Path, ...]:
    """缺失安装键是正常未安装，真实注册表读取失败不伪装为未安装。"""
    import winreg

    found: list[Path] = []
    key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key_path) as key:
                value: object = winreg.QueryValueEx(key, "")[0]
        except FileNotFoundError:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError("已安装浏览器的注册表路径无效。")
        found.append(Path(value))
    return tuple(found)


def _cache_paths(environ: Mapping[str, str]) -> tuple[Path, ...]:
    """仅查旧SDK指定或本机安装缓存，目录不存在不触发下载。"""
    configured = environ.get("PLAYWRIGHT_BROWSERS_PATH") or environ.get("PATCHRIGHT_BROWSERS_PATH")
    local = environ.get("LOCALAPPDATA")
    if configured:
        cache = Path(configured)
    elif local:
        cache = Path(local) / "ms-playwright"
    else:
        return ()
    if not cache.is_dir():
        return ()
    return tuple(
        child / directory / "chrome.exe"
        for child in sorted(cache.glob("chrome-*"), reverse=True)
        for directory in ("chrome-win64", "chrome-win")
    )
