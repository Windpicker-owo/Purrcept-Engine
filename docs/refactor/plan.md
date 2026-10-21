# Purrcept Engine 调查与设计计划

状态：2026-10-15，以 `9329231` 为基线的全后端架构重组已完成六阶段迁移。
当前结构为 domain / infrastructure / app、统一 plugin_system 与具体 plugins；
阅读入口见 [代码架构与调用链](../architecture.md)。Purrcept 业务、模型材料、
迁移保持基线语义，后续配置与模型材料变更以下方当前记录为准；旧内部模块路径
不提供兼容转发。
此前功能与外部验收记录保留在下文；本次仅做离线与本地 Worker 验证，
不扩大真实模型、消息、设备或部署的授权。

## 当前实施状态

2026-10-20 睡眠积压闭锁修复：只读核对当前库有256条 QQ 普通输入停在
submitted / subject.sleeping，恰好占满实例的256条接管额度；前台空闲仍不能
接管新私聊或 @。按用户确认将睡眠普通输入改为仅入历史、醒来按需回看，
不再进入后续前台快照。正式接管事务先处理旧睡眠积压，再核额度；新普通输入
在同事务以 processed / subject.sleeping.recorded 结案调度并释放待办。原文、
媒体、去重身份、证据和独立反思待办保留，不把此结案当成模型已看过。
高优先、claimed/processing 工作不被释放或豁免额度；单条大小与瞬时提交上限
不变。协调、取批复用相同门槛，当前休息不变，不自动起床或补投已拒收消息。
接管可能在协调者读取原文与版本之间结案旧普通输入；重新取批并复查提交后
阶段，避免把终态传给 submit_inputs 或登记升级判断造成调度器故障锁存。
新增7项真实临时 SQLite 回归覆盖条数/字节满额、大量睡眠原文/媒体/证据保留、
旧 submitted 满额重开后的三条恢复入口及受保护工作。完整 Host/Worker 睡眠
场景以256额度连续接管260条普通输入，再验证高优先唤醒及离床恢复；新增2项
确定性竞争验证普通/高优先批次读取期间入睡和正式接管，主脑仍可继续。
相关存储/调度77项、最终睡眠/协调68项及全量1271项通过，仅有既有第三方
Pydantic ReadOnly 提示；Ruff、319份文件格式、strict
Pyright（0错误）、compileall 与差异空白检查通过。wheel 非 editable 安装完成
Windows Worker、同库媒体重开和资源归还冒烟，模型调用0。库格式仍为14，
未修改用户配置、当前库或运行宿主；更新并重启后正常事务会收回旧睡眠积压。

2026-10-19 QQ 原文件保存：新增 QQ file_url，按实际私聊/群文件 ID 取得 HTTP(S)
链接，群 busid 有则原样传递；首回包和迟到回包均核验真实链接，保留原回包证据。
group_upload/offline_file 通知纳入接管并展示实际文件信息，私聊文件保底唤起，
群文件走普通注意；接管不自动下载。Notebook save_file 下载原字节到自身
attachments/，不覆盖，不解压或提取正文；下载 Markdown 不自动进入技能目录。
复用明确 infrastructure/files.py 技术工具，不建立插件间资源依赖或新 Kernel
端口。配置独立 file_max_bytes=20MiB、file_timeout_seconds=30秒，最多三次
逐跳核验重定向；完整接收后才 fsync 并用 Windows 不覆盖 rename 发布，目录/
暂存句柄均复核边界。取消网络关闭响应，实际磁盘线程仍由所有者等待归还。
新增 file_info，目录展示原始文件元信息，move/delete 对附件沿用真实版本核验；
QQ 发送仍受允许目录限制，未放宽本地任意文件访问。成功回执仅交路径、字节数
和 SHA256，原字节不进入 Worker IPC、媒体历史或记忆。README、架构、接口
契约和随包示例同步，数据库格式不变，未修改用户配置或线上库、未重启宿主。
离线传输、临时 SQLite 与真实 Windows Worker 已覆盖获取链接到保存及关闭重开，
包括路径/链接拒绝、超限、残缺、超时、取消、并发目标占用、版本冲突和迟到回包。
相关113项和最终全量1262项通过，仅有既有第三方 Pydantic ReadOnly 提示；
Ruff、318份文件格式、strict Pyright（0错误）和 compileall 通过。
wheel 非 editable 安装完成 Worker、同库媒体重开与资源归还冒烟，模型调用0。

2026-10-19 睡眠调度与一次历史维护：明确 rest 后，同事务将普通缓存、未决注意
和旧普通唤醒意图转为 submitted，原文/媒体与积压额度不变。取消在途注意并
拒绝迟到采用，暂停普通注意、最低唤起升级和主动思考；高优先输入继续保底
唤醒并合并暂存。取批与定时接管再次复查休息，避免排队上床被旧观察绕过；
leave 后恢复注意，主动思考保留开关并重新等待完整间隔，不追补睡眠周期。
上床所在主脑轮次结案后持久领取一次压缩机会，app 显式装配独立 sleep_history，
复用 Core 预览、HistoryPreparation 与原摘要预算/保护/归档/安装 CAS；只跳过
容量触发阈值，不减少近期保护或放宽真实容量。预览文字不成为刺激或主脑请求。
高优先处理不等待摘要，旧版本或非空闲会话拒绝迟到安装；关闭真实收回任务和
包装。RestState 的可选内部 JSON 字段 history_compaction_claimed 保存领取，
更新时长及重启不重复，新一次起床再入睡重置。无旧前缀或失败保留原历史，不
自动重试或伪报成功；反思保持独立。格式仍为14，无清库、用户配置或线上操作。
新增睡眠6项验证真实床工具/Worker、轮次结案顺序、摘要成功/失败与重启、在途
注意取消、优先唤醒与退出不等待摘要、旧唤醒和定时接管的睡眠复查；相关局部
105项及最终调度/存储28项通过。首次全量1237项通过；补强取批原子门槛后的
两次全量分别1236项通过/1项初始化错误、1235项通过/2项初始化错误，均为
pytest 创建 Windows 事件循环套接字时的 WinError 10055，业务尚未执行。
所有受影响用例单独复核通过，未修改业务或测试去规避该系统资源错误。
Ruff、316份文件格式、strict Pyright（0错误）、compileall、差异空白检查通过；
前端33项、类型/样式/构建与实际打包页面30组页面/视口和开关验证通过。
wheel 非 editable 安装完成真实 Windows Worker、同库媒体重开及资源归还冒烟，
模型调用0。未重启运行宿主；重启后应用新睡眠策略，真实模型效果待观察。

2026-10-19 主动思考双期限：沿用运行期开关和 proactive_idle_seconds（默认300秒），
改为无新刺激且注意空闲，或主脑未激活达到间隔，任一到期时在主脑空闲后唤起。
新接管只复位静默期限，实际取出主脑快照才复位激活期限；注意判断和轮内工具/
重试不刷新后者。唯一协调者在普通注意取批前检查期限，允许越过仍在等待的
注意调用，沿既有 priority 冲刷缓存并合并 observe/assessing 输入；迟到注意结果
仍由版本 CAS 拒绝。接管事务区分静默复查与主脑到期，后者允许新背景刺激和
注意待办，但已有 priority/foreground/claimed/processing 时不重复接管。
主脑忙碌时不积累周期输入，到期只在完成后合并触发一次；容量不足按完整间隔
退避。刺激正文说明实际触发原因，不再错误宣称期间没有新刺激。同步 WebUI、
随包配置说明和文档；不改配置键、数据库格式、用户配置、线上数据或运行宿主。
局部78项通过，新增6个参数化场景覆盖持续 observe 后到期唤醒、真实唤醒重置
期限、注意等待不阻塞、迟到 observe/urgent 失效及接管阶段的原子竞争。全量
1231项通过，仅有既有第三方 Pydantic ReadOnly 提示；Ruff、315份文件格式、
strict Pyright（0错误）、compileall 和差异空白检查通过。前端33项、类型、样式、
构建及实际打包页面30组页面/视口与开关验证通过，已检查系统页桌面/窄屏截图。
wheel 非 editable 安装完成真实 Windows Worker、同库媒体重开及资源归还冒烟，
模型调用0；重启后使用新行为，真实模型时延与缓存成本效果未做在线验收。

2026-10-19 记忆写入恢复：按用户要求先将既有本地修改提交为 `7419f0a`，再修复
反思调用边界。此前只读核对发现长期记忆为0；两次 revise_memory 分别使用证据
ID、截短片段引用，均被正确拒绝，随后仍以 no_change 结案。原文页现在明确提供
可复制的 memory_sources，错误回执列出完整引用与未解决写入；不猜测来源、不
自动删除坏引用，也不放宽真实呈现、版本或范围校验。本批失败意图须同 id 修正、
明确延期或具名说明放弃理由，理由随结案保存；未结案中断后仍保留原文待办，
新批次重新取得已读资格。反思提示词明确平淡生活的记忆价值，以及单次选择与
稳定偏好的区别，保留查重、噪声过滤和无写入配额。新增字段仅为结案 JSON 的
可选记录，存储格式仍为14；未修改线上数据库、历史结案、用户配置或运行宿主，
未自动重审旧材料或补写记忆。更新需重启后生效，真实模型记忆行为仍待观察。
局部记忆/反思64项及模板/反思32项通过；新增回归覆盖错误引用纠正、混合真伪
来源原子拒绝、明确延期/放弃、真实 Core 同批修复及关闭重开后的资格隔离。
首次全量仅因反思提示词冻结样本未同步而失败，逐段核对更新后全量1225项通过，
仅有既有第三方 Pydantic ReadOnly 提示。Ruff、315份文件格式、strict Pyright
（0错误）、compileall 和差异空白检查通过；wheel 非 editable 安装完成真实
Windows Worker、同库媒体重开和资源归还冒烟，模型调用0。

2026-10-18 统一历史媒体过期：原 audio_history 重命名为 media_history，唯一
expire_history_media 投影同时覆盖 AudioBlock 和 ImageBlock（字节及图像 URL），
递归替换嵌套工具回执。请求保留最近两个已完成轮次及当前后缀；更早图片以
“历史图片已过期”替换，音频保持“历史语音输入已过期”。不做 OCR、ASR 或推测
描述；工具协议、文字、隐藏推理、来源/时间、回执身份和原始证据保持。
会话恢复、容量预览和摘要继续共用投影，成功轮次才安装历史；失败和重试不
推进窗口。前台/反思混合媒体真实 Core/临时库覆盖轮内工具、失败、重试及重开，
并核对原始输入和冻结请求的图片/音频字节；另覆盖纯图片、0/1/2轮、URL及深层回执。
局部56项、全量1218项通过，仅有既有第三方 Pydantic ReadOnly 提示。Ruff、315份
文件格式、strict Pyright（0错误）、compileall 和差异空白检查通过；wheel 非
editable 安装完成真实 Windows Worker、同库媒体重开与资源归还冒烟，模型调用0。
无需存储迁移或清库；未修改用户配置/运行宿主，重启后现有历史自动应用。

2026-10-18 桌面截图文件：`me.computer.screenshot()` 先接管完整原图与来源证据，
再创建独立 JPEG，成功回执返回绝对 path、media_type、sha256 和 mirror_state。
新增电脑配置 screenshot_dir，默认 `.purrcept/computer-screenshots`，可沿 QQ
原媒体允许目录规则显式发送。连续观察不写文件，关闭不删除已交付文件；
同一所有者覆盖接管与写入，取消等待不遗失在途文件工作。目录或写入失败仍
保留原图及 evidence_ref，不返回假路径，实际写入失败清理本次残件。
局部电脑/QQ媒体32项通过；真实临时库、Windows Worker 与离线 OneBot 覆盖
私聊/群聊按图片或文件发送、默认/自定义目录及权限、失败和取消关闭边界。
首次全量因 Windows 在创建事件循环 socket 时返回 WinError 10055，出现16个
初始化错误；相关测试单独通过，未改生产逻辑。随后标准全量1213项通过，
仅有既有第三方 Pydantic ReadOnly 提示。Ruff、315份文件格式、strict Pyright
（0错误）、compileall、差异空白检查通过。wheel 非 editable 安装完成实际
Windows Worker、同库媒体重开及资源归还冒烟，模型调用0。
未改用户配置、在运行宿主或相邻仓库；真实桌面及 QQ 发送待重启后实机验证。

2026-10-17 VAD 自动停播开关：收音插件增加 `vad_interrupt_enabled`（默认 true）
及 `set_vad_interrupt` 能力，WebUI“设备 → 收音”可随时切换。配置决定重启默认值，
本次选择跨收音重开保留。分段状态始终运行，只在持续人声确认点决定是否发出
停播请求及通知；已发出的请求继续收尾，开启不补发已经跳过的确认。动态材料与
状态快照均显示当前选择，修正 start 能力中仍声称直接抢占生成的旧说明。
真实临时库和插件调用覆盖 ASR/原声切换、同一设备会话连续收音、关闭保留在途
接管及最终发言关联。全量1207项与前端33项通过，仅有既有第三方 Pydantic
ReadOnly 提示；Ruff、314份文件格式、strict Pyright、compileall 及差异空白检查通过。
实际打包页面完成30个页面/视口、鼠标/键盘开关及断线禁用验证；非 editable wheel
完成真实 Windows Worker、同库媒体重开及资源归还冒烟，模型调用0。
未修改用户配置或操作正在运行的宿主，更新需重启后使用，真实设备效果待验证。

2026-10-17 历史原声窗口：固定保留最近两个已完成的完整轮次及当前轮，较早
AudioBlock 原位替换为“历史语音输入已过期”，不接入 ASR 或猜测转写。
context/audio_history 提供唯一不可变投影，递归覆盖嵌套工具回执；文字、图片、
来源、时间和工具关联不变。会话恢复、容量首发预览及摘要读取共用投影，
来源冻结和摘要归档仍按原检查点核验。成功后随正常轮次提交，失败不改历史；
输入、真实模型请求及回执证据保留原音，无存储模式或用户配置变更。
局部53项验证覆盖轮内工具/传输重试、失败不推进窗口、原件与请求保留、关闭
重开及摘要不重新带回过期音频。最终全量1204项通过，仅有既有第三方 Pydantic
ReadOnly 提示；Ruff、314份文件格式、strict Pyright（0错误）、compileall 与
差异空白检查通过。wheel 非 editable 安装完成真实 Windows Worker、同库媒体
重开及资源归还冒烟，模型调用0。未操作用户数据或在运行宿主，实际延迟待验证。

2026-10-17 VAD 停播与模型生成分离：持续人声只请求音频所有者停止当前及
当时已有候播，起音以高优先通知保底交给下一轮，当前生成继续。来源取消、
重复接管和停音收尾不丢失通知，也不重复停止。speech_started 不授予直接
抢占；只有起音及普通缓存的批次不登记最低唤起升级，避免间接取消 QQ 等
其他通道的处理。最终发言内容及其他高价值输入仍按既有语义升级规则运行。
提供方用 Core PromptTemplate 生成停播请求说明，明确下一轮核对真实音频
回执，不能把请求当成停止已确认或把部分播放当作完整送达。未修改旧事实、
存储模式、用户配置或在运行宿主。局部70项及接管/关闭15项通过；全量1201项
通过，仅有既有第三方 Pydantic ReadOnly 提示。Ruff、格式、strict Pyright
（0错误）、compileall 与差异空白检查通过。wheel 非 editable 安装完成真实
Windows Worker、同库媒体重开和资源归还冒烟，模型调用0；实际设备待重启后验证。

