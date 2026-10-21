# 配置说明

Engine 读取显式指定的 TOML 配置。先用最小配置完成控制台交互，再添加设备。
完整字段及默认值见随包的 [default.toml](../src/purrcept_engine/app/default.toml)，
字段校验见 [app/config.py](../src/purrcept_engine/app/config.py)。未知字段和错误类型
会直接报错；修改配置后重启生效。WebUI 的运行期开关不会回写 TOML。

## 配置文件和路径

```powershell
purrcept-engine init
purrcept-engine --config C:\Purrcept\purrcept.toml check
purrcept-engine --config C:\Purrcept\purrcept.toml catalog
purrcept-engine --config C:\Purrcept\purrcept.toml run
```

`init` 写出完整模板，已有文件不会覆盖。`check` 校验结构、模型用途引用、插件声明
及依赖；`catalog` 展示当前配置的能力，两者都不探测模型、连接设备或启动主体。
第三方插件的工厂导入必须遵守纯声明契约。

`host.data_dir` 以及内置插件的相对路径以配置文件所在目录为基准，和启动时的
工作目录无关。Windows 路径建议使用 TOML 单引号，例如 `data_dir = 'D:\Purrcept\data'`，
或用正斜杠 `D:/Purrcept/data`。同一数据目录只能由一个宿主持有。
凭据直接填在本地配置中；不支持 `${ENV_VAR}` 字符串插值。不要提交真实配置。

## 最小可交互配置

以下是完整配置，可直接保存为 `purrcept.toml`。替换模型名、服务地址、凭据、实际
上下文窗口和输出上限；这里的窗口数字只是示例，不能据此推断服务端能力。

```toml
console = true
plugins = []

[host]
data_dir = ".purrcept-engine"

[models.main]
kind = "generation"
model = "openai/your-model"
api_base = "http://127.0.0.1:4000/v1"
api_key = "replace-with-your-key"
context_window = 32000
max_output_tokens = 4096
capacity_rule = "litellm-token-counter-v1"
media_rule = "unsupported"
supports_contents = ["text"]

[subject]
foreground = "main"
reflection = "main"
summary = "main"
attention = "main"

[subject.character]
name = "小咕"
one_line = "持续在线的伙伴。"
speech_style = "用简洁自然的中文交流。"
```

启动后在终端输入一行文字并回车。输入是主体的刺激；主体自行决定是否调用
`me.console.output` 表达，内部最终文字不会自动打印为回复。EOF 只停止控制台
输入，Ctrl+C 才关闭宿主。可先用 `python examples/action.py` 验证显式控制台
能力和真实 Worker；该示例要求 `console = true`，不调用模型。

`init` 模板演示独立专用判断模型。若使用上面的单生成模型方案，请整体替换
配置，或删除模板的 `[models.attention]` 并将 `subject.attention` 改为生成模型
名称；不要保留未使用的占位端点。

## 模型声明和用途

`[models.任意名称]` 声明资源，`[subject]` 用名称引用它。前台、反思和摘要必须
引用 `kind = "generation"` 的模型；三者可以共用端点，前台和反思各有独立会话。
注意可以引用生成模型或 `kind = "decision"` 的专用判断模型；省略 `attention`
时不装配注意评估器，普通输入按无注意模型路径请求前台。

| 字段 | 含义与约束 |
| --- | --- |
| `model` | 含协议前缀的模型名。生成路径当前只接入 `openai/`；专用判断使用 `typesafe/` 或 `openrouter/`。前缀选择传输协议，不表示自动选择服务地址。 |
| `api_base` | 明确的 HTTP(S) 端点，不能内嵌账号密码、查询串或片段。OpenAI 兼容服务通常包含 `/v1`。 |
| `api_key` | 必须明确提供非空凭据；本地无需认证的服务也应提供其接受的占位值。 |
| `context_window` | 仅生成模型需要；输入与输出共享的实际窗口。 |
| `max_output_tokens` | 仅生成模型需要；实际输出预留，必须小于窗口。 |
| `input_limit` | 专用判断必填，表示其协议输入上限；生成模型可选，用来进一步限制输入。 |
| `capacity_rule` | 对完整请求执行容量预检的规则，不能由模型名自动推断。 |
| `supports_contents` | 默认 `["text"]`，按真实传输能力声明 `image`、`audio`、`video`；声明不能替代实际媒体支持。 |
| `media_rule` | 默认 `unsupported`，表示当前容量规则不接纳原生媒体。 |

