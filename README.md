# Purrcept Engine

Engine 在独立仓库实施持续主体宿主。已批准的职责与验收要求见
[重构契约](docs/refactor/contract.md)，实施状态见 [计划](docs/refactor/plan.md)。

架构与接手阅读顺序见 [代码架构与调用链](docs/architecture.md)。后端按职责分层：

- `app`：配置、显式装配、Host 生命周期，以及 CLI/Web 等应用接入。
- `domain`：Purrcept 主体、执行契约、活动、认识、上下文和业务值。
- `infrastructure`：SQLite、模型 SDK、隔离执行器与进程等资源实现。
- `plugin_system`：插件及替换执行器的统一公开契约；`plugins` 保留具体设备业务。

从 `EngineHost` 进入 `EngineAssembly` 查看装配，进入 `SubjectService` 查看注意、
前台与独立后台的编排。`ContinuousSession` 继续借用 Core 的工具循环；
最终文字属于内部结果，只有实际能力调用对外输出。

宿主先取得数据目录锁，正式输入原文、媒体与待处理状态同事务保存。
关闭的调用者超时或取消不撤销后台收尾，也不提前释放目录锁。

配置结构与旧 Runtime 不兼容。新数据目录使用 `engine.sqlite3`，不会读取、
迁移或删除旧 Runtime 数据；切换前必须停止旧实例，避免同账号重复接收。

本版启动时将 Engine 格式12原子升级为13，只新增直接@升级判断的表与索引，
保留原文、BLOB、调用回执和业务关系。升级前停止宿主并复制完整数据目录；
升级拒绝会保留格式12，故障目录不可用空库替换。升级成功后回到旧代码须先
停止宿主、另存升级后的完整目录，再恢复升级前的目录副本；没有自动数据库降级。

```powershell
uv run purrcept-engine init
uv run purrcept-engine check
uv run purrcept-engine catalog
uv run purrcept-engine run
```

示例中的模型/凭据/计量规则是需要替换的显式字段。`check` 不连接模型或设备；
结构通过不能证明端点容量、媒体支持或实际重试行为。`console = true` 时每行
文字经同一正式接管入口处理；空行忽略，EOF 只结束控制台输入，宿主继续运行。
Ctrl+C 请求关闭，关闭期间第二次 Ctrl+C 强退本 CLI。安装 `web` extra 后可在
`[web]` 设置 `enabled = true`，启动时打印本机面板地址；默认端口 8765，
端口 0 由系统分配。页面刷新只查询已装配宿主，控制经同库身份去重。

内置插件显式启用：`notebook`、`clock`、`bed`、`computer`、`browser`、`qq`、
`microphone`（输出）、`voice_input`（收音）和 `live2d`。
可选依赖通过对应 extra 安装，例如 `uv sync --extra qq --extra browser`；
纯配置声明不打开设备，电脑仍须明确连接。Notebook 覆盖写入必须提供读取或
先前写入返回的版本，已有文件不能以省略版本覆盖。QQ 恢复不重发未知消息，
cursor 续读已保存快照，QZone feed 不支持深翻页。

重启活动首次 `me.activity(command="resume"/"end", activity_id=...)` 返回只读
`reconciliation` 和 `decision_required=true`，不接管控制。主脑在后续模型请求
完整看到核对回执后，再以新行动明确恢复或结束；未知停止状态只允许明确结束，
仍以驱动实际确认为准。页面恢复不能替代这一步。反思 `revise_memory` 可用
`conflicts=[{kind:"memory",id,version}]` 保留双向未决反证；召回与展开返回对方
确切版本及有效性，`read_evidence(id=..., version=...)` 可展开对应历史版本。

主体默认装配 `me.view_image(source=...)`，支持URL或本地图像（8MiB以内的
PNG/JPEG/GIF/WebP）；像素完整保存到同库，后续请求通过原生媒体取得。
TOML相对路径沿配置目录，程序式调用未给base_dir时沿数据目录。文字参考容量
规则不支持媒体，不能把看图回执当作模型已经看过像素。

声音输出需要已安装的 FFmpeg/FFplay，配置使用明确程序路径；不会自动安装
或开启声音设备，最小配置见[声音输出](src/purrcept_engine/plugins/audio/README.md)。
收音需要 `voice-input` extra、明确的设备/模式、固定版本
VAD 文件及 ASR 端点；[收音配置](src/purrcept_engine/plugins/voice_input/README.md)
说明显式下载与校验步骤，运行宿主不会自动下载。原生音频还须配置实际适用的
模型容量规则，支持音频声明本身不证明计量依据。

