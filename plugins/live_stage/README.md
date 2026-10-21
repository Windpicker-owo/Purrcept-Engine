# 直播舞台外部插件

本插件使用公开 `PluginFactory / PluginPorts / PluginRuntime` 契约，
通过 `local/live_stage:factory` 从主配置目录的 `plugins/live_stage` 显式加载。
不会扫描或自动运行其他目录里的插件。纯配置检查不联网。

将下列段落加入 **Engine 根目录的 purrcept.toml**，在确认可中断当前会话后重启 Engine：

```toml
[[plugins]]
instance_id = "live_stage"
entry = "local/live_stage:factory"
enabled = true
[plugins.config]
port = 18744
# room_id = 你的数字直播间号
submit_to_engine = true
max_messages = 50
```

不填写 room_id 时只提供 Engine 状态和真实播放字幕，弹幕显示“未设置房间”。
`submit_to_engine = true` 将真实普通弹幕经正式输入端口交给 Engine 的原注意流程；
不强制唤醒、不自动发送B站消息。设为 false 可仅显示弹幕。
关闭、重连都不会把已有展示缓存重新提交给模型。
需要现有 `httpx` 和 `websockets`；不会自动安装或更改其他依赖。

本机只读接口：`http://127.0.0.1:18744/snapshot`。
舞台服务仅将其中的消息、播放字幕与连接状态投影至同源 `/api/live`。
接口没有发送消息、播放、执行模型或操作直播软件的权限。
不把模型完整面板、内部思考、工具结果、密钥或账号信息发送到前端。

字幕来源是声音插件**播放器已经开始**的 `speak` 台词；排队、合成、record 文件导出
和主脑最终文字均不作为字幕。完成、中断、撤销或失联后清除。
仅整句台词，尚无逐字时间戳；显示仍受用户现有字幕框尺寸约束。
舞台预览里的示例是独立开关，不会进入捕获页。

显式选择 `source = "anonymous"` 时，弹幕采用B站网页协议的匿名接入，不读取现有浏览器Cookie、不创建持久凭据、不登录。
平台若要求登录、鉴权或限流，会明确显示 `authorization_required` 并停止该接入，
不会自动改用账号、绕过验证或伪报已连接。房间号并不保证平台允许匿名连接；
须在实际房间验证后才能声称线上可用。

断网采用1/2/4/8/16/30秒退避，关闭取消重连及连接资源。
只保存有界内存展示缓存。只有平台明确提供原生消息ID时去重；
没有ID不以文本或昵称猜测同一发言，因此不能承诺跨连接“恰好一次”。
输入接管失败明确停止消费，保留失败状态，不盲目重投不确定输入。

