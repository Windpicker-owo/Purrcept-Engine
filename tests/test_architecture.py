"""验证依赖分层、当前公开接口和历史数据库的明确边界。

依赖检查覆盖延迟和类型导入，避免纯声明悄悄带入资源实现。DDL 固件固定于
提交 9329231，不随当前实现重建；门面签名基线只随明确的公开 API 变更更新。
业务行为仍由各领域和真实跨层测试验证。
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


def baseline() -> JsonObject:
    """读取固定提交的接口与完整 DDL，不接受歧义 JSON 或隐式字段转换。"""
    return decode_object(_BASELINE.read_bytes())


def imports(tree: ast.AST, package: str) -> tuple[str, ...]:
    """包含嵌套及注解导入，父包形式同时核对明确引用的子模块。"""
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
    """业务不找资源，基础设施不找应用，插件不借用 SQL 或其他具体设备实现。"""
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


def test_host_signatures_and_root_exports_match_declared_api() -> None:
    """门面调用方式与顶层导出须符合显式维护的 API 基线，规则注册入口已移除。"""
    tree = ast.parse((_SOURCE / "app/host.py").read_text(encoding="utf-8"))
    host = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "EngineHost"
    )
    actual: JsonObject = {}
    for node in host.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            not node.name.startswith("_") or node.name == "__init__"
        ):
            actual[node.name] = {
                "arguments": ast.dump(node.args),
                "returns": ast.dump(node.returns) if node.returns else "",
                "async": isinstance(node, ast.AsyncFunctionDef),
                "decorators": [ast.dump(item) for item in node.decorator_list],
            }
    original = baseline()
    assert actual == original["host_methods"]
    assert purrcept_engine.__all__ == original["root_exports"]


async def test_legacy_architecture_database_is_rejected_without_mutation(tmp_path: Path) -> None:
    """历史冻结基线用于确认拒绝边界；当前迁移只接受格式15。"""
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
    """迁移完成只保留新归属，避免以后沿旧入口继续添加业务。"""
    for name in ("subject", "context", "execution", "storage"):
        assert list((_SOURCE / name).rglob("*.py")) == []
    assert not (_SOURCE / "plugins/base.py").exists()