### 容量与媒体

- `litellm-token-counter-v1`：生成请求使用 LiteLLM 包内 tokenizer，对完整 JSON
  及工具目录计量，是本地参考估算，不是服务端精确 usage。
- `reference-text-only-v1`：完整纯文本 JSON 的 UTF-8 字节参考量，一字节一单位，
  不等同于 token。可用于专用判断或显式选择此保守参考口径的生成路径。
- `gemini-3-inline-v1`：配合生成 token 规则计量 Gemini 3 默认内联单帧图片及
  WAV/MP3。原生图片/音频需同时加入 `supports_contents`；不能用于任意视觉模型。
  MP3 计量还需要 `voice-input` extra。当前没有通用 video 计量规则。

不支持的输入媒体会以文字引用呈现，原件仍保存；没有适用的媒体计量依据则明确
拒绝派发。只添加 `supports_contents = ["text", "image"]` 不能让模型成功看图。
布局和媒体窗口详见 [输入材料契约](input-materials.md)。

### 独立专用判断模型

例如以下声明可替换生成式注意，再设置 `[subject] attention = "attention"`：

```toml
[models.attention]
kind = "decision"
model = "typesafe/your-decision-model"
api_base = "http://127.0.0.1:4001/v1"
api_key = "replace-with-your-decision-key"
input_limit = 8192
capacity_rule = "reference-text-only-v1"
media_rule = "unsupported"
supports_contents = ["text"]
```

TypeSafe 路径追加 `/systemone`。OpenRouter 判断路径使用 `openrouter/` 前缀，
例如 `openrouter/typesafe/jev-1.13`，端点为 `https://openrouter.ai/api`，SDK
追加 `/alpha/decisions`，不要填聊天接口的 `/v1`。模型可用性以服务端为准。
Engine 不会在判断失败后切换到聊天端点。

## 主体、预算与历史

| 配置区 | 默认值与用途 |
| --- | --- |
| `[subject.character]` | `name`、`one_line`、`persona`、`relationship`、`speech_style`、`notes` 六项固定身份文字，缺省为 `None`；不从长期认识恢复。 |
| `subject.attention_batch_seconds` / `attention_batch_size` | `0.1` 秒 / `8` 条，普通注意合批门槛。 |
| `subject.proactive_thinking` / `proactive_idle_seconds` | `false` / `300.0` 秒。启用后按静默或主脑未激活期限，在主脑空闲时触发一次思考；会产生模型费用。 |
| `subject.reflection_batch_seconds` / `reflection_batch_turns` | `60.0` 秒 / `8` 轮，独立反思的合批触发条件。 |
| `[subject.foreground_retry]` 等 | `attempts = 2` 含首次；`attempt_seconds = 120.0`、`total_seconds = 180.0`。注意默认每次 `5.0`、总计 `8.0` 秒。可分别设置 `reflection_retry`、`summary_retry`、`attention_retry`。 |
| `[subject.foreground_turn]` / `[subject.reflection_turn]` | `max_requests = 8`、`total_seconds = 600.0`，限制整轮逻辑请求数与总时间；传输重试共用原逻辑请求预算。 |
| `[context]` | 占用达到 `summary_trigger = 0.8` 时尝试历史维护，目标 `summary_target = 0.5`，必须小于触发比例。`input_reserve_fraction = 0.1` 为下一批输入预留额度，`protected_turns = 2` 保护近期完整轮次，`page_chars = 8000` 限制展开页。 |

容量先扣除输出，再预留输入空间；若设置独立 `input_limit`，还会取更小上限。
历史维护不会突破完整轮次保护或通过静默丢弃事实来满足预算。

## 启用插件

安装 extra 只提供依赖，`[[plugins]]` 才声明启用。把最小配置顶层的 `plugins = []`
删除后，在文件末尾添加以下内容；TOML 不能同时定义空数组和同名数组表。

```toml
[[plugins]]
instance_id = "notebook"
entry = "notebook"
enabled = true
[plugins.config]
notebook_dir = "notes"
skills_dir = "skills"
seed_examples = true

[[plugins]]
instance_id = "clock"
entry = "clock"
[plugins.config]
timezone = "Asia/Shanghai"

[[plugins]]
instance_id = "bed"
entry = "bed"
```

