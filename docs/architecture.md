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
│   ├── subject/            刺激协调、被调用注意/前台、反馈、反思与连续会话
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
│   ├── audio/              可复用的 WAV 技术编码工具
│   └── files.py            有界 HTTP 文件下载与逐跳链接核验
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

QQ 文件保存由两个独立能力串联：QQ 插件 `file_url` 只读取实际平台下载链接，
Notebook `save_file` 复用 `infrastructure/files.py` 的有界下载后，在自身
`attachments/` 原子新建。两个插件不借用彼此的资源，所有模型行动仍经过执行
服务与冻结目录。附件字节不穿过 Worker JSON，也不自动写入媒体历史或长期
认识；当前文件由文件系统持有，调用和原生回包仍保留原有追溯。下载使用独立
20 MiB/30秒默认预算，取消关闭网络响应，进入磁盘阶段则等实际线程归还。
目录扫描只读附件元信息，下载的 Markdown 不自动进入技能目录。

## 源码契约与模型材料

注释规范见根目录 [CONTRIBUTING.md](../CONTRIBUTING.md)。模块先说明职责和
阅读入口，公开调用先解释业务目的，再按 `Args:`、`Returns:`/`Yields:`、
`Raises:` 与 `Note:` 展开适用契约。版本、时钟单位、范围上界、实际接管和
资源归还须按所属领域解释，不能仅从同名参数或返回类型推断。

有两类文档同时参与运行协议：`FunctionTool` 使用前台与反思工具方法的
docstring 生成模型描述；Pydantic 类及嵌套 dataclass 的说明参与参数 schema。
它们的维护契约写在紧邻声明的注释中，保留原文及原有格式，避免改变固定目录或
模型提示。这类协议文本的格式调整也属于模型材料变更，不能机械套用文档修复。
其余维护说明使用结构化 docstring，实际异常以实现与测试为准，不能把接口
示例中的假定异常复制成承诺。局部闭包用精炼说明解释交接和顺序，不复制外层
已有的完整契约；构建后的面板 JavaScript 由源码生成，不手工补写产物注释。

固定提示词统一由 Core `PromptTemplate` 定义和渲染：主体文字位于
`domain/subject/prompts.py`，上下文与摘要布局位于 `domain/context/templates.py`，
能力目录条目位于 `domain/execution/catalog.py`。调用方先准备显式字段，再使用
`instruction/message/reminder` 进入 Core 协议；不额外创建全局 Prompt 管理器或
第二套字段替换器。模板绑定为局部值，正文中的花括号不会再次求值。
角色卡默认值和可选行在主体模板中声明，纯空白及尾部换行通过显式 RenderPolicy
原样保留。Core 默认 optional 的空白抑制仅用于已经选择过的可选完整行。
结构化数据仍由 `serialization.render_yaml/render_field_tree` 编码，业务字段选择
归材料投影，围栏长度和原生媒体位置归各自材料边界；它们不属于模板字段插值。

前台固定正文已按用户提供的虚拟生命体版本替换，`{profile}` 仍由 Core 绑定角色卡。
时钟由 app/subject 的每请求刷新入口采样，发生在插件材料等待结束后；当前请求时间、
工作快照观察时间和执行回执记录时间分别展示。`context/materials` 为每条刺激补齐
信封的完整发生/接收时间；`context/presentation` 只将已知执行时间派生为 UTC，
不猜测平台送达时间。时间线同时进入完整行动工具结果、容量引用和当前调用动态尾部。
固定 system 前缀不含实时时钟；传输重试复用原材料，逻辑请求之间刷新。

应用装配注意模型时使用前台同源角色配置，经同一个角色卡模板渲染为固定 profile。
`attention_request` 通过 Core `ATTENTION_IDENTITY_PROMPT` 绑定角色卡与通用规则：
decision 写入 ChoiceQuestion.instructions，生成式注意写入 system 指令；动态刺激
和工作背景仍留在输入层。普通判断和最低唤起升级共用此入口，不按姓名字符串在
接入层强制唤起。完整角色卡参与 SDK 编译、容量预检和请求证据记录，不静默截断。

