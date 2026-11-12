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
    {"parameter": "PurrceptSmile", "sources": {"smile": 1.0}},
    {"parameter": "PurrceptHeadX", "sources": {"head_x": 1.0}}
  ],
  "lip_sync_offset_ms": 0.0
}
```

首次授权由 VTS 原生界面确认。模型或绑定不匹配时报告离线原因，不猜测参数。
需要 OVR 视素时，在配置中提供实际 `ovr_library` DLL 路径，并人工绑定
`viseme_sil`、`viseme_PP` 等对应通道；原生库只在独立分析进程加载。

`me.live2d.apply(intent={"valence": 0.4, "gesture": "nod"})` 提交独立语义姿态。
情绪与注视是局部更新：未填写或 `null` 表示不更新该字段、不延长它的原期限。
显式 `0` 设置中性目标；`gesture="none"` 取消当前动作，省略手势则让已开始
的动作继续。情绪、注视字段与一次手势分别到期，不会因新情绪重置视线或点头。
`effort` 是本次转向或手势的力度：`0` 放松，`1` 强调，中间值逐渐变化；缺省
采用本地 `default_effort`。例如 `{"gesture": "nod", "effort": 0.0}` 是柔和点头。
力度在动作开始时冻结，不继承上一次请求；单独传入力度不重定时已有动作。
关联身份和有效期由宿主创建，受理不等于实际视觉采用。已结束音频和过期
姿态不会在连接恢复后补演。声音、身体、收音拥有分别关闭和停止的边界。
插件启动登记唯一恢复任务，不等待 VTS 授权才允许宿主退出。初连与已上线
连接的传输失联各开一轮，最多5次建连，轮内等待0.5/1/2/4秒；建连超时3秒。
首次无令牌时原生授权最多120秒，拒绝、授权超时、协议错误、模型不符或
令牌文件错误停止该轮；耗尽不会自行重开。退出立即撤销恢复，实际连接与
令牌文件操作归还后才确认关闭。恢复只从自然基线开始，不保留旧平滑姿态。
每个模型的真实嘴型、参数绑定和视觉效果均需实机验收。

## 连续动作与校准

空闲时默认持续选择虚拟注视目标，带有随机停留、左右观察、轻微抬低头与回看。
眼睛先到位，头身按原有延迟跟随；这不会移动系统鼠标。显式注视及仍有效的语境
注视（包括看向正面）优先，空闲姿态同时减弱，避免持续抢走注意。

### 表演调节室

安装 `web` 与 `live2d` extra 后，可使用独立后台调整当前模型：

```powershell
python -m purrcept_engine.app.live2d_studio --config purrcept.toml --port 8767 --audio sample.mp3
```

打开 `http://127.0.0.1:8767/`，明确连接 VTS 后拖动滑块即可预览。测试语音来自
`--audio` 指定的本地文件（最多 8 MiB、30 秒），不调用在线合成；省略它仍可调节
姿态与空闲动作。独立调节室和正式 bot 不应同时向同一模型注入参数。页面刷新
不自动连接，关闭服务归还播放器与 VTS 连接。

可调项包括空闲活动间隔与幅度、头身默认角度、注视偏移、自然睁眼程度、口型
增益/曲线/响应时间，以及默认嘴型和表情幅度。角度为归一化通道偏移，方向依赖
实际绑定。头身偏移进入运动轨迹并参与眼球补偿，不能用偏移绕过输出限幅。
嘴部增益在惯性处理之前采用，闭口和短间隙保持不变；mouth_form 需要对应绑定。
仅能量驱动不提供逐字音素，OVR 仍按原有显式库与视素绑定使用。
启用 OVR 时，即使模型只有 mouth_open/mouth_form 两通道，也会将平滑后的
E/ih 与 oh/ou 权重投影为展唇/圆唇，`mouth_viseme_form_scale` 控制这部分幅度。
这是有损投影，方向需要人工校准，不替代完整视素绑定。

