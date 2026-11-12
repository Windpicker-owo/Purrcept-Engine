# 声音输出

`microphone` 插件拥有 TTS 连接、合成/播放队列、播放器及同库播放事实。
FFmpeg、FFplay 须由操作者提供；导入、配置和声明不会启动声音。
将主配置的 `plugins = []` 替换为数组项，并追加：

```toml
[[plugins]]
instance_id = "microphone"
entry = "microphone"
[plugins.config]
api_base = "https://fishaudio.org/api/open/v3"
# 需要代理时显式启用；省略则直连。
# proxy = "http://127.0.0.1:10808"
# 链路或同步生成较慢时可显式提高总期限（默认30秒，上限180秒）。
# timeout_seconds = 90.0
api_key = "填写显式凭据"
model = "fishaudio-s21pro-flash"
voice_id = "填写实际音色ID"
playback_backend = "streaming"
processing_executable = "C:/tools/ffmpeg/bin/ffmpeg.exe"
playback_executable = "C:/tools/ffmpeg/bin/ffplay.exe"
sample_rate = 32000
max_pending = 4
output_dir = ".purrcept/microphone"
postprocess_profile = ".purrcept/microphone/voice-profile.json"
```

`proxy` 支持不带凭据、路径或查询串的 HTTP(S) 代理地址；不会读取
`HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 或 `NO_PROXY`。能力目录、音色查询、
文件合成和流式合成使用同一代理，失败后不自动回退直连或重发。
`timeout_seconds` 默认为30秒，能力目录、音色查询和文件合成共享此期限；
修改配置后重启宿主生效。
传输失败回执区分能力目录查询、音色查询、文件合成和流式合成，保留异常类型及
HTTP 错误状态码；不回显请求正文、认证信息或第三方错误原文。达到总期限时
后续阶段不会获得新的完整预算，也不会自动重试付费请求。
每个阶段以调用 ID 关联开始和结束日志，记录直连／代理、阶段耗时、响应头和
首块响应体到达时间、已接收字节数、最后网络步骤及事件循环最大调度延迟。
客户端身份在同一实例复用期间保持一致；每阶段另记观测到的新 TCP 连接次数和
剩余总期限，便于比较独立测试与长期运行实例，不能把连接池等待当作生成耗时。
字节数计量解压前的响应体，不表示音频已完整生成或播放。超时回执也保留这些
安全进度，避免把整个调用的期限误判为上游实际生成耗时；普通取消保持取消。

程序路径需要实际存在。预设路径为空或文件尚未创建时不应用 DSP；已有坏文件
明确失败。预设由[独立调音室](../../app/studio/README.md)保存，只影响后续播放。
输出文件目录和预设路径沿主配置目录解析，`record(save_as=...)` 仍使用输出
目录内的相对路径。省略 save_as 时也会在该目录生成唯一持久文件，并返回
绝对路径；可直接传给 `me.phone.qq.send(voice=path, ...)`。关闭不删除已经导出
的素材，同文本再次生成不会覆盖旧文件。默认回执不再包含 base64。

`me.microphone.speak(text=...)` 先受理，再按接管顺序合成/播放；最多一项正在
执行和四项候播。排队计入调用期限，队满明确拒绝。播放开始/完成来自实际
媒体时钟；中断、失败和超时可能已发出部分声音，不能自动重播。
`me.microphone.interrupt()` 撤销当前及接管停止时已有候播的播放资格，之后
新接管的 speak 仍可发声。停止收音是 `me.voice_input.stop()` 的独立操作。
流式停音同时撤销本次 PCM 消费等待，再回收播放器及上游迭代器；即使网络或
解码暂时没有下一块数据，也不会只终止播放进程却继续占住现场队列。
解码/调音停止时同时排空并丢弃管道里剩余的 PCM，再确认进程退出；仅杀进程
不能解除已暂停读取的管道背压。被丢弃的旧声音不会继续播放或重放。

`speak(body={...})` 可借用独立 Live2D 身体；身体离线不阻断声音，其结果单独
记录。[身体校准](../live2d/README.md)不改变 TTS 或播放器的所有权。
可用 `body={"delivery": "explain", "effort": 0.0}` 选择柔和解释动作；其他交流方式
为 `neutral`、`question`、`deny`、`emphasize`。身体依据调音后 PCM 的停顿和能量
变化准备少量候选，按真实媒体进度执行；中断清除剩余动作。省略 `delivery` 不
自动摆头，显式 `gesture` 优先，详见身体说明中的语音韵律契约。