Live2D 使用 `live2d` extra、VTS模型校准与人工参数绑定；可选 OVR DLL 只在
独立分析进程加载。音频不借用身体连接，声音开始/终止与视觉采用分别记账。
配置和校准结构见[身体说明](src/purrcept_engine/plugins/live2d/README.md)。
VTS 初连与传输失联恢复每轮最多5次，间隔0.5/1/2/4秒；拒绝、协议错误或
次数耗尽停止，退出撤销恢复且不补演旧表达。真实声音与视觉效果待实机确认。

独立调音室安装 `web` extra 和 FFmpeg后运行 `purrcept-voice-studio --help`，
具体使用见[调音室说明](src/purrcept_engine/app/studio/README.md)。面板与调音室
静态资源均在Python包内，不需Node；面板源码重建见[前端说明](frontend/README.md)。

CLI的普通日志位于数据目录 `logs`，默认七天、每文件10MiB、最多七文件，
通过 `[logging]` 调整。模型流诊断默认七天与1MiB，通过 `[diagnostics]` 调整；
过期查询带独立标记，不删除原始参数、完整请求响应、输入、媒体或业务回执。
嵌入入口保留调用者日志配置。关闭故障仍报告原错：已释放的独立资源继续清理，
无法确认归还的资源、权威库和目录锁继续持有。

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
`uv.lock` 固定开发组合。独立安装使用同一份 Core/LiteLLM 工作区冻结产物，
不能只凭版本号换成其他源码或公开索引上的同名包；本轮没有提交它们原有修改。

以下命令从 Engine 目录构建 wheel 并导出带哈希依赖清单；兄弟 SDK 只作为只读
构建输入，包和清单写入 Engine 的 `build/verified-distribution`：

```powershell
uv run python -m build --wheel --no-isolation --outdir build/verified-distribution ../purrcept_core
uv run python -m build --wheel --no-isolation --outdir build/verified-distribution ../purrcept_litellm
uv run python -m build --wheel --no-isolation --outdir build/verified-distribution
uv export --quiet --locked --no-dev --no-emit-project --no-emit-local --no-annotate --no-header --output-file build/verified-distribution/requirements-minimal.txt
```

把已校验的补丁 wheel 放入该目录。`--all-extras` 导出全部可选依赖；只装所需
extra 时应导出对应清单。先按清单安装第三方依赖，再安装四个本地 wheel：

```powershell
uv venv --python .venv/Scripts/python.exe --no-python-downloads build/install-minimal
uv pip install --python build/install-minimal/Scripts/python.exe --require-hashes -r build/verified-distribution/requirements-minimal.txt
uv pip install --python build/install-minimal/Scripts/python.exe --no-deps build/verified-distribution/litellm-1.105.0+purrcept.1-cp310-abi3-win_amd64.whl build/verified-distribution/purrcept_core-0.6.0-py3-none-any.whl build/verified-distribution/purrcept_litellm-0.2.0-py3-none-any.whl build/verified-distribution/purrcept_engine-0.1.0-py3-none-any.whl
uv pip check --python build/install-minimal/Scripts/python.exe
```

安装后可使用该环境的 `purrcept-engine.exe` 运行 init/check/catalog/run。
本轮分发目录的机器清单记录 wheel、SDK 源文件及锁文件摘要，产物保留在本机，
未发布。Core/LiteLLM wheel 的输入字节已核对，升级 SDK 应重新构建并验收。

在验收用的临时 PowerShell 进程中运行下面的离线冒烟。此开关只选择冻结
LiteLLM 的包内价格表，避免 SDK 导入下载在线元数据；不会给 Engine 注入模型
凭据，也不替代窗口和媒体计量声明。脚本拒绝 editable，检查八份静态资源、
真实 Windows Worker、临时同库图片保存/重开和目录锁收回，不调用模型或设备：

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = "True"
build/install-minimal/Scripts/python.exe -I -X utf8 scripts/verify_installation.py
```

开发检查沿用 pytest、Ruff、strict Pyright 与 build：

```powershell
uv run python -m pytest -q
uv run ruff check src tests examples
uv run ruff format --check src tests examples
uv run pyright
uv run python -m compileall -q src tests examples
uv run python -m build
```

开发时可以引用兄弟 SDK 工作区。minimal/all 两种非 editable 安装候选已验证
依赖一致、SDK/Engine 导入、八份静态资源和 CLI；最终分发验证状态见实施计划。
真实模型、QQ、音频与设备验收尚未运行。

嵌入调用者可用 `EngineHost.execute` 明确执行行动，结果包括逐次调用回执。
取消等待不会撤销已开始行动；未知结果不能自动重放。插件与替换执行器的
契约分别见 `plugin_system` 的统一入口与协议文档。本机示例为
`examples/action.py`，需要配置启用控制台输出。
