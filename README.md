# Purrcept Engine

Engine 在独立仓库实施持续主体宿主。已批准的职责与验收要求见
[重构契约](docs/refactor/contract.md)，实施状态见 [计划](docs/refactor/plan.md)。

维护入口为 `app/cli.py` → `app/config.py` → `app/host.py`。`app/subject.py`
连接主体用途、模型与插件；`subject/coordinator.py` 串行筛选并交接前台输入，
`subject/session.py` 借用 Core 的连续工具循环。动态状态和技能每个逻辑请求
刷新到 user 尾部，传输重试复用同一份材料；最终文字属于内部结果，只有实际
能力调用对外输出。`subject/background.py` 接续持久反馈与独立反思批次。

历史容量预检和摘要见 `context/` 与 `app/history.py`，认识与反思提交见
`storage/knowledge.py` 和 `subject/reflection.py`。插件纯声明在 `plugins/base.py`，
装配在 `app/assembly.py`；行动从 `execution/service.py` 进入独立 `worker.py`，
`storage/execution.py` 保存独立真实回执。活动的控制/真实确认与累计运行额度
由 `subject/activities.py` 提交，不依赖页面或模型自行填写身份。
宿主先取得数据目录锁，正式输入原文、媒体与待处理状态同事务保存。
关闭的调用者超时或取消不撤销后台收尾，也不提前释放目录锁。

配置结构与旧 Runtime 不兼容。新数据目录使用 `engine.sqlite3`，不会读取、
迁移或删除旧 Runtime 数据；切换前必须停止旧实例，避免同账号重复接收。

```powershell
uv run purrcept-engine init
uv run purrcept-engine check
uv run purrcept-engine catalog
uv run purrcept-engine run
```

示例中的模型/凭据/计量规则是需要替换的显式字段。`check` 不连接模型或设备；
结构通过不能证明端点容量、媒体支持或实际重试行为。`console = true` 时每行
文字经同一正式接管入口处理；空行忽略，EOF 只结束控制台输入，宿主继续运行。
Ctrl+C 请求关闭，关闭期间第二次 Ctrl+C 强退本 CLI。Web 页面尚在实施，
当前启动保持 `web = false`。

内置插件显式启用：`notebook`、`clock`、`bed`、`computer`、`qq`。
可选依赖通过对应 extra 安装，例如 `uv sync --extra qq --extra computer`；
纯配置声明不打开设备，电脑仍须明确连接。Notebook 覆盖写入必须提供读取或
先前写入返回的版本，已有文件不能以省略版本覆盖。QQ 恢复不重发未知消息，
cursor 续读已保存快照，QZone feed 不支持深翻页。

当前候选组合固定 Python 3.12、OpenAI 2.49.0 和
`LiteLLM 1.105.0+purrcept.1`。官方 1.105.0 在 TypeSafe 连接错误下仍隐式重发；
限定补丁提供请求级 HTTP 客户端及关闭该重发的开关，保留原协议转换。

首次建立开发环境前，下载脚本中 `UPSTREAM_URL` 指定的官方 Windows wheel，
然后离线生成依赖产物；脚本强制核验官方 SHA-256、三个源码文件和完整补丁上下文，
不修改系统或兄弟仓库的安装环境：

```powershell
python scripts/prepare_litellm.py C:\path\litellm-1.105.0-cp310-abi3-win_amd64.whl
uv sync --locked --all-groups
```

产物在 `build/litellm-patched`，同目录清单保存输入、补丁与输出摘要。已有不同
产物不会被覆盖。原生扩展逐字节保留，wheel 的版本及 RECORD 重新生成。
`uv.lock` 固定开发组合；发布验收还须提供可引用的 Core/LiteLLM 产物和安装步骤。

开发检查沿用 pytest、Ruff、strict Pyright 与 build：

```powershell
uv run pytest -q
uv run ruff check src tests examples
uv run ruff format --check src tests examples
uv run pyright
uv run python -m compileall -q src tests examples
uv run python -m build
```

开发时可以引用兄弟 SDK 工作区。已验证非 editable Engine wheel 的导入、
包内模板和纯配置 CLI；完整独立依赖安装仍在后续交付验收范围。
真实模型、QQ、音频与设备验收尚未运行。

嵌入调用者可用 `EngineHost.execute` 明确执行行动，结果包括逐次调用回执。
取消等待不会撤销已开始行动；未知结果不能自动重放。插件与替换执行器的
契约分别见 `plugins/base.py` 与 `execution/ports.py`。本机示例为
`examples/action.py`，需要配置启用控制台输出。
