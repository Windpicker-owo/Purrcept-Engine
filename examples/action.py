"""嵌入宿主并执行显式控制台行动，不调用模型或启动设备。

配置须明确启用 console。等待与资源关闭分开：调用者即使不再观察某项行动，
仍须让宿主收回真实借用；关闭期限到达时继续等待同一次收尾。
"""

import asyncio
from pathlib import Path

from purrcept_engine import ConfigError, EngineHost, load_config


async def main() -> None:
    """读取本地配置，执行一次明确控制台行动并等待宿主归还资源。

    Raises:
        ConfigError: 配置不合法，或未明确启用 console 输出。

    Note:
        关闭期限到达后继续等待同一次收尾，不把观察超时当作资源已释放。
    """
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
