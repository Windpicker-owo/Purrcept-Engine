"""独立进程入口，拥有日志和操作系统信号，不将其带入嵌入式宿主。

init/check 只写示例或校验纯配置；run 显式持有宿主直至退出。一次信号请求
正常收尾，二次信号明确终止本 CLI；关闭超时仍可观察并等同次收尾任务。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from importlib.resources import files
from pathlib import Path

from purrcept_engine.errors import ConfigError, EngineError

from .config import load_config
from .host import EngineHost


def main() -> int:
    """解析命令并返回退出码；库入口不安装信号处理器或调用进程退出。"""
    parser = argparse.ArgumentParser(prog="purrcept-engine")
    parser.add_argument("--config", type=Path, default=Path("purrcept.toml"))
    parser.add_argument("command", choices=("init", "check", "catalog", "run"))
    args = parser.parse_args()
    config_path: Path = args.config
    try:
        if args.command == "init":
            # 新建采用排他模式，现有配置不会因为重复 init 被覆盖。
            template = files("purrcept_engine.app").joinpath("default.toml").read_bytes()
            with config_path.open("xb") as output:
                output.write(template)
            print(f"配置示例已写入 {config_path}；请填写实际模型契约。")
        elif args.command == "check":
            EngineHost(load_config(config_path))
            print("配置结构与引用校验通过；模型及设备尚未探测。")
        elif args.command == "catalog":
            host = EngineHost(load_config(config_path))
            print(f"目录版本：{host.catalog.version}\n{host.catalog.instructions}")
        else:
            logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
            asyncio.run(_run(config_path))
    except (ConfigError, EngineError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except FileExistsError:
        print("目标配置已存在，请另选路径。", file=sys.stderr)
        return 1
    return 0


async def _run(config_path: Path) -> None:
    """常驻拥有宿主，信号只向本进程提出退出，不触碰外部服务。"""
    config = load_config(config_path)
    if config.console or config.web:
        raise ConfigError("控制台接入和 WebUI 尚未装配；当前宿主基线请关闭这两项。")
    host = EngineHost(config)
    loop = asyncio.get_running_loop()
    requested = asyncio.Event()
    signalled = False

    def request_close(signum: int, frame: object) -> None:
        """第一次信号转为收尾请求，第二次执行明确的进程强退。"""
        nonlocal signalled
        del signum, frame
        if signalled:
            os._exit(130)
        # 第二次信号可能早于事件循环采用第一次通知；同步标志拥有该顺序。
        signalled = True
        print("正在关闭。再次 Ctrl+C 将强制退出本 Engine 进程。", flush=True)
        loop.call_soon_threadsafe(requested.set)

    previous = signal.signal(signal.SIGINT, request_close)
    previous_term = signal.signal(signal.SIGTERM, request_close)
    try:
        await host.start()
        print(f"Engine 核心存储已就绪，实例 {host.query().instance_id}。", flush=True)
        await requested.wait()
    finally:
        try:
            snapshot = await host.close()
            if snapshot.phase != "closed":
                print(f"关闭未完成：{', '.join(snapshot.pending_resources)}。", flush=True)
                await host.wait_closed()
        finally:
            signal.signal(signal.SIGINT, previous)
            signal.signal(signal.SIGTERM, previous_term)
