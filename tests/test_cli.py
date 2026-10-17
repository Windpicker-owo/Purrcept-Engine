"""纯配置和 CLI 退出所有权，不启动模型、真实设备或外部服务。"""

import asyncio
import signal
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from purrcept_engine.app import cli
from purrcept_engine.app.cli import main
from purrcept_engine.app.config import EngineConfig, load_config
from purrcept_engine.app.host import EngineHost
from purrcept_engine.app.host_types import HostSnapshot


def test_init_check_and_repeat_init_preserve_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "purrcept.toml"
    monkeypatch.setattr("sys.argv", ["purrcept-engine", "--config", str(config_path), "init"])
    assert main() == 0
    original = config_path.read_bytes()
    config = load_config(config_path)
    assert not config.console and not config.web.enabled
    assert not config.host.data_dir.exists()

    monkeypatch.setattr("sys.argv", ["purrcept-engine", "--config", str(config_path), "check"])
    assert main() == 0
    assert not config.host.data_dir.exists()

    monkeypatch.setattr("sys.argv", ["purrcept-engine", "--config", str(config_path), "catalog"])
    assert main() == 0
    assert not config.host.data_dir.exists()

    monkeypatch.setattr("sys.argv", ["purrcept-engine", "--config", str(config_path), "init"])
    assert main() == 1
    assert config_path.read_bytes() == original


@pytest.fixture
def signal_owner() -> Iterator[object]:
    """外层信号所有者防止模拟 SystemExit 混入 asyncio.run 自带的强退处理。"""

    def owner(signum: int, frame: object) -> None:
        del signum, frame

    previous = signal.signal(signal.SIGINT, owner)
    try:
        yield owner
    finally:
        signal.signal(signal.SIGINT, previous)


@pytest.mark.parametrize("pending", [(), ("plugin:browser", "storage", "data_directory_lock")])
@pytest.mark.parametrize("delayed", [False, True])
@pytest.mark.parametrize("cancelled_group", [False, True])
def test_close_failure_preserves_force_exit_only_while_resources_remain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pending: tuple[str, ...],
    delayed: bool,
    cancelled_group: bool,
    signal_owner: object,
) -> None:
    """资源未归还时仍接受第二次信号；已释放的原始错误则正常向外报告。"""
    template = Path(cli.__file__).with_name("default.toml")
    config_path = tmp_path / "purrcept.toml"
    config_path.write_bytes(template.read_bytes())
    host = EngineHost(load_config(config_path))
    original = RuntimeError("synthetic browser close failure")
    failure = (
        BaseExceptionGroup("failed accounting", [original, asyncio.CancelledError()])
        if cancelled_group
        else original
    )
    close_calls = 0
    handlers: list[object] = []

    def press_ctrl_c() -> None:
        """只调用本 CLI 安装的处理器，不向 pytest 或其他进程广播信号。"""
        handler = signal.getsignal(signal.SIGINT)
        handlers.append(handler)
        if not callable(handler):
            raise SystemExit(131)
        handler(signal.SIGINT, None)

    async def start() -> HostSnapshot:
        asyncio.get_running_loop().call_soon(press_ctrl_c)
        return HostSnapshot("offline", 1, "running", ())

    async def close() -> HostSnapshot:
        nonlocal close_calls
        close_calls += 1
        if delayed:
            return HostSnapshot("offline", 2, "closing", pending)
        raise failure

    async def wait_closed(*, wait_seconds: float | None = None) -> HostSnapshot:
        del wait_seconds
        raise failure

    def query() -> HostSnapshot:
        return HostSnapshot("offline", 2, "failed", pending)

    def force_exit(code: int) -> None:
        raise SystemExit(code)

    def create_host(config: EngineConfig) -> EngineHost:
        del config
        return host

    original_report = cli.traceback.print_exception

    def report(error: BaseException) -> None:
        original_report(error)
        asyncio.get_running_loop().call_soon(press_ctrl_c)

    monkeypatch.setattr(host, "start", start)
    monkeypatch.setattr(host, "close", close)
    monkeypatch.setattr(host, "wait_closed", wait_closed)
    monkeypatch.setattr(host, "query", query)
    monkeypatch.setattr(cli, "EngineHost", create_host)
    monkeypatch.setattr(cli.os, "_exit", force_exit)
    monkeypatch.setattr(cli.traceback, "print_exception", report)
    monkeypatch.setattr("sys.argv", ["purrcept-engine", "--config", str(config_path), "run"])
    if pending:
        with pytest.raises(SystemExit) as forced:
            main()
        assert forced.value.code == 130
        assert len(handlers) == 2 and handlers[0] is handlers[1]
        output = capsys.readouterr()
        assert "关闭失败，仍占用" in output.out and "storage" in output.out
        assert "synthetic browser close failure" in output.err
    else:
        with pytest.raises(BaseException) as raised:
            main()
        if delayed:
            assert raised.value is failure
        else:
            group = cast(BaseExceptionGroup[BaseException], raised.value)
            assert group.exceptions == (failure,)
    assert close_calls == 1
    assert signal.getsignal(signal.SIGINT) is signal_owner
