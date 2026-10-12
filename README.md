# Purrcept Engine

Engine 在独立仓库实施持续主体宿主。已批准的职责与验收要求见
[重构契约](docs/refactor/contract.md)，实施状态见 [计划](docs/refactor/plan.md)。

当前交付正在实施 M1：配置校验、单实例锁、SQLite/BLOB 输入接管与资源收尾。
主体、行动、内置插件及 WebUI 尚未装配；当前运行入口仅提供核心存储宿主，
不将最终模型文字或已接管输入自动当成对外回答。

维护入口为 `app/cli.py` → `app/config.py` → `app/host.py` → `storage`。
宿主先取得数据目录锁，正式输入原文、媒体与待处理状态同事务保存。
关闭的调用者超时或取消不撤销后台收尾，也不提前释放目录锁。

配置结构与旧 Runtime 不兼容。新数据目录使用 `engine.sqlite3`，不会读取、
迁移或删除旧 Runtime 数据；切换前必须停止旧实例，避免同账号重复接收。

```powershell
uv run purrcept-engine init
uv run purrcept-engine check
uv run purrcept-engine run
```

示例中的模型/凭据/计量规则是需要替换的显式字段。`check` 不连接模型或设备；
结构通过不能证明端点容量、媒体支持或实际重试行为。当前 `run` 要求
`console = false`、`web = false`；它在无标准输入时继续运行，Ctrl+C 请求关闭。

开发检查沿用 pytest、Ruff、strict Pyright 与 build：

```powershell
uv run pytest -q
uv run ruff check src tests examples
uv run ruff format --check src tests examples
uv run pyright
uv run python -m compileall -q src tests examples
uv run python -m build
```

开发时可以引用兄弟 SDK 工作区，发行验收须验证非 editable wheel 和冻结的
依赖产物。真实模型、QQ、音频与设备验收尚未运行。
