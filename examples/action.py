"""嵌入宿主并执行显式控制台行动，不调用模型或启动设备。

配置须明确启用 console。等待与资源关闭分开：调用者即使不再观察某项行动，
仍须让宿主收回真实借用；关闭期限到达时继续等待同一次收尾。
"""

import asyncio
from pathlib import Path

from purrcept_engine import ConfigError, EngineHost, load_config


async def main() -> None:
    """使用新 Engine 入口；旧 Runtime 的单次 ask 命令不做转发。"""
    config = load_config(Path("purrcept.toml"))
    if not config.console:
        raise ConfigError("本示例要求配置 console = true。")
    host = EngineHost(config)
    try:
        await host.start()
        result = await host.execute('from world import me\nawait me.console.output(text="你好。")')
        for call in result.calls:
            print(f"{call.call_id}: {call.state}")
    finally:
        snapshot = await host.close()
        if snapshot.phase != "closed":
            print(f"关闭未完成：{snapshot.pending_resources}")
            await host.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
