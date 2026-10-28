"""解析显式插件入口并汇总纯声明，运行资源由 EngineHost 取得。

配置校验与启动共用 prepare_plugins。只加载显式启用的工厂，不扫描插件市场，
也不因查询声明打开网络或设备。全部纯声明和依赖核验通过后才能执行 build；
Catalog 是模型目录、Worker 绑定与具体能力实现之间的唯一契约。
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from pydantic import ValidationError

from purrcept_engine.app.activity import ACTIVITY_DECLARATION
from purrcept_engine.app.config import PluginSettings
from purrcept_engine.app.console import console_blueprint
from purrcept_engine.domain.execution.catalog import Catalog, ObjectDeclaration
from purrcept_engine.errors import ConfigError
from purrcept_engine.plugin_system.contracts import PluginBlueprint, PluginFactory
from purrcept_engine.plugins.images import images_blueprint

_BUILTIN_ENTRIES = {
    "notebook": "purrcept_engine.plugins.notebook:factory",
    "clock": "purrcept_engine.plugins.time:factory",
    "bed": "purrcept_engine.plugins.rest:factory",
    "qq": "purrcept_engine.plugins.qq:factory",
    "computer": "purrcept_engine.plugins.computer:factory",
    "browser": "purrcept_engine.plugins.browser.plugin:factory",
    "meme": "purrcept_engine.plugins.meme:factory",
    "microphone": "purrcept_engine.plugins.audio.plugin:factory",
    "live2d": "purrcept_engine.plugins.live2d.plugin:factory",
    "voice_input": "purrcept_engine.plugins.voice_input:factory",
}


@dataclass(frozen=True, slots=True)
class PreparedPlugins:
    """已核验的冻结目录与按显式配置顺序排列的资源构造计划。

    Attributes:
        catalog: 全部已启用插件共同生成的冻结目录。
        blueprints: 按已核验依赖排列的插件实例身份与纯声明。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    catalog: Catalog
    blueprints: tuple[tuple[str, PluginBlueprint], ...]


def prepare_plugins(
    settings: tuple[PluginSettings, ...],
    *,
    base_dir: Path,
    factories: Mapping[str, PluginFactory],
    console_output: Callable[[str], None] | None = None,
) -> PreparedPlugins:
    """纯校验所有启用插件及依赖，失败前不构造任何实际资源。

    Args:
        settings: 当前组件已校验的配置或纯声明。
        base_dir: 解析相对配置路径的基准目录。
        factories: 按配置入口名显式登记的可信插件工厂。
        console_output: 显式控制台能力使用的文字输出回调。

    Returns:
        全部已核验 Blueprint 与冻结目录，不包含已启动的设备。

    Raises:
        ConfigError: 配置字段、资源声明或目录绑定不满足本入口的装配要求。

    Note:
        factories 是应用明确登记的内置或程序式工厂；外部 entry 采用包的
        module:attribute。导入工厂必须遵循纯声明契约，第三方 Python 插件属于
        可信宿主代码，不声称以 Worker 沙箱隔离插件本身。
    """
    if not base_dir.is_absolute():
        raise ConfigError("插件路径解析必须提供绝对基准目录。")
    if any(item.enabled and item.instance_id == "image" for item in settings):
        raise ConfigError("plugins: image实例身份已由主体默认看图能力占用。")
    if console_output is not None and any(
        item.enabled and item.instance_id == "console" for item in settings
    ):
        raise ConfigError("plugins: console 实例身份已由控制台接入占用。")
    objects = [
        ObjectDeclaration("subject", "me", "持续主体"),
        ObjectDeclaration("world", "world", "已装配环境"),
    ]
    blueprints: list[tuple[str, PluginBlueprint]] = [("image", images_blueprint(base_dir))]
    if console_output is not None:
        console = console_blueprint(console_output)
        objects.extend(console.objects)
        blueprints.append(("console", console))
    for item in settings:
        if not item.enabled:
            continue
        factory = factories.get(item.entry)
        if factory is None:
            factory = _external_factory(item.entry, base_dir)
        try:
            blueprint = factory.prepare(item.instance_id, item.configuration(), base_dir)
        except ValidationError as error:
            issues = error.errors(include_input=False, include_context=False, include_url=False)
            fields = [".".join(str(part) for part in issue["loc"]) for issue in issues]
            raise ConfigError(
                f"plugins.{item.instance_id}: 配置字段无效：{', '.join(fields)}"
            ) from None
        for module_name in blueprint.required_modules:
            try:
                available = importlib.util.find_spec(module_name) is not None
            except (ImportError, ModuleNotFoundError, ValueError):
                available = False
            if not available:
                raise ConfigError(f"plugins.{item.instance_id}: 缺少依赖 {module_name}")
        objects.extend(blueprint.objects)
        blueprints.append((item.instance_id, blueprint))
    # phone 是启用应用的实际包含关系；目录不生成没有业务的遥控能力。
    if any(obj.parent_id == "phone" for obj in objects):
        objects.append(ObjectDeclaration("phone", "phone", "手机应用", "subject"))
    capabilities = (
        ACTIVITY_DECLARATION,
        *tuple(capability for _, blueprint in blueprints for capability in blueprint.capabilities),
    )
    catalog = Catalog(tuple(objects), capabilities)
    return PreparedPlugins(catalog, tuple(blueprints))


def _external_factory(entry: str, base_dir: Path) -> PluginFactory:
    """只解析显式包入口；错误定位入口而不输出可能包含配置秘密的异常。"""
    # 内置别名同样只在明确启用时导入纯工厂；未启用可选设备不会加载依赖。
    module_name, separator, attribute = _BUILTIN_ENTRIES.get(entry, entry).partition(":")
    if (
        not separator
        or not module_name
        or not attribute.isidentifier()
        or attribute.startswith("_")
    ):
        raise ConfigError(f"插件入口未登记且不是 module:attribute：{entry}")
    try:
        module = (
            _local_module(module_name.removeprefix("local/"), base_dir)
            if module_name.startswith("local/")
            else importlib.import_module(module_name)
        )
        value: object = getattr(module, attribute)
    except (ImportError, AttributeError):
        raise ConfigError(f"无法加载插件纯声明入口：{entry}") from None
    if not isinstance(value, PluginFactory):
        raise ConfigError(f"插件入口必须导出 PluginFactory：{entry}")
    return value


def _local_module(name: str, base_dir: Path) -> ModuleType:
    """只加载显式 local/包名 对应的可信 plugins 子包，不扫描其他文件。"""
    if not name.isidentifier() or name.startswith("_"):
        raise ConfigError("本地插件入口必须为 local/包名:factory。")
    root = (base_dir / "plugins").resolve()
    package = (root / name).resolve()
    if package.parent != root:
        raise ConfigError("本地插件必须位于配置目录的 plugins 子目录。")
    source = package / "__init__.py"
    if not source.is_file():
        raise ConfigError(f"本地插件不存在：plugins/{name}/__init__.py")
    identity = "_purrcept_local_" + hashlib.sha256(str(package).encode()).hexdigest()[:20]
    existing = sys.modules.get(identity)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        identity, source, submodule_search_locations=[str(package)]
    )
    if spec is None or spec.loader is None:
        raise ConfigError(f"无法加载本地插件：{name}")
    module: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules[identity] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # 导入失败清除半成品，保留原异常；不自动重试导入或装配。
        for key in tuple(sys.modules):
            if key == identity or key.startswith(identity + "."):
                del sys.modules[key]
        raise
    return module
