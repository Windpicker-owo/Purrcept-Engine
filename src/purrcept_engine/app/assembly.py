"""解析显式插件入口并汇总纯声明，运行资源由 EngineHost 取得。

配置校验与启动共用 prepare_plugins。只加载显式启用的工厂，不扫描插件市场，
也不因查询声明打开网络或设备。全部纯声明和依赖核验通过后才能执行 build；
Catalog 是模型目录、Worker 绑定与具体能力实现之间的唯一契约。
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

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
    "microphone": "purrcept_engine.plugins.audio.plugin:factory",
    "live2d": "purrcept_engine.plugins.live2d.plugin:factory",
    "voice_input": "purrcept_engine.plugins.voice_input:factory",
}


@dataclass(frozen=True, slots=True)
class PreparedPlugins:
    """已核验的冻结目录与按显式配置顺序排列的资源构造计划。"""

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
            factory = _external_factory(item.entry)
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


def _external_factory(entry: str) -> PluginFactory:
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
        module = importlib.import_module(module_name)
        value: object = getattr(module, attribute)
    except (ImportError, AttributeError):
        raise ConfigError(f"无法加载插件纯声明入口：{entry}") from None
    if not isinstance(value, PluginFactory):
        raise ConfigError(f"插件入口必须导出 PluginFactory：{entry}")
    return value