刺激提供方通过 `InputSubmission.with_content(source, blocks)` 冻结来源和有序的
TextBlock/ImageBlock/AudioBlock。QQ 的 `plugins/qq/presentation.py` 只选择消息身份、
类型、参与者、目标、时间、正文及必要引用，生成 YAML；麦克风自行提供发言说明
与原生 AudioBlock。内核不解释平台字段，也不解析提供方的 YAML。
`domain/inputs/content.py` 保存文字与媒体索引布局，媒体字节仍进入原有同库 BLOB。
布局通过现有 JSON 扩展入账，纳入容量额度、指纹和原生重放，不重复编码媒体字节。
`context/materials.py` 的 `input_blocks` 按来源首次出现分组，同组保持接管顺序，
新增标题、证据和缺口 TextBlock，不支持的媒体原位替换为文字引用；每条输入内部
的文字/媒体位置顺序不改变。
注意和前台共用此入口。未声明布局时使用原文加附件的固定默认布局，平台原帧
留在权威库，不自动倾倒入模型。注意工作背景单独呈现精简 YAML。
完整回执仍由通用 `render_material` 呈现。详见[输入材料契约](input-materials.md)。
结构化存储和工具协议保持原状；新请求按实际渲染结果计量，既有会话消息不重写。

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

电脑单次截图在插件层先经 EvidencePort 保存原图及来源，再将同一字节写入
`screenshot_dir` 下的独立 JPEG。证据接管与文件写入共用一个运行时所有者，取消
调用观察不能遗失文件任务；关闭等待其结束。保存成功回执的 `path` 可沿现有
QQ 媒体允许目录校验交给发送，保存失败仍保留原生媒体和证据，不提供假路径。
连续观察不生成本地镜像，已交付的文件不在关闭时清理，也不绕过 Gateway 发送。

收音插件的 `vad_interrupt_enabled` 是默认停播策略，运行时持有独立可切换值。
WebUI 与模型能力共用 `set_vad_interrupt`，通过原控制账本去重与检查插件版本；
切换不重启设备。分段状态机始终工作，仅在持续人声确认点决定是否提交停播
通知，已发出的通知保留。状态快照与请求动态材料都提供当前开关值。

可选主动思考由主体持有的 `ProactiveThinking` 保存运行期开关及单调期限，唯一
`StimulusCoordinator` 在等待前检查，不新增模型循环或独立无限定时任务。输入的
最新接管游标来自同库，只对真实新输入变化复位，页面轮询/诊断写入不算刺激。
静默期限与主脑未激活期限独立：新输入及注意处理只复位前者；协调者实际取出
前台快照时复位后者，不因工具轮次或重试刷新。定时检查先于下一次普通注意取批，
任一期限到达且主脑空闲即可提交 `subject.idle`，持续背景流量不会饿死前台。
静默接管在同一 SQLite 提交段复查游标及待处理阶段；主脑期限到达则允许新普通
输入和未决注意，仍原子拒绝已有 priority/foreground/claimed/processing 工作。
刺激带来源、发生/接收时间及真实触发原因，走既有 priority 提交与前台快照，
一并取走 observe、assessing 和本次冲刷缓存；迟到注意结果由原版本 CAS 拒绝。
主脑忙碌不积累定时输入，已到期期限只在完成后合并触发一次。
容量不足记录背压，等完整间隔再尝试，不裁旧输入或连续补发多个周期。

明确睡眠以 TimeLedger 保存的 RestState 为准，不从插件名、自然语言或预计时长
猜测。协调者每轮先按同库休息事实将普通缓存、未决注意、暂存及睡前普通唤醒
意图仅记历史，再决定是否采用注意结果；在途注意取消并丢弃结果，高优先输入
仍直接唤醒，但
睡眠期间不做最低唤起升级判断。实际取批和定时接管事务也复查休息，避免排队
期间上床被旧唤醒绕过。睡眠普通输入使用 processed / subject.sleeping.recorded
结案调度，不表示模型已看过；原文、媒体和反思证据继续保留，醒来按需读取。
正式接管事务先收回旧睡眠普通积压，再核验新输入额度；新普通输入直接仅记
历史，不等待协调者释放名额，避免满队列挡住高优先唤醒。单条输入及瞬时线程
提交仍受原额度限制，高优先与 claimed/processing 待办不被释放或无限豁免。
主动思考只暂停，不改 enabled；起床后重建两个完整期限，不追补睡眠期间的周期。