协议参考：
- [blivedm 作者的网页接入实现](https://github.com/xfgryujk/blivedm/blob/master/blivedm/clients/web.py)
- [blivedm 的 WebSocket 实现](https://github.com/xfgryujk/blivedm/blob/master/blivedm/clients/ws_base.py)

这些是协议实现依据；替身测试不代表当前B站账号/直播间或真实声音已验收。


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
后观察字幕出现及清除。后续用户已提供服务端历史响应与普通/无痕窗口对照：
历史消息确实存在，会话影响历史可见性；这不能证明当时WS广播或匿名WS权限。
不再要求盲目重复发送，不重放历史或使用合成数据替代。

## 官方项目接入（2026-10-21 已通过真实弹幕上屏验收）

项目“杰瑞喵”ID `1783128932975`，固定房间 `1850493825`。
默认 `source = "anonymous"`；官方来源须显式选择，不自动升级身份或同时启用两路。

用户在本机双击 `plugins/live_stage/configure-open-live.cmd`，亲自填写
AccessKey ID、AccessKey Secret、主播身份码。三项均隐藏显示；保存后清空输入框。
窗口只调用Windows凭据管理器，不联网、不读取已有条目、不改配置或重启。
保存位置是当前Windows用户的通用凭据条目
`Purrcept/live_stage/bilibili-open/1783128932975`；会替换此精确项目条目。
请勿把字段发到聊天、提交Git或放入截图。

独立验证只能在正式官方来源已停止、且已授权真实连接的前提下，从Engine根目录运行；
当前实例已启用官方来源，请勿与它同时启动此验证：

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP = 'True'
.venv/Scripts/python.exe -m plugins.live_stage.verify_open_live
```

该命令读取指定条目并创建一个最多20秒观察窗口的官方场次；关闭另有有限的网络清理时间。
只输出UTC时间、项目/房间、状态与消息数，不输出秘密或弹幕正文，不接管Engine输入。
握手成功不等于收到真实弹幕。`room_mismatch`、`api_rejected`与数字码用于非敏感诊断；
`start_outcome_unknown`不自动重试，`session_end_uncertain`表示end未获确认。
凭据缺失/拒绝不回退匿名。先结束此独立验证，再启用Engine来源，避免双场次。

确认官方项目权限与房间一致性后，只在现有live_stage配置内替换/增加，再单独验收真实接收：

```toml
source = "open_live"
open_live_app_id = 1783128932975
room_id = 1850493825
```

这些字段不含秘密；不要复制第二个同名插件或替换完整purrcept.toml。
当前实例已按用户授权切换并重启；将来的修改及重启仍须遵循当次运行实例的授权。
官方来源采用签名HTTP start/heartbeat/end、同场次WS重连及原生msg_id去重。
收到的 `open_id` 保留命名空间，不能冒充B站数字UID。处理普通文字弹幕、礼物、SC及SC撤回；
其他事件及表情不作为角色输入，不强制唤醒、不发送B站消息、不操作推流开播。
目前解码官方protover 0/1/2（含zlib）；如平台返回其他版本则明确停止，不能假称连接正常。

需用户在 [项目页](https://open-live.bilibili.com/miniapp/overview/1783128932975)
核对项目是否可调用直播数据API、是否有待审核/调试范围限制；在
[管理中心](https://open-live.bilibili.com/open-manage)核对开发者状态及密钥是否可用。
仅需告知状态文字或接口错误码，不能发送密钥截图。项目截图的v0.0.0本身不证明API权限。
主播身份码按 [官方FAQ](https://open-live.bilibili.com/document/5dffc297-6fd2-41ff-bd45-6e8b89e2a68e)
从互动玩法入口获取；它也是授权令牌，不是公开房间号。

密钥原文只在本机参与签名；签名、项目ID及主播身份码发往官方`live-open.biliapi.com`，
临时auth_body仅用于校验后的B站WSS。不提供跨域重定向、第三方代理或Cookie读取。
Windows用户作用域保护不能防御同用户恶意进程、管理员或运行时内存读取。
退出/停用结束本场次；用户可在Windows凭据管理器删除上述条目。
本地删除和官方end不等于永久撤销平台权限；平台侧密钥/身份码需用户另行撤销或重置。

协议依据：[官方API](https://open-live.bilibili.com/document/eba8e2e1-847d-e908-2e5c-7a1ec7d9266f)、
[签名说明](https://open-live.bilibili.com/document/74eec767-e594-7ddd-6aba-257e8317c05d)、
[官方事件](https://open-live.bilibili.com/document/f9ce25be-312e-1f4a-85fd-fef21f1637f8)、
[上游官方来源实现](https://github.com/xfgryujk/blivedm/blob/master/blivedm/clients/open_live.py)。


2026-10-21 03:54–03:55 UTC：指定Windows凭据条目的有界真实验证成功，
官方start返回房间匹配1850493825，项目1783128932975的WS认证成功。
临时场次退出清理未报告end失败，验证进程exit 0；退出后才启动正式官方来源。
本次没有读取浏览器Cookie、开播或代发弹幕。正式连接状态与消息端到端须分开判断。

本机固定捕获入口（正式舞台服务18743）：
- 闲聊：http://127.0.0.1:18743/chat.html?layer=overlay
- 游戏：http://127.0.0.1:18743/game.html?layer=overlay
- 暂离：http://127.0.0.1:18743/away.html?layer=overlay
- 待机/开场：http://127.0.0.1:18743/starting.html?layer=overlay

四页面真实浏览器只读检查均显示各自固定场景与“引擎已连接”；没有示例消息残留。
用户已发送唯一文本“官方联动测试0421”，04:03:44 UTC正式输入收到1条。
04:05 UTC核对：官方桥、舞台投影各1条，四页面DOM各1条；闲聊/游戏/待机
实际可见，暂离页按原布局隐藏弹幕区（消息DOM已更新）。未见重复投递；
当前没有单独的原始包/去重丢弃计数，不能声称观察到了去重动作。
没有自然播放事件，字幕为空且隐藏；字幕出现/清除仍待实际发声现场验收。


## 礼物、SC与撤回

官方来源处理 `LIVE_OPEN_PLATFORM_SEND_GIFT`、`LIVE_OPEN_PLATFORM_SUPER_CHAT`、
`LIVE_OPEN_PLATFORM_SUPER_CHAT_DEL`，沿同一WS场次及正式输入端口接管。
输入类型分别为 `gift`、`super_chat`、`super_chat_delete`；观众内容作为外部数据，
不赋予系统指令权限，不强制唤醒或保证感谢/回复。不需要新增账号权限或配置密钥。

礼物显示名称×数量、免费/付费类别。付费礼物的 `price` 是单件标价，1000平台单位=1元，
界面明确写“标价¥…/件”；免费礼物即使有非零price也不显示人民币金额。
`r_price`原值保留在结构化输入，未把它擅自乘数量或声称为本次实际支付总额；
盲盒/折扣不能用标价推断实际花费。SC的 `rmb` 单位直接为元，不再除以1000。
字段依据：[上游模型](https://github.com/xfgryujk/blivedm/blob/master/blivedm/models/open_live.py)
及[官方来源示例](https://github.com/xfgryujk/blivedm/blob/master/open_live_sample.py)。

SC按 `message_id` 保持稳定身份，在 `start_time <= now < end_time` 时展示；
已过期的新到达SC不展示、不提交新输入。删除按 `message_ids` 处理，先到的删除也能
阻止后到SC。对已接管SC只提交一次不含原文的状态更正，不重新提交原留言；
到期只改变展示，不提交新输入。已经发生的模型思考或声音无法倒回。
同一进程的WS重连保留互动去重身份；同一原生输入另受Engine持久化去重保护。

撤回ID原子存于 `.purrcept-engine/live-stage-sc-deleted-<实例/项目/房间摘要>.json`，
仅含版本和数字留言ID，没有正文、昵称或凭据，跨重启防止撤回SC复活。
不要为了恢复显示删除此文件。互动去重或撤回记录上限各20000项，容量耗尽明确停接，
不会通过淘汰撤回记录悄悄恢复旧SC；去重输入的崩溃不确定结果不会自动补投。
直播桥 `event_counts` 只输出各类解析事件、重复、过期及先撤回计数，前端不投影该诊断字段。

舞台SC高亮位于现有互动栏，礼物进入原滚动列表，不覆盖模型或游戏区域。
暂离页继续隐藏互动栏。SC到期在后端与前端都检查；失联立即隐藏SC，恢复后只取当前快照。
需要刷新已打开的捕获网页以加载新版JS/CSS；URL不变，不代操作直播姬。

离线验证：56项插件/临时库/Windows Worker/音频合并回归通过，Ruff、格式、
strict Pyright及compileall通过；隔离浏览器36/36项（合成数据仅18745/18746）。
实际普通弹幕此前已通过端到端验收；真实礼物、SC与删除事件尚未发生验证，
无需付费送礼测试，不能将替身结果当作真实到账或平台权限证明。
