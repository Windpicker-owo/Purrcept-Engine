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

程序路径需要实际存在。预设路径为空或文件尚未创建时不应用 DSP；已有坏文件
明确失败。预设由[独立调音室](../../app/studio/README.md)保存，只影响后续播放。
输出文件目录和预设路径沿主配置目录解析，`record(save_as=...)` 仍使用输出
目录内的相对路径，不任意改写能力参数。

`me.microphone.speak(text=...)` 先受理，再按接管顺序合成/播放；最多一项正在
执行和四项候播。排队计入调用期限，队满明确拒绝。播放开始/完成来自实际
媒体时钟；中断、失败和超时可能已发出部分声音，不能自动重播。
`me.microphone.interrupt()` 撤销当前及接管停止时已有候播的播放资格，之后
新接管的 speak 仍可发声。停止收音是 `me.voice_input.stop()` 的独立操作。

`speak(body={...})` 可借用独立 Live2D 身体；身体离线不阻断声音，其结果单独
记录。[身体校准](../live2d/README.md)不改变 TTS 或播放器的所有权。