2026-10-17 VAD 停音后的解码管道背压：只读核对本地回执发现，20:10:21
播放开始后被 VAD 停止，旧播放一直没有 finished，后续语音仅接管/候播，
180秒后旧调用变为 unknown。真实本地 FFmpeg 回归复现：提前关闭消费端时
stdout 缓冲积压，杀进程后仅 wait 仍不能完成管道归还。解码和调音共用显式
资源收尾：先终止进程与输入生产，再并行分块丢弃输出及等待诊断，最后确认
进程退出；取消启动也沿同一路径归还。原来的失败/未知事实不改写、不重播。
声音局部43项通过，覆盖两级解码/调音提前关闭、背压或上游停顿的 VAD 中断、
同一队列后续语音正常完成及真实本地进程退出。最终全量1199项通过，仅有
既有第三方 Pydantic ReadOnly 提示；Ruff、格式、strict Pyright（0错误）、
compileall 与差异空白检查通过。wheel 非 editable 离线安装完成真实 Windows
Worker、同库媒体重开和资源归还冒烟，模型调用0。
没有连接真实 TTS 或设备，未修改用户运行配置、数据库或在运行宿主。

2026-10-17 主动思考开关：默认关闭，TOML 的 subject.proactive_thinking 与
proactive_idle_seconds 提供启动默认值及正空闲间隔（默认300秒）。WebUI 系统页
通过原本机控制账本实时切换，按实例、请求身份与选项版本核验，状态取自宿主；
页面不计时、不乐观翻转，不改写配置默认值，重启重新计时。
ProactiveThinking 仅持有选项、锁与单调期限，唯一刺激协调者负责到期检查。
真实新刺激按同库接管游标复位，重复接收和状态写入不复位；前台/注意及未提交
输入优先，忙碌期间不积累周期，结束后重新等待完整间隔。到期原子复查游标与
输入阶段，再沿正式接管保存带时间的 subject.idle，保底唤起但不抢占；observe
收件箱可一起处理。容量不足显式记录背压，下周期再尝试，不裁输入或补发离线周期。
开关修改与空闲接管串行，关闭不撤回已接管事实。新增选项等待暴露的关闭竞争已修正：
等待故障发生即锁存，不能被清理其他等待者期间的关闭取消覆盖；屏障回归保留原异常。
局部51项、关闭/主动思考16项、控制与资源联合32项、前端33项通过；浏览器验证30个页面/视口组合、
点击/键盘切换与断线禁用，截图已检查。wheel 非 editable 安装完成真实 Windows
Worker、同库媒体重开和资源归还冒烟，模型调用0。最终全量1195项通过；Ruff、
格式、strict Pyright（0错误）、compileall 与差异空白检查通过，只有既有第三方
Pydantic ReadOnly 提示。没有连接真实模型、QQ 或设备，也未修改用户运行配置。
全量复跑另遇到既有 Windows 整进程句柄等号的波动（句柄减少7个）；资源
测试现直接记录并逐一核验本次实际创建的 Job/进程句柄已失效，同时要求全进程
句柄不增长，与现有失败路径的资源证明一致，不修改生产 Windows 进程实现。

2026-10-16 注意判断的主体身份：AttentionModel 显式持有由应用使用前台同源
配置和 Core 角色卡模板生成的完整 profile。ATTENTION_IDENTITY_PROMPT 统一
绑定角色卡及原判断规则，decision 放入 ChoiceQuestion.instructions，生成式
注意放入 system；普通批次与 minimum_wake_assessments 复用同一请求入口。
规则要求群聊中明确叫主体姓名/已有别称、搭话或提问时，即使无 @ 也选择 wake，
并区分同名他人、无关引用和重复闲聊。提名不直接变成 urgent 或自动对外回复；
来源过滤及私聊/@ 的可信保底唤起不变，没有增加内核关键词匹配或隐式身份猜测。
角色卡保留字面花括号、不串主体，完整计入 SDK 请求容量；超限保留未完成判断并
交主脑，不静默裁角色卡或刺激。离线 Host/Core/真实临时 SQLite 和 QQ 传输替身
回归覆盖无 @ 群消息、两种注意后端、同源角色卡、忙碌升级与角色卡超限。
局部82项及 Windows Worker 独立11项通过；Ruff、格式、strict Pyright（0错误）、
compileall 与差异空白检查通过。首轮全量1182项通过，既有 Windows 资源测试因
进程总句柄数从639降至632、不满足严格等号而失败；未修改其实现或断言，
全量复跑1183项全部通过，仅有既有第三方 Pydantic ReadOnly 提示。
未连接真实模型或 QQ，实际语义识别效果仍须实机观察。

2026-10-16 前台提示词与时间材料：按用户提供的虚拟生命体正文整体替换主脑
系统指令，仍由 Core PromptTemplate 绑定 profile。保留先前冻结的角色卡、注意、
反思和摘要基线，新正文另存用户批准的独立测试样本，不重写其它模板预期。
每条刺激展示 ISO 8601 发生/接收时间；每逻辑请求在插件准备结束后采样当前
宿主时间，放在动态尾部，同请求重试复用原材料。完整行动结果附 UTC 时间线，
容量引用仍保留时间；当前调用动态投影补可读时间，迟到版本使用自己的记录时间。
保留权威 JSON、原始数字时间戳与确切回执版本，不把宿主记录冒充平台送达证明。
真实临时 SQLite、Host、Core 和 Windows Worker 的离线回归验证：msg-2 在旧轮
思考期间、对 msg-1 的行动之前到达，下一批仍展示其真实时间；后续逻辑请求刷新
时钟，传输重试材料完全相同，system 前缀稳定，实际效果只发生一次。
验证：局部42项、全量1178项通过，仅有既有第三方 Pydantic ReadOnly 提示；
Ruff、格式、strict Pyright（0错误）、compileall 与差异空白检查通过。
未连接真实模型、QQ 或设备，未修改 Core、存储模式或现有用户数据目录。

2026-10-16 Core 提示词模板接入：角色卡、前台、注意与反思固定文字集中到
domain/subject/prompts.py；上下文、历史摘要和动态提醒布局集中到
domain/context/templates.py，能力目录条目也直接使用 Core PromptTemplate。
角色默认值使用模板默认绑定，调用方只准备值并通过 Core 的 render、instruction、
message、reminder 输出；不新增全局管理器或字段替换器。结构化字段树移到
serialization，与原有 JSON/YAML 数据编码同层；原生媒体仍按原块位置交付。
冻结迁移前材料验证默认/完整/空值/纯空白/花括号角色卡、系统指令、摘要消息、
注意背景和动态尾部，目录示例额外核验 Python 字典与未知花括号不被插值。
验证：模板与材料局部29项、相关主体/反思/插件局部115项通过；最终全量1177项
通过，只有既有第三方 Pydantic ReadOnly 提示。Ruff、格式、strict Pyright
（0错误）、compileall 与差异空白检查通过。wheel 在非 editable 环境完成真实
Windows Worker、同库媒体重开和资源归还冒烟；模型调用为0。Core 与现有数据
目录未修改，没有新增依赖、存储迁移或配置项。

2026-10-16 刺激协调重构：StimulusCoordinator 成为唯一刺激调度循环；注意改为
被调用评估器，主脑每次只推进外部唤醒时取出的输入快照。observe 连续提交但
不唤醒，后续 wake 原子排空收件箱；忙碌时普通 urgent 和高价值批次升级绑定旧
生成，迟到判断不能更改已取走输入。移除注意 defer 配置、字段和工作器。
格式14原子升级12/13，保留原文、媒体、会话与真实回执，旧未执行 defer 转为
一次保底唤醒，旧升级观察原样归档；未打开本工作区现有用户数据库。
回归包含多批积累、忙碌评估、独占唤醒快照、工具期间不拉取输入、高价值冲刷、
迟到判断、旧轮预算与失败隔离、关闭取消、迁移和恢复。背景准备期间旧生成结束
的回归还验证：未启动请求的资格到期只留下未完成判断，不误报请求证据损坏。
最终后端1166项、前端32项测试通过；Ruff、格式、strict Pyright（0错误）、
compileall 与差异空白检查通过。前端产物已重建；新 wheel 在非 editable 环境
完成真实 Windows Worker、同库媒体重开和资源归还冒烟，模型调用为0。
只有既有第三方 Pydantic ReadOnly 提示；未连接真实模型、QQ 或设备。

2026-10-16 QQ 私聊最低唤起：用户确认私聊消息应与直接 @ 同级。QQ 接入在
既有来源过滤及原生去重之后，为私聊 qq.message.received 设置 minimum_wake，
文字、字符串消息和图片均直接交给主脑；普通群聊及通知的原规则保留。
主体忙碌时复用既有最低唤起升级判断，只有绑定原代次的 urgent 才抢占，
observe/defer/wake 均不能降级或新设延期。没有增加核心 QQ 字段解析、模型
提示、自动回复、停音或重试；既有已结案输入及原生重放仍沿用第一次接管事实。
回归覆盖真实 QQ 消费者、Host/Core 与临时 SQLite 的空闲唤起、无需普通
合批、忙碌升级及四种选择，以及消息类型、通知边界、过滤与原生去重。
验证：局部 33 项、最终全量 1166 项通过；Ruff、格式、strict Pyright
（0 错误）、compileall 与差异空白检查通过，仅有既有 Pydantic ReadOnly
提示。首次全量中既有 Windows 进程句柄总数相等断言发生下降波动；独立
11 项及全量复验通过，未改 Worker 实现或放宽其校验。测试使用离线模型、
传输替身和真实临时库，未发送实际 QQ 消息；重启后新接管私聊采用该规则。

2026-10-15 超时结案重复取消及退出修复：现场异常组由整轮 TimeoutError 和
失败入账 get_session 期间的 CancelledError 组成，同一锁存故障随后传播到 QQ
与收音。离线复现确认：会话仍按 busy 接受 VAD/关闭取消，未区分生成和结案。
成功提交与异常结案首次等待前现在封存代次，拒绝后续抢占；失败经历和原始
预算原因正常提交，独立下一轮可继续处理。真实行动及已批准副作用语义不变。
QQ 和收音补齐资源释放明证，传输/设备关闭失败保持否定，已结束的历史异常
不再拖住权威库与目录锁。采集驱动新增显式 resources_released()，无旧适配；
收音归还失败仍等待识别/起音任务收回再报告。CLI 同时接住包含取消的异常组，
资源未确认归还期间保留二次 Ctrl+C，不提前退出到非守护线程等待。
回归覆盖真实临时库中的超时/故障结案与迟到 VAD/关闭、正常后续轮次、真实
Host/Core 的超时接续、QQ/收音双失败后的存储线程和 OS 锁归还，以及原生关闭
失败保持资源所有权。仅使用离线模型、设备/传输替身，未改用户数据和运行配置。
全量复跑发现既有结构化调用测试使用 30 ms 墙钟，可能在目标开始提交前提前
超时；改为在真实开始记录提交后推进测试时钟，保留超时及 Provider 未进入的
原断言，不改变执行器代码或生产预算。
验证：最终全量 1154 项通过，Ruff、格式、strict Pyright（0 错误）与
compileall 通过；仅有既有第三方 Pydantic ReadOnly 提示。实际设备与服务
尚待重启后复测，本轮不将离线归还证据冒充实机效果确认。

2026-10-15 单次原生缺口恢复：新增 CaptureGap 作为采集驱动的有序边界，
不再因一次 PortAudio input_overflow 撤销整场收音。丢样块之前的不足一帧余量
与重采样器被丢弃；运行时只取消跨越缺口的当前残句，重建 VAD 私有状态，
后续发言沿同一声卡和会话接管。已结束但等待 ASR 的前句保留，起音迟到入账
补回原缺口关联；没有付费识别重传、录音重放或隐式设备重开。
五秒内最多三个缺口，第四次明确暂停；队列满与设备断开仍终止。此前“原生
溢出一律暂停”的策略由本条替代，模型、权限、数据库与行动协议不变。
回归覆盖转写/原声后续入账、慢起音确认、已结束前句保留、采样不跨缺口、
重采样/VAD 重建、持续故障停止和稳定窗口后故障额度释放。
验证：局部30项及全量1138项通过，Ruff、格式、strict Pyright（0错误）和
compileall 通过；仅有既有第三方 Pydantic ReadOnly 提示。使用原生库替身、
真实临时 SQLite 和离线模型，未开启实际设备、连接服务或修改运行配置及用户
数据。现场仍可能发生驱动丢样，本次保证可识别的孤立缺口不再永久中止会话。

2026-10-15 原生收音溢出排查：采集取消固定 10 ms 回调要求，使用 PortAudio
自选块大小，再连续组装为原有 512 样本 VAD 帧。补齐主事件循环中尚未移出的
检查点恢复、编码与完整校验，复用受控工作线程准备；转换返回后复核原轮预算，
取消仍等待纯计算归还，不允许迟到历史覆盖新轮。原生丢样与有界队列满继续
明确暂停，不拼接残缺发言、不隐式重开设备。故障日志在原生回调之外记录设备、
采样率、队列深度和消费停顿间隔，区分原生驱动丢样与宿主/ASR 积压。
回归覆盖可变原生块的样本连续性、溢出清理与日志，以及独立回调线程在慢容量
准备、慢检查点恢复/保存期间持续采集。历史现场没有这些时序诊断，不能据此
断言真实声卡本次丢样的唯一原因；设备效果仍待实际复测。
验证：全量1130项通过，Ruff、格式、strict Pyright（0错误）和 compileall
通过；仅有既有第三方 Pydantic ReadOnly 提示。未连接真实模型、消息或设备，
未修改运行配置、已有用户数据或相邻 SDK；重启宿主后采用新的采集策略。

2026-10-15 语音打断后队列恢复：FFplay 停音同步撤销本次 PCM 消费任务，
不再仅终止子进程后等待仍悬停在网络或解码读取中的 feed。重复停止不再次
取消正在退出的消费者，旧播放身份仍不能停止新声音。取消经过既有真实终态
与同库音频事实提交，保留已播部分，不重放旧音频；同步完整合成的所有权不变。
回归使用无声 Python 播放子进程、真实临时 SQLite 和可选真实 FFmpeg，覆盖
上游停顿时打断、finished/control_confirmed 入账、资源归还与同队列下一次发声。
验证：全量1126项通过，1项既有 Windows 总句柄相等断言波动（633→631）；
对应 Worker 文件单独重跑11项通过，未放宽断言。音频、VAD 交接与设备局部
测试通过，Ruff、格式、strict Pyright、compileall 通过。未连接真实模型、
消息或音频设备，未修改已有用户数据；实际收音需重启宿主后另行复测。

2026-10-15 模型内容声明与刺激投影：生成和 decision 配置统一使用
`supports_contents`，默认仅 text，允许 image/audio/video；加载后冻结，拒绝未知、
重复或缺少 text 的声明，移除独立音频布尔项，不保留旧字段适配。注意与前台
分别按各自声明渲染同一冻结布局：不支持的媒体原位换成 TextBlock，包含明确的
未交付说明、MIME、证据身份、字节数及已有名称/alt_text，不读取或发送原字节。
支持的媒体仍恢复完整原件，缺失、损坏、格式与计量故障不降级。过滤附件后按
原索引回填，保留交错与重复位置；请求记录和容量校验使用实际投影结果。