“保存偏好”才写盘，原字节按摘要备份到同目录 `backups/`，旧页面版本不能覆盖
新文件。默认偏好路径是校准文件替换最后一个后缀为 `.preferences.json`，例如
`q-model.live2d.json` 对应 `q-model.live2d.preferences.json`；可用插件配置
`preferences_path` 指定。偏好包含模型 ID，不允许套用到其他模型。
正式控制器连接期间约每秒在工作线程读取一次；有效文件整体热更新，不重置
动作、口型或语音所有权。坏文件保留最后有效版本并通过快照 `preferences_error`
报告。新代码首次部署仍需重启旧进程。偏好文件的口型响应设置优先于下述静态
style 配置；独立调节室预览不改写 `purrcept.toml` 或 VTS 参数绑定。

`motion.py` 的 `MotionController` 只负责同步动作状态，运行时负责连接、播放身份、
发送确认和口型。内部固定 120 Hz 推进动作，头部采用有限时长轨迹，身体采用
临界阻尼跟随，按真实时间差产生输出，
15/30/60 Hz 发送不会改变事件抽样顺序。超过半秒的停顿最多追赶半秒，过期动作
不补演；重连清空所有旧目标、速度和准备材料。

注视采用最短保持和切换门槛，显著转向可以提前打断；显式回正与期限失效不受门槛
阻挡。眼睛先到位，头部延后参与，持续的大幅目标才让身体跟随。眼球补偿在手势与
头身动态更新之后计算，因此点头、摇头也参与补偿。待机采用低频姿态目标与保持，
眨眼使用非固定间隔以及闭合、保持、睁开曲线，完成后恢复当前表情的睁眼程度。
放松手势平滑往返；力度超过 0.45 后逐渐混入起势、短主动作、停留和收势。
这些是可调的表演节奏，不是肌肉力学仿真。头部不再经过第二层弹簧过滤，
到位后精确保持。打断时继承位置、速度与加速度，以短轨迹收势。眼球目标转移
保持平滑，但头动补偿直接作用于本帧，避免快速点头时补偿滞后。

主要风格配置位于 `[plugins.config.style]`：

```toml
head_turn_seconds = 0.24
relaxed_head_turn_seconds = 0.70
default_effort = 0.15
body_response_seconds = 0.65
body_gaze_scale = 0.50
speech_gesture_scale = 0.80
speech_gesture_interval_seconds = 3.0
speech_expression_scale = 0.90
mouth_open_scale = 0.75
mouth_attack_seconds = 0.07
mouth_release_seconds = 0.14
mouth_gap_hold_seconds = 0.08
gaze_hold_seconds = 0.35
head_delay_seconds = 0.12
body_delay_seconds = 0.55
blink_interval_seconds = 3.75
motion_scale = 1.0
idle_motion_scale = 1.0
```

两个头部转向时间分别对应强调/放松端点，按本次力度插值；身体响应时间是动态系统的时间尺度，
不是保证到达时间。眨眼间隔是随机分布尺度。
持续注视越过 0.2 死区后，躯干转向连续增加至 `body_gaze_scale`，并带少量侧倾；
短暂扫视不启动身体跟随。实际可见幅度仍取决于模型身体绑定的角度范围。
`motion_scale` 只缩放待机和手势，明确注视与口型不受影响。旧的
`speech_motion_scale`、`micro_expression_scale` 已移除，不再用音量驱动持续摆头
或用独立正弦叠加微表情。语音韵律使用下述稀疏事件调度。

模型校准 JSON 可以增加 `gaze`：

```json
{"gaze": {"head_x": 0.8, "head_y": 0.8, "body_x": 0.3, "body_y": 0.3}}
```

