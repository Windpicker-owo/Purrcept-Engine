# 宿主电脑截图、观察与命令

电脑插件必须先明确连接；查询状态、WebUI 刷新和装配不采集桌面。
`me.computer.screenshot()` 取得当前原始分辨率 JPEG，同时保存数据库证据和
一份本地文件。成功回执的 `path` 是可供后续能力使用的绝对路径，`evidence_ref`
和原生图像仍供主脑看图、追溯；截图本身不自动发送消息，也不生成重复输入。

默认文件目录是主配置目录下的 `.purrcept/computer-screenshots`，可在电脑插件
配置中设置 `screenshot_dir`。相对路径沿主配置目录解析，只有明确截图时才创建：

```toml
[[plugins]]
instance_id = "computer"
entry = "computer"
[plugins.config]
screenshot_dir = ".purrcept/computer-screenshots"
```

每次显式截图创建一个独立文件，不覆盖已有文件；返回前完成写入与关闭。
连续观察仍只交付数据库媒体，不逐帧保存 JPEG。关闭电脑连接或 Engine 不删除
已保存截图，避免其他能力仍在使用路径时失效；不再需要的文件由使用者清理。

在 `play_action` 中可串联截图和 QQ 发送，填写实际目标 QQ 号：

```python
from world import me

await me.computer.connect()
shot = await me.computer.screenshot(target="foreground_monitor")
await me.phone.qq.send(chat="private", target_id="123456", images=[shot["path"]])
```

发送为文件时改用 `files=[shot["path"]]`；群聊改为 `chat="group"` 并填写群号。
默认目录已经位于 QQ 默认允许的 `.purrcept` 目录内。若自定义到该目录之外，
须在 QQ 插件的 `extra_media_roots` 中显式允许实际保存目录；不会自动扩大权限。
QQ 和电脑插件须均已启用。文件发送还要求 OneBot 实现端可访问该文件路径，
图片则沿 QQ 既有内联图片/本地文件规则传输。

文件保存失败返回 `failed`、`mirror_state="failed"` 和实际错误，不提供 `path`；
已经接管的原图、来源和 `evidence_ref` 继续保留。不能把保存失败当作图片没有
取得，也不能猜测路径后继续发送。取消调用等待不取消已开始的保存，关闭等待
实际文件工作结束；这不代表 QQ 已经发送，发送结果仍以独立的 QQ 回执为准。

## 宿主命令

启用电脑插件后，`me.computer.command` 允许主体以 Engine 当前账号在真实宿主机上
运行 Windows PowerShell 命令，具有该账号的文件、程序和网络权限，并继承宿主环境。
它通过能力执行服务调用与入账；行动 Worker 本身的导入、文件和网络限制不变。
必须先显式连接电脑，查询状态或启用插件不会执行命令。

```python
from world import me

await me.computer.connect()
result = await me.computer.command(
    command="Get-ChildItem -Force",
    cwd=".",
    timeout_seconds=30,
    max_output_bytes=32768,
)
return result
```

- `command` 是非空 PowerShell 脚本，最多 8000 个字符；可调用已安装的外部程序。
- 每次使用独立、隐藏、非交互、无 profile 的系统 Windows PowerShell。标准输入为 EOF，
  不支持交互提示或持久终端会话；不要通过它启动需要在回执结束后继续驻留的后台进程。
- `cwd` 默认主配置目录，相对路径也沿主配置目录解析；必须是已有目录。上次命令修改的
  shell 变量或工作目录不会带入下次调用。同一电脑的命令串行执行。
- `timeout_seconds` 默认 30 秒，范围 0.1–300 秒，仍受行动剩余期限约束；能力自身最多
  等待 310 秒，默认行动总期限为 180 秒。命令超时、显式断开或插件关闭会回收本次进程树。
- `max_output_bytes` 是 stdout、stderr **各自**保留的前缀字节数，默认 32768，范围
  256–65536。达到上限后继续排空并丢弃多余字节，避免堵塞。文本按 UTF-8 解码，
  无效字节和截断处的不完整字符使用替换字符；输出不另存完整文件。

回执包含 `started`、`cwd`、`exit_code`、`stdout`、`stderr`、两个 `*_truncated` 标记、
`timed_out` 和 `interrupted`。退出码零为 `confirmed`，非零为 `failed`；PowerShell
错误默认停止并返回 1，外部程序沿用最后一个原生命令退出码，也可用 `exit` 明确指定。
启动失败返回 `failed` 和 `started=false`。超时或断开返回 `unknown`，保留已有输出；
超时没有正常退出码时 `exit_code=null`。文件写入等先前效果不会因失败或终止而回滚。
宿主总期限先到时，执行服务可能先记录未知回执，再独立保存实际清理后的结果。
取消调用方的等待不会撤销已经开始的命令，也不会自动重试。

此版本为已启用的电脑插件新增命令权限；原有配置无需增加字段。部署时应按上述
宿主账号权限理解电脑插件的授权范围。
