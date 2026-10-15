# Engine 代码架构与调用链

本文面向第一次接手仓库的维护者。先读应用主流程，再按需要打开领域规则和资源实现。
业务约束见 [重构契约](refactor/contract.md)，实施与验收记录只维护在
[计划](refactor/plan.md)。本次结构迁移以 `9329231` 为基线。

## 先建立目录地图

```text
src/purrcept_engine/
├── app/                    配置、显式装配、Host 门面与 CLI/Web/调音室
│   ├── host.py             EngineHost：嵌入调用入口
│   ├── bootstrap.py        EngineAssembly：取得资源并连接依赖
│   ├── lifecycle.py        HostLifecycle：阶段、启动与实际资源归还
│   ├── ingress.py          InputAdmissionService：正式接管后的可信交接
│   ├── queries.py          HostQueries：只读查询和面板投影
│   └── host_state.py       EngineState：当前实例的部分启动与资源事实
├── domain/
│   ├── inputs/             输入原文、来源、媒体与接管记录
│   ├── subject/            注意、前台、延期、反馈、反思与连续会话
│   ├── context/            动态材料、历史维护、工具交付和公开材料呈现
│   ├── model_requests/     请求预算、唯一重试规则与证据记录
│   ├── execution/          目录、能力契约、执行端口与纯源码校验
│   ├── activities/         活动规则、驱动契约与唯一状态提交者
│   ├── knowledge/          认识、检索、索引工作器及后端端口
│   └── audio/、time/、controls/  共享业务值、规则与有限端口
├── infrastructure/
│   ├── sqlite/             一个提交器和各领域账本，不决定模型展示
│   ├── models/             SDK 编译、客户端、计量与向量 HTTP 实现
│   ├── execution/          默认隔离执行器、Worker 会话、IPC 与调用调度
│   ├── processes/          Windows 进程资源工具
│   └── audio/              可复用的 WAV 技术编码工具
├── plugin_system/          插件作者的统一契约入口，不发现或加载实现
├── plugins/                显式启用的具体设备与应用业务
├── values.py               严格 JSON 边界
└── serialization.py        公开模型材料编码，独立于 JSON 值与 IPC 帧
```

依赖从应用装配指向领域规则与基础设施实现；基础设施实现领域端口。
`domain` 可以借用 Core 公共协议，不导入 app、具体插件、SQL 或模型 SDK。
`plugin_system` 只聚合领域值与契约。插件只拿有限端口；通用 Store、EngineState
和 SDK 客户端不成为插件接入方式。技术驱动可复用明确的基础设施工具。

普通内部协作直接借用具体对象；跨资源边界才定义协议。这里没有全局 Manager、
自动发现或通用依赖容器，模块导入不启动设备或工作任务。

## 路径一：启动与装配

从 [EngineHost](../src/purrcept_engine/app/host.py) 开始。构造准备纯声明，
建立当前实例状态以及装配、生命周期、输入和查询协作者。`start()` 交给
[HostLifecycle](../src/purrcept_engine/app/lifecycle.py)，后者只创建一个启动任务，
多个调用者观察同一任务，取消观察不会丢失资源所有者。

[EngineAssembly.start](../src/purrcept_engine/app/bootstrap.py) 按依赖取得目录锁、
Store、模型资源和插件实例，连接活动与执行器，再通过
[build_subject](../src/purrcept_engine/app/subject.py) 装配认识、上下文和主体。
取得的资源立即记入 EngineState，部分启动失败也有完整收尾事实。
插件开始接入以前，目录、执行器和主体必须已经就绪。

配置发现留在 app；插件 `prepare` 只给声明，`build` 才借用有限端口。
替换模型或执行器在 Host 构造时显式注入，不改变领域服务的选择逻辑。
CLI 仍负责进程信号、日志入口及 Web 服务，不把进程职责放进 EngineHost。

## 路径二：输入到真实行动