这是归一化头身输出到注视偏移的换算系数，零值关闭对应补偿，默认值只是校准起点。
须连同 ParameterBinding 的符号、VTS 输入映射与最终画面核对；控制器无法从输入
注入成功推断画面已经保持注视。绑定应使用最终表情通道，例如嘴角用 `mouth_form`、
笑眼用 `smile`，避免绕过合成层重复叠加情绪。

独立身体控制需要把 `body_x/y/z` 绑定到模型身体参数，并解除模型内部写入同一身体
参数的头身物理；头发与服饰物理仍由模型负责。修改模型前保留原配置和物理文件。
VTS 的额外参数平滑可能增加响应延迟，校准时先降低它再观察控制器的动态响应。

## 语音韵律与交流意图

通过实际发声能力传入，例如：

```python
await me.microphone.speak(
    text="这里有两个地方需要解释。",
    body={"delivery": "explain", "effort": 0.0, "valence": 0.2},
)
```

`delivery` 只作用于这一项实际播放；独立 `live2d.apply` 传入它会返回
`BodySpeechContextRequired`。`neutral`（也包括省略）不自动摆头；`explain`
在能量突出附近选择小幅点头，`emphasize` 使用较大幅度点头，`deny` 选择摇头，
`question` 在停顿附近选择偏头。力度仍单独由 `effort` 控制，强调语义不会
自动变成大力动作。语义由调用方给出，分析器不从文本或音量推断情绪。

交流方式同时提供一层眉眼与嘴角姿态：解释偏友好，疑问使用不对称抬眉，
否定略收眉与嘴角，强调抬眉。它们是角色表演设计，否定不自动写入愤怒情绪。
显式 `valence`、`tension` 等继续独立叠加；播放结束只撤回交流层，不清除
后来独立更新的情绪。表情约以 0.2 秒时间常数起效、0.45 秒回落，眨眼在
表情合成之后作用于眼睑。`speech_expression_scale=0` 可关闭这一层。

`prosody.py` 在本地对已调音的 20 ms PCM 做能量包络、相对上升和停顿分析，
不需要 OVR DLL；没有音高估计、词级时间戳或语言重音识别。PCM 即使提前到达，
也只生成候选，动作必须等真实播放开始、首帧发送确认及媒体时钟到达。
音频缺口重置包络，重复块忽略，过时超过 250 ms 的候选直接略过。
候选最多保留 512 项，满时丢新候选，不阻塞声音或挤掉未播放的开头。

自动手势默认至少间隔 3 秒，每次发言最多六个，幅度额外乘以
`speech_gesture_scale`；恒定大音量不会不断触发。显式 `gesture`（包括 `none`）
禁止同一发言自动选择头部动作，独立显式手势也可抢占正在执行的自动动作；
占用期间的候选不排队。停顿只提供眨眼机会，仍受概率和两秒最短间隔限制。

自动手势的阶段跟随媒体进度：暂停时保持，进度突然跨越超过 250 ms 时丢弃
旧动作并收势。播放结束或中断清除剩余候选与该播放拥有的姿态；收势使用
本地单调时钟，声音已经停止也能平滑恢复。口型保持原有独立控制权。

`mouth.py` 在取到当前媒体位置的能量/视素后生成最终口型：默认张口上限
为归一化 0.75，开合采用不同时间常数，并限制速度。最多 80 ms 的短闭口
间隙可以合并，细小目标抖动采用死区；结束或中断立即撤销保持并连续闭口。
OVR 视素同样平滑，避免直接绑定视素时绕过动态层。此处是在视觉上抑制
快速变化的风格化处理，不声称模拟真人面捕误差，也不延迟声音或添加音频缓存。

## 语境注意与持续情绪

启用身体插件且配置了 `subject.attention` 时，同一次注意请求附带 `emotion` 和
`focus` 判断，不增加逐帧模型调用。decision 后端使用各问题的原始置信指标；
generation 后端必须显式返回两个置信值。它们不是跨提供方校准的正确率。
默认门槛为 0.65，各字段独立接纳；`unchanged`、拒答和低置信度均不更新或续期。
不装配身体时保留单一注意问题；不配置注意模型时仍可使用显式姿态和语音表演。