上床所在前台轮次完成后，协调者持久领取本次睡眠的历史维护机会。RestState 的
history_compaction_claimed 是可选 JSON 内部字段，更新睡眠时长保留它；新一次
起床再上床重置，领取不等于成功。无可压缩旧前缀、失败或关闭均不自动重试。
app 装配独立 sleep_history 维护所有者，复用 HistoryPreparation/HistoryMaintenance，
force 只跳过占用阈值，不减少保护轮数、绕过未决保护、改写原件或放宽 CAS。
仅用于睡眠容量比较的 Core 预览不作为新刺激/主脑请求，也不包含行动工具；
下一次真实唤醒仍对完整输入和工具目录预检。维护使用一个逻辑摘要预算与原重试
策略，可与高优先前台并行等待，版本/idle 变化使迟到安装失效；关闭归还该任务
及独立摘要包装。反思调度保持独立，不属于睡眠停用的注意模型。

页面通过原本机控制账本提交 `subject / set_proactive_thinking`，按宿主实例、
request_id 和开关 revision 核验；它是宿主内部调度选项，不伪装世界 Capability。
开关修改与空闲接管共享异步锁，已接管输入不回滚。状态进入有限快照，断线页面
只读旧值、禁止切换；不改写 TOML 或持久默认值，重启从配置重新计时。

```text
EngineHost.submit / admit_input
  → InputAdmissionService：输入原文、媒体和待处理状态同事务接管
  → SubjectService.input_admitted：成功接管后交接可信抢占信号
  → StimulusCoordinator：合批、提交收件箱、采用判断、唤醒与抢占
  → AttentionService：接受完整批次与工作背景，返回判断和请求证据
  → ForegroundService：接受唤醒快照并推进一轮前台
  → ContinuousSession：借用 Core Conversation 和工具循环
  → play_action → EngineHost.execute → ExecutionService
  → WorkerSession ↔ 隔离 Worker → CallDispatcher → 能力实现
  → 真实调用回执入账 → 前台 / 反馈 / 活动
```

[SubjectService](../src/purrcept_engine/domain/subject/service.py) 持有唯一会话和资源；
[StimulusCoordinator](../src/purrcept_engine/domain/subject/coordinator.py) 是唯一刺激
调度循环，最多同时持有一个注意调用和一个前台调用。注意等待不阻塞高价值
批次提交。输入依次经过 attention/priority 缓存、assessing/submitted/foreground
收件箱、claimed 唤醒快照和 processing 轮次；foreground 是持久唤醒意图，
observe 只留下 submitted，不自动重启主脑。前台完成后由协调者检查下一次唤醒。
版本 CAS 拒绝已取走批次的迟到判断；旧生成代次检查拒绝抢占新轮次。
VAD 起音正式接管后只交音频所有者停播，同时以高优先通知进入下一轮，
不直接撤销当前生成，也不单凭起音登记最低唤起的紧急升级。最终发言内容
仍可独立判断紧急语义；其他显式抢占仍核对原生成代次。正文关键词本身不提供控制权限。

QQ 插件把已通过过滤的私聊消息与直接 @ 一同标为最低 WAKE；普通群聊继续
进入注意缓冲。空闲时直接交前台，忙碌时由同一最低唤起升级通道判断是否
需要 urgent，observe 不能撤销已有 WAKE。私聊戳一戳等通知不继承
消息规则；自发消息回显只进入缓存。框架只采用可信标记，不解析 QQ 私聊字段。

