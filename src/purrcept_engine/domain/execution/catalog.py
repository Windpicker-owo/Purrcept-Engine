"""启动时冻结对象与能力声明，并由同一绑定生成模型目录和执行路径。

Catalog 校验父链、路径和能力身份冲突；它不保存设备在线状态、闹铃、休息或
笔记正文。模型稳定目录只包含声明的契约，实时状态由主体的动态尾部投影。
具体插件实现与资源所有者由 app 装配，目录自身不打开设备。
"""

from __future__ import annotations

import hashlib
import json
import keyword
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel

from purrcept_engine.errors import ConfigError


@dataclass(frozen=True, slots=True)
class ObjectDeclaration:
    """插件声明的稳定对象，parent_id 表示实际设备/应用包含关系。

    Attributes:
        object_id: 目录中稳定的对象身份，区别于展示名称和模型路径。
        segment: 当前消息中的一个原生分段对象。
        description: 向调用者或模型说明可观察行为的文字。
        parent_id: 目录内父对象的稳定身份；None 表示根对象。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        segment 是父对象下的 Python 路径片段，根必须显式命名为 me 或 world。
        展示说明不参与对象身份；没有通用物品转让或人员可见名单状态。
    """

    object_id: str
    segment: str
    description: str
    parent_id: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityDeclaration[Parameters: BaseModel]:
    """插件提供调用契约，由 app 在启动时与一个实现精确绑定。

    Attributes:
        capability_id: 声明中与实现绑定的稳定能力身份。
        object_id: 目录中稳定的对象身份，区别于展示名称和模型路径。
        name: 对象路径下的公开 Python 能力名，不是稳定 capability_id。
        description: 向调用者或模型说明可观察行为的文字。
        parameters: 用于生成 schema 和校验真实调用的 Pydantic 参数模型。
        timeout_seconds: 本次操作的等待上限（秒），不得扩大所属行动或活动期限。
        example: 一行合法调用示例，目录冻结时解析 {path} 占位符。
        usage: 何时使用此能力及其必要限制的说明。
        concurrency: parallel 允许并行，target_serial 要求同一目标串行。
        control: 是否使用独立控制名额，避免普通输入阻塞真实回执收尾。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        参数模型拥有真实调用边界的字段校验。timeout_seconds 是该能力自己的
        有限期限；行动/活动剩余时间可进一步收紧。target_serial 防止同一目标
        的普通调用重叠；control 标记由宿主装配决定，模型参数不能自称控制请求。
        example 中的 {path} 在冻结目录时替换为完整能力路径，使可配置父节点
        不需要在声明阶段复制目录的路径解析规则；其他字符保持原文。
    """

    capability_id: str
    object_id: str
    name: str
    description: str
    parameters: type[Parameters]
    timeout_seconds: float
    example: str
    usage: str = ""
    concurrency: Literal["parallel", "target_serial"] = "parallel"
    control: bool = False


