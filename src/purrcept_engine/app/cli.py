"""命令行入口：提供配置初始化向导、插件速查、静态校验、数据库维护与宿主运行管理。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
import traceback
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from purrcept_engine.app.config import LogLevel, load_config
from purrcept_engine.app.host import EngineHost
from purrcept_engine.app.host_types import HostSnapshot
from purrcept_engine.app.onboarding import render_plugin_guide, run_interactive_init
from purrcept_engine.app.stdin import ConsoleIngress, WindowsLineReader
from purrcept_engine.domain.health import ComponentHealth
from purrcept_engine.errors import ConfigError, EngineError
from purrcept_engine.infrastructure.logging import CliLogging, console_logging, redact_secrets

if TYPE_CHECKING:
    from purrcept_engine.app.web import WebServer


def _is_interactive_tty() -> bool:
    isatty = getattr(sys.stdin, "isatty", None)
    return bool(callable(isatty) and isatty())


def _has_readable_stdin() -> bool:
    """检查当前标准输入是否具备原生文件描述符（后台服务或测试重定向伪流无 fileno 时跳过读取）。"""
    stream = sys.stdin
    if stream is None or getattr(stream, "closed", True):
        return False
    try:
        _ = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    return True


def main() -> int:
    """解析命令行参数并执行对应子命令，返回进程退出码。"""
    parser = argparse.ArgumentParser(
        prog="purrcept-engine",
        description="Purrcept Engine 连续主体宿主命令行工具",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("purrcept.toml"),
        help="TOML 配置文件路径（默认：purrcept.toml）",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="开启 DEBUG 级别详细日志与完整异常堆栈输出",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default=None,
        help="显式指定运行日志级别（覆盖配置文件中的 logging.level）",
    )
    parser.add_argument(
        "--template",
        action="store_true",
        help="配合 `init` 使用：跳过交互式问答，直接写出带完整注释的模板文件",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="配合 `init` 使用：若目标配置文件已存在，先备份原文件再覆盖生成",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="配合 `db-*` 维护命令使用：直接指定已有数据目录，无需读取配置文件",
    )
    parser.add_argument(
        "command",
        choices=(
            "init",
            "plugins",
            "check",
            "catalog",
            "run",
            "db-check",
            "db-stats",
            "db-clean",
            "db-vacuum",
        ),
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="可选子目标（如 `purrcept-engine plugins qq` 查看指定插件配置详情）",
    )
    args = parser.parse_args()
    config_path: Path = args.config
    cli_level: LogLevel | None = "DEBUG" if args.debug else args.log_level

    try:
        if args.command == "plugins":
            print(render_plugin_guide(args.target))
        elif args.command.startswith("db-"):
            from purrcept_engine.infrastructure.sqlite.maintenance_cli import maintain

            selected = None if args.data_dir is not None else load_config(config_path)
            data_dir = (
                args.data_dir.resolve()
                if args.data_dir is not None
                else selected.host.data_dir
                if selected is not None
                else Path.cwd()
            )
            result = maintain(
                data_dir,
                args.command,
                retention_days=7 if selected is None else selected.diagnostics.retention_days,
                max_bytes=1_073_741_824
                if selected is None
                else selected.diagnostics.retained_bytes,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "init":
            if config_path.exists() and not args.force:
                raise FileExistsError(str(config_path))
            use_wizard = _is_interactive_tty() and not args.template
            content = (
                run_interactive_init(config_path)
                if use_wizard
                else files("purrcept_engine.app").joinpath("default.toml").read_bytes()
            )
            config_path.parent.mkdir(parents=True, exist_ok=True)
            if config_path.exists() and args.force:
                backup = config_path.with_name(f"{config_path.name}.bak-{int(time.time())}")
                config_path.replace(backup)
                print(f"已将原有配置备份至 {backup}。")
            with config_path.open("xb") as output:
                output.write(content)
            if use_wizard:
                print(
                    f"\n配置已生成：{config_path}\n"
                    f"下一步：\n"
                    f"  1. 校验配置：uv run purrcept-engine --config {config_path} check\n"
                    f"  2. 启动运行：uv run purrcept-engine --config {config_path} run"
                )
            else:
                print(
                    f"配置模板已写入 {config_path}。\n"
                    f"请编辑文件中的 [models.main]，或运行 `purrcept-engine plugins` 查看各插件配置说明。"
                )
        elif args.command == "check":
            EngineHost(load_config(config_path))
            print(f"配置校验通过（{config_path}）：模型引用、插件声明与本地依赖均正常。")
        elif args.command == "catalog":
            host = EngineHost(load_config(config_path))
            print(f"目录版本：{host.catalog.version}\n{host.catalog.instructions}")
        else:
            initial_level: LogLevel = cli_level or "INFO"
            with console_logging(level=initial_level):
                asyncio.run(_run(config_path, level_override=cli_level))
    except (ConfigError, EngineError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except FileExistsError:
        print(
            f"目标配置文件 {config_path} 已存在（如需备份并重新生成，请加 `--force`；或另选 `--config` 路径）。",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\n操作已取消。", file=sys.stderr)
        return 130
    except (Exception, BaseExceptionGroup) as error:
        detail = redact_secrets(str(error)).strip()
        summary = f"{type(error).__name__}: {detail}" if detail else type(error).__name__
        print(f"Engine 运行异常退出（{summary}）。", file=sys.stderr)
        if args.debug:
            print(redact_secrets(traceback.format_exc()), file=sys.stderr)
        return 1
    return 0


async def _run(config_path: Path, *, level_override: LogLevel | None = None) -> None:
    """加载配置并常驻运行宿主，处理 Ctrl+C 优雅关闭与二次信号强制退出。"""
    started = time.perf_counter()
    config = load_config(config_path)
    effective_level: LogLevel = level_override or config.logging.level
    logging.getLogger("purrcept_engine").setLevel(effective_level)
    for handler in logging.getLogger("purrcept_engine").handlers:
        handler.setLevel(effective_level)

    early_timings = {"configuration": (time.perf_counter() - started) * 1000}
    logging.getLogger(__name__).info(
        "配置已加载（日志级别=%s）。",
        effective_level,
        extra={"stage": "configuration", "duration_ms": early_timings["configuration"]},
    )
    logs: CliLogging | None = None

    def acquired(data_dir: Path) -> None:
        """取得数据目录锁后挂载文件滚动日志。"""
        nonlocal logs
        try:
            logs = CliLogging.open(
                data_dir,
                retention_days=config.logging.retention_days,
                max_bytes=config.logging.max_bytes,
                max_files=config.logging.max_files,
                level=effective_level,
                include_console=False,
            )
            for stage, duration in early_timings.items():
                logs.record_startup_phase(stage, duration)
        except (OSError, UnicodeError) as error:
            logging.getLogger(__name__).warning(
                "日志文件初始化失败（%s），本次仅输出终端日志。",
                error,
                extra={"error_type": type(error).__name__},
            )

    declared = time.perf_counter()
    host = EngineHost(config, on_data_dir_acquired=acquired)
    early_timings["plugin_declarations"] = (time.perf_counter() - declared) * 1000
    logging.getLogger(__name__).info(
        "已校验 %d 个插件声明。",
        len([p for p in config.plugins if p.enabled]),
        extra={
            "stage": "plugin_declarations",
            "plugin_count": len(config.plugins),
            "duration_ms": early_timings["plugin_declarations"],
        },
    )
    loop = asyncio.get_running_loop()
    requested = asyncio.Event()
    signalled = False

    def request_close(signum: int, frame: object) -> None:
        """首次 Ctrl+C 触发优雅收尾，再次按下立即强制退出进程。"""
        nonlocal signalled
        del signum, frame
        if signalled:
            os._exit(130)
        signalled = True
        print("正在关闭 Engine（再次按 Ctrl+C 将立即强制退出）…", flush=True)
        loop.call_soon_threadsafe(requested.set)

    previous = signal.signal(signal.SIGINT, request_close)
    previous_term = signal.signal(signal.SIGTERM, request_close)
    ingress: ConsoleIngress | None = None
    web: WebServer | None = None
    log_maintenance: asyncio.Task[None] | None = None
    observers: set[asyncio.Task[bool | None]] = set()
    try:
        if config.web.enabled:
            from purrcept_engine.app.web import WebServer

            web = WebServer(
                host,
                port=config.web.port,
                config_path=config_path,
                owner_data_dir=config.host.data_dir,
            )
            web_started = time.perf_counter()
            await web.start()
            early_timings["web"] = (time.perf_counter() - web_started) * 1000
            logging.getLogger(__name__).info(
                "WebUI 本机面板已启动。",
                extra={"stage": "web", "duration_ms": early_timings["web"]},
            )
            print(f"本机面板：{web.url}", flush=True)
        await host.start()
        if logs is not None:
            log_maintenance = asyncio.create_task(_maintain_logs(logs, requested))
        instance_id = host.query().instance_id
        logging.getLogger(__name__).info(
            "宿主已就绪（控制台输入=%s）。",
            "已开启" if config.console else "已关闭",
            extra={"host_instance": instance_id},
        )
        stdin_available = config.console and _has_readable_stdin()
        if config.console and not stdin_available:
            logging.getLogger(__name__).info(
                "标准输入无原生句柄，已跳过终端键盘监听（me.console.output 仍正常可用）。"
            )
        hint = (
            "直接输入文字回车即可发送刺激；按 Ctrl+C 退出。"
            if stdin_available
            else "按 Ctrl+C 退出。"
        )
        print(f"Engine 已就绪（实例 {instance_id}）。{hint}", flush=True)
        if stdin_available:
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
    except BaseException as error:
        if not isinstance(error, asyncio.CancelledError):
            ComponentHealth("cli").fail(error)
        raise
    finally:
        try:
            for observer in observers:
                observer.cancel()
            if observers:
                await asyncio.gather(*observers, return_exceptions=True)
            if ingress is not None:
                await ingress.close()
            try:
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
            except (Exception, BaseExceptionGroup) as failure:
                ComponentHealth("shutdown").fail(failure)
                pending = host.query().pending_resources
                if pending:
                    # 若底层非 daemon 线程（如数据库或子进程）仍未释放，保留二次 Ctrl+C 强退处理器以免死锁卡死。
                    print(
                        f"关闭失败，仍占用：{', '.join(pending)}。"
                        "再次 Ctrl+C 将强制退出本 Engine 进程。",
                        flush=True,
                    )
                    await asyncio.Future[None]()
                raise
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
    """后台定期清理过期日志文件。"""
    while not stopped.is_set():
        try:
            result = logs.cleanup()
            delay = 1.0 if result.has_more else 3600.0
        except (OSError, UnicodeError) as error:
            print(f"日志文件定期清理失败：{error}", file=sys.stderr)
            delay = 3600.0
        try:
            async with asyncio.timeout(delay):
                await stopped.wait()
        except TimeoutError:
            continue
