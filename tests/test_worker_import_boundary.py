"""验证独立 Worker 的包引导不跨入宿主资源或 SDK 导入边界。

在隔离 Python 中阻止危险依赖的真实加载；不执行模型行动或打开设备。
真实握手、IPC 和资源限制由既有 Windows Worker 测试覆盖。
"""

import subprocess
import sys
from importlib.resources import files
from pathlib import Path

_PROBE = r"""
import importlib.abc
import runpy
import sys
from pathlib import Path
from types import ModuleType

worker = Path(sys.argv[1])
root = worker.parents[2]
blocked = {"purrcept_core", "purrcept_litellm", "pydantic", "openai", "litellm", "httpx", "sqlite3"}

class HostBoundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (
            fullname.split(".")[0] in blocked
            or fullname.startswith(("purrcept_engine.app", "purrcept_engine.plugins"))
        ):
            raise AssertionError("Worker attempted host import: " + fullname)
        return None

sys.meta_path.insert(0, HostBoundary())
# 根导出是嵌入入口；探针先提供命名空间，再验证 Worker 自身的实际引导函数。
package = ModuleType("purrcept_engine")
package.__path__ = [str(root)]
sys.modules["purrcept_engine"] = package
scope = runpy.run_path(str(worker), run_name="worker_import_probe")
scope["_install_package_namespace"]()
assert sys.modules["purrcept_engine"].__path__ == [str(root)]
assert not any(name.split(".")[0] in blocked for name in sys.modules)
print("isolated-worker-ok")
"""


def test_isolated_worker_bootstrap_never_loads_host_dependencies(tmp_path: Path) -> None:
    """包移动后仍只加载标准库与显式协议，顶层导出不得悄悄加载进程资源。"""
    worker = Path(str(files("purrcept_engine.infrastructure.execution").joinpath("worker.py")))
    result = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(worker)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated-worker-ok"
