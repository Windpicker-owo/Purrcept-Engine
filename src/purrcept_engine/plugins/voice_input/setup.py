"""显式取得固定 Silero VAD 模型，导入插件与宿主运行绝不自动下载。

只接受固定版本和校验和，先完整校验再原子替换目标文件。命令由操作者
明确执行；本模块不打开设备，不获取模型服务凭据或修改主体配置。
"""

from __future__ import annotations

import argparse
import hashlib
import os
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import httpx

URL = "https://raw.githubusercontent.com/snakers4/silero-vad/v5.1.2/src/silero_vad/data/silero_vad.onnx"
SHA256 = "2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f"
MAX_MODEL_BYTES = 4 * 1024 * 1024


def install_model(path: Path, client: httpx.Client) -> None:
    """用调用方显式传输下载固定模型，坏响应不留下可加载的半截文件。

    Args:
        path: 此操作定位的明确路径。
        client: 此组件显式借用的客户端，资源寿命由创建者负责。

    Raises:
        ValueError: VAD 模型下载超过固定限额；Silero VAD 模型校验和不匹配，目标文件未改动。
    """
    data = bytearray()
    with client.stream("GET", URL) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            if len(data) + len(chunk) > MAX_MODEL_BYTES:
                raise ValueError("VAD 模型下载超过固定限额。")
            data.extend(chunk)
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("Silero VAD 模型校验和不匹配，目标文件未改动。")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    """只有显式调用命令才下载，不自动安装 Python 依赖或启动收音。

    Args:
        argv: 命令行参数；None 读取当前进程参数。
    """
    parser = argparse.ArgumentParser(description="显式下载并校验本机 Silero VAD 模型。")
    parser.add_argument(
        "--output", type=Path, default=Path(".purrcept/voice-input/silero_vad.onnx")
    )
    path = parser.parse_args(argv).output.resolve()
    with httpx.Client(timeout=60, trust_env=False, follow_redirects=False) as client:
        install_model(path, client)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
