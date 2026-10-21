"""明确授权后运行的有界官方接入验证；只输出状态和计数，不接管Engine输入。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from purrcept_engine.values import ActionProtocolError, JsonObject

from .bilibili import ProtocolError
from .credentials import CredentialError, WindowsCredentialStore
from .open_live import OpenLiveFailure, OpenLiveSource

APP_ID = 1783128932975
ROOM_ID = 1850493825


async def verify() -> JsonObject:
    """验证一个已保存项目的短时接收，关闭时结束场次。

    Returns:
        无秘密的UTC时间、项目/房间、状态和消息计数。

    Note:
        此入口会读取指定条目并创建真实官方场次，须用户填好凭据且授权后手动执行。
        不修改Engine配置、不读取浏览器、不输出事件正文、不发送聊天。
    """
    statuses: list[str] = []
    report: JsonObject = {
        "app_id": APP_ID,
        "room_id": ROOM_ID,
        "started_utc": datetime.now(UTC).isoformat(),
    }
    source = OpenLiveSource(
        ROOM_ID, APP_ID, lambda: WindowsCredentialStore().read(APP_ID), statuses.append
    )
    stream = source.messages()
    count = 0
    try:
        async with asyncio.timeout(20):
            async for _ in stream:
                count += 1
    except TimeoutError:
        report["result"] = "observation_finished"
    except CredentialError:
        report["result"] = "credential_required"
    except OpenLiveFailure as error:
        report["result"], report["code"] = error.status, error.code
    except (ProtocolError, ActionProtocolError):
        report["result"] = "protocol_error"
    finally:
        await stream.aclose()
    report["connected"] = "connected" in statuses
    report["events_received"] = count
    report["last_status"] = statuses[-1] if statuses else "not_started"
    report["ended_utc"] = datetime.now(UTC).isoformat()
    return report


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify()), ensure_ascii=False))
