# 宿主电脑截图与观察

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
