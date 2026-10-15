"""独立调音室命令，仅监听固定回环地址，不打开声卡或调用模型。"""

from __future__ import annotations

import argparse
import secrets
from collections.abc import Sequence
from pathlib import Path


def main(argv: Sequence[str] | None = None) -> int:
    """解析明确路径和端口后常驻服务，安装包中的静态资源无需 Node。

    Args:
        argv: 命令行参数；None 读取当前进程参数。
    """
    parser = argparse.ArgumentParser(description="Purrcept 独立声线调音室")
    parser.add_argument("--profile", type=Path, default=Path(".purrcept/voice-profile.json"))
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--sample-rate", type=int, default=32000, choices=(16000, 24000, 32000, 44100, 48000)
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.port <= 65535:
        parser.error("--port 必须在1到65535之间。")
    try:
        import uvicorn

        from purrcept_engine.app.studio.server import create_studio
    except ModuleNotFoundError as error:
        if error.name not in {"uvicorn", "starlette"}:
            raise
        parser.error("声线调音室需要web extra，请先安装 purrcept_engine[web]。")
    token = secrets.token_urlsafe(32)
    app = create_studio(
        arguments.profile,
        port=arguments.port,
        sample_rate=arguments.sample_rate,
        executable=arguments.ffmpeg,
        token=token,
    )
    print(f"声线调音室：http://127.0.0.1:{arguments.port}/；Ctrl+C 关闭。")
    uvicorn.run(app, host="127.0.0.1", port=arguments.port, access_log=False, proxy_headers=False)
    return 0