[ForegroundService](../src/purrcept_engine/domain/subject/foreground_service.py)
只处理唤醒时原子取出的全部已提交输入，运行中不再接管新刺激；[ContinuousSession](../src/purrcept_engine/domain/subject/session.py)
串行推进连续会话，Core 拥有模型工具循环和整轮事务。前台最终文字仍是内部结果，
只有真实能力调用产生外部输出。抢占撤销生成资格，不撤销已开始的行动。

请求材料先由 `domain/context` 组装和检查交付，再由
[请求服务](../src/purrcept_engine/domain/model_requests/service.py) 记录证据、控制
预算和重试。应用注入 `MaterialCompiler`，SDK 编译位于 infrastructure/models。
不要在传输层另加重试，也不要因辅助服务拆分而重置会话或请求预算。

刺激布局与模型能力分别归提供方和应用配置。`input_blocks` 保留提供方块顺序，
按每个模型的 `supports_contents` 交付原生内容或在原位置插入文字引用；默认仅 text。
原件保留在权威库，支持媒体的存储/计量故障仍显式失败。decision 另受 Core 的
文字/图片协议限制，video 声明不代表当前已有原生视频传输。详见
[输入材料契约](input-materials.md)。

[ExecutionService](../src/purrcept_engine/infrastructure/execution/service.py)
拥有行动资源；WorkerSession 拥有进程和帧，CallDispatcher 拥有能力队列与回执。
`execute` 与 `invoke` 共用绑定、并发限制和真实调用链。Worker 只接收 JSON
Bootstrap 与 IPC，独立入口绕过顶层包初始化，不加载插件、SDK 或应用装配。
后续失败保留先前效果；取消观察不推断 Provider 已经释放或效果没有发生。

声音插件的停音收尾同时回收 FFplay 消费者和 FFmpeg 解码/调音进程。
停止消费时 PCM 管道可能已因背压暂停读取，因此终止进程后须分块排空废弃
输出，再等待输入/诊断任务和进程实际归还；不能只等待进程退出而让现场
语音队列永久停留在旧播放。旧 PCM 不再交回设备，也不自动重播未知调用。

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

ReflectionTools 的原文页显式给出可复制的 `memory_sources`，独立于读取时使用的
evidence_ref。来源目录只提供纠正材料，实际呈现、当前版本和范围资格仍由原
EvidenceDeliveries/Store 核验，不自动补全、删去或替换模型给错的引用。
该批唯一工具实例记录进入方法后失败的写入意图，只有同 id 修正成功、对应范围
明确延期或 `abandoned_writes` 具名说明重判理由，才允许继续结案；放弃理由随
FinishSubmission 保存到结案回执。无法识别错误引用指向的范围时，延期保守覆盖
本批必需范围。工具参数在 Core 解析前已被拒绝的调用未进入此意图登记；普通
参数错误仍按 Core 回执纠正，不伪造已发生写入。
此状态仅归当前批次，不新增持久调度器或重试预算。取消、关闭或预算结束导致
未结案时，原批次中断且未复核证据仍由账本保留；新批次重新取得已读资格。
模型仍可因真实噪声、重复材料或无关碎片不写入；平淡生活不自动等于无记忆价值。

历史维护与摘要属于上下文；数据库检查点中的媒体 BLOB 保存属于 SQLite 实现。
`context/media_history.expire_history_media` 是媒体窗口的唯一纯投影：按 Core
完成边界保留最近两轮及当前后缀，旧 AudioBlock/ImageBlock（含嵌套工具结果、
原生图片字节和图像 URL）原位变为
固定过期文字。ContinuousSession 恢复、HistoryPreparation 预览和摘要请求组装
共用此规则；摘要资格及归档仍核对完整原来源，不能用投影视图冒充原检查点。
投影后的历史随正常成功轮次提交，失败不安装；没有新数据库模式或独立写入者。
前台、反思分别按自己的轮次计算窗口。client_managed 继续由 Core 抑制服务端
continuation，避免隐藏服务端历史绕过实际请求中的媒体窗口。原输入及当时的
请求/回执继续保存原件，过期不生成 ASR、OCR 或图像描述，也不把占位当成
已理解媒体内容的证明。工具协议和隐藏推理仍按原协议保留。
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
