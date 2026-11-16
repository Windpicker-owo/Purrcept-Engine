"""为本机手机向导准备与提交 Phone 配置，不启动主体或访问模型。

prepare 只读配置并生成可审阅计划；apply 在用户点击保存后取得或借用真实数据目录锁，
再校验文件版本、准备 TLS、备份并原子替换配置。原有模型字段和其他插件保持
原值与注释，已存在的证书身份不自动轮换。WebUI 路由位于 phone_web。
"""

from __future__ import annotations

import ipaddress
import os
import socket
import ssl
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import tomlkit
from tomlkit.exceptions import ParseError
from tomlkit.items import AoT, Array, Table

from purrcept_engine.app.config import EngineConfig
from purrcept_engine.app.phone_cli import initialize
from purrcept_engine.errors import ConfigError
from purrcept_engine.infrastructure.locking import InstanceLock
from purrcept_engine.plugins.phone.plugin import PhoneSettings


@dataclass(frozen=True, slots=True)
class PhoneSetupPlan:
    """本次只读预览的固定版本；只有 apply 接收它并写入。

    配置正文可能含模型凭据，不能进入 repr、界面、日志或网络。公开字段仅供
    向导显示路径、地址和将创建的身份。原字节同时用于防止覆盖别人的新编辑。
    """

    path: Path
    public_url: str
    invitation: Path
    certificate: Path
    private_key: Path
    data_dir: Path
    create_identity: bool
    original: bytes = field(repr=False)
    proposed: str = field(repr=False)


def candidate_addresses() -> tuple[str, ...]:
    """读取本机 IPv4 候选，不发送探测包或联系手机。

    Returns:
        排除回环、链路本地和未指定地址后的去重列表。多网卡选择仍由用户决定。
    """
    addresses: set[str] = set()
    for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        address = ipaddress.IPv4Address(item[4][0])
        if not (address.is_loopback or address.is_link_local or address.is_unspecified):
            addresses.add(str(address))
    return tuple(
        sorted(addresses, key=lambda value: (not ipaddress.ip_address(value).is_private, value))
    )


def prepare(path: Path, address: str, port: int) -> PhoneSetupPlan:
    """读取有效 Engine 配置并预览唯一内置 Phone 实例的改动。

    Args:
        path: 用户选择的现有 Engine TOML 文件。
        address: 手机可访问的电脑 IP 或 DNS 主机名，不包含协议、端口和路径。
        port: 手机监听端口。保存会明确启用相应地址族的全部网卡监听。

    Returns:
        不含副作用的配置计划；原配置正文只保存在隐藏字段。

    Raises:
        ConfigError: 原配置、地址、Phone 声明或证书路径无法安全用于本次操作。
        OSError: 文件无法读取。未修改任何文件。
        ValidationError: 既有 Phone 声明不符合当前字段契约；调用者不得回显原始校验输入。
    """
    path = path.resolve()
    original = path.read_bytes()
    try:
        text = original.decode("utf-8-sig")
        raw = tomllib.loads(text)
        config = EngineConfig.validate(raw, base_dir=path.parent)
        document = tomlkit.parse(text)
    except (UnicodeError, tomllib.TOMLDecodeError, ParseError):
        raise ConfigError("无法解析配置；请先修复 TOML 格式，原文件未改动。") from None

    address = address.strip()
    try:
        parsed_ip = ipaddress.ip_address(address)
    except ValueError:
        parsed_ip = None
    host = f"[{address}]" if parsed_ip is not None and parsed_ip.version == 6 else address
    try:
        url = urlsplit(f"https://{host}:{port}")
        valid_address = (
            address.lower() not in {"localhost", "localhost.localdomain"}
            and bool(address)
            and not any(c.isspace() for c in address)
            and not any(c in address for c in "/@?#\\")
            and url.hostname == address.lower()
            and url.port == port
            and 1 <= port <= 65535
            and not (
                parsed_ip is not None
                and (parsed_ip.is_loopback or parsed_ip.is_unspecified or parsed_ip.is_multicast)
            )
        )
    except ValueError:
        valid_address = False
    if not valid_address:
        raise ConfigError("请填写手机可访问的电脑地址；不要填写 localhost、协议、端口或路径。")
    if config.web.enabled and port == config.web.port:
        raise ConfigError("此端口已由 Engine WebUI 使用，请为手机选择其他端口，例如 8767。")

    matches = [index for index, plugin in enumerate(config.plugins) if plugin.entry == "phone"]
    if len(matches) > 1:
        raise ConfigError("存在多个 Phone 声明，请先保留一个明确的手机实例。")
    if not matches and any(plugin.instance_id == "phone" for plugin in config.plugins):
        raise ConfigError("phone 实例名已被其他插件占用，请先调整该实例名。")

    if matches:
        index = matches[0]
        settings = PhoneSettings.model_validate(config.plugins[index].configuration())
    else:
        index = None
        settings = PhoneSettings(
            public_url=f"https://{host}:{port}",
            certificate=Path(".purrcept/phone/phone-cert.pem"),
            private_key=Path(".purrcept/phone/phone-key.pem"),
        )
    certificate = _absolute(path.parent, settings.certificate)
    private_key = _absolute(path.parent, settings.private_key)
    if certificate.exists() != private_key.exists():
        raise ConfigError("已有 TLS 身份缺少证书或私钥；不会自动覆盖，请先恢复完整身份。")
    create_identity = not certificate.exists()
    if create_identity and (
        certificate.name != "phone-cert.pem"
        or private_key != certificate.with_name("phone-key.pem")
    ):
        raise ConfigError("自定义证书文件尚未准备好；请先提供完整身份，或使用默认 Phone 证书路径。")
    if not create_identity:
        _validate_identity(certificate, private_key)

    entries = document.item("plugins") if "plugins" in document else None
    if isinstance(entries, Array) and len(entries) == 0:
        del document["plugins"]
        entries = None
    if entries is None:
        entries = tomlkit.aot()
        document["plugins"] = entries
    if not isinstance(entries, AoT):
        raise ConfigError("插件数组不是可编辑的数组表；原文件未改动。")
    if index is None:
        selected = tomlkit.table()
        selected["instance_id"] = "phone"
        selected["entry"] = "phone"
        entries.insert(len(entries), selected)
    else:
        selected = entries[index]
    selected["enabled"] = True
    values = selected.item("config") if "config" in selected else None
    if values is None:
        values = tomlkit.table()
        selected["config"] = values
    if not isinstance(values, Table):
        raise ConfigError("Phone 配置不是标准配置表；原文件未改动。")
    for key, value in {
        "listen_host": "::" if parsed_ip is not None and parsed_ip.version == 6 else "0.0.0.0",
        "port": port,
        "public_url": f"https://{host}:{port}",
        "certificate": str(settings.certificate).replace("\\", "/"),
        "private_key": str(settings.private_key).replace("\\", "/"),
        "pairing_enabled": True,
        "invitation_file": str(settings.invitation_file).replace("\\", "/"),
    }.items():
        values[key] = value
    proposed = tomlkit.dumps(document)
    EngineConfig.validate(tomllib.loads(proposed), base_dir=path.parent)
    return PhoneSetupPlan(
        path,
        f"https://{host}:{port}",
        _absolute(path.parent, settings.invitation_file),
        certificate,
        private_key,
        config.host.data_dir,
        create_identity,
        original,
        proposed,
    )


