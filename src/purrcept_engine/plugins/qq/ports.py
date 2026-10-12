"""QQ 的有限传输边界：一次连接、接收帧与明确关停，不拥有主体或权威库。

生产适配器沿原 Python websockets asyncio API；离线样例只替换这个外部传输口。
连接异常与 RPC 阶段有不同意义，临时网络故障可恢复，协议/凭据错误不得循环。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from purrcept_engine.errors import EngineError
from purrcept_engine.execution.protocol import JsonObject

from .settings import QQSettings


@dataclass(frozen=True, slots=True)
class Reply:
    """同一连接代次收到的原始应答，字段解释和持久快照归具体能力。"""

    action: str
    generation: int
    echo: str
    response: JsonObject
    received_at: float


class ConnectionUnavailable(EngineError):
    """明确的临时网络或服务不可用；仅连接恢复所有者可以消耗有限尝试预算。"""


class ConnectionRejected(EngineError):
    """凭据、配置或协议失败；恢复同一配置不能解决，必须停止自动尝试。"""


class QQCallError(EngineError):
    """一次已关联 RPC 的阶段与平台错误，不授权重发或自动改用别名动作。"""

    def __init__(
        self,
        message: str,
        *,
        action: str,
        phase: Literal["not_started", "unknown", "rejected"],
        generation: int,
        echo: str | None = None,
        response: JsonObject | None = None,
    ) -> None:
        """保留真实代次/echo及收到的响应；未知不等同平台拒绝或确定未发送。"""
        super().__init__(message)
        self.action = action
        self.phase = phase
        self.generation = generation
        self.echo = echo
        self.response = response


class Connection(Protocol):
    """连接由 Connector 创建、QQ 所有者收回；收包与业务输入消费分离。"""

    async def send(self, data: str) -> None:
        """只发送这一帧，不做重试；开始 await 后失败须视为效果可能已发生。"""
        ...

    async def receive(self) -> str | bytes:
        """返回完整帧或明确断线/协议错误，不静默跳过非法数据。"""
        ...

    async def close(self) -> None:
        """实际解除收包等待并关闭连接，重复关闭不得重新取得连接。"""
        ...


class Listener(Protocol):
    """reverse 监听所有者；停止新接入与现有回包连接的关闭必须分开。"""

    @property
    def port(self) -> int:
        """返回已取得的真实监听端口。"""
        ...

    async def stop_accepting(self) -> None:
        """停止新握手，保留已经开始请求仍使用的连接。"""
        ...

    async def close(self) -> None:
        """收回监听及它持有的既有连接，不重复启动监听。"""
        ...


class Connector(Protocol):
    """插件 build 选定的单次传输适配器，不自行建立恢复循环或解码业务。"""

    async def connect(self, settings: QQSettings) -> Connection:
        """一次 direct 建连，临时故障与终止错误分类明确；取消收回半建资源。"""
        ...

    async def listen(
        self, settings: QQSettings, accepted: Callable[[Connection], Awaitable[None]]
    ) -> Listener:
        """实际建立 reverse 监听，外部接入经已配置 Bearer 校验后交给所有者。"""
        ...
