"""拒绝常见 Python 对象图与 I/O 入口，保留既定行动组合语法。"""

import pytest

from purrcept_engine.execution.validation import ActionSourceError, validate_source


@pytest.mark.parametrize(
    "code",
    [
        "import os",
        "import math.sin",
        "from world import *",
        "from world import _client",
        "from .world import me",
        "from world import me as _me",
        "obj.__class__",
        "obj.cr_frame",
        "obj.get_loop()",
        "obj.format",
        "obj.format_map",
        "getattr(obj, 'secret')",
        "open('secrets')",
        "eval('1')",
        "exec('1')",
        "def fn(): pass",
        "async def fn(): pass",
        "class Object: pass",
        "yield 1",
        "with value: pass",
        "global value",
        "lambda: 1",
        "_private = 1",
    ],
)
def test_source_rejects_forbidden_entry_points(code: str) -> None:
    with pytest.raises(ActionSourceError):
        validate_source(code)


def test_action_composition_and_controlled_parallelism_remain_valid() -> None:
    code = (
        "from world import me\nimport asyncio\n"
        "results = await asyncio.gather(me.a.read(), me.b.read())\n"
        "try:\n    if results:\n        return results[0]\n"
        "except Exception as error:\n    return str(error)\n"
    )
    assert validate_source(code).startswith("async def purrcept_action():\n")
    assert validate_source(" ") == "async def purrcept_action():\n    return None\n"


def test_source_limit_counts_utf8_bytes_and_rejects_invalid_text() -> None:
    with pytest.raises(ActionSourceError):
        validate_source("'中文'", max_bytes=4)
    with pytest.raises(ActionSourceError):
        validate_source("\ud800")
