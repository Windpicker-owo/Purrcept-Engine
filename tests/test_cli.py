"""纯配置命令只使用包内模板，不取得宿主锁或启动模型。"""

from pathlib import Path

import pytest

from purrcept_engine.app.cli import main
from purrcept_engine.app.config import load_config


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
