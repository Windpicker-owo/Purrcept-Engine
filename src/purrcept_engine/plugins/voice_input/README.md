# 本机收音

插件入口为 `purrcept_engine.plugins.voice_input:factory`。声明、查询状态及页面刷新
不打开设备；明确 `start` 才取得本机麦克风、单会话 VAD 和有限识别任务。
`devices` 明确枚举并更新设备缓存，`stop` 只停止收音。停止输出使用独立的
`me.microphone.interrupt`。

ASR 使用配置中的百炼 Fun-ASR WebSocket 地址、模型和显式密钥。原生音频模式
要求生成模型显式声明 `supports_audio`，实际网关效果仍需要另行实机验收。
来源显示名 `source_name`、会话和发言身份进入输入事实，不提供人员权限。

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