def apply(plan: PhoneSetupPlan, *, owner_data_dir: Path | None = None) -> Path:
    """用户确认后备份并提交计划，不停止或重启 Engine。

    Args:
        plan: prepare 生成且尚未失效的预览。
        owner_data_dir: 仅供已持有运行锁的本机 WebUI 使用；必须是当前宿主的数据目录。
            WebUI 只保存下次启动的配置，不重载当前插件。外部调用保持 None 并自行取得锁。

    Returns:
        与原文件相同目录、排他新建的备份位置。

    Raises:
        InstanceLocked: Engine 仍持有数据目录；用户须先正常关闭它。
        ConfigError: 预览后文件发生变化，拒绝覆盖。
        OSError: 写入失败；备份与已创建的 TLS 保留供恢复或重试。

    Note:
        锁只覆盖写入资格，不能将锁文件是否存在视为 Engine 是否运行。
        同目录临时文件以 os.replace 发布，按平台能力设置私有权限。
        Windows 备份与临时文件继承配置目录 ACL。
    """
    if owner_data_dir is not None and owner_data_dir.resolve() != plan.data_dir.resolve():
        raise ConfigError("配置的数据目录与当前宿主不一致，拒绝保存。")
    lock = InstanceLock(plan.data_dir) if owner_data_dir is None else None
    if lock is not None:
        lock.acquire()
    try:
        if plan.path.read_bytes() != plan.original:
            raise ConfigError("预览后配置已被修改；请重新读取后再保存。")
        if plan.create_identity:
            initialize(plan.certificate.parent, urlsplit(plan.public_url).hostname or "")
        else:
            _validate_identity(plan.certificate, plan.private_key)
        backup = plan.path.with_name(plan.path.name + ".phone-backup-" + uuid4().hex[:12])
        with backup.open("xb") as output:
            backup.chmod(0o600)
            output.write(plan.original)
        descriptor, temporary = tempfile.mkstemp(prefix=".phone-setup-", dir=plan.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(plan.proposed.encode("utf-8"))
                output.flush()
                os.fsync(output.fileno())
            # 其他编辑器不共享数据目录锁；提交前再查版本，但这与外部编辑不是原子事务。
            if plan.path.read_bytes() != plan.original:
                raise ConfigError("保存期间配置已被其他编辑器修改，请重新读取。")
            os.replace(temporary, plan.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return backup
    finally:
        if lock is not None:
            lock.release()


def _absolute(base: Path, path: Path) -> Path:
    return path if path.is_absolute() else base / path


def _validate_identity(certificate: Path, private_key: Path) -> None:
    """只核验 TLS 成对可用，不打开网络；固定错误不回显私钥或解析正文。"""
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # GUI 不拥有私钥口令交互；加密私钥必须立即报错，不能让 OpenSSL 在终端等候密码。
        context.load_cert_chain(certificate, private_key, password=lambda: b"")
    except (OSError, ssl.SSLError):
        raise ConfigError("已有证书和私钥无法配套使用；原身份不会被覆盖。") from None