@dataclass(frozen=True, slots=True)
class Binding:
    """同一声明的冻结路径及 schema 文本，供目录、Worker 和执行服务共同使用。

    Attributes:
        path: 冻结目录中的完整能力调用路径，例如 me.console.output。
        declaration: 待绑定或计量的不可变声明。
        schema_json: 与参数模型一致的规范 JSON schema 文本。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    path: str
    declaration: CapabilityDeclaration[BaseModel]
    schema_json: str


@dataclass(frozen=True, slots=True, init=False)
class Catalog:
    """由 app 一次创建的完整不可变目录；运行期重连不重建或删掉能力。

    Attributes:
        objects: 按稳定 object_id 索引的只读对象声明。
        object_paths: 按对象身份索引的只读完整模型路径。
        bindings: 按完整调用路径索引的冻结能力绑定。
        instructions: 稳定 system 指令序列，实时状态不得混入缓存前缀。
        version: 目标记录的确切版本，区别于可变的当前版本。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        objects 和 capabilities 来自全部启用插件的纯声明。构造失败表示装配
        不成立，不能开始外部输入。版本由完整模型目录内容摘要确定，不混时间、
        请求身份、实时状态或剩余预算。变更通过重新装配及重启生效。
    """

    objects: Mapping[str, ObjectDeclaration]
    object_paths: Mapping[str, str]
    bindings: Mapping[str, Binding]
    instructions: str
    version: str

    def __init__(
        self,
        objects: tuple[ObjectDeclaration, ...],
        capabilities: tuple[CapabilityDeclaration[BaseModel], ...],
    ) -> None:
        """校验对象父链，再冻结全部能力路径。

        Args:
            objects: 用于构建目录的稳定对象声明及父子关系。
            capabilities: 与对象声明绑定的完整能力契约集合。

        Raises:
            ConfigError: 配置字段、资源声明或目录绑定不满足本入口的装配要求。

        Note:
            目录和派发不存在两套手填路径。
        """
        by_id: dict[str, ObjectDeclaration] = {}
        for item in objects:
            if not item.object_id or item.object_id in by_id:
                raise ConfigError("对象身份为空或重复。")
            _segment(item.segment)
            by_id[item.object_id] = item

        paths: dict[str, str] = {}
        for object_id in by_id:
            _object_path(object_id, by_id, paths, set())
        if len(set(paths.values())) != len(paths):
            raise ConfigError("不同对象占用了同一个模型路径。")

        bindings: dict[str, Binding] = {}
        capability_ids: set[str] = set()
        for declaration in capabilities:
            _segment(declaration.name)
            if not declaration.capability_id or declaration.capability_id in capability_ids:
                raise ConfigError("能力身份为空或重复。")
            capability_ids.add(declaration.capability_id)
            parent_path = paths.get(declaration.object_id)
            if parent_path is None:
                raise ConfigError("能力目标没有对应的对象声明。")
            path = f"{parent_path}.{declaration.name}"
            if path in bindings or path in paths.values():
                raise ConfigError("能力与对象路径冲突。")
            if (
                isinstance(declaration.timeout_seconds, bool)
                or not isfinite(declaration.timeout_seconds)
                or declaration.timeout_seconds <= 0
            ):
                raise ConfigError("能力必须声明正的有限超时。")
            if not declaration.description or not declaration.example:
                raise ConfigError("能力必须声明使用语义及调用示例。")
            if not issubclass(declaration.parameters, BaseModel):
                raise ConfigError("能力参数必须声明 Pydantic 模型。")

            # Pydantic 生成 JSON schema；保存规范文本，避免调用者改写字典使
            # 模型看见的契约与实际参数模型分离。
            schema_json = json.dumps(
                declaration.parameters.model_json_schema(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            bindings[path] = Binding(path, declaration, schema_json)

        # 构造阶段一次安装冻结值；运行期不能替换目录属性而沿用旧版本。
        object.__setattr__(self, "objects", MappingProxyType(by_id))
        object.__setattr__(self, "object_paths", MappingProxyType(paths))
        object.__setattr__(self, "bindings", MappingProxyType(dict(sorted(bindings.items()))))
        instructions = self._instructions()
        object.__setattr__(self, "instructions", instructions)
        object.__setattr__(
            self, "version", hashlib.sha256(instructions.encode("utf-8")).hexdigest()
        )

    def resolve(self, path: str) -> Binding:
        """解析精确已装配路径，不按别名猜目标或借设备在线状态隐藏声明。

        Args:
            path: 冻结目录中的完整能力调用路径，例如 me.console.output。

        Returns:
            精确路径对应的冻结绑定；不会按别名或在线状态猜测目标。

        Raises:
            LookupError: 参数、材料或当前状态不满足此入口的约束。
        """
        try:
            return self.bindings[path]
        except KeyError:
            raise LookupError(f"不存在已装配能力路径：{path}") from None

    def _instructions(self) -> str:
        """按确定次序生成稳定能力契约，不包含运行中的设备和工作状态。"""
        entries: list[str] = []
        for path, binding in self.bindings.items():
            declaration = binding.declaration
            example = declaration.example.replace("{path}", path)
            entries.append(
                f"{path}\n{declaration.description}\n"
                f"usage: {declaration.usage}\n"
                f"parameters: {binding.schema_json}\nexample: {example}"
            )
        return "\n\n".join(entries)


def _segment(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.isidentifier()
        or keyword.iskeyword(value)
        or value.startswith("_")
    ):
        raise ConfigError("模型路径片段必须是公开 Python 标识符。")


def _object_path(
    object_id: str,
    objects: dict[str, ObjectDeclaration],
    paths: dict[str, str],
    visiting: set[str],
) -> str:
    """沿父链生成路径并拒绝循环；同一对象只计算一次，不维护第二份父链。"""
    known_path = paths.get(object_id)
    if known_path is not None:
        return known_path
    if object_id in visiting:
        raise ConfigError("对象父链形成循环。")
    visiting.add(object_id)
    item = objects[object_id]
    if item.parent_id is None:
        if item.segment not in {"me", "world"}:
            raise ConfigError("根对象路径必须为 me 或 world。")
        path = item.segment
    else:
        if item.parent_id not in objects:
            raise ConfigError("对象父节点不存在。")
        parent_path = _object_path(item.parent_id, objects, paths, visiting)
        path = f"{parent_path}.{item.segment}"
    visiting.remove(object_id)
    paths[object_id] = path
    return path
