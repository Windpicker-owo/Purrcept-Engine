"""QQ 的有限传输边界：一次连接、接收帧与明确关停，不拥有主体或权威库。

生产适配器沿原 Python websockets asyncio API；离线样例只替换这个外部传输口。
连接异常与 RPC 阶段有不同意义，临时网络故障可恢复，协议/凭据错误不得循环。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from purrcept_engine.errors import EngineError
from purrcept_engine.plugins.qq.settings import QQSettings
from purrcept_engine.values import JsonObject


@dataclass(frozen=True, slots=True)
class Reply:
    """同一连接代次收到的原始应答，字段解释和持久快照归具体能力。

    Attributes:
        action: 原生 OneBot 操作名，与回包 echo 分别关联。
        generation: 当前会话或设备的代次，用于拒绝迟到结果更新新状态。
        echo: 当前原生请求的回包关联身份，不是业务幂等键。
        response: 当前后端或原生协议实际交付的完整响应。
        received_at: 此事实实际接收的时刻。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

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
        """保留真实代次/echo及收到的响应。

        Args:
            message: 对调用方公开的原因或错误说明。
            action: 原生 OneBot 操作名，与回包 echo 分别关联。
            phase: 宿主实际生命周期阶段，与查询版本同时更新。
            generation: 当前会话或设备的代次，用于拒绝迟到结果更新新状态。
            echo: 当前原生请求的回包关联身份，不是业务幂等键。
            response: 当前后端或原生协议实际交付的完整响应。

        Note:
            未知不等同平台拒绝或确定未发送。
        """
        super().__init__(message)
        self.action = action
        self.phase = phase
        self.generation = generation
        self.echo = echo
        self.response = response


class Connection(Protocol):
    """连接由 Connector 创建、QQ 所有者收回；收包与业务输入消费分离。"""

    async def send(self, data: str) -> None:
        """只发送这一帧，不做重试。

        Args:
            data: 当前边界接收的原始协议或媒体内容。

        Note:
            开始 await 后失败须视为效果可能已发生。
        """
        ...

    async def receive(self) -> str | bytes:
        """返回完整帧或明确断线/协议错误，不静默跳过非法数据。

        Returns:
            当前原生连接实际交付的文本或二进制消息。
        """
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
        """一次 direct 建连，临时故障与终止错误分类明确。

        Args:
            settings: 当前组件已校验的配置或纯声明。

        Returns:
            当前尝试实际取得的原生连接，由创建它的所有者最终关闭。

        Note:
            取消收回半建资源。
        """
        ...

    async def listen(
        self, settings: QQSettings, accepted: Callable[[Connection], Awaitable[None]]
    ) -> Listener:
        """实际建立 reverse 监听，外部接入经已配置 Bearer 校验后交给所有者。

        Args:
            settings: 当前组件已校验的配置或纯声明。
            accepted: 接受新连接时移交实际连接所有权的异步函数。

        Returns:
            当前所有者创建的反向连接监听资源。
        """
        ...