```text
EngineHost.submit / admit_input
  → InputAdmissionService：输入原文、媒体和待处理状态同事务接管
  → SubjectService.input_admitted：成功接管后交接可信抢占信号
  → AttentionService：普通合批、最低唤起与迟到判断检查
  → ForegroundService：开放缓冲取批、轮内接入与前台推进
  → ContinuousSession：借用 Core Conversation 和工具循环
  → play_action → EngineHost.execute → ExecutionService
  → WorkerSession ↔ 隔离 Worker → CallDispatcher → 能力实现
  → 真实调用回执入账 → 前台 / 反馈 / 活动
```

[SubjectService](../src/purrcept_engine/domain/subject/service.py) 展示三个固定工作器，
它们借用同一资格和提交状态。注意判断结果必须核对事务版本；过期判断不能
覆盖已经拉取的输入。延期由 DeferredInputService 到期交接，不另建输入状态。
VAD 停音和生成抢占通过正式接管关联，正文关键词本身不提供控制权限。

[ForegroundService](../src/purrcept_engine/domain/subject/foreground_service.py)
负责轮次编排；[ContinuousSession](../src/purrcept_engine/domain/subject/session.py)
串行推进连续会话，Core 拥有模型工具循环和整轮事务。前台最终文字仍是内部结果，
只有真实能力调用产生外部输出。抢占撤销生成资格，不撤销已开始的行动。

请求材料先由 `domain/context` 组装和检查交付，再由
[请求服务](../src/purrcept_engine/domain/model_requests/service.py) 记录证据、控制
预算和重试。应用注入 `MaterialCompiler`，SDK 编译位于 infrastructure/models。
不要在传输层另加重试，也不要因辅助服务拆分而重置会话或请求预算。

[ExecutionService](../src/purrcept_engine/infrastructure/execution/service.py)
拥有行动资源；WorkerSession 拥有进程和帧，CallDispatcher 拥有能力队列与回执。
`execute` 与 `invoke` 共用绑定、并发限制和真实调用链。Worker 只接收 JSON
Bootstrap 与 IPC，独立入口绕过顶层包初始化，不加载插件、SDK 或应用装配。
后续失败保留先前效果；取消观察不推断 Provider 已经释放或效果没有发生。

## 路径三：反馈、反思与认识

[BackgroundService](../src/purrcept_engine/domain/subject/background.py) 连接
FeedbackService 和 ReflectionService，持有独立反思会话及共享后台事实。
SubjectService 启动它；Host 分别等待主体与后台实际关闭。

FeedbackService 交接真实回执、计划和监管；ReflectionService 冻结反思批次，
在原预算内串行推进连续会话。反思可以修订认识，没有世界执行工具。
认识的读取与检索经过
[KnowledgeService](../src/purrcept_engine/domain/knowledge/service.py)；索引工作器
与可选向量后端分别拥有任务和连接，具体资源由 app/knowledge_resources 装配关闭。
认识须保留确切版本、证据和有效性，不把检索结果当作可改写的当前事实。

历史维护与摘要属于上下文；数据库检查点中的媒体 BLOB 保存属于 SQLite 实现。
严格 JSON、模型可见材料编码、Core 检查点和 IPC 帧各有边界，不能合并其接受范围。

## 路径四：活动控制与恢复

[ActivityService](../src/purrcept_engine/domain/activities/service.py) 是唯一活动状态
提交者。`control.py` 保存纯控制规则，`state.py` 保存运行时计时与恢复证明，
`contracts.py` 定义驱动接管与核对契约。辅助对象不独立写活动状态或增加预算。

能力控制和面板控制最终进入同一服务。驱动接管、实际执行、反馈与计时依次确认，
不能通过 Event 订阅顺序决定授权。重启后先只读核对，再等主体完整看到回执后
用新行动明确恢复或结束；未知外部效果不自动重放。具体设备协议留在 plugins。

## 路径五：关闭与恢复所有权

