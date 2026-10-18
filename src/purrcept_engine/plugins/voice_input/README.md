# 本机收音

插件入口为 `purrcept_engine.plugins.voice_input:factory`。声明、查询状态及页面刷新
不打开设备；明确 `start` 才取得本机麦克风、单会话 VAD 和有限识别任务。
`devices` 明确枚举并更新设备缓存，`stop` 只停止收音。停止输出使用独立的
`me.microphone.interrupt`。

ASR 使用配置中的百炼 Fun-ASR WebSocket 地址、模型和显式密钥。原生音频模式
要求生成模型的 `supports_contents` 包含 `audio`、配置非 `unsupported` 的 `media_rule`，
以及适用的容量规则；仅声明模型支持音频不能启用原声。Host 已内置注册
`litellm-token-counter-v1`，CLI 与嵌入宿主都可直接使用。Gemini 3 配置示例：

```toml
capacity_rule = "litellm-token-counter-v1"
media_rule = "gemini-3-inline-v1"
supports_contents = ["text", "image", "audio"]
```

完整请求的 JSON 文字及工具定义使用 LiteLLM 内置 cl100k tokenizer 参考值，
音频按实际 WAV/MP3 时长计入 25 token/秒，内联单帧图片按默认分辨率计入
1120 token。媒体依据见 [Gemini 3 媒体分辨率](https://ai.google.dev/gemini-api/docs/media-resolution)。
这是本地估计，不是 Gemini 私有 tokenizer 或服务端 usage；上下文窗口仍使用
显式配置及余量。未知媒体、远端 URL 和损坏媒体明确失败，原始请求不被改写。
实际网关效果仍需要另行实机验收。
来源显示名 `source_name`、会话和发言身份进入输入事实，不提供人员权限。

面板分别显示实际收音设备、最近一句的权威处理阶段及语音输出阶段。收音成功、
输入已处理和播放器已开始是不同事实，不能互相替代。失败原件保留在同库；后续
交互带失败原文、回执和明确未展开的媒体引用，不自动再次投递导致失败的音频。

主脑历史只保留最近两个已完成轮次加当前轮次的原生音频；更早声音在请求中
原位替换为“历史语音输入已过期”，不自动转写。输入原件和当时的请求证据
仍保存在同库。这是图片和音频共用的连续上下文窗口，不影响当前发言的完整
接管或原声交付；旧图片以“历史图片已过期”原位替换。

将主配置中的 `plugins = []` 替换为数组项，再追加此声明；路径相对主配置目录。
下面的密钥须替换为显式凭据，启用插件仍不会自动开始收音：

```toml
[[plugins]]
instance_id = "voice_input"
entry = "voice_input"
[plugins.config]
source_name = "本机麦克风"
vad_model_path = ".purrcept/voice-input/silero_vad.onnx"
asr_api_key = "填写显式凭据"
asr_url = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
asr_model = "fun-asr-realtime"
interrupt_ms = 2000
vad_interrupt_enabled = true
```

运行时先明确枚举 `me.voice_input.devices()`，再调用
`me.voice_input.start(mode="asr", device=设备编号)`，或使用面板同名控制。
省略设备编号采用当前默认输入。切换模式前先停止当前收音；`audio` 模式除了
`supports_contents` 包含 `audio`，还必须配置适用的媒体计量规则，文本参考规则不能使用。

`vad_interrupt_enabled` 默认 `true`。WebUI“设备 → 收音 → VAD 自动停播”或
`me.voice_input.set_vad_interrupt(enabled=False)` 可在运行中关闭；选择立即生效，
停止再开始收音仍保留，重启采用配置默认值。关闭仅跳过后续持续人声确认的
停播请求及对应起音通知，VAD 分段、ASR/原声和最终发言保底唤起保持正常。
已经发出的请求与通知继续完成；重新开启不补发当前发言已经跳过的确认。

开启时，持续人声达到 `interrupt_ms` 才接管 `speech_started`；宿主在接管后停止当前与
已有候播，当前模型生成继续。起音是高优先停播通知，绕过普通注意过滤并保底
进入下一轮，不单凭起音调用紧急升级判断。通知区分已请求停播与实际停止回执，
不能把未播完的语音当作已完整送达。最终转写或原生音频沿用同一发言关系，
不重复停播；最终内容仍按高价值输入规则决定是否紧急升级。
起音前缓存有界，连续发言最长 60 秒，原生附件每段最长 20 秒。识别名额、
上传帧队列或采集队列过载时暂停并记录缺口，不拼接丢样发言，不重传付费请求。
取消只结束观察和未接管缓存，已经接管的事实仍由原 `input_id` 查询。

原生采集使用 `blocksize=0`，由设备后端选择回调大小，再连续重采样并组装为
512 样本的 VAD 帧；声卡不需要每 10 ms 向 Python 回调。
[sounddevice 的流契约](https://python-sounddevice.readthedocs.io/en/0.5.3/api/streams.html)
说明固定回调块在部分后端和高负载下更容易失稳。连续历史的恢复、编码与请求
准备在工作线程执行，避免阻塞同一事件循环中的采集消费。

“麦克风输入溢出”来自 PortAudio 的原生丢样标志，与“麦克风缓存已满”及
“识别上传缓存已满”分别表示不同边界。孤立丢样记录缺口，作废跨越缺口的
未完成发言，重建重采样器及 VAD 状态后继续同一收音会话；已正常结束的前句
仍可等待 ASR 入账，已接管的起音事实仍保留。不会拼接缺口、重传旧录音或
重新打开声卡。受影响的那句话需要重新说，后续完整发言可正常提交。

固定容错窗口为五秒内最多三个原生缺口，第四次暂停；稳定运行超过窗口后
不累积旧故障次数。队列满、设备断开等仍明确暂停，需要重新开始。
扩展 CaptureDriver 可在有序帧流中返回 `CaptureGap`，缺口两侧必须独立分段；
驱动若无法确定新的连续片段，应抛 `VoiceInputError`。
扩展驱动必须提供 `resources_released()`，只在设备已实际归还时返回 True；
异步帧流结束不能替代原生 close 的成功证明。输入历史故障可以继续报告，
但设备与识别/接管等待均已结束时，宿主会继续释放数据库和目录锁。
运行日志记录选定设备、采样率、
`queued_blocks` 和 `consumer_delay_ms`，后者是最近一次 PCM 交付到观察故障的
间隔，不是声卡延迟；`recovery=gap/paused` 区分继续收音和持续故障暂停。
断点暂停、系统调度或设备问题仍可能导致真实丢样。

安装 `voice-input` extra 后，明确执行以下命令取得固定版本的本机 VAD 模型：

```powershell
python -m purrcept_engine.plugins.voice_input.setup --output .purrcept/voice-input/silero_vad.onnx
```

该命令会下载公开模型并核对固定校验和。插件导入和正常启动不自动下载模型或
依赖；模型文件缺失时开始收音明确失败。设备使用自己的原生采样率，连续转换
成 16 kHz mono S16 后做 VAD；持续溢出或其他致命采集错误会释放同一输入流。

下载命令使用直连，不读取环境代理。无法直连时可先从 `setup.py` 的固定官方
地址取得文件，用 SHA-256 `2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f`
核对后放到配置路径；不能因网络失败改用未校验的其他版本。
