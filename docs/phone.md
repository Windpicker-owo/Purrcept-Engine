# 安卓手机接入

Phone 插件将 Purrcept Go 作为一个远程手机设备。用户消息沿现有输入端口成为
刺激，主体通过 `me.phone.chat.send` 显式回应或主动发言；手机网络在线与主脑
的设备操作会话独立。Android 不运行 Engine，也不接收模型凭据。

## Engine WebUI 配对

电脑端操作统一在 Engine WebUI 的「设备 → 连接安卓手机」完成。Go 只负责手机
扫码、核对电脑、连接和聊天，不再提供电脑端程序或配对服务。

1. 正常打开 Engine 和本机 WebUI，进入「设备」。
2. 首次未启用 Phone 时展开「设置手机连接」，选择手机可访问的电脑地址和端口，
   点击「查看将要保存的改动」，核对后保存。只修改当前 CLI 使用的配置文件中的
   Phone 设置，保留模型设置、其他插件和已有证书，并备份原配置。
3. 新增插件或更改监听配置需正常关闭并重新打开 Engine。WebUI 不强制停止主体，
   保存不会重载正在运行的插件。嵌入式 WebServer 未提供配置路径时只支持已有 Phone 配对。
4. Phone 就绪后点击「显示配对二维码」。手机打开 Purrcept Go 扫码、核对电脑后连接。
5. 邀请有效两分钟。过期点击「显示配对二维码」即可生成新窗口，无需重启 Engine；
   「刷新配对二维码」立即撤销旧二维码。已有配对时明确显示已配对手机，不生成第二份凭据。

重装 App 或更换手机导致手机丢失凭据时，点击「重新配对」，核对提示后选择
「撤销旧配对并显示二维码」。旧手机凭据失效且已有连接断开，随后由新安装的
App 扫码建立新配对。Engine 中的历史记录保留；新设备不会自动获得旧设备的
聊天投递和媒体权限，也不会自动恢复操控授权。已派发的操作不能被撤销，未知
效果不向新设备重放。取消确认不会执行任何撤销；旧确认也不能撤销其他页面新建的配对。

普通刷新只读取，不开启或延长配对窗口。二维码直接来自运行中 Phone 监听器，
不依赖邀请文件。界面在过期、已消费、读取失败和断线时撤下二维码。短时邀请
仅经同源本机端口返回，禁止缓存，不进入模型上下文、普通快照或浏览器存储。

安装 Engine 的 `web` extra 包含设置与二维码依赖。配置和备份仍包含原有凭据，
应置于同一个私有目录。WebUI 不更改 Windows 防火墙或手机权限。

## 命令行准备与配对

安装 `phone` extra。以下地址替换为手机可以访问的电脑地址：

```powershell
uv sync --extra phone --all-groups
uv run purrcept-phone init --address 192.168.1.10
```

证书和私钥生成在当前目录的 `.purrcept/phone/`；已经存在时拒绝覆盖。
在现有 `purrcept.toml` 的插件数组中追加：

```toml
[[plugins]]
instance_id = "phone"
entry = "phone"
[plugins.config]
listen_host = "0.0.0.0"
port = 8766
public_url = "https://192.168.1.10:8766"
certificate = ".purrcept/phone/phone-cert.pem"
private_key = ".purrcept/phone/phone-key.pem"
pairing_enabled = true
invitation_file = ".purrcept/phone-invitation.json"
```

已有顶层 `plugins = []` 时，先移除该空数组声明再追加数组表。
路径以配置目录为基准。允许手机访问 TCP 8766 后启动 Engine。
首次未配对且 pairing_enabled 开启时，启动会将120秒有效的邀请导出到指定本地文件；
WebUI 刷新后旧文件中的邀请失效，日常配对请以 WebUI 当前二维码为准。可在 Engine WebUI 设备页显示或刷新
二维码，手机扫描核对后连接；也保留在 Purrcept Go 配对页粘贴邀请的入口。邀请包含准确证书指纹，Android 不接受其他证书；不要
外发邀请或私钥。配对成功即消费邀请并删除文件；长期凭据仅交给该手机。
过期后在 WebUI 显式开启新窗口，无需重启。配对后建议将 `pairing_enabled` 关闭。

