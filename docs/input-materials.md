# 输入提供方与框架的职责

提供方知道平台语义，负责选择必要字段并渲染内容。框架按来源分组、插入标题与
证据 TextBlock，并按模型声明投影内容类型；不解析平台字段或 YAML，不改写原文，
不移动媒体位置。

例如 QQ 交付的一个 TextBlock 可以是以下合法 YAML。采用 messages 列表，避免在
同一层混合映射和数组；原生 ID 和昵称均保持字符串类型。

```yaml
group: "猫猫群(1055894123)"
messages:
  - message_id: "123456"
    type: "reply"
    time: "16:02"
    sender: "拾风(3431391539)"
    target: "message:123455"
    text: "完整回复正文"
```

普通消息为 message，回复为 reply，戳一戳为 poke，回应为 reaction。只有真实存在
事件自身的消息身份时才保留 message_id；回复和回应的 target 使用 message:<原生ID>，
poke 的 target 是用户。空字段、实现标签、原始事件帧和装饰图片 URL 不自动注入。
戳一戳实际动作保留在 text，非空后缀保留为 suffix；图片引用、未知消息段和转发
缺口只在存在时提供。完整发言不截断，卡片内容不缩成空的类型占位符。
实际直接 @ 时保留 mentioned_self=true，已确认的回复主体关系保留 reply_to_self，
这些是判断所需字段，不依赖框架重新解析原生帧。

群名与昵称取本次事件和同账号同会话的已知记录，缺失时只显示账号，不额外请求
平台。时间按宿主本地时区显示；首次接管后冻结。只有真实目标等于当前账号时才标“你”。

## 提供有序的原生内容块

`InputSubmission` 从 `purrcept_engine.plugin_system` 导出；内容块使用 Core 的不可变
模型值。在提交之前调用 with_content，文字和媒体可任意交错：

```python
from purrcept_core.models import AudioBlock, AudioBytes, TextBlock

submission = InputSubmission(
    source=source,
    text=original_text,
    occurred_at=occurred_at,
    received_at=received_at,
    context=InputContext(details_json=raw_details_json),
).with_content(
    "me.microphone",
    (
        TextBlock("以下是本次发言的原始音频。"),
        AudioBlock(AudioBytes(wav_bytes, "audio/wav"), alt_text="本次发言原声"),
        TextBlock("录音结束；这段文字属于音频之后。"),
    ),
)
admission = await ports.inputs.submit(submission)
```

AudioBlock/ImageBlock 必须提供已取得的原生字节，不接受待下载 URL。媒体进入原有
media 附件/BLOB；有序布局只存文字、索引和 alt_text，不将音频 base64 塞进 JSON。
已有相同媒体复用并保留名称；重复出现的位置保留。所有既有附件都必须在布局中
明确出现，不能借展示隐藏附件。调用方传入的列表立即冻结，后续修改不影响提交。

`text` 和 `details_json` 仍保存原文和原始平台事实。布局位于 JSON 扩展保留键
engine.input_presentation，包含 source 和 blocks，根字段与块种类严格校验。原始
平台对象内部的同名键不获得布局语义。没有布局的普通文本输入采用 TextBlock(text)
再接原附件的固定默认顺序，框架不自动展示原始平台字段。

## 框架如何组装

原生媒体进入当前轮时保持完整。连续历史另有固定媒体窗口：每份会话保留最近
两个已完成轮次，加上当前正在处理的轮次。轮次依据 Core 的真实完成边界，
不是单条消息、附件段数或工具请求数；失败轮次和传输重试不推进窗口。
更早 AudioBlock 原位替换为 TextBlock("历史语音输入已过期")，ImageBlock 原位
替换为 TextBlock("历史图片已过期")，覆盖图片字节、图像 URL 和嵌套工具结果。
来源/时间、其余文字、隐藏推理、工具调用及回执身份保持；不会因为内容块不是
TextBlock 就删除协议。固定文字只表示本次请求不再携带旧媒体，不表示已经
识别、转写、理解或删除原件。当前可持久化的原生媒体是图片和音频；其他附件
目前按输入材料能力提供文字引用，不会通过此机制新增视频或文件原生协议。

会话恢复、首发容量预览和摘要材料共用同一投影；摘要占位不增加完整轮次计数。
投影在新轮恢复时冻结，整轮工具循环内不继续过期。成功后才随常规会话事务
保存新历史；失败保留原检查点，下次请求仍重新应用窗口。权威输入、当时真实
模型请求和工具回执中的原始媒体不改写，因此重启可恢复连续性，原件仍能追溯。

`domain/context/materials.py` 的 input_blocks 为注意与前台提供同一个入口：

1. 按来源首次出现的顺序建立分组，同来源且同账号的输入保持接管顺序。
2. 插入“以下为本批已接管的最新刺激”和“===来自 me.phone.qq 的最新刺激===”等标题。
3. 为每条输入插入证据身份、来源发生时间 occurred_at 和宿主接收时间 received_at，
   随后按提供方的内容序列交付。
