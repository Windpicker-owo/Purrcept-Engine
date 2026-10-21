# Purrcept Engine

Engine 在独立仓库实施持续主体宿主。已批准的职责与验收要求见
[重构契约](docs/refactor/contract.md)，实施状态见 [计划](docs/refactor/plan.md)。

架构与接手阅读顺序见 [代码架构与调用链](docs/architecture.md)。后端按职责分层：

- `app`：配置、显式装配、Host 生命周期，以及 CLI/Web 等应用接入。
- `domain`：Purrcept 主体、执行契约、活动、认识、上下文和业务值。
- `infrastructure`：SQLite、模型 SDK、隔离执行器与进程等资源实现。
- `plugin_system`：插件及替换执行器的统一公开契约；`plugins` 保留具体设备业务。

开发及注释要求见 [贡献指南](CONTRIBUTING.md)。公开 Python 接口使用中文
Google 风格契约，先说明用法，再列参数、结果和典型异常；资源、事务、并发与
取消约束集中到 `Note:`。会进入工具描述或 Pydantic schema（含嵌套 dataclass）的
文档属于协议材料，维护者说明放在相邻源码注释中，修改其原文须按模型材料
变更核验。

提示词使用 Core 的 `PromptTemplate`。主体的角色卡、前台、注意和反思定义集中在
[`domain/subject/prompts.py`](src/purrcept_engine/domain/subject/prompts.py)；摘要、
输入材料和动态提醒布局集中在
[`domain/context/templates.py`](src/purrcept_engine/domain/context/templates.py)。
调用方准备值，通过 `render()`、`instruction()`、`message()` 或 `reminder()`
取得协议内容。JSON/YAML 与字段树编码仍归序列化层，工具描述继续由 Core
`FunctionTool` 读取方法说明和参数 Schema，不再自行插值提示词正文。

