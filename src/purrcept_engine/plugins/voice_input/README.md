# 本机收音

插件入口为 `purrcept_engine.plugins.voice_input:factory`。声明、查询状态及页面刷新
不打开设备；明确 `start` 才取得本机麦克风、单会话 VAD 和有限识别任务。
`devices` 明确枚举并更新设备缓存，`stop` 只停止收音。停止输出使用独立的
`me.microphone.interrupt`。

ASR 使用配置中的百炼 Fun-ASR WebSocket 地址、模型和显式密钥。原生音频模式
要求生成模型显式声明 `supports_audio`、配置非 `unsupported` 的 `media_rule`，
以及适用的容量规则；仅声明模型支持音频不能启用原声。Host 已内置注册
`litellm-token-counter-v1`，CLI 与嵌入宿主都可直接使用。Gemini 3 配置示例：

```toml
capacity_rule = "litellm-token-counter-v1"
media_rule = "gemini-3-inline-v1"
supports_audio = true
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
```

运行时先明确枚举 `me.voice_input.devices()`，再调用
`me.voice_input.start(mode="asr", device=设备编号)`，或使用面板同名控制。
省略设备编号采用当前默认输入。切换模式前先停止当前收音；`audio` 模式除了
`supports_audio = true`，还必须配置适用的媒体计量规则，文本参考规则不能使用。

持续人声达到 `interrupt_ms` 才接管 `speech_started`；宿主在接管后停止当前与
已有候播并取消旧生成。最终转写或原生音频沿用同一发言关系，不能再次抢占。
起音前缓存有界，连续发言最长 60 秒，原生附件每段最长 20 秒。识别名额、
上传帧队列或采集队列过载时暂停并记录缺口，不拼接丢样发言，不重传付费请求。
取消只结束观察和未接管缓存，已经接管的事实仍由原 `input_id` 查询。

安装 `voice-input` extra 后，明确执行以下命令取得固定版本的本机 VAD 模型：

```powershell
python -m purrcept_engine.plugins.voice_input.setup --output .purrcept/voice-input/silero_vad.onnx
```

该命令会下载公开模型并核对固定校验和。插件导入和正常启动不自动下载模型或
依赖；模型文件缺失时开始收音明确失败。设备使用自己的原生采样率，连续转换
成 16 kHz mono S16 后做 VAD；输入溢出会停止会话并释放同一输入流。

下载命令使用直连，不读取环境代理。无法直连时可先从 `setup.py` 的固定官方
地址取得文件，用 SHA-256 `2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f`
核对后放到配置路径；不能因网络失败改用未校验的其他版本。