`instance_id` 在整个配置中唯一；`entry` 选择工厂，`config` 由对应插件严格校验。
`enabled = false` 时不导入插件。上例时钟在 Windows 上需安装 `time` extra。
`skills_dir` 相对 Notebook 根目录；上述示例对应配置目录的 `notes/skills`。

QQ 的最小接入还需安装 `qq` extra，并在 OneBot 实现中启用相同的反向 WebSocket
地址与令牌。以下示例监听 `ws://127.0.0.1:8095`，只允许明确列出的私聊，群聊全部拒收：

```toml
[[plugins]]
instance_id = "qq"
entry = "qq"
[plugins.config]
mode = "reverse"
host = "127.0.0.1"
port = 8095
access_token = "replace-with-your-onebot-token"
private_list_type = "whitelist"
private_list = ["123456789"] # 替换为实际允许接收的 QQ 号。
group_list_type = "whitelist"
group_list = []
```

`whitelist` 只接收列出的账号或群，空白名单拒收全部；`blacklist` 排除列出项，
空黑名单允许全部。默认是空黑名单，连接前请明确选择接收范围。
插件设置见 [QQSettings](../src/purrcept_engine/plugins/qq/settings.py)。
声音、收音、电脑、Live2D 的端点、设备和校准示例从 README 的内置能力表进入。
浏览器需安装 `browser` extra，并显式准备 Patchright 浏览器程序，运行时不下载。

外部已安装包使用 `entry = "package.module:factory"`；本地插件放在配置目录的
`plugins/包名/` 中并导出 `factory`，使用 `entry = "local/包名:factory"`。
根目录 `plugins/` 不受 Git 追踪，也不随 Engine 分发。内置插件仍随包提供。

## 面板、存储与执行额度

安装 `web` extra 后添加 `[web] enabled = true`；`port` 默认 `8765`，`0` 表示系统
分配端口。启动后终端打印含访问令牌的回环地址；面板只连接当前宿主，不需要 Node。
数据写入 `host.data_dir/engine.sqlite3`，备份前应停止宿主并复制完整数据目录。
`host.close_seconds = 30.0` 是正常关闭的观察期限，超时不代表资源已释放。

`[execution]` 的常用上限是：`action_seconds = 180.0`、`max_calls = 64`、
`max_workers = 4`、`max_provider_calls = 16`、`max_pending_inputs = 1024`，
`pending_input_bytes = 268435456`。CPU、内存、源码、输出和 IPC 帧也有独立上限，
完整列表见默认模板。额度限制并发、等待和材料大小，不承诺撤销已发生的外部效果。
`[activities]` 默认最多 8 个活动，初次与最大剩余运行额度均为 3600 秒，
检查点间隔 5 秒；暂停和离线时间不扣减。

`[logging]` 默认保留 7 天、单文件 10 MiB、最多 7 份；`[diagnostics]` 默认保留
7 天、详细模型流 1 MiB。这些清理只约束诊断，原始输入、媒体、请求及真实回执
不会随诊断过期删除。`[knowledge.embedding]` 缺省时仅本地检索；启用语义检索需
明确填写 OpenAI 兼容的 `model`、向量空间版本 `model_id`、`api_base` 及可选凭据，
配置契约见 [EmbeddingSettings](../src/purrcept_engine/domain/knowledge/settings.py)。

## 常见问题

- **校验通过但无法请求模型**：`check` 只核验声明；检查地址、凭据、协议前缀、
  实际窗口及网络连通性，模型失败证据位于数据目录及面板记录中。
- **启动后没有回答**：先确认 `console = true` 或已接入输入插件；主体还需显式
  调用输出能力。通过 `catalog` 检查输出通道是否装配，并检查输入处理回执。
- **启用插件提示缺少依赖**：在同一个环境安装对应 extra，再重启。
- **数据目录已占用**：关闭原宿主或为新实例选择不同数据目录；不要删除活跃锁。
- **启用媒体后容量拒绝**：核对传输支持、`supports_contents` 和 `media_rule` 三者，
  不要通过放宽窗口或把媒体按零量计算来绕过错误。