Core decision 仍仅交付文字/图片，其他声明类型转文字引用；Core 尚无 VideoBlock，
video 是允许的声明值，不代表已有原生视频传输，声明后尝试原生交付仍明确失败。
证据工具、旧历史与数据库格式不变。收音准入改为检查列表中的 audio，仍要求
适用的计量规则；本机配置迁移为前台 text/audio、注意 text，未更改端点或凭据。
Pyright 显式选择项目 .venv，VS Code 默认解释器同步；修正筛选后元组的类型声明，
没有放宽 strict 检查或加入 ignore。

验证：全量1125项通过，包含真实临时库、模型传输替身及 Windows Worker；仅有
既有第三方 Pydantic ReadOnly 提示。Ruff、格式、直接运行 strict Pyright（0错误）、
compileall 通过。wheel 构建、非 editable 离线安装与真实临时库重开冒烟验证同一
录音在 text 模型中为占位，在 audio 模型中仍保持原字节、alt_text 与重复块顺序。
补测注意为 text、前台为 audio 的独立投影，以及不支持视频的引用与声明后的显式
传输失败。未连接真实模型、QQ或设备，未修改相邻 Core/LiteLLM 仓库。

2026-10-15 刺激提供方预渲染与有序原生块：新增
`InputSubmission.with_content(source, blocks)`，接入方冻结 TextBlock、ImageBlock、
AudioBlock 的原始交错顺序。布局只保存来源、文字、媒体索引及 alt_text，字节仍
进同库附件；纳入原有接管额度、指纹和证据事务，不新增数据库格式或媒体 base64。
QQ 自行选取必要字段生成 YAML，明确消息身份、事件类型、发送者、目标、时间、
正文及必要 @/回复/媒体缺口；通知不伪造自身 message_id。麦克风交付说明与原声。
框架仅按来源首次出现分组，同组保持接管顺序，插入标题、证据和缺口 TextBlock，
不解析 YAML 或平台原帧，不移动媒体；注意和前台共用入口。失败媒体只在原位置
替换为未展开引用。原始平台记录、正文和媒体仍保留，重放沿用第一次冻结布局。
注意工作背景改为精简 YAML，去掉重复包装；固定判断题和工具 schema 不变。

验证：最终全量1105项通过，1项既有 Windows 进程总句柄相等断言失败
（612→605）；该文件单独运行11项通过，没有修改 Worker 或放宽断言。Ruff、
格式、strict Pyright、compileall 与 diff 检查通过。独立 YAML 阅读器核验字符串
身份、嵌套结构、多行正文、数值与 Unicode 换行保持。wheel 构建及非 editable
安装验证真实临时库重开后原声字节、alt_text、重复位置和块顺序一致。未连接真实
模型、QQ或设备，未更改相邻仓库、运行配置或用户数据；新格式用于新接管输入，
已有会话消息不重写。接口和提供方示例见 `docs/input-materials.md`。

2026-10-15 注释整改：按 CONTRIBUTING.md 检查全部207个后端模块与1277个
公开函数/方法入口，补齐中文参数、结果、典型异常及资源/事务/取消契约，
整理模块阅读入口和关键私有交接说明。52个前端公开函数补充 JSDoc，输入、
时间单位、未知状态、控制身份与页面观察寿命有明确说明；离线构建脚本和
嵌入示例同步整理。删除无上下文的裸 TODO，保留实际原声准入条件的原因说明。

协议文档按真实生成链单独核对：130个 Pydantic 模型 schema 与11个 Core
工具 spec 和整改前完全一致。工具原 docstring、参数模型及嵌套 MediaReference
说明保留原文；维护契约移到相邻注释，10处原工具说明的 D205 格式现状也
保留，避免整改改变模型提示。其余 docstring 的 D205/D209 已核验。
全部 Python 文件去除 docstring 后的 AST 一致；22份前端/调音室源码的非注释
token 一致。没有更改执行逻辑、配置、数据库格式、依赖或相邻仓库源码。

本轮验证：1088项后端测试、32项前端测试、前端类型检查、Ruff、格式、strict
Pyright 和 compileall 通过，保留既有第三方 Pydantic ReadOnly 提示。没有新增
行为测试，没有重新构建包或面板产物；实际 Windows Worker 随后端既有测试
验证，未连接真实模型、QQ或设备。

2026-10-15 阶段六：内置插件统一从 plugin_system 引用契约，移除对 SQLite
与其他具体插件内部模块的公共依赖。音频事实和时区/闹铃纯规则归 domain，
共享 WAV 编码归基础设施；模型请求证据与重试配置协议归独立 ports。
新增架构阅读指南，README、AGENTS 与契约同步指向五条业务阅读路径。
迁移完成的旧命名空间没有源文件或转发别名，wheel 中也没有旧路径。

新增依赖方向、Host 公开签名/顶层导出与基线 DDL 重开检查，固件明确提取自
`9329231`。七项架构检查加上一项 Worker 隔离导入检查，使后端总数从1080
增至1088；原行为断言保留，仅按新归属调整导入和装配替身。

最终验收：1088 项后端测试和32项前端测试通过，前端类型检查及生产构建、
Ruff、292 个 Python 文件格式、strict Pyright（286文件，零错误/警告）、
compileall 与 diff 检查通过。另做56项请求/架构针对性验证。保留第三方
Pydantic 对 SDK ReadOnly 注解的既有提示，没有放宽校验或关闭检查。

安装验收在 build/refactor-verification 中新建最小/完整依赖两套环境，
依照现有哈希清单从本地缓存离线安装，SDK 与 Engine 均使用非 editable
wheel。两套依赖检查和 `python -I scripts/verify_installation.py` 均通过：
实际 Windows Worker 调用确认、8个包内资源、合成图像像素和引用重开、行动
查询及目录锁归还，模型调用数均为0。Engine wheel SHA-256 为
`9bdbcbd3bde97b909b02f41dcbac0dc52cf59d9603d2c4fad819ad9b68bc73d5`。
仅从相邻 SDK 源码构建验收 wheel，未修改或提交相邻仓库原有变更；未改变
数据库格式、运行配置或用户数据，没有启动真实模型、消息或设备。

2026-10-15 阶段五：EngineHost 保留原公开签名，缩为 258 行嵌入门面。
EngineAssembly 装配，HostLifecycle 持有唯一启动/关闭任务与显式依赖收尾，
InputAdmissionService 持有接管交接，HostQueries 提供只读事实；EngineState
只在应用内部保存部分启动和真实资源归还事实。关闭同步撤派发、并行停止
所有者、依赖归还后解锁的顺序保留。1081 项全量离线测试、30 项关闭相关
验证、Ruff、格式、strict Pyright 与编译检查通过。


2026-10-15 阶段四：默认 ExecutionService 归入 infrastructure/execution，
WorkerSession 负责进程/帧资源，CallDispatcher 负责共享能力队列与真实回执，
二者借用同一行动状态。领域保留目录、Provider 契约、执行端口和源码校验。
Worker 新路径继续绕过顶层导出，明确建立标准库命名空间；Windows 进程实现
独立为技术工具。原 1080 项全量测试、101 项执行相关验证及新增隔离导入测试
通过；Ruff、格式和 strict Pyright 通过。AST、builtin、IPC 和额度不变。


2026-10-15 阶段三：SubjectService 显式连接注意、前台和延期工作器，绑定
独立反馈/反思后台；各流程借用同一协调状态与原连续会话，不复制资格。
领域、上下文、活动和认识迁入 domain；活动输入校验与纯控制规则独立，
实际提交、时钟与驱动仍归单一 ActivityService。SDK 编译通过 MaterialCompiler
显式注入，检查点媒体持久化与纯 Core 编解码分开，领域不再导入存储实现
或模型 SDK。1080 项离线测试、Ruff、格式和 strict Pyright 通过。


2026-10-15 阶段二：SQL 实现迁入 infrastructure/sqlite，SqliteExecutor
独占连接、队列、通知和关闭，Store 和各账本共用同一提交器；初始化与原有
升级事务集中到 schema，不改变格式。模型客户端、向量 HTTP、计量、目录锁
和文件日志归入基础设施，客户端借用最小只读配置。1080 项离线测试、Ruff、
格式及 strict Pyright 通过。SDK 编译注入随下一阶段主体端口一起迁移。


2026-10-15 架构重构以 `9329231` 为基线，按 domain/infrastructure/app 分层；
只调整组织和依赖，不改变 Purrcept 业务、数据库或模型材料。此前标为未提交
的功能修改已包含在该基线。阶段一迁出领域记录、公开材料编码、严格 JSON
值以及共享插件契约，保持类名、字段和编码规则。1080 项后端测试、Ruff、
格式与 strict Pyright 通过；仍保留第三方 Pydantic ReadOnly 提示。


2026-10-14 模型状态精简（未提交）：`app/subject.py` 不再自动注入插件诊断
snapshot，模型状态由各插件的 `prepare_request` 显式提供，不按实例名推测类型。
`context/presentation.py` 从完整宿主工作对象投影业务字段：保留全部未决计划、
监管、活动、来源引用和关键调用事实，移除会话版本及运行代次；回执去重仍先
核对内部完整对象，未知效果、迟到确认及原始参数/结果不按诊断字段过滤。
预算和身体意图只提供组装时剩余秒数，状态观察时间转为 ISO 时间。

电脑只交连接/观察、最后窗口标题/采集时间、待处理输入及错误；QQ 保留账号、
连接与缺口；收音保留实际阶段、可用方式及缺样原因，省去电平、未定转写和
历史预览；声音保留播放/候播及真实收尾回执；身体保留连接和采用状态。
时钟只交一份时间/闹铃，浏览器页面、休息状态和技能读取入口保留；目录总体
散列不再进入模型，文件/证据版本不作统一删除。原始输入、媒体、工具回执、
数据库和面板诊断查询未改写，已有会话历史不重排。第三方插件需通过显式
prepare_request 提供模型材料，仅提供 snapshot 时不再自动注入。

验证：Engine 全量 1080 项离线测试通过，保留第三方 Pydantic ReadOnly 提示；
Ruff、27 个相关文件格式、全项目 strict Pyright 和 diff 检查通过。临时库和
SDK 编译接缝验证保存/实际请求一致、全量未决项与最新回执仍可见；合成屏幕、
音频和 QQ 场景验证诊断保留但不混入自动状态。没有调用真实模型或设备，未
修改运行配置或用户数据。重启 Engine 后观察模型实际判断与交互效果。

2026-10-14 模型材料可读性调整（未提交）：`context/presentation.py` 统一呈现
新输入、注意工作背景、失败经历和动态宿主状态。正文、来源、关系、附件及
材料缺口分节展开，关系对象不再作为 JSON 字符串二次转义；原始平台字段、
关键行动回执、原生媒体及同版本回执去重保留。内部状态和工具协议仍用原结构，
不重写既有历史，不调整固定提示词或判断题。实际渲染材料纳入原容量检查，
完整保留字段意味着本次不保证 token 减少。

验证：新增呈现/媒体边界测试通过，更新旧 JSON 展示断言后监管上下文 13 项
通过。最终全量 1077 项通过，1 项 Windows 全进程句柄相等断言失败（538→527）；
该文件在隔离测试进程中 11 项通过，尚未证明全量环境下的句柄波动原因。
本批相关 Ruff/格式、全项目 strict Pyright 及 diff 检查通过。未调用真实模型，
未改运行配置或数据库；重启 Engine 后新输入及当前状态采用新表示，旧历史
仍保留原格式，实际模型理解与交互体验待观察。

2026-10-14 OpenRouter Jev 接入（未提交）：判断资源改为 `DecisionResource`，
宿主允许显式 `typesafe/` 和 `openrouter/` 前缀，沿用既有 LiteLLM Decisions
编译及实例 HTTP 所有权。OpenRouter 示例为 `openrouter/typesafe/jev-1.13`、
`api_base = "https://openrouter.ai/api"`，SDK 派发到 `/alpha/decisions`。
模型请求、SDK 边界、配置和宿主 126 项离线测试通过，覆盖两条判断路径的
实际 HTTP 模型名/端点/凭据、问题顺序、缺失答案拒答、无隐式重试、取消传播、
关闭及冲突环境隔离；Ruff/格式和 strict Pyright 通过。Windows 沙箱内首次
pytest 未推进并已中止，同组检查在沙箱外使用 HTTP 替身完成。未修改运行
配置、依赖或数据库，未调用真实模型；OpenRouter 在线可用性仍待验证。