身体只接收通过真实批次版本检查的结果，重复、过期或被其他轮次取走的判断不再生效。
判断资格从评估开始最多 15 秒；慢响应、离线和重连不产生待补演队列。
情绪可选中性、友好、关切、好奇、兴奋和不悦；缓慢进入，默认最多保持 8 秒，
之后以 6 秒时间尺度自然衰减。显式情绪覆盖同名语境字段，播放结束只归还播放所有权。
语境额外眉眼在显式情绪出现时平滑退出；说话交流层继续独立合成。

注视对象包括观众、聊天、游戏、工作、发言者和短暂移开。默认切换至少间隔 1.2 秒，
仅保存最新候选，候选等待不会续期；相同对象可续期但不重启停留窗口。
已采用的 `urgent` 批次可跳过语境停留，仍不抢占显式姿态或自动提高力度。
注视默认最多保持 6 秒，随后回到观众。目标坐标是角色虚拟布局，不能当作屏幕定位；
在模型 profile 中可加下列 `focus` 字段调整，并与实际方向一起人工核对：

```json
{"focus": {"audience": [0.0, 0.0], "chat": [-0.65, -0.1], "game": [0.5, 0.0],
           "work": [0.25, -0.45], "speaker": [0.0, 0.08], "away": [-0.35, 0.25]}}
```

本地风格还可配置 `context_confidence`、`context_focus_hold_seconds`、
`context_focus_seconds`、`context_emotion_seconds`、`context_emotion_attack_seconds`
和 `context_emotion_decay_seconds`。空间布局归模型校准，时序策略归 style。
语境更新不触发点头/摇头，不自动把每个事件变成强调动作。

## 本机观察

下面的演示只启动正式身体运行时，不启动主体或播放语音。需要事先启用 VTS API，
并准备与当前模型身份相符且已实际绑定的校准文件：

```powershell
uv run python examples/live2d_preview.py --profile .purrcept/live2d/profile.json --token .purrcept/live2d/token --stop-file .purrcept/live2d/preview.stop --seconds 180
```

约 49 秒一轮：放松左右注视与身体跟随、轻点头/摇头、强调转向、中等与强调点头、
强调摇头、放松偏头过程中只更新微笑。
创建指定 stop-file 可正常停止；每次启动选择尚不存在的停止文件。正常结束先回正，
随后关闭连接；Ctrl+C 也会归还连接。演示不改写模型绑定或 profile。

语音验收脚本会实际播放明确指定的本地音频（最长 30 秒），不调用 TTS：

```powershell
uv run python examples/live2d_speech_preview.py --profile .purrcept/live2d/profile.json --token .purrcept/live2d/token --audio test.mp3 --stop-file .purrcept/live2d/speech-preview.stop --seconds 180
```

同一声音依次指定中性、解释、疑问、否定和强调，用于动作对照；控制台标记
阶段及真实播放器状态。可用 `--ffmpeg`、`--ffplay` 指定本机程序路径。

完整宿主验收使用独立数据目录，经真实输入接管、注意提交、主脑工具调用和 Windows
Worker 播放四轮语音；模型为脚本后端，声音复用本地素材，不验证真实 JEV 判断质量：

```powershell
uv run python -m examples.live2d_dialogue_preview --profile .purrcept/live2d/profile.json --token .purrcept/live2d/token --audio test.mp3 --stop-file .purrcept/live2d/dialogue.stop --data-dir .validation/dialogue-preview --seconds 120
```

每次运行使用新的数据目录和停止文件。只装配声音与身体，不加载日常配置或消息插件。
依次观察友好阅读聊天、好奇处理工作、关切倾听、兴奋关注游戏；头身、持续情绪、
交流表情和口型都经过正式合成链路。连接首帧确认后才提交输入，结束关闭整个宿主。