阅读 HostLifecycle 的 `close → finish_close → release_resources`。
`close` 同步撤销派发资格，建立唯一收尾任务；超时或取消只结束当前调用者观察。

先停止输入并完成已接管交接，再建立主体、后台、活动、面板和执行器的收尾所有者。
这些所有者并行结束互相借用的调用，不能替换为简单逆序清理栈。确认借用归还以后，
才关闭插件、认识后端、模型与数据库；目录锁最后释放。发生原始故障仍继续释放
独立资源，尚未确认归还的资源连同其依赖保留。重复关闭观察同一任务和同一失败。

| 所有者 | 持有与归还责任 |
|---|---|
| EngineAssembly / EngineState | 取得资源后立即记录；状态只供 app 内部协作 |
| HostLifecycle | 启动/关闭任务、阶段、期限、依赖归还和最终目录锁 |
| SubjectService / BackgroundService | 固定工作任务、前台/反思会话、模型借用与提交等待 |
| ContinuousSession / Core | 串行会话资格与历史；Core 执行模型工具循环与轮次事务 |
| ActivityService | 唯一活动状态、计时、驱动接管与两阶段恢复 |
| ExecutionService | 行动、Worker 和 Provider 实际释放；保留部分效果与迟到回执 |
| SqliteExecutor | 唯一连接、线程、普通/控制队列、背压、故障锁存和关闭 |
| 插件 Runtime | 自己取得的客户端与设备；Host 借用归还后才关闭 |
| 嵌入调用者 | 显式注入的模型后端；Host 只收回自己的借用 |

Store 和各账本共用 SqliteExecutor。跨输入/轮次/回执的原事务边界保留，
不要把一次提交拆成多个 await 或独立连接。

## 扩展与迁移入口

插件和替换执行器从 `purrcept_engine.plugin_system` 引用声明、生命周期、
有限端口、能力与活动契约；协议文档拥有完整共同契约，具体插件仅说明差异。
统一入口不提供数据库或服务定位器。公开顶层 EngineHost、EngineConfig、
load_config、记录和错误继续直接导出，Host 参数及返回语义保持不变。

| 迁移前位置 | 当前归属 |
|---|---|
| storage 中的记录与协议 | domain 各领域的 models / ports |
| storage 中的 SQL、线程与连接 | infrastructure/sqlite |
| subject、context | domain/subject、domain/context 与 domain/model_requests |
| execution 的契约、目录、校验 | domain/execution |
| execution 的执行器、Worker、IPC、Windows | infrastructure/execution、infrastructure/processes |
| app 中的模型客户端、计量、锁与文件日志 | infrastructure 对应模块 |
| plugins/base、共享音频与身体契约 | plugin_system，相关值与纯规则归 domain |

旧模块路径不提供转发别名。仓库内脚本、测试和独立进程均使用新路径。
本次迁移不修改数据库格式、运行配置、用户数据、提示、工具 schema 或能力 ID；
原格式12到13的升级规则仍属于基线行为，不因代码搬迁新增升级。

## 怎样验证改动

`tests/test_architecture.py` 检查依赖方向、顶层导出、Host 签名及旧 DDL 重开；
固定接口和 DDL 来自 `9329231`，不从当前实现反生成。
`tests/test_worker_import_boundary.py` 独立检查真实 Worker 入口的导入隔离。
各领域测试继续覆盖合批、抢占、预算、真实回执、恢复和关闭故障。

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
uv run python -m pytest -q
uv run ruff check src tests examples scripts
uv run ruff format --check src tests examples scripts
uv run pyright
uv run python -m compileall -q src tests examples scripts
```

环境变量只选择 SDK 本地元数据，不写运行配置。前端在 frontend 运行
`npm test`、`npm run typecheck`、`npm run build`。包路径变更还须构建 wheel，
用非 editable 独立安装运行 `scripts/verify_installation.py`，核对包内资源、
真实 Windows Worker、临时库和媒体重开。离线替身验证不能代表真实模型或设备效果。
