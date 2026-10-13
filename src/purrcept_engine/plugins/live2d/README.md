# Live2D 身体

安装 `live2d` extra，准备本机 VTube Studio 的实际模型与人工校准，再显式
启用下列插件。路径相对主配置目录，令牌不与其他账号配置合并：

```toml
[[plugins]]
instance_id = "live2d"
entry = "live2d"
[plugins.config]
vts_url = "ws://127.0.0.1:8001"
profile_path = ".purrcept/live2d/profile.json"
token_path = ".purrcept/live2d/token"
sample_rate = 32000
reaction_seconds = 4.0
frame_hz = 30.0
```

校准文件记录实际 VTS 模型 ID 和自定义输入。下面只展示结构，必须替换模型
ID，并在 VTS 中将输入绑定到模型相应参数；接口创建成功不证明视觉已生效：

```json
{
  "model_id": "填写实际VTS模型ID",
  "bindings": [
    {"parameter": "PurrceptMouth", "sources": {"mouth_open": 1.0}, "minimum": 0.0},
    {"parameter": "PurrceptSmile", "sources": {"valence": 1.0}},
    {"parameter": "PurrceptHeadX", "sources": {"head_x": 1.0}}
  ],
  "lip_sync_offset_ms": 0.0
}
```

首次授权由 VTS 原生界面确认。模型或绑定不匹配时报告离线原因，不猜测参数。
需要 OVR 视素时，在配置中提供实际 `ovr_library` DLL 路径，并人工绑定
`viseme_sil`、`viseme_PP` 等对应通道；原生库只在独立分析进程加载。

`me.live2d.apply(intent={"valence": 0.4, "gesture": "nod"})` 提交独立语义姿态。
关联身份和有效期由宿主创建，受理不等于实际视觉采用。已结束音频和过期
姿态不会在连接恢复后补演。声音、身体、收音拥有分别关闭和停止的边界。
插件启动登记唯一恢复任务，不等待 VTS 授权才允许宿主退出。初连与已上线
连接的传输失联各开一轮，最多5次建连，轮内等待0.5/1/2/4秒；建连超时3秒。
首次无令牌时原生授权最多120秒，拒绝、授权超时、协议错误、模型不符或
令牌文件错误停止该轮；耗尽不会自行重开。退出立即撤销恢复，实际连接与
令牌文件操作归还后才确认关闭。恢复只从自然基线开始，不保留旧平滑姿态。
真实嘴型、模型绑定和视觉效果仍待实机验收。