前台按完整日期、秒与时区核对刺激和行动顺序：每条刺激显式展示来源发生时间
`occurred_at` 与宿主接收时间 `received_at`；每次逻辑请求在插件材料准备后刷新
动态尾部 `request_time.current_time`（附 Unix 秒数），传输重试保持原请求。
行动及逐次调用额外提供可读 UTC 时间，容量引用和迟到回执也保留相应时间。
这些是宿主派发/结果记录，不能冒充平台精确送达时间；跨批历史的位置也不代表
事件实际先后。详情见[输入材料契约](docs/input-materials.md#时间与跨批次顺序)。

VS Code 启动配置、decision 预筛选断点与暂停超时说明见
[断点调试指南](docs/debugging.md)。

从 `EngineHost` 进入 `EngineAssembly` 查看装配，进入 `SubjectService` 查看注意、
前台与独立后台的编排。`ContinuousSession` 继续借用 Core 的工具循环；
最终文字属于内部结果，只有实际能力调用对外输出。

QQ 私聊消息与协议确认的直接 @ 同为最低唤起：通过入站过滤后直接交给主脑，
不等待普通注意合批，也不被 observe 降级。主脑忙碌时只对高价值输入所在
批次进行旧生成紧急升级判断；是否回复、如何回复仍由主脑决定。

QQ 附件可由主体显式保存到 Notebook：`me.phone.qq.file_url` 按实际 `file_id`
取得私聊或群文件链接，再调用 `me.notebook.save_file(url=..., path="attachments/qq/报告.pdf")`。
输入已带 HTTP(S) 链接时可直接保存。PDF、Word、ZIP 等保留原字节，成功回执提供
相对/绝对路径、大小和 SHA256 版本；`tree` 查看目录，`file_info` 查看版本，
`move/delete` 沿用版本核验。下载不覆盖、不自动重试，不解析附件正文或自动装载
附件中的 Markdown 技能。Notebook 配置 `file_max_bytes` 默认 20 MiB、
`file_timeout_seconds` 默认 30 秒，与 Markdown 的 `max_bytes` 分开。
QQ 再发送文件仍受 `extra_media_roots` 等允许目录约束；需要回发时显式加入
Notebook 附件目录。文件链接协议见 [NapCat 文件文档](https://napneko.github.io/develop/file)。

注意模型与主脑共用同一份固定角色卡，专用 decision 和生成式注意均可参考
姓名、已有别称、兴趣与关系。群聊中没有 @ 但明确叫主体名字、向其搭话或提问，
规则要求 wake；同名他人、无关引用和重复闲聊仍按语境判断。文字提名继续走普通
注意，不自动升级为私聊/@ 的保底唤起或 urgent，也不要求主体必定回复。

长期记忆由独立反思调用 `revise_memory` 写入，不以聊天历史或反思摘要代替。
原文页的 `memory_sources` 提供可直接复制的完整片段引用；证据 ID 只用于展开。
写入失败会返回引用目录和未解决意图，不能仅改成 `no_change` 结案：须修正成功、
明确延期，或在 `abandoned_writes` 中具名说明重新判断不应记忆的理由。普通生活
片段、偏好和相处约定可有据沉淀，不要求重大事件，也不强制每批写入。

刺激协调者统一合批、提交和唤醒。observe 将整批保留在主脑收件箱而不立即
唤醒，wake 请求下一次思考，urgent 进一步尝试抢占所观察的旧生成。主脑每次
被唤醒时原子取尽已提交输入，运行中不再拉取新刺激；Core 仍拥有轮内工具
循环。注意模型是被调用的批次评估器，不拥有定时、提交或抢占。

VAD 持续人声只停止当前及已有候播的语音，不取消主脑生成。起音事件以高优先
停播通知保留到下一轮，不单凭此事件发起紧急升级判断；最终语音内容仍沿正常
高价值输入路径处理。通知说明停播请求，实际停止结果及部分播放以音频回执为准。

WebUI“设备 → 收音 → VAD 自动停播”可随时关闭此行为，正常收音和分段继续。
收音插件 `[plugins.config]` 的 `vad_interrupt_enabled = true` 控制启动默认值；
设为 `false` 默认关闭。页面修改仅本次运行有效，不撤回已经发出的停播请求。

WebUI 的“系统 → 她的思考 → 主动思考”可随时开关空闲唤醒，默认关闭。
启动默认值在 `[subject]` 设置 `proactive_thinking = false`，空闲间隔由
`proactive_idle_seconds = 300.0` 配置（有限正秒数）。页面修改仅在本次运行生效，
重启采用配置默认值并重新计时，不补发停机期间的周期。
开启后，无新刺激且注意空闲满一个间隔，或主脑未激活满一个间隔，协调者保存
带确切时间和触发原因的 `subject.idle`，保底唤起主脑。新刺激只重置静默期限；
主脑实际取批才重置激活期限，attention/observe 不推迟它，因此持续背景输入也会
定期交给主脑。已有 observe、尚在判断及普通缓存的刺激一并处理；迟到判断按版本
拒绝。主脑忙碌时不生成定时输入，超时期间只合并为完成后的一次唤醒。
它不是用户发言，不要求对外回复或行动；关闭不取消已接手的思考或撤回已接管刺激。

`me.bed.rest(...)` 明确进入睡眠后，普通刺激继续接管但只存历史，attention 停止调用，
只有高优先刺激能唤醒主脑；主动思考暂停且不改变开关。上床所在轮次结束后，
尝试一次上下文压缩，跳过容量阈值但保留近期完整轮次、未决事实和原始证据。
没有可压缩旧历史则跳过，失败保留原历史；领取事实跨重启保存，更新睡眠时长
不会重复压缩。普通睡眠消息不占待处理额度，醒来不会自动整批塞入主脑；原文、
媒体和证据保留，供按需回看与独立反思。已存在的睡眠积压也按此规则仅记历史，
不会阻塞新私聊或 @ 的接管。高优先唤醒不自动起床，显式调用
`me.bed.leave()` 后恢复普通注意，主动思考从完整间隔重新计时。预计睡眠到期
不自动起床或发言。

输入插件通过 `InputSubmission.with_content(source, blocks)` 交付按原始顺序排列的
TextBlock、ImageBlock 和 AudioBlock。QQ 自行选择必要字段生成 YAML，麦克风交付
原始音频；框架只按来源分组并插入标题，不解析平台字段、不把媒体移动到文字末尾。
模型统一通过 `supports_contents = ["text"]` 声明内容能力，可加入 `image`、`audio`、
`video`；刺激组装将不支持的媒体原位替换为明确文字引用，原件仍保留。
注意与前台分别按各自模型声明投影同一份冻结内容，具体限制及插件示例见
[输入材料契约](docs/input-materials.md)。

连续会话只在当前请求保留最近两个已完成轮次及当前轮次的原生媒体。更早的
图片和音频（含嵌套工具结果）原位替换为“历史图片已过期”或“历史语音输入已过期”，
不做 OCR、ASR 或推测描述。完整轮次包含所有中间工具调用，重试和失败不推进
窗口；文字、来源时间、工具关联与行动回执保留。会话恢复、容量预览和摘要
使用同一窗口；原始媒体仍可从输入、真实请求及回执证据追溯。已有历史在下次
恢复时应用，不需迁移数据库；成功轮次正常提交新历史。

宿主先取得数据目录锁，正式输入原文、媒体与待处理状态同事务保存。
关闭的调用者超时或取消不撤销后台收尾，也不提前释放目录锁。

配置结构与旧 Runtime 不兼容。新数据目录使用 `engine.sqlite3`，不会读取、
迁移或删除旧 Runtime 数据；切换前必须停止旧实例，避免同账号重复接收。

本版启动时将 Engine 格式12/13原子升级为14，分离缓存、收件箱与唤醒状态，
移除注意 defer。旧未执行的 defer 转为一次保底唤醒，旧已结案 observe 不重放；
原文、BLOB、会话、请求和真实回执保留，格式13的旧升级判断原样归档到
minimum_wake_assessments_v13。升级前停止宿主并复制完整数据目录；升级拒绝
保留原格式与数据，没有自动降级。移除配置项 attention_defer_seconds；自定义
TOML 需删除该键，随包默认配置和本工作区 purrcept.toml 已同步。

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

`me.computer.screenshot()` 同时保存数据库原图和本地 JPEG，成功回执返回
绝对 `path`，可传给 QQ `send(images=[path])` 或 `send(files=[path])`。
默认目录 `.purrcept/computer-screenshots` 位于 QQ 默认媒体允许范围内；连续观察
不逐帧写文件。配置、失败及文件寿命见[电脑截图说明](src/purrcept_engine/plugins/computer/README.md)。

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
轮次超时或开始提交结案后，迟到抢占/关闭只等待原结案，不再次取消失败入账。
QQ 与收音分别提供真实资源归还明证；历史输入异常仍报告，但已归还的设备不再
拖住数据库退出。包含取消的故障组同样保留 CLI 的二次 Ctrl+C 处理器。

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
