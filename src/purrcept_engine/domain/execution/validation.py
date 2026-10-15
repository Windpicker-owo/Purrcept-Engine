"""检查行动的受限 Python，并生成仅由 Worker 编译的异步函数。

宿主预检用于提前反馈，Worker 必须独立复检；AST 约束不替代进程资源限制。
允许结果复用、条件、循环、异常处理及受控并行，禁止真实 I/O 导入、动态代码、
私有对象图和帧反射。运行期模块与 builtin 白名单由 Worker 再次限制。
"""

import ast
import textwrap

from purrcept_engine.errors import EngineError

ALLOWED_MODULES = frozenset(
    {"asyncio", "datetime", "itertools", "json", "math", "re", "statistics", "world"}
)
_BANNED_CALLS = frozenset(
    {
        "breakpoint",
        "compile",
        "delattr",
        "dir",
        "eval",
        "exec",
        "getattr",
        "globals",
        "help",
        "input",
        "locals",
        "memoryview",
        "open",
        "setattr",
        "type",
        "vars",
        "__import__",
    }
)
_BANNED_ATTRIBUTES = frozenset(
    {
        "format",
        "format_map",
        "get_loop",
        "cr_frame",
        "cr_code",
        "cr_await",
        "cr_origin",
        "gi_frame",
        "gi_code",
        "gi_yieldfrom",
        "ag_frame",
        "ag_code",
        "ag_await",
        "f_back",
        "f_builtins",
        "f_code",
        "f_globals",
        "f_locals",
        "tb_frame",
        "tb_next",
    }
)
_BANNED_NODES = (
    ast.AsyncFunctionDef,
    ast.AsyncWith,
    ast.ClassDef,
    ast.Delete,
    ast.FunctionDef,
    ast.Global,
    ast.Lambda,
    ast.Match,
    ast.Nonlocal,
    ast.With,
    ast.Yield,
    ast.YieldFrom,
)


class ActionSourceError(EngineError):
    """模型可纠正的源码或资源边界错误；拒绝发生在执行任何行动之前。"""


def validate_source(code: str, *, max_bytes: int = 64 * 1024) -> str:
    """校验 UTF-8 大小与 AST，返回供 Worker 使用的异步包装源码。

    Args:
        code: 供调用方分类处理的稳定错误码。
        max_bytes: 此边界允许保留或接管的最大字节数。

    Raises:
        ActionSourceError: 典型拒绝原因：行动源码必须是字符串；行动源码包含无效 UTF-8 字符；
            参数、材料或当前状态不满足此入口的约束。
        ValueError: 源码字节上限必须是正整数。

    Note:
        白名单固定于此实现，不让模型通过 bootstrap 或 import 参数扩大范围。
        不在宿主编译或执行模型代码。空源码等价于返回 None。
    """
    if not isinstance(code, str):
        raise ActionSourceError("行动源码必须是字符串。")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("源码字节上限必须是正整数。")
    try:
        source_size = len(code.encode("utf-8"))
    except UnicodeEncodeError:
        raise ActionSourceError("行动源码包含无效 UTF-8 字符。") from None
    if source_size > max_bytes:
        raise ActionSourceError(f"行动源码超过 {max_bytes} 字节。")

    body = code if code.strip() else "return None"
    try:
        tree = ast.parse(body, filename="<purrcept-action>", mode="exec")
    except SyntaxError as error:
        raise ActionSourceError(f"行动语法第 {error.lineno} 行：{error.msg}。") from None

    for node in ast.walk(tree):
        reason = _rejection(node)
        if reason is not None:
            location = getattr(node, "lineno", None)
            raise ActionSourceError(f"行动第 {location} 行：{reason}。")

    return "async def purrcept_action():\n" + textwrap.indent(body, "    ") + "\n"


def _rejection(node: ast.AST) -> str | None:
    """检查显式语法入口；字符串和输入正文不作为宿主身份或许可。"""
    if isinstance(node, _BANNED_NODES):
        return f"不支持 {type(node).__name__}"
    if isinstance(node, ast.Name) and node.id.startswith("_"):
        return "不允许私有名字"
    if isinstance(node, ast.Attribute):
        # Security: format 字段可以在 AST 看不到的字符串中遍历私有对象图。
        # 帧、协程和 Future 的公开反射入口也不能因名称无下划线而放行。
        if node.attr.startswith("_") or node.attr in _BANNED_ATTRIBUTES:
            return f"不允许访问属性 {node.attr}"
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in _BANNED_CALLS:
            return f"不允许调用 {node.func.id}"
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        if isinstance(node, ast.ImportFrom):
            if node.level or node.module not in ALLOWED_MODULES:
                return "只允许已声明的顶层模块"
            if any(alias.name == "*" or alias.name.startswith("_") for alias in node.names):
                return "不允许星号或私有成员导入"
        elif any(alias.name not in ALLOWED_MODULES for alias in node.names):
            return "只允许已声明的顶层模块"
        if any(alias.asname is not None and alias.asname.startswith("_") for alias in node.names):
            return "不允许私有导入别名"
    return None