2026-10-14 QQ 贴纸显示修复（未提交）：`plugins/qq/plugin.py` 的 send_sticker
图片段字段由错误的 `subtype` 改为 `sub_type: 1`，保留原文件 URI、标签摘要和
回执路径。依据 SnowLuma 官方提交
[`0cc06b6b`](https://github.com/SnowLuma/SnowLuma/commit/0cc06b6b5eb959cada484414dc2817f2f8ebb7fa)
的 OneBot 发送段及私聊/群聊图片表情编码；该上游修复包含于 v1.14.20。
QQ 媒体/插件 32 项离线测试、Ruff/格式及 strict Pyright 通过，覆盖重启后从
贴纸库发到私聊和群聊的实际线协议参数，普通图片仍不带贴纸标记。没有发送
真实 QQ 消息或修改协议端；当前部署版本和客户端实际显示仍未验证。

2026-10-14 摘要容量恢复（未提交）：`subject/history.py` 将历史图片/音频按附件
序号关联到原生媒体块，嵌套工具结果仍保留调用身份及正文。派发前按完整轮次
逐次减半选择可容纳前缀；未选历史原样保留，不拆工具轮次、不重置单请求预算。
预检在 reserve_summary 前完成，容量拒绝不再占用未派发来源的尝试资格。
候选仍须满足原版本、保护边界、原文归档、真实响应和完整请求缩小/可容纳校验。

95 项摘要/会话/模型接缝测试及全量 1061 项离线回归通过；最后将覆盖散列移到
线程后再验证 11 项摘要测试，Ruff/格式和 strict Pyright 通过。

已获单次真实无工具摘要授权并完成当前会话恢复：请求
`8325ce23-748a-4809-95ce-7290bc547142` 只有一次尝试且成功，压缩旧 68 个完整轮次，
前台版本 138→139。摘要输入估算 397916；包含当前失败经历/真实回执及冻结尾部的
完整校验布局从 874444 降至 569866/782035。原检查点与正式摘要归档完全相同，
未选的 278 条消息、最近 2 轮、reminders/continuation 均不变。与操作前一致性
备份逐行核对：656 条 inputs、75 条 actions、85 条 calls、218 条 failed_turns、
655 条 turn_inputs、656 条 input_processing 以及 5 条媒体 BLOB 全部原样保留；
反思会话未改。宿主锁已释放，可正常启动 Engine；没有启动 QQ、语音、设备或
注意模型，也未重投失败输入。451 未处理，不能将单次摘要成功视作全链路体验验收。

本机恢复脚本位于数据目录 `recovery/restore_summary.py`；原库备份为
`recovery/before-summary-20261014-154852-327694.sqlite3`。脚本限定原版本 138，
不可重复运行来重压缩当前会话；它不是产品 CLI 或运行依赖。回退备份须停止宿主，
会丢弃备份之后的新记录；不自动覆盖继续使用后的数据库。

2026-10-14 开放刺激缓冲与轮内接入（未提交）：Engine 基线 `7e63508`、Core 基线
`a691529`；保留已有语音与 WebUI 工作区修改。前台通过 SQLite 事务取尽当前
attention/foreground 新输入，注意单次上限不再限制主脑取批；活动会话暂停普通
注意筛选，旧判断按版本失效。连续观察使用最低唤起，语音沿用原最低唤起入口。
Core `Conversation.ask(input_poll=...)` 在每次模型响应及请求准备前接收完整新材料；
已返回工具继续执行并保持回执配对顺序。追加来源进入 turn_inputs、请求证据和
实际连续历史；失败结案读取权威来源集合，取消等待也不遗留处理中输入。预算
沿用当前轮次，既有独立中断控制继续有效。

验证：Core 全量 733 项、Engine 全量 1058 项通过，Ruff/格式与 strict Pyright
通过。覆盖超过旧批量上限、高优先后继续到达、stop/tool call 两种响应、工具期间
追加输入、迟到注意 CAS、预算耗尽、旧 TurnRecord 取消结案及数据库重开。首次
整体检查发现忙碌等待屏蔽 QQ 独立升级通知，已修正并通过最终整套回归；保留
第三方 Pydantic 的既有 ReadOnly 提示。没有调用真实模型、设备或修改运行数据。
重启 Engine 采用相邻 editable Core 与 Engine 源码后进行实机观察；本批未调整
截图压缩参数或变化检测周期，不能将本批完成视为这两项也已实施。

2026-10-13 WebUI 控制阻塞修复（未提交）：数据目录重建后，浏览器仍持有旧实例的
16 条控制身份，404 被永久视作在途而堵住新控制。仅在实例已变化且原身份查询明确
404 时标记“历史回执不可用”，不认定成功/未执行、不重发，退出等待名额；仍存在的
旧回执继续采用实际结果，网络故障和当前实例缺失仍保留未确认。近期列表优先淘汰
最旧非在途记录，不再只检查第 16 条；真正满载提示与浏览器保存失败分离，取得结果
后自动解除。HTTP 明确拒绝在刷新后保留，缺少时间不再伪称“刚刚”。
32 项前端测试、类型检查、构建和 12 项 Web/Panel 接缝测试通过；运行中服务返回的
panel.js 与新构建摘要一致，刷新页面即可采用，无需为本次面板修复重启宿主。
未改用户数据库、清除浏览器记录或提交真实设备操作；此前语音修复保留未提交。

2026-10-13 麦克风缓存积压修复（未提交）：原始回调队列仅容纳 64 × 10 ms，
请求编译/分词/媒体计量却在同一事件循环同步运行；本机首次计量测得约 3.1 秒。
现将前台、注意、反思和历史维护的本地请求准备移到线程，取消等待仍收回原任务，
不允许迟到准备派发模型或并发修改本轮交付状态。持续人声入库及短噪声收尾不再
阻塞采样，最终材料等待正确起音身份后顺序入账；连续缓存帧主动让识别任务排空。
缓存额度、VAD 门槛、提示词、容量规则及真实缺样时的暂停语义均保持不变。

验证：独立回调线程、生产 SoundDeviceCapture 与真实临时 SQLite 连续会话复现
旧同步准备导致“麦克风缓存已满”；修复后多轮 0.85 秒慢计量期间持续读取，未溢出。
慢入库、短噪声、识别调度和重复取消均有回归。整体 1053 项测试通过，随后短噪声
收尾调整的 17 项相关回归通过；Ruff、格式、strict Pyright 及 diff 检查通过。
未调用真实模型/ASR/TTS、打开物理音频设备或重启现有宿主；实际连续语音体验待重启验收。

2026-10-13 工作区复审：修正前台工具说明中的监管回应值为 accept/partial/reject，
QQ 固定指令改为引用实际装配目录，不再提供不存在的 world.qq 路径。65 项相关测试、
Ruff、格式与 strict Pyright 通过；实际生成的工具说明中的三个值均通过临时 SQLite
监管回应验证。本批同时提交下列收尾、媒体计量、音频、提示词和 WebUI 工作区改动。
复审整体证据：Engine 1049 项 Python 与 28 项前端测试、前端类型/构建及合成快照浏览器
验收通过。未调用真实模型、发送消息或操作现有用户数据；其余验收缺口保持待办。

2026-10-13 语音输出失败续修（本批提交）：本机 FFplay 2026-05-06 已不接受
输入参数 -ac 1，以同一 PCM 命令和 SDL dummy 输出复现退出码 1、Option not found。
播放器改用 -ch_layout mono；真实二进制验收又发现 stats 的 ANSI 清行指令被误判
为错误，现解析前去除显示控制。播放失败保留退出码及有限 stderr，管道先断时等待
诊断读取，避免覆盖为“播放器未确认正常结束”。37 项音频/宿主/解码回归通过，
含真实 FFplay dummy 无声播放、媒体时钟、ANSI 尾帧及两种启动失败顺序。
未访问真实 TTS 或物理音频设备，未重启正在运行的 Engine；新代码需重启采用。

2026-10-13 按用户要求接入 LiteLLM token 计量（本批提交）：新增 Host 内置的
litellm-token-counter-v1，完整请求 JSON（含工具 schema、回执、动态尾部）
使用 LiteLLM 内置 cl100k 分词参考。冻结的 LiteLLM 1.105.0 本地计数器不接收
input_audio，因此显式 gemini-3-inline-v1 使用 Gemini 3 文档的音频 25 token/秒、
默认单帧图片 1120 token；从实际内联媒体验证时长和格式，不分词 base64。
本地结果明确记录 token_estimate，与服务端 usage 分离。未知媒体、远端 URL、
分辨率覆盖或损坏内容不按零量放行。首次 SDK 导入仅使用随包价格表和本地分词资源。
主配置的 Gemini 前台已改用新规则，音频支持保持启用，重启后恢复原声选项；
专用 TypeSafe 判断保持原来的独立输入协议。此前 ASR-only 限制已由此接入取代。

验证：122 项容量、配置、宿主、收音、主体与历史回归通过，涵盖真实 LiteLLM
离线分词、WAV/MP3 时长、图片、完整工具 schema、原件不变、超额拒绝及真实
临时库到替身模型的原声链路。未调用真实模型、ASR/TTS 或设备，未重启在运行的宿主。
Ruff、格式检查及 strict Pyright（212 文件，0 错误）通过。

2026-10-13 收音无回应排障与修复（本批提交）：只读核对当前本机事实，17:34–17:35
的四段 NVIDIA Broadcast 原生音频均已入账，前台随后 CapacityError 失败，未产生
语音播放请求。配置只有 reference-text-only-v1 / unsupported，Host 却只凭
音频布尔声明暴露原声模式。现准入同时要求媒体声明与已注册非文本计量规则；
当时 CLI 使用 ASR；其后已按上面的 LiteLLM 接入恢复原声准入，不隐式转换用户选定模式。

失败经历保留完整正文、来源、真实行动回执及原件引用，明确附件未展开，通过
read_evidence 可请求原件；不自动把同一失败媒体再次塞入所有后续请求，修复原声
失败持续阻断新文本的接续问题。当前输入仍完整交付，原库和音频未删除或改写。
WebUI 补充实际输入设备及最近一句权威处理阶段，区分合成准备与真实播放开始；
固定指令移除不存在的 me.voice.speak 示例，使用装配目录中的实际能力路径。

已通过 74 项宿主/收音/主体/面板/Web 回归、28 项前端测试、类型检查和构建；
新增真实临时 Store 场景证明媒体失败后的文本可正常处理、旧失败阶段及音频保留。
未调用真实 ASR/TTS/生成模型，未重新开启收音、未改系统输出或重启正在运行的宿主。
代码更新需重启 Engine 后采用；此前四段失败原声不会自动重播或转写。

2026-10-13 WebUI 对比度与动效续修（本批提交）：同步更新 Design 的白色画布、中性辅底及
边界 Token，Engine 使用墨色导航轨；首页以完整状态舞台、行动路径、左侧记录/目标及
右侧关注/连接形成固定阅读结构。普通列表连续承载，不给每组内容套卡片。同级页面与
桌面详情直接替换，导航文字和反白背景同帧更新，首次请求不再闪出断线警告；手机 Sheet
在 Design 中统一改为 snap 弹簧。规范、迁移说明及两仓库生成产物同步更新。

验证：Engine strict typecheck、26 项 Vitest、构建、样式 lint、8 项 Web 接缝及
strict Pyright 通过；Design 85 项 React 测试、85 项 Python 测试、typecheck、构建、
Token 一致性、样式/格式检查、Pyright 及画廊截图通过。浏览器新增首次连接等待和普通
动效下 28 帧快速切页检查，30 个页面/宽度组合及原有交互检查通过；使用合成快照，
未调用真实模型或设备。26 项源于移除已取消的滑动方向模型测试，切换行为改由逐帧验收。

2026-10-13 WebUI 视觉完善（本批提交）：参考 Linear 的导航与信息层级、LangGraph
Studio 的记录/详情联动，来源与取舍记录于 frontend/README.md。保持 Cel 为唯一
Token/状态/组件来源，补齐首页双栏、平面列表、时间线数据列与窄屏布局，系统章节
统一使用 Section，修复 Region 引用失效导致的构建错误。新增时间线本地搜索、原生
键盘筛选与清除入口；首页增加设备入口，行动路径优先展示仍在执行的行动，路径
静态标记保证吉祥物是唯一持续运动源。静态产物已重新构建。

验证：前端 strict typecheck、27 项 Vitest、构建、Design 样式 lint、Web 接缝
8 项、Ruff、221 文件格式检查及指定项目解释器的 strict Pyright（0 错误）通过。
新增 scripts/check_web.mjs 使用内存合成宿主和真实产物，六页 × 五种宽度的
30 个组合无水平溢出，搜索/键盘过滤/部分失败分组/手机 Sheet 关闭/断线禁用/
当前行动路径/空状态检查通过，截图位于 .validation/webui。真实模型与设备未调用，
这些验证不替代 P1/P2 或外部验收。保留本轮开始已有后端改动。

2026-10-13 补充修复（本批提交）：Windows Ctrl+C 会同时触发 Patchright SDK 的
默认关闭处理，导致宿主随后 detach/关闭上下文时连接已断。自建浏览器启动时
显式关闭 SDK 的 SIGINT/SIGTERM/SIGHUP 处理，退出仍由宿主负责。CLI 在立即
收尾或超时后收尾失败且仍占用资源时，报告原始异常和资源清单，继续保留二次
Ctrl+C 强退；已全部归还资源的故障仍正常向外传播，不吞错或重做失败清理。
相关 CLI、浏览器、宿主关闭和 Web 测试 51 项通过；本次四文件 Ruff、格式和
strict Pyright 通过。隔离隐藏控制台、临时 Edge 无头空白页和真实 Win32 信号
复现默认 SDK 的 TargetClosedError；修复后的完整 CLI（含 Web）一次 Ctrl+C
退出 0，模拟关闭失败且非 daemon 线程仍存活时第二次 Ctrl+C 退出 130。
没有调用真实模型、QQ 或使用现有浏览器配置。保留本轮开始已有前端改动；
这些证据不替代其余服务、媒体与设备验收，也不关闭此前活动核对/终态回收竞态。

Engine 分支为 `codex/engine-refactor`。已提交批次为 `33803f1`（配置/单实例/存储）、
`8014ffb`（显式模型资源/限定依赖补丁）、`fdc6508`（目录/Worker/真实回执）、
`8c7b04a`（连续主体/反思/历史/业务数据）、`a69681e`（媒体/本机面板/有界诊断）
、`293affd`（原文交付/未决保护/真实释放/独立安装验收）和
`fdb41f9`（VTS有限恢复/首帧确认/主体关闭竞态）。
开始空基线为 `84f6ae5`；原设计文档按保护约定仍未暂存。

本轮开始提交为 `fdb41f9`，源码及测试原先干净，仅原有 docs 未跟踪。复审修复
包括：Host在任何退出等待前同步撤下一调用资格；QQ从权威首次原生帧恢复接管；
活动核对回执完整交给后续模型请求后才允许新行动决定；直接@保留最低WAKE并
绑定旧生成作一次升级判断；反思修订原子登记未决关系，召回与展开保留对方确切
版本及有效性。旧QQ信封缓存与独立冲突写旁路已删除。两个P1有失效实现的红证据；
退出52项、QQ78项、直接@新13及相关54项、活动49项、认识69项已通过。
格式12→13只增一表一索引，完整启动恢复共事务；相关160项及迁移18项通过，
旧JSON/BLOB/FTS影子表/关系逐行保留。模型／调用／活动后置损坏也回退此前恢复
写入和DDL，版本保持12。整体最终1020项通过（142.28秒），新增81项；Ruff、格式
214文件、strict Pyright0、compileall和diff通过。新version字段的前台bool转int
问题由实际Core红证据确认，已改严格正整数，受影响47项及最终整体通过。
独立审查未发现剩余可靠实现问题。没有运行真实服务或处理用户数据库。

| 里程碑 | 当前结果 | 证据及剩余边界 |
| --- | --- | --- |
| M0 | 批准与基线核对完成 | 本实施 chat 明确批准整体 contract 和 M1–M7；原仓库不作为改写目标。 |
| M1 | 配置、单实例宿主、SQLite/BLOB及模型资源已接实际 CLI/Host | 输入原文/媒体/待处理同事务，关闭保留真实借用；TypeSafe 补丁与 SDK传输边界验证通过。 |
| M2 | 冻结能力、Worker及真实回执完成，退出复审修复通过 | closing同步撤新派发，三个实际Worker竞态有红／绿证据；已开始调用仍确认，相关52项通过。其余Job/迟到Provider/容量与来源轮转基线保留；无宿主exec。 |
| M3 | 注意、连续前台与当前尾部完成，直接@升级复审通过 | 最低WAKE保留；唯一注意工作器按旧代次／输入版本有限升级，新13及相关54项通过，不无条件紧急、不重复入队。原批量/defer/VAD及请求预算基线保留。 |
| M4 | 反思、认识、摘要与活动完成，两个入口缺口已贯通 | 核对后须完整成功模型请求呈现再由新行动决定，活动49项通过；认识关系同修订事务，召回／展开保留确切对方版本，69项通过，旧写旁路删除。原文已读、finish、CAS与摘要投影基线保留。 |
| M5 | 保留能力与VTS恢复接实际入口，QQ重放复审通过 | 首次原生事实由同库查回复用，真冲突拒当前帧并继续接收，78项通过；旧信封缓存删除。其余Notebook/时钟/电脑/浏览器/声音/身体/收音/看图离线基线保留；真实平台、音频、设备与视觉未验。 |
| M6 | 本机面板、调音室与诊断保留实现完成 | Host/Origin/令牌、同库控制去重、真实状态/断开行为、包内面板及调音室各4份资源；TS strict/14项/构建/Design样式通过。桌面/390窄屏面板、Studio桌面及预设保存/载入通过有限浏览器验证；上传/波形/A-B/导入浏览器验收未完成，ASGI/解码/预设27项通过不能替代体验。 |
| M7 | 复审修复的离线与安装验收通过，外部待条件 | 最终1020项通过；静态检查、构建与双非editable64/90包一致，实际Worker/BLOB/重开/8资源通过，模型调用0。17项CLI属于未改变入口的fdb基线，本轮不重复。媒体计量及真实服务／设备／体验门槛未完成。 |
| M8 | WebUI 产品化：决策已确认（contract §7“WebUI 产品化扩展”），P0 已完成，P1/P2 未开始 | P0 纯前端已完成：hash 路由与六页信息架构、首页舞台状态推导、待处理模型、用户文案、Inspector 技术细节折叠、组件拆分；验证：前端 typecheck、vitest 27 项、构建、Design lint_styles、`tests/test_web.py` 8 项通过，替身宿主下 1440/390 宽截图核对。P1 只读端口：在途调用/输入来源、宿主控制历史、系统健康、SSE。P2：只读对话时间线、结构化配置编辑（tomlkit）、调音室外壳统一、Design 深色 Token。 |

已通过验证均为临时库、真实本机Worker/回环服务、SDK编译与MockTransport或合成设备；
没有调用付费模型、ASR/TTS、真实QQ/SnowLuma，也没有打开真实收音、声音或VTS。
仅已知Pydantic ReadOnly警告。既有SDK新环境两项因可选OpenTelemetry缺失未通过，
两个真实模型检查未运行；没有扩大依赖或删除约束。早期整进程句柄绝对相等断言失败
已按实际资源证据修正为本次句柄失效和总量不增长，后续Windows及整体检查通过。

上一稳定批次已中文提交 `293affd`：默认 `me.view_image` 与Pillow基础依赖、完整下一请求的
工具交付Gate、音频/身体释放明证、监管/承诺当前投影及保护、安装验证/使用说明。
图像68项、工具交付/未决状态最终117项（含新交付12/状态13）、音频/身体56项及
Host释放明证故障注入通过。预览新增样例的Core构造/输出额度与字段断言错误已按
实际契约修正，没有放宽容量门槛；初次整体879通过/1新样例失败，随后整体880通过。
真实关闭只凭实际明证继续清理Store/锁，未知连接池继续持有，原错误仍报告。

独立分发在 ignored `build/verified-distribution`，Core/LiteSDK的54/13包文件逐字节
匹配当前源且无缺失/额外/差异/pyc；Engine149包文件逐字节匹配，包括8份静态资源。
Core wheel SHA256为 `e7ac6d5b4b4947e77a7ef67cb032a0dcefe18a2744b49de19ee9da9cb67f7a21`，
LiteSDK为 `c026aef71da5656abe4da81e38f465c73580b8576933e6b34f9f5fe1d12b56b6`，
本轮Engine为 `5797601c2c37c756735002fd0666ed46eac1998cc40916b447ebb792ac23c849`。
`manifest.json`/`sdk-source-manifest.json`记录源、Git状态、锁、构建工具及产物摘要；
本轮manifest SHA256为 `87b8ad1ab300fd7656350151af89766b7ef91e8bc626c85ecbaac84d7e0df262`，
SDK清单为 `441b1f73d7db343d448a1aa2dbd8a0e61e47eec82d651256998a0bd697eeeb1a`。
源码aggregate算法有明确framing，不与第一轮不同算法的digest直接比较。
SDK与Engine均从site-packages加载且无editable，minimal/all的真实Worker控制台替身
确认、完整图片BLOB/关系重开与目录锁收回通过，模型调用为0。最终17项console/module
CLI证据在 `build/install-entry-audit/vts-results.json`，旧 `final-results.json` 保留。
最终局部关闭修正未改变CLI入口；最终wheel已重新安装并通过两环境实际路径验证，
不重复未改变的17项CLI。缺web extra清晰退出2。

导入冻结LiteLLM时，现有SOCKS环境因socksio缺失会影响在线价格表元数据初始化；
安装检查显式使用官方 `LITELLM_LOCAL_MODEL_COST_MAP=True` 本地价格表模式，
只作用于验收子进程，不改变Engine凭据来源或依赖。固定Silero v5.1.2公开ONNX实际
下载2327524字节，SHA256 `2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f`
与源码一致，两生产会话合成零PCM形状/有限概率/状态隔离通过；不是人声准确率验收。
直连DNS11004失败，仅验证Client显式使用现有HTTP代理成功，生产setup仍直连，说明
已记录。CUA文件选择器挂起路径已中止，两个临时面板/Studio服务实际退出，没有残留
已拥有的浏览器页；不把未触发的scroll-lock CSP或上传试听路径记为通过。

原有工作保护：Runtime仍为 `4a29b7d4c6107ac3fde205659256f986e69ea5d3`，未修改；
Core初始src/pyproject/lock共56文件aggregate SHA256为
`4864a0b0fe23f3cc4926cfd4dd48423098910e0851a9f698757f9310b8364a87`。
LiteLLM初始15文件aggregate为 `65c7db81b683e3cea04334ac8791f7b4e843502e3cd5abf4a8a2ebce38a8bc21`，
初始相关Git二进制diff为 `28ca390d505e1a0e88055cdddf1318abbe70237bfeb698648f8daa0efa597d25`。
SDK仅本次参数证据修补提交 `7dcb82e`、`5e375fd`，原有修改/未跟踪内容未提交；
版本号不能代表这些可引用源码状态。

冻结组合为Python3.12.11/Core0.6.0/PurrceptLiteLLM0.2.0/OpenAI2.49.0/
LiteLLM1.105.0+purrcept.1；uv.lock固定开发与可选依赖，pytest9.1.1/Ruff0.16.0/
Pyright1.1.411/build1.6.0/Hatchling1.32.4。官方1.104.2和1.105.0均已证实TypeSafe
ConnectError隐式重发，依批准后备方案仅补请求级客户端与禁连接重发，保留转换和归一化。
官方wheel SHA256为 `6ff237f1fe64a086d91b1453116347e0c1b2aed0fe8c8b9014d5a019c6a6b5d6`，
补丁为 `b4005bd94df18869a0b78ddffd855fd44c65a05f5b2ee882561ff8d78a1777fe`，
输出为 `d17e3afc8122a91d13172c20113320d836024e46b292def5422371c8542dee0f`。
没有修改兄弟仓库的安装环境。Windows受限沙箱的asyncio/socket与console launcher限制，
经既有审批机制改用同一纯本地提升检查，不把它当业务回归。

上一基线批次：按批准已接通 `plugins/live2d/runtime.py` 与 `vts.py` 的唯一有限恢复
所有者。初连及已成功连接后的独立传输失联各为一轮，最多5次；轮内等待
0.5/1/2/4秒、建连3秒、首次原生授权120秒，不套用QQ稳定期。认证拒绝、原生授权
超时/失联、协议错、模型不符和令牌文件错停止本轮，耗尽不自行重开。插件start登记
所有者后归还Host启动，stop/close立即撤恢复资格并等资源实际归还；恢复清旧姿态、
平滑通道和PCM，保留旧已发生采用事实。原身体/音频/实际Host和Worker50项通过，
最终身体/VTS74项（含有限恢复20项、协议/IO31项）通过；包含音频的上一范围105项
通过。第二connect观察者也等待同一首帧，旧资源清理后重查停止资格。
整体检查曾930通过/1主体关闭竞态；真实Store事件屏障在原实现四个恢复位置稳定
复现，修正为同步撤工作器并保留清理前原始故障，新增7项及相关53项通过。
另有SDK首次导入挤入反思测试3秒等待：隔离通过，完整收集诊断测得导入2.59秒；
完全串行仍超时，不能仅归因并发。最终仅在反思测试fixture、原HTTP拦截之后准备
纯编译器，三秒等待、实际编译与全部行为断言保持；反思35项和最终整体939项通过。
最终双独立wheel安装及实际Worker/BLOB/资源通过，最新中文提交为 `fdb41f9`；
临时诊断脚本已删除。当前产物清单已更新为 `30a995d` 及149份包文件／8份资源，
双安装环境均重新装入新wheel并通过实际路径冒烟；冻结SDK产物未变。uv包检查
初次被受限沙箱缓存权限阻止，同一纯本地提升检查通过，不是依赖或业务回归。

网关原生媒体的窗口/计量依据仍缺，需实际文档或规则后实现并对完整请求核验；
文字参考规则不能用于图片/音频。本次答复明确暂无依据并保留门槛，不猜测计量。
实际网关/TypeSafe、SnowLuma协议、真实声音、设备、视觉及人工体验另需明确授权；
调音室上传/波形/A-B/导入仍缺浏览器路径验收。当前工具仅有IAB/MCP Apps，
没有新的上传通路或活跃验收进程；同一外部条件已连续三轮恢复核对，保持阻塞。
下一条可执行步骤：取得实际媒体计量依据后实现／核验完整请求，或在明确授权及
服务／设备条件下执行真实代表场景；补调音室上传／波形／A-B／导入体验。
上一轮“没有剩余独立编码任务”的判断已被本次复审纠正，五项缺口现均有实际
完成证据；本轮没有剩余已知独立编码任务，不据此宣称总体目标已完成。
旧数据不迁入Engine；恢复须停止两
实例后返回原Runtime/原目录。Engine格式13不能随代码回退自动降级，排错保留故障
目录副本，不新建空库冒充恢复。源码与测试已提交，工作区仅原始docs未跟踪，
contract未改、plan是当前状态单一来源；其他仓库未修改，未推送或发布。直接@
持久判断的格式12→13在隔离副本验证，使用及实际恢复步骤见README。

## 调查基线

仓库位置均在 `C:/Projects/python/Purrcept/` 下。以下是初勘时的工作区快照，不是已锁定的发行组合。

| 仓库 | HEAD | 已有工作区状态 |
| --- | --- | --- |
| purrcept_runtime | `4a29b7d4c6107ac3fde205659256f986e69ea5d3` | `git status --short` 为空。 |
| purrcept_core | `5697dd209b6c3bf4f7a32ad05c7a9ad74bf6bd98` | 有修改和未跟踪文件，详见下表。 |
| purrcept_litellm | `7739d13bcea82590b5c86da0a60bd3eb86c01860` | 有修改和未跟踪文件，详见下表。 |
| purrcept_design | 无首个提交 | 已初始化 Git，列出的文件均未跟踪。仅查看状态、维护约定和 README，未审查 UI。 |
| purrcept_engine | 无 Git 仓库 | 初勘时目录为空；本轮仅新建本计划和契约，不初始化 Git。 |

已有未提交内容不得作为本轮变更提交、清理或覆盖：

| 仓库 | 状态 | 路径 |
| --- | --- | --- |
| Core | 修改 | CHANGELOG.md、README.md、README.zh-CN.md、RELEASING.md、docs/audio-input.md、docs/models.md、pyproject.toml、src/purrcept_core/__init__.py、src/purrcept_core/models/__init__.py、src/purrcept_core/models/content.py、tests/models/test_content.py、tests/test_public_api.py、uv.lock |
| Core | 未跟踪 | adr/0007-decision-models.md、docs/decisions.md、src/purrcept_core/models/decisions/、tests/models/test_decisions.py |
| LiteLLM | 修改 | CHANGELOG.md、README.md、RELEASING.md、pyproject.toml、src/purrcept_litellm/__init__.py、src/purrcept_litellm/backend.py、src/purrcept_litellm/config.py、src/purrcept_litellm/observability.py、tests/test_audio_input.py、tests/test_json_repair.py、tests/test_observability.py、tests/test_public_api.py、tests/test_real_integration.py、uv.lock |
| LiteLLM | 未跟踪 | docs/decisions.md、examples/03_decisions.py、examples/04_image_decision.py、src/purrcept_litellm/_callbacks.py、src/purrcept_litellm/_config.py、src/purrcept_litellm/_decision.py、src/purrcept_litellm/decision.py、tests/test_decision_sdk.py、tests/test_decisions.py |
| Design | 未跟踪 | .gitattributes、.gitignore、AGENTS.md、CHANGELOG.md、DESIGN.md、README.md、dist/、pyproject.toml、react/、scripts/、tests/、uv.lock |

已读 Runtime、Core、LiteLLM 的 AGENTS.md，未发现父目录或 Engine 自有 AGENTS.md；初勘时 Design 仅作边界定位，随后补查规范与组件入口，范围见覆盖表。未实现 UI 或进行视觉验收。

依赖与工具事实：Runtime 声明 Python >=3.11、Core >=0.5.1,<0.6.0、LiteLLM >=0.1.3,<0.2.0，uv.sources 指向相邻可编辑仓库。相邻工作区版本已经是 Core 0.6.0、LiteLLM 0.2.0。Runtime 的现有 .venv 为 Python 3.12.11，dist-info 仍标记 Core 0.5.1/LiteLLM 0.1.3，direct_url 指向相邻可编辑仓库；安装元数据不能证明当前源码满足声明的发行组合。

Runtime 配有 pytest/pytest-asyncio、Ruff、strict Pyright 和 Hatchling/build；本轮 git ls-files 未发现受版本控制的 .github 工作流或 pre-commit 配置。没有运行测试、构建、依赖同步、应用或外部服务，也未读取运行配置中的秘密和现有用户数据库。

近期变更线索：HEAD 大幅替换认知内核并修改配置、日志和多项认知测试；前几次提交分别移除单 LLM 控制器、旧 WebUI、内核对内置插件的隐式依赖，以及调整群聊参与/取消和上下文预算。这些是优先检查调用链的线索，不作为缺陷证明。

## 子系统覆盖与证据

状态表示实际阅读范围，不代表逐文件审查或测试通过。“方向已定”表示已讨论并决定处理方向，“待补查”表示源码/契约仍有缺口，“待验证”表示尚无执行证据。Runtime 路径以下均相对 src/purrcept_runtime；SDK 路径按所属仓库解释。决定只写在 contract.md，本表不另设批准来源。

| 子系统 | 已沿调用链检查的主要证据 | 本次处理方向与尚缺内容 |
| --- | --- | --- |
| 启动/关闭 | app/application.py:start/_start/_close_resources；runtime.py:start/close；CLI/console 中断入口；kernel/plugins.py 挂载回滚/逆序释放；scheduling/scheduler.py 注销 | 装配、常驻/EOF、关闭期限与 CLI 强退方向已定；具体设备失败分支仍需按插件验证 |
| 输入/来源/积压 | QQ inbound/transport、app/bundled/dialogue/service.py:route、host.admit/ingest、ApplicationGate 的 pending/drain、voice_input 的 listen/recognize/deliver、computer 候选/_submit | 身份关联子系统删除，保留平台来源/路由及麦克风配置身份名；过载语义与积压/并发初值已定。QQ 原生事件标识及拒收链仍需补齐 |
| 注意/连续会话 | host._gate/_attention_scene/_run_foreground、app/perception.py、VAD、Core Conversation/run_model_turn 及最后轮拒绝工具测试 | 保留并重整；整轮预算已定。WAKE 下限与紧急升级、最终语音不二次抢占、预览与来源完整性需具体接口验证 |
| 上下文/历史 | projection.py、assembly.py、maintenance.py、tokens.py、session.py、checkpoints.py；host 的执行/计划/监管投影与 resolve_plan；Core RequestTokenBudget/编译器 | 保护数量、摘要阈值及窗口/估算方向已定，具体 Provider 计量待补；局部重写引用/提交边界。现有 cl100k_base 与每二进制块 8192 不能视为精确 Provider 用量；Core 预算超限会删旧轮，不直接启用 |
| 认识/反思 | ledger.revise/revoke、retrieval、ReflectionWork.complete 与 Outcome 类型、host revise/supervise/respond/reflect 与 trigger/wait 链、ApplicationReflectionPolicy | 当前有效性/替代/冲突、触发节奏和完成工具方向已定，每项提交保留。integrations 分开提交需合并；同批格式纠错不得重置整批预算 |
| 活动 | activities.py command/feedback/expire/close、ActivityControlsPlugin、旧 Spire control/recover/_unknown | 两阶段确认/恢复核对/累计预算、初始额度及显式追加已定；当前预算仅内存 sleep；旧尖塔不实现 |
| World/目录 | models/perception/store；Phone/QQ/Browser 挂载；host._catalog/play_action、ContextProjection.foreground/normalize；Core SystemInstruction 与 LiteLLM 缓存编译 | 保留轻量对象目录，拆业务状态；父链与 namespace 独立。通用关系/转让不迁入；完整目录固定 system 前缀与模型路径/参数名保留范围已定 |
| 行动/Worker | ActionEngine 两入口、Gateway、FunctionProvider、validation 全文、WorkerClient、Executor、Windows resource 跳过 | 局部重写有界 IPC 和平台限制；尚无完整逃逸审计或真实 Windows 限制试验，不作恶意程序强隔离保证 |
| 权威存储/媒体 | MemoryStore、checkpoints/media/session 编码、CognitionResources.open、clock/bed World 组件 | SQLite/BLOB、单宿主及故障规则已定。专属数据库线程、业务事务和版本提交为委托设计，尚未实现 |
| 配置 | config 类型/解析、ConfigurationService、Loader、VoiceInputSettings/VoiceProfile、computer 偏好 | Pydantic/默认值/路径整理；TOML 直接凭据。SDK 省略凭据时的环境读取逐端点核实 |
| 事件/诊断 | Publisher/Bus、Gateway/ActionEngine 通知失败、QQ/控制台入账前发布、AlarmClock._fire；Observer/TraceStore/日志 | 必需调用、可靠交接、展示失败拆分；修关联和资源所有权。现默认投影未注入 subject，旧认知事件接线有残留 |
| QQ/QZone | send/read、QZone 九项调用形状/通用响应解释与列表投影，传输/路由/入站过滤及代表测试；其他扩展仍有仅入口核对项 | 保留并适配；请求值不能冒充实际值，短卡片关联原文覆盖。SnowLuma 验收范围与拒绝 feed 深翻页已确认；没有全部端点/版本审计，真实取消赞等未验 |
| 笔记/时间 | NotebookStore 元数据/read/write/move/delete/_parse_file、插件各 Provider 与 _refresh_catalog、代表测试；AlarmClock hydrate/fire/commit、bed | 技能自动发现补齐；实际模型请求缺接线。直接 write_bytes 无版本条件，插件目标串行不覆盖外部编辑；刷新与冲突方向已定。闹铃/休息必须在真实重开库场景验证 |
| 浏览器/电脑 | Browser mount/session.read 与 extract 全文、capabilities.read_page、WindowsDesktop、computer session/observe/_submit/voice_frame | 读取分段、宿主观察确认与删除 react 已确认；媒体与真实驱动/站点失败仍需验证 |
| 语音/身体 | SpeechSessions 全文、speak/streaming、voice_input、Live2D service/inbox/intent/simulator/worker 关键链 | 音频与身体分离、主脑语义表达；当前主脑 correlation_id 未接旧语音来源分支。真实音频时钟/VTS 映射未验 |
| 调音室/WebUI | studio server 的上传/试听/原声/预设/静态路由及 security 全文；device_ports、console、设备控制/快照；Design 状态/按钮/反馈相关段、React 导出/画廊/构建 | 调音室独立保留；复用 DSP 与预设规则，主面板新建。未全面审查浏览器脚本与视觉，不把 Design 画廊当已连接产品 |
| Core/LiteLLM | Core 会话/工具/Decisions；LiteLLM request/response/stream/decision/errors；SDK 部分重试线索 | 复用现有 SDK，不复制循环。SDK 归一化、原始参数记录、实际重试和版本组合仍需具体集成验证 |
| 交付/工具 | pyproject/extras、Loader、stdio CLI、Design 库构建、Ruff/Pyright/pytest/build 配置 | 一包交付、可选依赖；需要 wheel 安装验证，不仅源码 editable。未执行工具链或真实服务 |

### 关键维护代价的依据

- 输入原文已存在但 request 未写入时，重复 ingest 可提前返回：需要同事务接管，不能以“有去重”宣称接收完整。
- 会话失败后输入出队、Core 临时轮次不提交：单纯数据库保留原文不保证下一轮自然看见失败经历。
- call Provider 已完成后通知失败，被翻译为行动失败但 effect_committed 保留：应分离业务结果与交付结果。
- 活动 control 等待期间 feedback 可写终态，之后旧 row 覆盖：需要当前版本与运行代次检查。
- 旧 media_assets 虽去重，检查点和请求仍 base64 重复：须在所有权威编码路径用不可变引用。
- World/技能组件存在，不等于主脑上下文使用；当前目录测试手动格式化，不能证明模型已看见技能。
- 普通流式调用、单个结构化能力、长期设备循环目前有不同运行责任：统一回执/生命周期规则不意味着强塞同一个执行框架。

## 委托设计的落实清单

本节追踪内部设计成果，不构成第二阶段授权。

| 项目 | 本轮收敛内容 | 待验证/限制 |
| --- | --- | --- |
| 模块依赖 | app 装配，subject/context/execution 不反向导入设备；业务状态归所属组件 | 以实际修改场景和导入依赖图检查，避免纯转发层 |
| 提交与连接 | 一个存储线程/连接，纯数据业务提交，数据库事务不等待网络；取消等待不撤销提交 | 队列有界、收尾容量、调用开始前再次核对状态，BLOB 延迟尚无实测 |
| 记录身份 | 输入/模型尝试/行动调用/控制命令/播放身份分开，保留因果关联 | 无原生事件 ID 的通道不能承诺跨重连去重；不凭文本或时间相似合并 |
| 会话/摘要 | Core 历史归属不变；归档、摘要和候选检查点同事务，成功后替换空闲会话内存 | 版本过期、持久化失败和容量不足不破坏旧历史 |
| 活动/认识 | 活动单一状态提交者；认识修订包含有效性/来源/替代/索引请求与整合回执 | 迟到确认、跨 await 冲突、过期来源/旧向量不复活 |
| 日志/UI | 日志入口拥有 handlers；读取权威投影；UI 命令带身份/版本，状态确认与受理分开 | 详细模型证据和秘密边界；断线快照明确过期 |
| Worker | 有界 JSON 消息；Windows Job Object 首选 ctypes 小范围封装，放行代码前确认限制 | 原生接口官方资料已查，未实现或验收；不等同于文件/网络强隔离 |
| 配置分组 | 命名模型配置与用途引用、职责所属预算、显式插件入口；统一校验顺序见契约第 8 节 | 不创建示例配置或运行初值；无凭据端点的环境隔离仍待逐路径落实 |
| 权威记录 | 原文/媒体、输入交接、模型会话、行动控制、活动、认识反思与时间休息分属明确记录组；补 finish_reflection 原子提交 | 不按记录组机械造服务层；SQL 字段/索引在第二阶段落实，诊断和查询投影不替代权威事实 |
| 宿主与插件 | 宿主提交/等待/查询/控制/关闭分工；插件纯声明、装配、启动接入、停止新输入及资源回收契约见第 3 节 | 关闭 QQ 业务输入不能提前关闭在途发送回包；等待取消不撤销已接管工作；部分失败清理有明确所有者 |
| 输入与迟到反馈 | input_id/call_id/command_id 与来源代次分别关联，真实历史回执与当前状态采用分开 | 电脑按真实处理状态解除在途等待、删除 react 及独立解释字段已确认；不把处理完成宣称为理解或对外发言 |

Windows 原生设计依据已在契约引用 Microsoft Learn 的 Job Objects、基本/扩展限制。Job 限制的提交内存不等于 RSS，CPU 时间不等于墙钟；嵌套 Job、失败清理和句柄继承需要实际验证，不能用“设置过字段”替代证明。未引入依赖或调用这些接口。

## 代表性行为基线与现有测试证据

所有现有测试仅阅读，未运行。下表既说明已有证据，也说明第二阶段要补的真实回归保护；不按私有布局或源码文本写测试。

| 场景 | 已读测试/代码定位 | 新系统验证目标 |
| --- | --- | --- |
| 连续主体与显式输出 | test_cognition_v4: foreground_continuity_dedup_and_internal_final_output、foreground_tool_executes_once_and_persists_real_receipt | 输入身份、请求顺序、输出调用和回执，不逐字比较自然语言 |
| 取消/迟到结果 | test_cognition_v4: cancelled_provider_cannot_advance_conversation_late | 旧生成不能推进新会话或启动未开始行动；已开始行动保留结果 |
| 部分效果 | test_action_runtime: action_reports_partial_commit_when_later_code_fails | 后续失败不抹去已发送事实，不自动重放 |
| 通知失败 | test_review_regressions: notification_failure_retains_committed_receipt；test_events 的因果/发布顺序 | 业务成功与通知失败分开，独立消费者继续；明确保留必要顺序 |
| 持久化/恢复 | test_cognitive_runtime: recovery_marks_inflight_unknown_and_preserves_both_mailboxes、state_and_outbox_rollback_atomically | 真实临时库重开；提交崩溃点；原测试主动抛异常不等于磁盘故障注入 |
| 认识/来源 | test_cognition_v4 的版本/原文及索引迟到；test_cognition_integrity 的旧向量/撤销摘要；test_reflection_contract 的未读来源拒绝 | 当前/历史查询、替代、未决冲突、逐项提交后批次失败 |
| 历史压缩 | test_context_projection 的目录去重；test_context_maintenance 的超限保留近期与无效摘要 | 实际请求材料和引用可还原；超大必要材料明确阻塞；模型摘要质量另验 |
| 活动 | test_cognition_v4 模拟活动完成/重复命令；activities 与旧 Spire 暂停/反馈链 | 两阶段确认、迟到确认不覆盖终态、实际运行计时、重启核对。旧游戏不是验收依赖 |
| 插件/关闭 | test_plugins_scheduler 部分挂载失败重试；test_review_regressions 线程结束后清理、取消 close 仍等租约 | 核心失败与外部离线分开；有限收尾、在用资源不提前关闭 |
| 冻结绑定 | test_capability_invocation 的 frozen_binding_survives_world_change_and_plugin_retirement、参数篡改/重复调用 | 目录/目标一致，Worker 不能改绑定；无需恢复产品热装卸 |
| 注意/积压 | test_perception_gate 的合批/最低唤起/伪造正文/urgent/defer；QQ 注册前缓冲和过载断线测试 | 明确 @ 不强制回复、文字停止按上下文、VAD/最终语音不重复抢占、过载缺口可见 |
| 实时语音 | test_voice_input 的 final_once、queued_voice_reply、short_input_keeps_playback、heartbeat_expires | 区分当前主脑接线与旧服务测试；页面关闭不停止、显式停止及部分播放回执 |
| 身体 | test_live2d 的拒旧播放和无生成自然链；test_live2d_motion 的预缓冲/欠载无动作 | 音频实际进度、去重/期限、身体离线不拖累音频、恢复不补演 |
| 笔记/闹铃 | test_notebook_plugin 的手动目录呈现；test_hydrate_daily_alarm_from_world_component | 实际 ModelRequest 中的技能目录；文件正文追溯；真实持久库闹铃跨重启 |
| 模型/JSON | LiteLLM test_streaming 的取消/关闭；test_json_repair 全文 | 半成品不执行、尝试不拼接、原始与修复参数关联、内部异常不伪装输入错误 |
| 专用判断 | LiteLLM test_decisions 的映射并发；test_decision_sdk 的 JEV 补零/拒答与类型错配 | 显式输入/候选/拒答、SDK 可见边界、取消与重试；HTTP 替身不证明线上效果 |
| 媒体/诊断 | checkpoints/media 编码；test_image_read_keeps_owned_pixels_after_plugin_unmount；诊断 reopen 与模型 usage 入账测试 | BLOB 引用还原，缺失显式，诊断淘汰不删原文；原测试不证明全局磁盘容量正确 |

### 真实效果与工具链

- 第二阶段先运行与改动相符的局部测试、类型检查和格式/lint，再在跨层闭环形成后跑代表性集成；不对每个小改动重复全链路。
- 真实模型基线记录确切配置、输入、目录、工具 schema、请求顺序及实际效果；体验由代表场景确认，不用随机自然语言逐字相等。
- QQ/QZone、TTS/ASR、电脑、浏览器和 VTS 的真实测试须明确授权及可用条件；缺少条件列未验项，不造替代平台宣称等价。
- 不安装/同步旧 Runtime 的不匹配依赖以制造绿色基线。Core/LiteLLM 的现有工作区必须先确定可引用版本，未经授权不提交或修改它们。
- 第一阶段至今没有执行应用、构建、测试、设备采集、消息发送、付费模型或现有数据库操作。

## 依赖与 SDK 待落实项

| 项目 | 已知接缝 | 收敛条件 |
| --- | --- | --- |
| 版本组合 | 当前 Core 0.6.0/Purrcept LiteLLM 0.2.0 工作区，适配包自身锁定 LiteLLM 1.104.2/openai 2.49.0，虚拟环境 Python 3.12.11；Runtime 为另一套旧安装 | 第二阶段候选起点见契约第 8 节；记录可引用源码状态和依赖锁，不自动提交既有改动或把版本号视为完整源码证明 |
| 原始参数修复证据 | Core ToolCallDelta/provider_metadata 可复用；真实流片段先于修复，非流 synthetic_stream_events 来自已转换对象，转换失败没有成功响应 | 契约第 8 节限定 SDK 改动已纳入第二阶段；观察入口需覆盖转换前原参数和失败，Engine 持久化。无需 Core 新公共字段；当前不实施 |
| 模型重试 | Runtime create_model 传 timeout，ModelConfig 默认 None；AgentSession.run 用 active_seconds 包整轮，旧默认 120 秒；SDK 错误分类已有，未实现 Engine 统一重试 | 固定实际 Provider 路径，验证每层尝试数、取消与总时间；本地 LiteLLM OpenAI 源码有 max_retries，部分分支用 or 默认值，不能仅凭传零断言关闭成功。其他路径未全面审查 |
| 判断适配 | Core Decisions 与 LiteLLM adecisions 已有实现；旧 typesafe 原生接口更严格检查部分原始字段 | 对实际注意问题比较输入表示、候选顺序、拒答、量化概率及用量；不伪造原始完整性 |
| Windows 限制 | 原生 Job Object 可用作设计基础，初始资源数值见契约第 6 节 | 限制建立失败明确拒绝，实际消耗/父退出/嵌套 Job/句柄回收测试；数值已确认但尚未经实机验证 |
| 凭据与端点来源 | Runtime ModelConfig/create_model 将 None 传入适配层；_request._base_options 与 _decision.compile_decision 对 None 不传字段。LiteLLMDecisionConfig 明确允许上游环境默认 | 新配置不能只改注释就宣称禁用环境。需凭据路径先校验显式值；无凭据端点按固定传输路径验证。禁止修改全局环境造成嵌入/并发串扰；必要 SDK 接口变化另行提出 |

## 实施里程碑草案

以下顺序已获本次实施批准；完成条件是可见结果和关键不变量，不是文件数量。内置插件按实际依赖可局部交错，不能在基础契约未成立时批量迁移。当前状态与证据统一见开头。

| 阶段 | 改动目标 | 验证与完成条件 |
| --- | --- | --- |
| M0 设计锁定 | 设计材料与逐项决定已形成，完成整体方案审阅；不重复询问已确认的局部决定 | 两份规划材料可独立交接；整体批准与第二阶段明确授权；不以第一阶段未运行实机检查阻止形成设计 |
| M1 最小宿主、存储与 SDK 接入验证 | 配置/路径、实例锁、SQLite/BLOB、提交/日志/关闭；确定可引用 SDK 源码状态及候选依赖，验证两条模型路径的材料、重试和凭据契约 | 无设备可启动；重复实例拒绝；临时库原子提交/重开；可观察传输替身检验实际尝试和参数；若需超出已确认范围的 SDK 改动，先提出具体方案，不在此基础未成立时批量迁移 |
| M2 对象目录与执行 | 插件声明/生命周期、能力目录、Windows Worker、数据协议、逐次回执 | 本地替身能力通过真实 Worker；部分效果、超时/限制、并发与关闭均可观察 |
| M3 主体闭环 | Core 会话、请求证据/重试、输入去重/积压、注意与失败经历接续 | 控制台与可重放输入形成闭环，取消不重发；模型请求可重建 |
| M4 认识、容量与活动 | 有效性/替代/冲突、反思逐项提交、摘要安装、活动两阶段控制/预算/恢复 | 版本/来源竞争、压缩失败、暂停完成竞争及跨重启场景成立 |
| M5 内置能力适配 | QQ/QZone、时钟休息、Notebook/技能、浏览器电脑、语音与身体、独立调音室 | 每项按清单验证输入/输出/回执和资源释放；旧尖塔排除；无真实条件的不冒充通过 |
| M6 WebUI 与交付 | Design 面板、控制/状态接口、静态资源打包、独立工具命令、可选依赖边界 | UI 只消费宿主事实，多页面/刷新不重开设备；安装产物能运行且不需开发服务器 |
| M7 行为验收与切换 | 代表场景实机验收、文档与示例、版本冻结及切换说明 | 必需场景通过或明确阻塞；旧 Runtime 数据不迁移、不删除；真实账号切换避免双运行重复处理 |

M0 核对的是设计完整性与剩余限制的明确归属，不能声称已逐文件审查或实机验证。首批路径的实际容量规则、SDK 尝试数和设备效果分别在 M1/M3/M5/M7 建立证据；需要真实服务的部分另需明确授权。实现前补读具体待改模块，不把未审区域机械复制为“保留旧实现”。

## 调查交接时的验证清单

逐项决定统一见契约；本计划不重复维护批准清单。M0 已完成整体批准与第二阶段授权。内部队列按所有者与已有名额控制，见契约第 6 节；实现时不得另建无限任务/缓冲绕过这些边界。

| 未验证项 | 最早处理位置 | 不满足时的处理 |
| --- | --- | --- |
| SDK 可引用源码、实际尝试/取消、凭据来源和请求证据 | M1 | 保留原工作区，明确接缝；需要额外 SDK 变化时先讨论，不批量迁移 |
| 本机网关的文本/图片/音频计量依据与真实能力 | M1 建立配置/计量契约，M3/M7 验证实际请求与效果 | 不借别名猜公式，不用固定余量掩盖未知，不静默改模型/媒体 |
| Windows Job Object、IPC 与持续资源收尾 | M2 | 不能降级成无限额执行；未解决的进程/资源行为阻塞相应验收 |
| SQLite/BLOB、来源覆盖、摘要/反思/活动提交与重开 | M1/M4 | 故障注入使用临时数据；权威写失败不切内存或新建空库 |
| QQ/QZone、ASR/TTS、电脑/浏览器、Live2D | M5/M7，真实验证另需授权 | 缺条件列未验；不由替身通过宣称真实效果等价，不删保留能力绕过 |
| WebUI、独立调音室、安装产物 | M6/M7 | 控制回执与页面状态必须来自宿主；静态包、可选依赖和非 editable 安装需实际验收 |

工作区基线与未提交路径清单见调查基线。本段描述第一阶段调查时点：当时没有修改源码工作区，也没有运行应用、构建、测试或真实服务。当前实施结果只维护在开头；状态检查不证明其他进程从未改过文件内容。

## 关键机制的证据与回归检查

以下保留第一阶段按机制归并的调查证据，不重复契约中的批准内容。路径沿覆盖表的仓库约定；调查时只阅读这些旧测试。列出的检查目标由当前实施状态中的证据覆盖，替身测试不能证明真实模型或设备等价。

| 机制 | 已检查的代码事实与现有证据 | 第二阶段重点 |
| --- | --- | --- |
| 公开 Python 边界 | 顶层导出、公开 API 测试及 Application 示例；搜索相邻 Core/LiteLLM src 和 Design React src/gallery 未发现运行时导入，不覆盖仓库外脚本 | 新嵌入示例、安装产物和调用者切换；不把此结论扩展到其他 SDK 公共 API |
| 启动装配 | kernel/plugins.py:Plugin/PluginContext/_mount_unlocked/_unmount_unlocked；QqPlugin.mount 在主体创建前启动 client，InboundRouter._queue/_flush 依赖注册事件，队列超限 popleft | 完整声明/主体入口就绪后开接入；部分失败仍持有清理所有者；test_plugins_scheduler 的依赖顺序/循环/失败重试不证明新启动协议 |
| 常驻与关闭 | cli._build_parser/_dispatch、run_console 的 ask/EOF 随 Application 退出；Runtime.close 用 shield/drain_owned_task，Application._close_resources 最后清空引用 | EOF 不关宿主；单一关闭期限、关闭未完成仍可查询/等待、在用资源不早释放、二次信号只退出自己。test_thread_finishes_before_plugin_resources_are_closed、test_unmount_resumes_after_cancelled_lease_wait、test_runtime_close_waits_for_leases_despite_caller_cancellation 是已有顺序证据 |
| 输入有界性 | host.ingest 每输入建 _gate task；ApplicationGate._pending 是列表；max_pending 只限制一次取批，不能证明总积压有界；QQ 接收泵独立分派 echo，业务队列满会断线 | 原子预留数量/媒体字节；原文与待处理同提交；普通满载仍可收回包/存回执；公平性、超时不迟启动、未接管缺口 |
| 来源与过滤 | DialogueService.route 将 QQ 账号映射为 person；语音用配置 person，控制台用 local。QQ _filter_reason 在解析媒体/remember/Stimulus 前执行，@ 不绕过；owner_id 是主体实体而非人类主人 | 去除关联层仍保留来源/路由/去重；麦克风身份名实际进入请求；过滤不变成发送许可，不递归删主动历史/引用。test_blacklist_drops_group_without_waking_actor 已读；未发现专门覆盖旧账号关联的测试源 |
| 注意材料与调度 | ApplicationGate._state 截 text[:1000]、仅媒体数量；host._attention_scene 的输入/承诺/活动背景有截限。test_judgment_batches_share_bounded_scene_and_keep_source_ids 允许截断后 observe；_drain 与 _run_foreground 各等待 coalesce；_due 将 defer 原身份直接交前台 | 完整当前材料与背景覆盖分开；容量拆批/不适配只交前台一次；固定窗口不重置、不双等；到期提交重开不漏投/重复。test_deferred_input_delivers_original_evidence_once 只覆盖正常路径 |
| 请求布局与缓存 | host._run_foreground 把 context JSON/媒体作为本轮 user 消息；ContextProjection.foreground/normalize 按目录内容散列去重。Core ReminderSource/loop/PromptCompiler 每逻辑请求编译，动态提醒不入历史；LiteLLM _apply_prompt_cache 给稳定 system/工具/历史边界加提示 | 输入/工具实际先后不改写，动态尾部刷新，重试冻结材料；目录/执行一致、固定前缀不混状态。test_request_mapping 中 stable/volatile/reminder/AUTO/PREFER 已读；未测线上命中、节费或当前目录 token 量 |
| 历史维护 | prepare_compression 保留完整轮次；ContextAssembler.install 对候选目标有硬检查。tokens.py 用本地参考分词及统一二进制预留；Core RequestTokenBudget 超窗会 _drop_oldest_complete_turn | 关键未知事实跨近期窗口保留；摘要不能冒充新真实轮次；未达软目标但足够安全可安装；无新增可压缩来源不重复摘要；归档/候选检查点原子安装。test_overfull_reflection_maintenance_keeps_recent_turns_and_archives、test_invalid_maintenance_leaves_history_untouched 已读 |
| 长文/原文覆盖 | BrowserSession.read/extract.py 截前段加省略号；QQ read_chat/_fetch_history 的缓存有 source 标记但缺失败/覆盖。host.read_evidence(evidence) 将整项 ID 记作已读；Retriever 保留层次 | 续读同快照不取网，首段不算整份、Worker 内读取不算模型已读，局部证据可引用；缓存与平台失败区分。test_html_to_markdown_clips、test_outcome_rejects_unread_sources_and_fabricated_memory_receipt 不证明新片段覆盖 |
| Notebook/技能 | NotebookStore.write 直接 write_bytes，无 expected_version；_refresh_catalog 在挂载/笔记调用后执行；_parse_file(missing_ok=True) 对不可读/超限/非法 UTF-8 返回 None | 外部编辑下次请求可见、坏条目有路径/原因；版本冲突/新建碰撞不覆盖；写成功后目录刷新失败不抹结果。test_write_read_tree_and_catalog_hides_ordinary_notes、test_size_cap_rejects_huge_write、test_mkdir_move_delete 只覆盖已有接口，不证明外部编辑器竞争完全消失 |
| 反思完成 | ReflectionWork.complete 校验范围、来源、实际修订与 findings；host._reflect_batch 依最终 JSON 并可额外纠错。complete_review 不是仅限监管事项；review_context/propose_compression 与维护重叠 | finish_reflection 保留全部校验；成功提交后收尾失败不重投，未调用则散文不确认，逐项修订/意见仍保留。test_reflection_contract 的 outcome_for、普通散文与伪造修订回执拒绝已读 |
| 反思触发 | ApplicationReflectionPolicy.immediate 看 correction/纠正或更正前缀/status；host urgent/pressure 也立即触发，维护在反思前。test_prose_does_not_acknowledge_evidence_and_success_resolves_failure 覆盖失败不确认 | 普通输入固定批次触发，VAD/文字前缀不额外反思，无新证据不空转，摘要独立；同批纠错共享预算，暂缓不自循环 |
| 活动 | ActivityManager.command/_expire/feedback：caller 提供 command_id/activity_id；等待控制后旧状态可覆盖反馈；计时只在内存 sleep，resume 可重新取得完整额度，暂停不取消计时 | 宿主身份与重复控制、两阶段确认、迟到不复活终态；暂停/离线不扣、追加不自动恢复；计时检查点与结算竞争、崩溃只采用已保存累计并标记缺段。test_simulated_spire_activity_reports_completion_without_exposing_private_actions 仅证明旧显式命令去重 |
| 模型预算/重试 | AgentSession.run/_RecordedBackend.generate、LiteLLM errors.py/test_errors 的暂时故障与 Retry-After 分类；Core run_model_turn 最后允许响应含工具时先抛 ToolLoopLimitError | 实际尝试数/总等待/取消、无片段拼接/工具重做；最后轮不执行工具；同因果接续不刷新预算。test_round_limit_fails_before_unusable_tool_side_effects 已读，不代表新时间预算已通过 |
| 原始工具参数 | LiteLLM _response.convert_response/StreamAccumulator/_arguments_object/synthetic_stream_events 与 backend 非流分支；真实流先报告片段，非流事件来自修复后的对象，失败无成功 ModelResponse | 非流修复前材料和最终对象配对、失败仍留证、多工具/尝试不串、取消无假 Completed、解析/执行语义不变；test_json_repair 双路径与 length 拒绝测试已读 |
| Worker 与 IPC | ActionLimits、worker._apply_resource_limits、Executor.execute/_receive：Windows 缺 resource 会跳限额；connection.recv 解码后才校验字典，每 call 建 handler；max_result_bytes 只限制最终结果 | Windows 实际 CPU/提交内存/父退出/嵌套 Job/句柄回收；解析前帧限制、调用预算、巨大参数拒绝前无副作用、回传过大保留效果、目录分帧完整性；行动相关测试未全部阅读，禁止宣称逃逸审计完成 |
| QQ 连接 | WebSocketOneBotClient.start/_start_direct 一次连接，断开后 _started 阻止普通 start 重建；reverse 持续监听；call 丢弃 idempotency_key，echo 只匹配回包 | 有限恢复与稳定期重置、终止错误、关闭不续连、过载先等容量、旧代次回包不串、新连接不重发旧请求。loopback/过载测试不证明恢复或线上恰好一次 |
| QZone | 九项 Provider 调用及 _call_qzone/_call_data、unwrap_onebot_data、qzone 投影；缺失状态可通过，set_right 可回填请求值，缺 tid/comment_id 得空串；短卡片裁文并跳无法投影项 | 平台成功声明与实际字段完整性分开；合法空 data 不误判、矛盾信封显式、未知效果不重发；卡片原文/未解析覆盖。test_all_nine_qzone_action_payloads 仅内存动作参数，busy/permission 测试仅失败隔离；实机取消赞未验 |
| VAD/音频/身体 | voice_input interrupt 分支只发 speech_started，ApplicationGate urgent、host 仅撤销生成；microphone.speak 的 TARGET_SERIAL/合成播放、SpeechSessions consumed/source、FFplay 锁及 BodyIntent 已读 | VAD 真实事件直达停音和抢占；合成/播放/候播结果独立，迟到合成不播、后续新请求可播；音频独立于身体、姿态按实际进度采用。test_queued_voice_reply_rechecks_source_and_interrupts_all_speech 手写 voice_input 来源，主脑实际为 execution ID，不是端到端证明 |
| 电脑观察 | ComputerService._submit 等 computer:<connection>:<revision>:<uuid>；react 传入主脑 execution correlation_id，静态路径不匹配。超时暂停；snapshot.result 由 reacted 更新 | 保持安静也能完成本帧；暂缓/失败不假确认，旧代次不更新新预览，真实迟到效果归原记录。test_voice_frame_refresh_pause_and_stale_reaction 直接设 inflight/调 service，不证明主脑确认；未实机复现 |
| WebUI/调音室 | device_ports、设备 snapshot/start/invoke、ActivityControlsPlugin；Design StatusMark/Timeline/Inspector 等规范。Studio 的全部服务路由与 LocalControlMiddleware 已读，audio_files 有上传/时长限制 | 页面刷新不重开设备，控制受理/完成分开，本机来源与过期身份；调音室样本内存归属、串行试听、旧 clip 拒绝、预设版本仍成立。DSP/浏览器脚本未全审；区分外部无效音频与内部错误，不用广泛捕获掩盖后者 |

### 当前模型与依赖的静态证据

只读解析 Runtime purrcept.toml，仅输出白名单模型名、能力/窗口声明、端点 hostname 和凭据是否存在；未输出密钥、URL 查询串或完整配置。生成为 openai/gemini-3.8-flash-high，本机网关，配置声明上下文 1000000 且支持音频；注意为 typesafe/jev-1.13.0、api.typesafe.ai，配置声明输入上限 12000，两者均填写凭据。这些是配置事实，不证明网关真实模型、容量或在线状态。解析使用标准库和 -B，未导入应用/SDK。

Runtime .venv 的 LiteLLM 元数据为 1.100.0；Purrcept LiteLLM 自身环境与锁文件为 1.104.2，openai 为 2.49.0，Python 为 3.12.11。相邻 Core/Purrcept LiteLLM 为可编辑工作区，正式验证需保存准确源码状态，不能用发行版本号忽略已有修改。

| 接缝 | 在 LiteLLM 1.104.2 已读位置与结论 | 尚未证明的部分 |
| --- | --- | --- |
| 容量计量 | utils.token_counter；litellm_core_utils/token_counter 的 get_image_dimensions、openai_tokenizer_encoding_name、_count_content_list：URL 可取网、未知模型回退分词、内容失败可用默认值；已读内容分支无 input_audio 支持 | 首批网关的实际文本/图片/音频规则；不能将参考估算或固定余量当精确保证 |
| 凭据来源 | main._resolve_openai_api_base/_complete_custom_openai；decisions._resolve_api_key/_prepare_request 和 TypeSafe endpoint：显式 key/base 覆盖对应环境回退；Purrcept _request/_decision 对 None 省略字段 | 组织标识、其他全局项、无凭据端点与所有 Provider 的完整隔离；未读取实际环境变量值 |
| 尝试控制 | utils._get_wrapper_num_retries 区分 None/0；OpenAIChatCompletion.completion → async_streaming → _get_openai_client 传 max_retries；可用既有 num_retries/max_retries 选项作为接入设置 | 实际网络次数仍须替身检验；drop_params 的全局/请求开关可能导致删参数重发，非流与其他 Provider 未全审 |
| 判断与媒体 | Purrcept _decision.compile_decision、test_image_mapping_preserves_order_and_bytes；Core Decisions 与 LiteLLM adecisions 输入/结果契约 | 真实端点媒体支持、概率归一化/拒答及上游信息完整性；不能因图像映射测试就声称原生音频也支持 |

第二阶段验证尝试数、无隐式改参重发、取消、显式凭据来源及媒体编码。不得修改全进程环境或 SDK 全局变量绕过问题；超出已确认范围的 SDK 接口调整需提出具体最小方案。

外部资料只读取公开文档：Windows Job Object 官方依据、SnowLuma 应答/好友动态/取消赞链接见契约。SnowLuma 修改可见性动作页本次抓取失败，未声称已审其完整 schema；公开文档不证明部署实例版本或真实效果。没有连接 QQ/模型/设备、试调接口或操作现有数据库。

## 第一阶段交接状态与实施门槛

- 已确认的决定以 contract.md 为唯一来源；内部队列责任已补齐，不将局部批准写成整体或实施批准。
- 主要子系统均有保留、整理、局部重写或删除方向；覆盖表明确仅入口调查、未全审与未运行范围。源码细节在改动前继续阅读，不机械照搬旧实现。
- 进入第二阶段后，先确定可引用的 SDK 源码/依赖组合并建立首批模型传输与容量证据；若接入契约不成立，先解决该接缝，不能继续批量迁移掩盖问题。
- 完整工具绑定、最终提示文本与配置示例在实现时逐项对照契约；新增模型可见语义、重要依赖或范围变化需另行讨论。
- 第一阶段调查时，真实网关计量与体验、SnowLuma、音频/VTS、Windows 限制和安装产物均未验收。第二阶段 Windows 限制与独立安装已取得开头列出的实际证据；真实服务验证仍需明确授权，未验项不能降低“保留能力”的完成标准。
- 第一阶段调查仅修改本计划与契约；其后整体设计及 M1–M7 已在本实施 chat 明确批准。当前实施、验证与下一步以开头记录为准，不沿用交接时的未授权状态。


## 2026-10-20 本机直播舞台插件联动

仅按本轮用户要求增加根目录 `plugins/live_stage` 与显式 `local/name:factory` 加载，
保留既有 PluginFactory/PluginPorts/PluginRuntime 所有权；新增只读播放字幕端口，
音频所有者在真正开始播放时投影 speak 台词。未从完整模型响应提取字幕或泄露内部思考。
外置插件包含匿名B站普通弹幕输入和127.0.0.1:18744只读展示；舞台18743同源投影，
四个固定页面chat/game/away/starting各自锁定布局，保留用户当前主题/字体/桌面/倒计时。
无关原有未提交修改未覆盖、未提交或重置；舞台资产及保存状态17项哈希保持不变。

本轮证据：影响范围57测试通过（真实临时SQLite/Windows Worker/音频播放边界/
loopback WebSocket重连）；Ruff、314文件格式、全项目和外部插件strict Pyright、
compileall均通过。wheel构建及临时目录非editable离线配置/声明冒烟通过。
浏览器隔离验收82/82，正式四页面×双层只读验收42/42，游戏内矩形全透明。
详细本轮报告在舞台 `QA.md`；测试截图中的fixture不代表真实平台或TTS。

经用户明确许可重启且配置房间1850493825；最终Engine PID28104运行，权威存储可用，
QQ online、microphone/browser ready、Live2D connected；原关闭的电脑观察和语音输入未打开。
原PID3484正常信号关闭监听后滞留，最终仅终止旧主进程。第一次新实例继承工具SOCKS代理，
QQ缺socksio失败；正常关闭后仅移除子进程代理环境重新启动恢复，未修改系统环境或依赖。
B站匿名握手与舞台连接成功；验收时消息数0，真实弹幕端到端及自然真实发声字幕仍待现场验收。
未向B站/QQ发送测试消息、未开播、未操作直播软件、未登录或保存新凭据。

代码备份 `.validation/live-stage-before-20261020T165137Z.zip`；
配置备份 `.validation/purrcept-before-live-stage-20261020T170447Z.toml`（仅本机保存）。
舞台备份 `_history/before-engine-link-20261020T170851Z.zip`。


## 2026-10-20 真实弹幕精确核对与心跳修复

用户测试文本为“123测试”。配置房间、平台canonical room、getDanmuInfo请求id及实际
鉴权包roomid均为1850493825；请求protover=2，处理普通DANMU_MSG及其冒号后缀、zlib嵌套帧。
核对时平台可用最近历史为空，展示缓存为空；SQLite以mode=ro检查最近2000条输入，
覆盖2026-10-18至2026-10-20 17:05 UTC，未见该文本或live_stage来源。
这些记录不能证明平台当时接受或拒绝了发送，也不能把空历史当完整历史。

确认并最小修复了一项接收可靠性缺陷：websockets默认RFC Ping在43.1秒触发
1011/keepalive ping timeout，尽管B站op3心跳正常。关闭库自动ping、继续使用已有
op2/op3心跳和75秒无回包超时；HTTP和WebSocket握手使用一致User-Agent。
同房间被动对照：原设置43.1秒断线；修正设置65秒收到3次op3且未断线。
该缺陷造成接收空窗，但未取得证据证明“123测试”因此丢失。

源码备份 `.validation/live-stage-before-heartbeat-20261020T175648Z.zip`。
插件19项回归、Ruff、格式、strict Pyright通过。按原授权正常重启同一Engine加载修复，
舞台服务未重启。正式桥连续70秒同一session、15次采样均connected，revision始终2，
跨越原43秒故障窗口未重连；Engine running、权威存储可用、QQ online。
正式四页面再次只读验收正常。未发送弹幕/消息、未登录、未改凭据或开播。

当前submit_to_engine=true：每条有效弹幕同时进入展示队列和正式InputSubmission，
由Engine正常注意/唤醒流程决定是否回应；没有强制唤醒或保证每条都调用speak。
“123测试”仍未获得接收/渲染证据。未触发speak不能认定字幕故障；需要实际现场发声
后观察字幕出现及清除。下一步仅需用户在同一房间再次发“123测试”并确认平台页面可见，
随后同步核对接收，不重放历史或使用合成数据替代。


## 2026-10-21 官方直播来源准备（未启用）

用户已提供杰瑞喵项目ID1783128932975，并授权自己本机输入/保存官方凭据。
新增外部插件OpenLiveSource，通过官方HMAC start/heartbeat/end与双心跳接收普通DM；
同一场次WS重连、固定房间1850493825核验、原生消息ID去重、open_id命名空间，
沿既有展示/InputSubmission链路处理，不新增内核事件总线或自动切换身份。
认证错误停止，未知start结果不自动重试，end失败保留非敏感状态。

新增Windows用户凭据管理器精确项目条目与本机隐藏输入窗口，保存需用户点击，
不读取浏览器Cookie、不创建明文秘密文件、不自动连平台。提供20秒独立验证入口，
须用户保存后另行调用；本轮没有读取真实条目、真实认证、改purrcept.toml或重启。
现有匿名来源继续运行，用户舞台文件没有修改。来源默认anonymous，官方模式必须显式配置。

本轮38项合并回归通过，含19项既有舞台/真实临时库/Windows Worker/音频边界检查，
及官方签名固定跨语言向量、HTTP替身、真实loopback WS重连/拒绝/取消、场次清理、
去重接管、假凭据保险库读写/清零、本机隐藏窗口（假存储）验收。
Ruff/格式、strict Pyright和compileall验收按最终代码执行；正式凭据保存和官方权限仍待用户现场操作。
初次源文件备份.validation/open-live-before-20261021T030701Z.zip；后续增量均带哈希保护和独立备份。
当前Engine PID42208健康，QQ online、microphone ready、Live2D connected；当前Windows用户与Engine相同。

已有用户历史响应与普通/无痕对照确认会话影响历史可见性；不能将其等同WS权限证明。
项目v0.0.0截图不代表直播API已获准。下一步用户仅需填写本机入口，报告“已保存”，
再做脱敏真实官方握手/房间一致性验证，确认后方可切换生产来源；不再盲发弹幕。


## 2026-10-21 官方来源真实启用

用户确认本机已保存凭据，并授权限定官方验证及成功后的来源切换/必要重启。
03:54:37–03:55:00 UTC有界验证成功：项目1783128932975、房间1850493825匹配，
WS connected、事件0；临时场次清理未报end失败，进程exit 0后才切换正式配置。
只读取指定Windows凭据条目，未输出秘密、签名或完整响应。

修改前再次检查AGENTS、git状态及10个安装文件哈希，没有覆盖并发修改。
purrcept.toml只在既有live_stage.config内添加source=open_live和open_live_app_id；
严格解析和完整结构对照确认其他配置未变，来源不自动回退、不与匿名源并行。
非敏感插件设置备份：.validation/live-stage-settings-before-official-20261021T035701Z.json。
旧Engine PID42208正常Ctrl+C退出，按原uv启动方式重启为PID55796，监听8766/18744。
舞台PID20108的18743服务保持原进程（启动于01:57 UTC，早于本轮重启）。
正式Engine running、权威存储可用、QQ online、microphone ready、Live2D connected，
原关闭的电脑观察与语音输入未启用。官方桥connected、failure=null。

04:01 UTC四个固定捕获页面真实浏览器只读验收：chat/game/away/starting各保持自身场景，
均引擎已连接、消息0、字幕隐藏。没有注入合成消息或触发角色外发。
既有38项回归及Ruff/格式/strict Pyright/compileall通过；本轮仅配置与文档更新，
没有再次修改运行源码或用户舞台。真实弹幕接收→正式输入→DOM渲染及自然播放字幕
出现/清除尚待一次协调现场验收，静默观察不算E2E通过。


## 2026-10-21 官方弹幕真实端到端核对

用户确认发送唯一文本“官方联动测试0421”。04:05:00 UTC只读核对：
官方桥及舞台投影各有1条，sender=拾风_ya，平台时间戳1792555423；
原生ID=open_live:1897492a-e8da-4a3d-92d2-72e14f7b4829。
正式SQLite输入input_id=7b09856a21bb478ca0d31110654e1948，
adapter=live_stage、conversation=1850493825、event=danmaku，
received_at=2026-10-21T04:03:44.252343+00:00；最近2000条中精确匹配1条。
04:05:25–04:05:30 UTC独立浏览器核对四页面DOM各出现1条对应真实消息，
chat/game/starting可见；away既有布局隐藏弹幕区，DOM更新但不可见，未改用户布局。
缓存/输入/页面没有重复条目；原始WS包与去重丢弃次数未暴露，不能据此虚构该计数。

本轮只读查询未发送消息、注入数据、读取凭据或重启服务。字幕当前隐藏。
最近100条音频事实的最新时间早于本次官方启动，从启动以来无新播放事实；
未取得自然speak及字幕显示/清除证据，不判断字幕故障，也不把字幕标为已验收。
本地证据：task/engine-link/official-test-audit.json 与 official-test-browser.json。


## 2026-10-21 礼物、SC与撤回端到端补齐

用户明确要求补齐官方礼物/SC，沿用当前项目/房间授权，不增加账号访问范围。
新增events.py明确Gift/SuperChat/SuperChatDelete；官方WS复用原场次、签名及心跳，
仅新增解析与类型分发。普通弹幕/字幕原路径保留。礼物用原生msg_id、SC用稳定message_id，
重复或重连重放不再次提交；字段/金额/房间不合法明确停接。
礼物区分paid，price按1000单位=1元显示单件标价，免费礼物不折算人民币；
r_price保留平台原值，不擅自推断订单实际支付总额；SC rmb直接为元。

正式InputSubmission类型gift/super_chat/super_chat_delete，原文属于外部用户数据，
没有最低唤起或系统权限。已接管SC撤回只提交不含原文的状态更正；到期不创建输入，
已过期到达的SC忽略。先到撤回阻止后到SC。新增receipts.py原子持久化已删除数字ID，
按实例/项目/房间隔离，防重启复活，不保存正文/昵称/凭据。20k容量保护不淘汰旧撤回身份。
已发生的角色思考/声音不能倒回，无法保证模型对每条互动回应。

舞台只扩展白名单投影、现有右栏内SC区及礼物样式，全部textContent安全渲染；
SC双端到期检查，断线清除、重连读取当前状态。闲聊/游戏/待机保持固定场景，
暂离继续隐藏互动区。主题、字体、桌面、倒计时、资产、.stage-state.json均未改。

56项合并pytest通过（含原38项）：金额/边界、重复、删前到达、过期、未来有效期、
容量、官方真实loopback重连重放/清理、真实临时库去重、撤回记录跨重启、既有音频与Worker。
修正原心跳失败测试的20ms竞争：握手就绪后才注入心跳失败，没有改变生产故障语义。
Ruff、格式、strict Pyright、compileall通过。隔离18745/18746浏览器36/36通过，
覆盖四页布局、XSS纯文本、SC撤回/重放/到期及断线删除重连。首次隔离页冷启动7秒超时，
加浏览器异常采集后重跑通过；最终独占监听版本再次36/36通过，没有向正式端口注入事件。

部署前哈希保护与逐次备份。初次Engine备份.validation/before-gift-sc-20261021T043649Z.zip；
舞台备份_history/before-gift-sc-20261021T044734Z.zip。purrcept.toml哈希仍与官方切换后相同。
更新时发现同一舞台目录两个遗留进程20108/52424复用18743，均通过路径/控制台核验后
正常Ctrl+C关闭；新增StageServer在Windows独占监听，隔离端口验证重复bind被拒绝。
新唯一舞台PID44820。旧Engine55796正常Ctrl+C退出，确认8766/18744释放后才启动新实例；
原清理路径负责end，未独立持久化平台end回执。新EnginePID40984运行，官方桥connected，
QQ online、microphone ready、Live2D connected，原关闭的电脑观察/语音输入保持原状态。

04:49 UTC正式四固定页真实DOM验收均引擎已连接、新SC容器存在、消息0/SC0/字幕隐藏。
真实礼物/SC/删除尚无现场事件，不能称平台真实礼物验收已通过，不要求用户花钱测试。
已有浏览器源需用户手动刷新一次加载新JS/CSS；不代操作直播姬、不代开播或赠礼。
本地证据task/engine-link/gift-browser-report.json、gift-production-browser.json、
gift-production-verification.json；测试截图task/engine-browser-evidence/gift-chat.png与gift-game.png为隔离合成数据。
