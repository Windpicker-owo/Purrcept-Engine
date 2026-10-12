# 独立声线调音室

调音室不启动 Engine 主体、ASR、TTS 或扬声器。安装 `web` extra，
并显式提供可用的 FFmpeg 程序；安装后页面静态资源随同一个 Python 包提供，
无需 Node 开发服务器。

```powershell
purrcept-voice-studio --profile .purrcept/voice-profile.json --port 8766 --ffmpeg ffmpeg
```

也可使用 `python -m purrcept_engine.app.studio`。服务固定监听 `127.0.0.1`；
同端口 Host、实际回环客户端、同源 Origin 与页面会话令牌由宿主共用的
`LocalHttpBoundary` 校验，不采信代理头。关闭服务后上传素材释放。

上传仅接受 WAV、MP3、FLAC，最大 20 MiB；解码时按实际 PCM 限制最长 90 秒。
FFmpeg 只允许管道协议和明确解码器，上传文件名不参与磁盘路径。服务只持有
一个样本，正忙时拒绝新上传和试听，取消后先回收原解码器再释放串行锁。
旧 `clip_id` 不能操作已替换的新样本。

浏览器的单一播放器提供 A/B、循环试听、波形、处理后 WAV 导出、平直重置和
预设导入/导出。处理版与现场音频共用 `audio.dsp.VoiceProfile` 和滤镜，保存
只影响后续语音。服务按实际文件字节计算预设版本，旧页面保存冲突不会覆盖
新版本；坏预设和内部程序故障不会悄悄采用空配置。
