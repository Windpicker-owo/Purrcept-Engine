"""准备手机 TLS 身份或在宿主停止后撤销配对，不启动 Engine 或模型。

init 用排他新建文件避免覆盖既有证书。revoke 必须先持有实际数据目录锁，
通过同库账本撤销设备凭据；历史原文和行动回执不删除。
"""

import argparse
import asyncio
import ipaddress
from datetime import UTC, datetime, timedelta
from pathlib import Path


def initialize(directory: Path, address: str) -> None:
    """生成自签 TLS 证书；手机通过本机邀请中的完整证书指纹验证身份。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    directory.mkdir(parents=True, exist_ok=True)
    certificate_path, key_path = directory / "phone-cert.pem", directory / "phone-key.pem"
    if certificate_path.exists() or key_path.exists():
        raise FileExistsError("手机 TLS 文件已经存在，拒绝覆盖。")
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Purrcept Phone")])
    try:
        san = x509.IPAddress(ipaddress.ip_address(address))
    except ValueError:
        san = x509.DNSName(address)
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([san]), critical=False)
        .sign(key, hashes.SHA256())
    )
    with key_path.open("xb") as output:
        key_path.chmod(0o600)
        output.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    with certificate_path.open("xb") as output:
        output.write(certificate.public_bytes(serialization.Encoding.PEM))
    print(f"手机 TLS 已创建于 {directory}。配置 public_url=https://{address}:8766；不要外发私钥。")


async def revoke(directory: Path, instance_id: str) -> None:
    """实际取得目录锁后撤销同库设备，运行中宿主不会被抢占。"""
    from purrcept_engine.infrastructure.locking import InstanceLock
    from purrcept_engine.infrastructure.sqlite.store import Store

    if not (directory / "engine.sqlite3").is_file():
        raise FileNotFoundError("未找到现有 Engine 数据库。")
    lock = InstanceLock(directory)
    lock.acquire()
    try:
        store = await Store.open(directory / "engine.sqlite3")
        try:
            await store.phone(instance_id).revoke()
        finally:
            await store.close()
    finally:
        lock.release()
    print("手机配对已撤销，历史消息和行动记录已保留。")


def main() -> int:
    """提供证书准备和离线配对撤销入口；日常配对在 Engine WebUI。"""
    parser = argparse.ArgumentParser(prog="purrcept-phone")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("init")
    create.add_argument("--directory", type=Path, default=Path(".purrcept/phone"))
    create.add_argument("--address", required=True)
    remove = sub.add_parser("revoke")
    remove.add_argument("--data-dir", type=Path, required=True)
    remove.add_argument("--instance-id", default="phone")
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.directory.resolve(), args.address)
    else:
        asyncio.run(revoke(args.data_dir.resolve(), args.instance_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
