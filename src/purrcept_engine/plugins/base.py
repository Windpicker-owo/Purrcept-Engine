"""声明插件的配置、冻结能力和资源寿命，不启动外部接入。

插件工厂先将配置解析成纯 Blueprint，app 汇总完整目录后才调用 build。
Runtime 的停止接入与资源释放分开：已开始能力仍可借用资源直到 execution
确认收尾。插件不取得主脑会话或通用 SQLite 连接，也没有运行期热替换。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from purrcept_engine.execution.catalog import CapabilityDeclaration, ObjectDeclaration
from purrcept_engine.execution.protocol import JsonObject
from purrcept_engine.execution.providers import CapabilityImplementation
from purrcept_engine.storage.models import Admission, InputRecord, InputSubmission


class InputPort(Protocol):
    """宿主借给输入插件的专用接管端口，不授予模型或存储访问权限。

    外部适配器提交不可变原文后保存 input_id；取消等待不会撤销已接管事务。
    重连按原生事件身份重投或查询，不能以通知丢失推断此前未接管。
    """

    async def submit(self, submission: InputSubmission) -> Admission:
        """原子接管或明确拒收，接管不等同于主脑已经处理。"""
        ...

    async def query_input(self, input_id: str) -> InputRecord | None:
        """读取实际接管及处理状态，不执行模型或再次发送输入。"""
        ...


@dataclass(frozen=True, slots=True)
class PluginPorts:
    """app 显式注入的有限依赖；字段增加须对应真实业务交接。

    inputs 用于接管与查询；snapshot 读取宿主提交后的只读投影。设备客户端
    和通用数据库不在端口中。更多业务使用所属子系统的专用协议注入。
    """

    inputs: InputPort
    snapshot: Callable[[], JsonObject]


@dataclass(frozen=True, slots=True)
class PluginRuntime:
    """build 交回的资源所有者及实际能力绑定，由宿主一次取得和收回。

    start 在完整目录及输入端口就绪后调用；stop_inputs 只停止新接入及重连，
    不关闭仍被已开始调用使用的连接。close 在 execution 收尾后调用一次。
    无对应资源时不必提供空生命周期函数。内部异常原样失败，外围离线应由
    具体插件在其状态投影中表达。所有后台任务必须归这些函数的所有者管理。
    """

    capabilities: tuple[CapabilityImplementation, ...]
    start: Callable[[], Awaitable[None]] | None = None
    stop_inputs: Callable[[], Awaitable[None]] | None = None
    close: Callable[[], Awaitable[None]] | None = None
    snapshot: Callable[[], JsonObject] | None = None


@dataclass(frozen=True, slots=True)
class PluginBlueprint:
    """由工厂纯解析得到的声明和构造函数，配置校验不执行 build。

    required_modules 是明确启用时必须可导入的依赖，不用于自动安装。对象父链
    与能力声明经统一 Catalog 核验；build 返回的实现须精确对应这些声明。
    工厂可把已校验配置保存在 build 闭包，不能在声明阶段打开设备或网络。
    build 返回前取得的资源由具体插件所有者登记；部分构造失败必须在该
    所有者中清理并保留原错误，不能要求尚未取得 Runtime 的宿主猜测资源。
    """

    objects: tuple[ObjectDeclaration, ...]
    capabilities: tuple[CapabilityDeclaration[BaseModel], ...]
    build: Callable[[PluginPorts], Awaitable[PluginRuntime]]
    required_modules: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PluginFactory:
    """内置登记或外部 module:attribute 导出的纯插件配置工厂。

    app 传入唯一实例身份、原始配置和主配置绝对基准目录。prepare 负责自身
    字段与声明路径解析，并拒绝未知字段；它不能探测网络或打开设备。
    使用 typed_factory 可让具体插件保持精确参数类型而无需 cast。
    """

    prepare: Callable[[str, object, Path], PluginBlueprint]


def typed_factory[Settings: BaseModel](
    settings_type: type[Settings],
    declare: Callable[[str, Settings, Path], PluginBlueprint],
) -> PluginFactory:
    """连接具体 Pydantic 配置与纯声明；字段脱敏由应用配置边界统一完成。"""

    def prepare(instance_id: str, raw: object, base_dir: Path) -> PluginBlueprint:
        settings = settings_type.model_validate(raw)
        return declare(instance_id, settings, base_dir)

    return PluginFactory(prepare)
