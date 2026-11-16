"""验证核心分层依赖方向、顶层公开入口与历史数据库版本边界。

只守护真正防止架构腐化的单向依赖边界和核心门面能力存在性，不再用 ast.dump 锁死
EngineHost 的具体形参结构，允许向门面自然增加可选参数或管理接口。
"""

import ast
import sqlite3
from pathlib import Path

import pytest

import purrcept_engine
from purrcept_engine.infrastructure.sqlite.store import Store
from purrcept_engine.values import JsonObject, decode_object

_ROOT = Path(__file__).parents[1]
_SOURCE = _ROOT / "src/purrcept_engine"
_BASELINE = Path(__file__).with_name("fixtures") / "architecture_baseline.json"

_REQUIRED_HOST_METHODS: dict[str, bool] = {
    "__init__": False,
    "start": True,
    "close": True,
    "submit": True,
    "execute": True,
    "invoke": True,
    "query": False,
    "query_health": False,
    "query_panel": True,
    "submit_control": True,
    "query_control": True,
    "query_reviews": True,
    "query_review": True,
}


def baseline() -> JsonObject:
    """读取历史数据库 DDL 基线用于测试旧版本升级拒绝边界。"""
    return decode_object(_BASELINE.read_bytes())


def imports(tree: ast.AST, package: str) -> tuple[str, ...]:
    """提取模块内的全部导入目标（含相对导入与类型注解导入）。"""
    result: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.extend(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                prefix = parts[: len(parts) - node.level + 1]
                module = ".".join(prefix + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            result.append(module)
            result.extend(module + "." + item.name for item in node.names)
    return tuple(result)


@pytest.mark.parametrize("area", ["domain", "infrastructure", "plugin_system", "plugins"])
def test_dependency_direction_has_no_reverse_resource_import(area: str) -> None:
    """守护核心单向依赖：领域层不反向依赖应用/基础设施，插件之间不私自跨包耦合。"""
    forbidden = {
        "domain": (
            "purrcept_engine.app",
            "purrcept_engine.infrastructure",
            "purrcept_engine.plugins",
            "purrcept_engine.plugin_system",
            "purrcept_litellm",
            "litellm",
            "openai",
            "sqlite3",
            "subprocess",
            "multiprocessing",
            "ctypes",
            "importlib",
        ),
        "infrastructure": ("purrcept_engine.app", "purrcept_engine.plugins"),
        "plugin_system": (
            "purrcept_engine.app",
            "purrcept_engine.infrastructure",
            "purrcept_engine.plugins",
        ),
        "plugins": ("purrcept_engine.app", "purrcept_engine.infrastructure.sqlite"),
    }[area]
    violations: list[str] = []
    for path in (_SOURCE / area).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        package = ".".join(("purrcept_engine", *path.relative_to(_SOURCE).parent.parts))
        for dependency in imports(tree, package):
            if any(dependency == item or dependency.startswith(item + ".") for item in forbidden):
                violations.append(f"{path.relative_to(_SOURCE)} -> {dependency}")
        if area == "domain":
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "httpx"
                    and node.func.attr in {"AsyncClient", "Client"}
                ):
                    violations.append(f"{path.relative_to(_SOURCE)} constructs HTTP resource")
        if area == "plugins":
            own = path.relative_to(_SOURCE / "plugins").parts[0].removesuffix(".py")
            for dependency in imports(tree, package):
                for other in ("audio", "live2d", "computer", "browser", "qq", "voice_input"):
                    prefix = "purrcept_engine.plugins." + other
                    if other != own and (
                        dependency == prefix or dependency.startswith(prefix + ".")
                    ):
                        violations.append(f"{path.relative_to(_SOURCE)} -> {dependency}")
    assert violations == []


def test_host_core_entrypoints_and_root_exports_exist() -> None:
    """验证 EngineHost 核心公开方法与顶层包导出可用，不锁死可选参数扩展。"""
    tree = ast.parse((_SOURCE / "app/host.py").read_text(encoding="utf-8"))
    host = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "EngineHost"
    )
    methods: dict[str, bool] = {}
    properties: set[str] = set()
    for node in host.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            is_prop = any(
                isinstance(dec, ast.Name) and dec.id == "property" for dec in node.decorator_list
            )
            if is_prop:
                properties.add(node.name)
            else:
                methods[node.name] = isinstance(node, ast.AsyncFunctionDef)

    assert {"config", "catalog"} <= properties
    for name, expected_async in _REQUIRED_HOST_METHODS.items():
        assert name in methods, f"EngineHost 缺少公开方法 {name}"
        assert methods[name] is expected_async, f"EngineHost.{name} 异步契约不匹配"

    original = baseline()
    expected_exports = original["root_exports"]
    assert isinstance(expected_exports, list)
    assert set(expected_exports) <= set(purrcept_engine.__all__)


async def test_legacy_architecture_database_is_rejected_without_mutation(tmp_path: Path) -> None:
    """确认不受支持的远古数据库格式会被安全拒绝且不破坏原文件字节。"""
    from purrcept_engine.errors import StorageError

    original = baseline()
    statements = original["statements"]
    assert isinstance(statements, list)
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        for statement in statements:
            assert isinstance(statement, str)
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version={original['format_version']}")
        connection.execute(f"PRAGMA application_id={original['application_id']}")
    before = path.read_bytes()
    with pytest.raises(StorageError) as error:
        await Store.open(path)
    assert error.value.code == "unsupported_format"
    assert path.read_bytes() == before


def test_migrated_namespaces_have_no_compatibility_modules() -> None:
    """确认旧顶层目录已清理干净。"""
    for name in ("subject", "context", "execution", "storage"):
        assert list((_SOURCE / name).rglob("*.py")) == []
    assert not (_SOURCE / "plugins/base.py").exists()
