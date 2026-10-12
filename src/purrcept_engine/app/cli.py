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
from typing import TYPE_CHECKING

from purrcept_engine.errors import ConfigError, EngineError

from .config import load_config
from .host import EngineHost, HostSnapshot
from .logging import CliLogging
from .stdin import ConsoleIngress, WindowsLineReader

if TYPE_CHECKING:
    from .web import WebServer


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
    ingress: ConsoleIngress | None = None
    web: WebServer | None = None
    logs: CliLogging | None = None
    log_maintenance: asyncio.Task[None] | None = None
    observers: set[asyncio.Task[bool | None]] = set()
    try:
        await host.start()
        try:
            logs = CliLogging.open(
                config.host.data_dir,
                retention_days=config.logging.retention_days,
                max_bytes=config.logging.max_bytes,
                max_files=config.logging.max_files,
            )
        except (OSError, UnicodeError) as error:
            # 普通日志是诊断，文件不可用不改变已装配业务或权威存储资格。
            print(f"普通日志文件不可用：{type(error).__name__}。", file=sys.stderr)
        if logs is not None:
            log_maintenance = asyncio.create_task(_maintain_logs(logs, requested))
            logging.getLogger(__name__).info(
                "宿主已就绪。", extra={"host_instance": host.query().instance_id}
            )
        if config.web.enabled:
            from .web import WebServer

            web = WebServer(host, port=config.web.port)
            await web.start()
            print(f"本机面板：{web.url}", flush=True)
        print(f"Engine 已就绪，实例 {host.query().instance_id}。", flush=True)
        if config.console:
            ingress = ConsoleIngress(
                host,
                WindowsLineReader(sys.stdin, max_bytes=config.execution.pending_input_bytes),
            )
            ingress.start()
            close_requested = asyncio.create_task(requested.wait())
            input_failed = asyncio.create_task(ingress.wait_failed())
            observers.update((close_requested, input_failed))
            done, _ = await asyncio.wait(observers, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        else:
            await requested.wait()
    finally:
        try:
            for observer in observers:
                observer.cancel()
            if observers:
                await asyncio.gather(*observers, return_exceptions=True)
            if ingress is not None:
                await ingress.close()
            # 同时关闭页面观察与宿主入口，不先为HTTP等待花一份新期限。
            # HTTP取消不撤销PanelController已接管的独立业务所有者。
            closes = [host.close()]
            if web is not None:
                results = await asyncio.gather(*closes, web.close(), return_exceptions=True)
            else:
                results = await asyncio.gather(*closes, return_exceptions=True)
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise BaseExceptionGroup("应用接入与宿主收尾失败。", errors)
            snapshot = results[0]
            if not isinstance(snapshot, HostSnapshot):
                raise TypeError("宿主关闭没有返回生命周期快照。")
            if snapshot.phase != "closed":
                print(f"关闭未完成：{', '.join(snapshot.pending_resources)}。", flush=True)
                await host.wait_closed()
        finally:
            try:
                requested.set()
                if log_maintenance is not None:
                    await log_maintenance
            finally:
                try:
                    if logs is not None:
                        logs.close()
                finally:
                    signal.signal(signal.SIGINT, previous)
                    signal.signal(signal.SIGTERM, previous_term)


async def _maintain_logs(logs: CliLogging, stopped: asyncio.Event) -> None:
    """CLI的一个固定维护所有者清普通日志，不把日志失败当作业务失败。"""
    while not stopped.is_set():
        try:
            result = logs.cleanup()
            delay = 1.0 if result.has_more else 3600.0
        except (OSError, UnicodeError) as error:
            print(f"普通日志清理未完成：{type(error).__name__}。", file=sys.stderr)
            delay = 3600.0
        try:
            async with asyncio.timeout(delay):
                await stopped.wait()
        except TimeoutError:
            continue
