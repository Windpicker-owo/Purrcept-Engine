"""验证 CLI 初始化向导、插件配置速查、参数覆盖与信号退出行为。"""

import asyncio
import io
import signal
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest

from purrcept_engine.app import cli
from purrcept_engine.app.cli import main
from purrcept_engine.app.config import EngineConfig, load_config
from purrcept_engine.app.host import EngineHost
from purrcept_engine.app.host_types import HostSnapshot
from purrcept_engine.app.onboarding import render_plugin_guide, run_interactive_init


def test_init_check_plugins_and_force_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "purrcept.toml"
    monkeypatch.setattr(
        "sys.argv", ["purrcept-engine", "--config", str(config_path), "init", "--template"]
    )
    assert main() == 0
    original = config_path.read_bytes()
    config = load_config(config_path)
    assert config.console is True and not config.web.enabled
    assert not config.host.data_dir.exists()

    monkeypatch.setattr("sys.argv", ["purrcept-engine", "--config", str(config_path), "check"])
    assert main() == 0
    assert not config.host.data_dir.exists()

    monkeypatch.setattr("sys.argv", ["purrcept-engine", "--config", str(config_path), "catalog"])
    assert main() == 0
    assert not config.host.data_dir.exists()

    monkeypatch.setattr("sys.argv", ["purrcept-engine", "plugins"])
    assert main() == 0
    assert "notebook" in capsys.readouterr().out

    monkeypatch.setattr("sys.argv", ["purrcept-engine", "plugins", "qq"])
    assert main() == 0
    assert "OneBot" in capsys.readouterr().out

    # 默认不覆盖已有文件；传入 --force 时自动备份并重新生成
    monkeypatch.setattr(
        "sys.argv", ["purrcept-engine", "--config", str(config_path), "init", "--template"]
    )
    assert main() == 1
    assert config_path.read_bytes() == original

    monkeypatch.setattr(
        "sys.argv",
        ["purrcept-engine", "--config", str(config_path), "init", "--template", "--force"],
    )
    assert main() == 0
    backups = list(tmp_path.glob("purrcept.toml.bak-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original


def test_interactive_init_wizard_generates_valid_cloud_and_local_configs(tmp_path: Path) -> None:
    cloud_answers = iter(
        [
            "1",
            "https://api.deepseek.com/v1",
            "deepseek-chat",
            "${DEEPSEEK_KEY}",
            "杰瑞喵",
            "傲娇猫娘助手",
            "y",
            "n",
        ]
    )
    buf = io.StringIO()
    cloud_toml = run_interactive_init(
        tmp_path / "cloud.toml",
        input_fn=lambda _: next(cloud_answers),
        output=buf,
    )
    (tmp_path / "cloud.toml").write_bytes(cloud_toml)
    loaded_cloud = load_config(
        tmp_path / "cloud.toml", environ={"DEEPSEEK_KEY": "sk-test-12345678"}
    )
    assert loaded_cloud.models["main"].model == "openai/deepseek-chat"
    assert loaded_cloud.subject.character.name == "杰瑞喵"
    assert any(p.entry == "notebook" for p in loaded_cloud.plugins)

    local_answers = iter(["2", "http://127.0.0.1:11434/v1", "qwen2.5:14b", "", "小咕", "", "n", "n"])
    local_toml = run_interactive_init(
        tmp_path / "local.toml",
        input_fn=lambda _: next(local_answers),
        output=io.StringIO(),
    )
    (tmp_path / "local.toml").write_bytes(local_toml)
    loaded_local = load_config(tmp_path / "local.toml")
    assert loaded_local.models["main"].api_key.get_secret_value() == "local-no-auth"
    assert "notebook" in render_plugin_guide("notebook")


@pytest.fixture
def signal_owner() -> Iterator[object]:
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

    def create_host(
        config: EngineConfig, *, on_data_dir_acquired: Callable[[Path], None] | None = None
    ) -> EngineHost:
        del on_data_dir_acquired
        del config
        return host

    original_report = cli.ComponentHealth.fail
    reported: list[BaseException] = []

    def report(owner: cli.ComponentHealth, error: BaseException) -> None:
        original_report(owner, error)
        reported.append(error)
        if pending and owner.component == "shutdown":
            asyncio.get_running_loop().call_soon(press_ctrl_c)

    monkeypatch.setattr(host, "start", start)
    monkeypatch.setattr(host, "close", close)
    monkeypatch.setattr(host, "wait_closed", wait_closed)
    monkeypatch.setattr(host, "query", query)
    monkeypatch.setattr(cli, "EngineHost", create_host)
    monkeypatch.setattr(cli.os, "_exit", force_exit)
    monkeypatch.setattr(cli.ComponentHealth, "fail", report)
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
        assert main() == 1
        if delayed:
            assert reported[-1] is failure
        else:
            group = cast(BaseExceptionGroup[BaseException], reported[-1])
            assert group.exceptions == (failure,)
    assert close_calls == 1
    assert signal.getsignal(signal.SIGINT) is signal_owner