手机点击“设备在线”后建立 WSS。聊天不需要无障碍服务，也不需要“允许设备
操控”。第一次验证请只发送文字并确认 Engine 产生真实 input_id。

## 主脑能力

```python
await me.phone.chat.send(text="你好，我在这里。")
await me.phone.connect()
screen = await me.phone.screen()
await me.phone.tap(frame_id=screen["data"]["frame_id"], element_id=1)
await me.phone.disconnect()
```

实际能力结果的包装以行动工具交付为准。设备操作还包括 `apps`、`open_app`、
`foreground`、`screenshot`、`input_text`、`swipe` 和 `key`；参数由冻结目录
列出。`screen` 元素编号只在对应 frame_id 有效；每次已接受的界面操作后
重新读取 screen。Android 执行前重读并比对有界可见树，未发生实际变化的
普通内容通知不会直接使快照失效；窗口切换和当前窗口滚动则立即撤销。
`stale_frame` 表示必须读取新 screen 并按新元素重新决定，不能原样自动
重试。回执的 `data.stale_reason` 提供树变化、窗口变化、滚动、操作后撤销
等诊断，不携带界面正文；该校验不提供 Android 渲染与操作的原子性保证。
截图经认证上传、同库保存，并作为原生图像证据交给主脑，不另造用户输入。
Android 完成手势仅证明手势回调完成，不证明第三方应用的业务效果。

`chat.send` 返回 queued 代表 Engine 已持久接管，离线手机尚未收到；历史
可通过 `chat.history` 查询。用户普通直接消息提供最低唤起资格，但仍遵循
当前睡眠规则，不默认抢占旧生成。

手机聊天界面区分本机已保存、Engine 已接管等待同步和事件已同步。接管
回执不会提前移除手机待发项，直到对应事件落盘才替换气泡；重连仍可按
原身份幂等提交。上述状态不代表主脑已读或已经作答，协议没有已读回执。

## 恢复与所有权

手机先写 Room 再发送消息，Engine 按原生 message_id 去重。入站原文归现有
inputs，手机事件日志引用 input_id；接管后展示提交前崩溃的缺口在重连时
从原文幂等修复，不重新调度。出站文字和事件游标保存于同一 engine.sqlite3。

手机先落盘事件再确认序号。网络重连只补送聊天和查询未决命令，不恢复操作
会话，不重发设备操作。命令派发意图在写网络前提交，手机也在调用 Android
前写执行日志。崩溃或丢失回执保留 unknown；迟到实际回执沿 LateResultPort
追加，不改写原行动结论。若手机已经没有旧执行记录，只能报告 unknown。

关闭插件先停止新接管，执行器收尾期间保留回执连接，最后关闭监听器。TLS
监听器不创建第二个主体，不复用本机 WebUI 的会话令牌。

## 撤销配对

停止 Engine 后使用同一数据目录撤销配对。命令会取得真实实例锁，宿主仍在
运行时拒绝操作；不会删除输入、媒体或行动历史。

```powershell
uv run purrcept-phone revoke --data-dir .purrcept-engine --instance-id phone
```

手机也需解除本机配对，再重新开始邀请流程。

## 本轮范围

当前实现文字聊天、可靠补送、显式设备连接和基础 Android 操作。图片上传
目前用于设备截图；安卓扫码已接入。聊天附件选择、语音、连续观察、桌宠和
主脑主观已读回执尚未实现。运行时权限、前台服务和截图效果需要测试手机
验收；离线协议测试不代表真实设备已通过。


## 安卓实时体验

Phone 的 welcome 声明 `engine_state` 特性，并推送独立、无需确认的状态帧。
公开名字由应用从当前角色卡装配进有限 PluginPorts，插件不取得角色正文。
PhoneStore.progress 以同库只读事务投影本设备最近24条入站消息、确切轮次、
当前前台逻辑模型请求与运行中行动，不复制模型材料、行动参数或回执正文。
全局工作可以由其他来源发起，手机气泡只按确切 turn_id 关联，不能由时间推断。

短行动可能在采样间结束；每条消息可附最近一次实际派发的能力路径与当前回执
阶段供查询，不能为动效制造额外执行时间。状态只在公开投影变化后发送，不占
聊天补送 seq；断线后立即失去实时资格。详细字段见同级 Go 的
`docs/purrcept-protocol.md`，手机主仓为 `purrcept_go`。
