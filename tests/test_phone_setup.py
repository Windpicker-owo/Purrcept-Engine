"""真实临时配置验证手机向导的预览、锁、备份和证书身份，不启动模型或设备。"""

import tomllib
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

import pytest

from purrcept_engine.app.config import load_config
from purrcept_engine.app.phone_setup import apply, prepare
from purrcept_engine.errors import ConfigError, InstanceLocked
from purrcept_engine.infrastructure.locking import InstanceLock


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """占位模型契约仅用于配置边界，测试不会联系端点。"""
    path = tmp_path / "purrcept.toml"
    template = files("purrcept_engine.app").joinpath("default.toml").read_text("utf-8")
    path.write_text(template + "\n# user-comment-must-survive\n", encoding="utf-8")
    return path


def test_preview_is_read_only_and_apply_preserves_unrelated_configuration(
    config_path: Path,
) -> None:
    before = config_path.read_bytes()
    original = tomllib.loads(before.decode())
    plan = prepare(config_path, "192.0.2.10", 8766)
    assert config_path.read_bytes() == before
    assert not plan.certificate.exists()
    assert "replace-with-explicit-local-key" not in repr(plan)
    backup = apply(plan)
    assert backup.read_bytes() == before
    after = tomllib.loads(config_path.read_text("utf-8"))
    assert {key: value for key, value in after.items() if key != "plugins"} == {
        key: value for key, value in original.items() if key != "plugins"
    }
    assert "# user-comment-must-survive" in config_path.read_text("utf-8")
    assert plan.certificate.is_file() and plan.private_key.is_file()
    settings = load_config(config_path).plugins[0]
    assert settings.configuration()["public_url"] == "https://192.0.2.10:8766"
    assert settings.configuration()["listen_host"] == "0.0.0.0"
    assert settings.configuration()["pairing_enabled"] is True


def test_existing_phone_identity_and_other_plugin_comments_survive(config_path: Path) -> None:
    apply(prepare(config_path, "192.0.2.10", 8766))
    config_path.write_text(
        config_path.read_text("utf-8").replace('instance_id = "phone"', 'instance_id = "my-phone"')
        + '\n[[plugins]]\ninstance_id = "clock" # keep-clock\nentry = "clock"\n',
        encoding="utf-8",
    )
    old_certificate = (config_path.parent / ".purrcept/phone/phone-cert.pem").read_bytes()
    second = prepare(config_path, "192.0.2.11", 8899)
    assert not second.create_identity
    apply(second)
    assert second.certificate.read_bytes() == old_certificate
    config = load_config(config_path)
    assert [plugin.instance_id for plugin in config.plugins] == ["my-phone", "clock"]
    assert config.plugins[0].configuration()["public_url"] == "https://192.0.2.11:8899"
    assert "# keep-clock" in config_path.read_text("utf-8")


def test_running_engine_lock_blocks_all_writes(config_path: Path) -> None:
    plan = prepare(config_path, "192.0.2.10", 8766)
    lock = InstanceLock(plan.data_dir)
    lock.acquire()
    try:
        with pytest.raises(InstanceLocked):
            apply(plan)
        assert config_path.read_bytes() == plan.original
        assert not plan.certificate.exists()
    finally:
        lock.release()


def test_changed_configuration_requires_a_new_preview(config_path: Path) -> None:
    plan = prepare(config_path, "192.0.2.10", 8766)
    updated = plan.original + b"\n# another editor\n"
    config_path.write_bytes(updated)
    with pytest.raises(ConfigError, match="重新读取"):
        apply(plan)
    assert config_path.read_bytes() == updated
    assert not plan.certificate.exists()


@pytest.mark.parametrize(
    "address,port",
    [
        ("127.0.0.1", 8766),
        ("localhost", 8766),
        ("0.0.0.0", 8766),
        ("https://192.0.2.1", 8766),
        ("192.0.2.1:8888", 8766),
        ("bad/path", 8766),
        ("192.0.2.1", 0),
        ("192.0.2.1", 65536),
        ("224.0.0.1", 8766),
    ],
)
def test_invalid_targets_never_modify_configuration(
    config_path: Path, address: str, port: int
) -> None:
    before = config_path.read_bytes()
    with pytest.raises(ConfigError):
        prepare(config_path, address, port)
    assert config_path.read_bytes() == before


def test_failed_replace_keeps_original_and_backup_and_supports_repreview(config_path: Path) -> None:
    plan = prepare(config_path, "192.0.2.10", 8766)
    with patch("purrcept_engine.app.phone_setup.os.replace", side_effect=PermissionError):
        with pytest.raises(PermissionError):
            apply(plan)
    assert config_path.read_bytes() == plan.original
    backups = list(config_path.parent.glob("*.phone-backup-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == plan.original
    assert not list(config_path.parent.glob(".phone-setup-*"))
    fresh = prepare(config_path, "192.0.2.10", 8766)
    assert not fresh.create_identity
    apply(fresh)
    assert load_config(config_path).plugins[0].entry == "phone"


def test_incomplete_existing_identity_is_never_overwritten(config_path: Path) -> None:
    plan = prepare(config_path, "192.0.2.10", 8766)
    plan.certificate.parent.mkdir(parents=True)
    plan.certificate.write_bytes(b"existing-certificate")
    with pytest.raises(ConfigError, match="缺少"):
        prepare(config_path, "192.0.2.10", 8766)
    assert plan.certificate.read_bytes() == b"existing-certificate"
    assert not plan.private_key.exists()


def test_phone_port_cannot_collide_with_webui(config_path: Path) -> None:
    """WebUI 与 Phone 的 IPv4 监听不能共用端口，拒绝后仍可选择新端口预览。"""
    import tomlkit

    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    document["web"] = {"enabled": True, "port": 8766}
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")
    before = config_path.read_bytes()
    with pytest.raises(ConfigError, match="WebUI"):
        prepare(config_path, "192.0.2.10", 8766)
    assert config_path.read_bytes() == before
    assert prepare(config_path, "192.0.2.10", 8767).public_url == "https://192.0.2.10:8767"