4. 按目标模型的 supports_contents 恢复支持的原生字节和 alt_text；不支持的媒体原位
   替换为文字引用，保留 TextBlock → 媒体或占位 → TextBlock 的位置。
5. 必要时追加明确的材料缺口，不修改前面的提供方内容。

跨来源分组不表示全局时间先后；提供方应明确细分消息关系。批次可能受输入数量
或模型容量限制，因此不能声称本批包含缓冲区的“所有”刺激。框架不合并或重写
同一来源的 YAML，只连接各输入提供的块；提供方可以在自己的一个 TextBlock 内
按 group/messages 组织多条事件。

失败经历续接保持同一布局，只在媒体原位置插入“仅提供引用，未展开像素或音频”
的 TextBlock。read_evidence 可以请求原件，但媒体引用
本身不表示已经看过像素或听过原声。

## 时间与跨批次顺序

信封的 occurred_at 和 received_at 以 ISO 8601 完整日期时间展示，包含秒、已有的
小数秒和时区偏移。正式存储统一到 UTC，提供方正文不改写；QQ 正文的 HH:MM 仅是
简写，模型应同时参考框架提供的完整时间。occurred_at 来自提供方的发生时间声明，
不是框架对远端时钟准确性的认证；received_at 是接入端记录的宿主接收时刻。

应用在每次逻辑模型请求的插件 prepare_request 完成后采样宿主本地墙钟，动态尾部
request_time.current_time 提供完整日期与本地时区偏移，unix_seconds 提供同一瞬间
的 Unix 秒数。它表示请求组装时刻，不是模型完成时刻；同一次逻辑请求的传输重试
使用相同材料，下一次工具后续请求重新采样。work.observed_at 则是更早读取 SQLite
工作快照的时刻。实时变化不进入固定 system 前缀，也不把快照变成执行许可。

play_action 保留完整权威 JSON，另附 UTC 时间线：行动 created_at/finished_at、
逐次调用 prepared_at/started_at/finished_at 及确切 receipt_revision。正文超出容量
而只交付引用时，仍保留同一时间线。动态尾部对当前调用增加 recorded_times_utc，
迟到回执按新版本的结果记录时间展示，不沿用旧 unknown 版本的时间。
这些字段分别是宿主准备、派发和结果记录时间；null 表示尚未记录，不能把它们解释
为平台送达或外部效果精确发生的时刻。原始证据及数字时间戳不改写。

主脑一次唤醒只取得当时的输入快照。如果 msg-2 在思考期间到达、对 msg-1 的行动
之前发生，下一批虽在历史中晚于行动出现，仍会带 msg-2 当时的发生/接收时间。
模型应据此比较时序，不能把模型材料中的位置当作现实世界的发言顺序。框架提供
可核对事实，不承诺模型必然正确理解，也不保证接入平台的时钟已同步。

## 模型支持的内容类型

生成与 decision 模型统一使用 supports_contents，缺省为仅 text，例如：

```toml
# 放入对应的 [models.<名称>] 表内。
supports_contents = ["text", "image", "audio"]
```

合法类型为 text、image、audio、video；必须包含 text，不接受重复或未知类型。
列表加载后冻结。旧的独立音频布尔配置已移除，旧字段会明确报错，需改成此列表。
注意模型与前台模型独立声明：同一录音可在文字注意模型中显示引用，在支持音频的
前台模型中交付原声。原始文字不改写，不支持的媒体不读取 BLOB，不发送 base64；
占位包含 MIME、证据身份、字节数和已有名称/alt_text，明确说明未交付原件，不能
把提供方说明当作转写、OCR 或已经感知的内容。实际请求记录和容量检查针对投影结果。

内容声明不替代传输实现和容量检查。Core 的 decision 协议当前仅接受文字和图片，
即使列表声明 audio/video，也只交付明确文字引用。Core 当前没有 VideoBlock，
video 是允许的声明值；未声明支持时视频附件变成引用，声明支持后尝试原生交付
仍会显式报告传输不支持，不声称已有原生视频能力。
文本和媒体共用一个内置多模态 token 估算器：正文按字符启发式估算，图片按
实际尺寸、音频按实际时长估算，base64 不参与正文计数，无需额外配置规则。
媒体仅在内存中读取，媒体读取、解码与存储、传输故障仍明确失败，不会自动变成占位。此投影用于新刺激组装，不重写旧会话和证据工具结果。

来源、布局、原文和媒体同事务保存，计入接管额度与指纹。重放恢复第一次接管的
上下文，不使用新昵称缓存或新本地时区重绘旧刺激。输入布局不修改数据库格式或
工具 schema；既有会话消息不重写。注意的固定身份由应用单独注入，decision 与
生成式注意均参考和主脑同源的完整角色卡，见[架构说明](architecture.md#源码契约与模型材料)。
角色卡不从刺激正文中提取，姓名/别称唤起也不成为接入层的关键词硬规则。
容量预检、请求记录与实际模型交付使用同一份组装结果。
render_attention_context 只负责有限工作背景的精简 YAML，通用
render_material 继续呈现完整回执，不承担输入来源解析。
