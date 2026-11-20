# 配置说明

Purrcept Engine 使用清晰易读的 `purrcept.toml` 进行配置，支持**交互式问答向导**、**环境变量插值**（`${ENV_VAR}`）以及**本机/局域网免密模型接入**（如 Ollama、vLLM、LM Studio）。

## 1. 一分钟快速初始化

在终端运行以下命令启动交互式引导向导：

```powershell
uv run purrcept-engine init
```

向导会依次引导你选择模型接入类型（云端 API 或本地免密服务）、填写模型地址与密钥、设定角色名字，并自动为你开启常用的 `notebook`（随身笔记记忆）与 `bed`（自主休息）插件。

如果你更喜欢直接编辑带完整注释的模板文件，可加 `--template` 参数（若需备份覆盖已有文件加 `--force`）：

```powershell
uv run purrcept-engine init --template
```

随时可在终端查阅内置插件的依赖与完整配置示例：

```powershell
uv run purrcept-engine plugins          # 列出所有内置插件概览
uv run purrcept-engine plugins qq       # 查看指定插件的字段解释与可直接粘贴的 TOML 片段
```

校验与启动：

```powershell
uv run purrcept-engine check            # 校验配置语法、模型引用与插件依赖
uv run purrcept-engine run              # 正常启动（终端默认输出人话生命周期日志）
uv run purrcept-engine --debug run      # 开启 DEBUG 级别日志与详细堆栈排查问题
```

## 2. 最小单模型配置示例

只需声明一个 OpenAI 兼容模型即可同时驱动主脑（`foreground`）、后台反思（`reflection`，省略时自动沿用 `foreground`）与收件箱注意评估（`attention`）：

```toml
console = true

[host]
data_dir = ".purrcept-engine"

[models.main]
model = "openai/deepseek-chat"
api_base = "https://api.deepseek.com/v1"
api_key = "${DEEPSEEK_API_KEY}"   # 支持 ${VAR} 与 ${VAR:-默认值} 环境变量引用

[subject]
foreground = "main"
attention = "main"

[subject.character]
name = "小咕"
one_line = "住在你电脑里的连续在线 AI 伙伴。"
speech_style = "用自然、简短的中文交流。"
```

`api_base` 为可选字段，省略时使用模型服务商的默认地址。生成模型按 LiteLLM
前缀选择协议，例如 `openai/模型名` 使用 OpenAI 兼容协议，`gemini/模型名`
使用 Gemini 原生协议；自建或第三方网关仍可显式填写 `api_base`。
省略地址时仍需提供远程模型的 `api_key`。

### 为模型启用代理

在需要代理的模型下填写 `proxy`，支持 HTTP(S) 代理和混合端口的 HTTP 协议：

```toml
[models.studio]
model = "gemini/gemini-4-hydrogen"
api_key = "${GEMINI_API_KEY}"
proxy = "http://127.0.0.1:10808"
```

将端口替换为代理软件的 HTTP 或混合代理端口。`proxy` 也支持 `${ENV_VAR}`
插值；省略时直连，不自动读取 `HTTP_PROXY`、`HTTPS_PROXY` 或 `ALL_PROXY`。
生成、缓存查询及缓存创建共用此模型的代理；专用判断模型也支持该字段。
`api_base` 继续表示模型服务端点，不用于填写本地代理。

### 本地免密模型（Ollama / vLLM / LM Studio）

当 `api_base` 指向本机回环（`localhost`、`127.0.0.1`、`::1`）或局域网私有地址（如 `192.168.x.x`）时，`api_key` 可直接省略或留空 `""`：

```toml
[models.main]
model = "openai/qwen2.5:14b"
api_base = "http://127.0.0.1:11434/v1"
api_key = ""
```

## 3. 核心配置表速查

| 配置块 / 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `console` | `true` | 是否开启终端打字输入与 `me.console.output` 控制台表达能力。 |
| `[web] enabled` / `port` | `false` / `8765` | 启用本机 WebUI 面板（需安装 `uv sync --extra web`）。 |
| `[logging] level` | `"INFO"` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR`，也可通过 CLI `--debug` 或 `--log-level` 覆盖。 |
| `[models.<名称>]` | — | `model`（如 `openai/模型ID` 或 `gemini/模型ID`）、可选的 `api_base`（省略使用服务商默认地址）、`api_key`、`context_window`（默认 `64000`）、`max_output_tokens`（默认 `4096`）、`supports_contents`（默认 `["text"]`，可选加 `"image"`、`"audio"`）。 |
| `[subject] foreground` | 必填 | 主脑连续对话与行动引用的生成模型名。 |
| `[subject] reflection` | 同 `foreground` | 后台行为评审引用的生成模型名。 |
| `[subject] attention` | `None` | 收件箱合批注意评估引用的模型名（可填同一个生成模型，或独立 `kind = "decision"` 专用判断模型）。 |
| `[subject] proactive_thinking` | `false` | 从主脑轮次结束起空闲满 `proactive_idle_seconds`（默认 `300.0` 秒）时是否自动唤醒思考一次。 |
| `[context] media_cleanup_threshold_mb` | `30` | 完整 SDK 请求的 UTF-8 JSON 超过此 MB 数时，一次清理已完成历史中的媒体；包含 base64 编码，1 MB = 1,000,000 字节。当前轮次媒体保留。 |
| `[subject.character]` | 全空 | 固定角色卡六项：`name`、`one_line`、`persona`、`relationship`、`speech_style`、`notes`。 |

## 4. 启用内置插件

在 `purrcept.toml` 末尾直接追加 `[[plugins]]` 段落即可启用插件（无需在文件顶部写 `plugins = []`）：

```toml
[[plugins]]
instance_id = "notebook"
entry = "notebook"
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

内置插件的带注释配置示例见 [default.toml](../src/purrcept_engine/app/default.toml)，也可运行 `uv run purrcept-engine plugins <插件名>` 查看。外部 B 站插件的本地清单与替换适配器见 [B 站插件](bilibili.md)。

### OpenAI 生图与编辑

启用 Computer 插件，并在运行 Engine 的环境中提供 `OPENAI_API_KEY`。
在 `purrcept.toml` 追加下列配置。`computer_id` 对应 Computer 插件的 `instance_id`。
生图插件直接调用 OpenAI Images API，Computer 设备无需先执行 `connect()`。

```toml
[[plugins]]
instance_id = "gpt_image"
entry = "gpt_image"
[plugins.config]
computer_id = "computer"
api_key = "${OPENAI_API_KEY}"
model = "gpt-image-2.6-flare"
```

Bot 可用 `me.computer.gpt_image.generate(prompt=..., size="1536x1024")` 从文字生图，
用 `me.computer.gpt_image.edit(images=[media_id], prompt=...)` 继续编辑。
已有的 `entry = "image_generation"` 配置也会注册到新路径。

两个工具都接受可选的 `model`，省略时使用插件配置。要比较 `gpt-image-2.6-flare`
和 `gpt-image-2.6-sunburst`，可用相同的提示词与出图设置分别调用，回执中的 `model`
会标明每张图所用的模型。

`images` 依次接受底图和参考图。入站图片可用 `{"input_id": input_id, "image_index": 0}`
指定，序号从 0 开始。也可传 `me.view_image` 或生图结果的 `media_id`、
HTTPS 图像 URL 或 OpenAI Files API 的 `file_id`。局部编辑可传首张底图对应的
透明 PNG `mask` 或 `markup`。本地原件通过 data URL 上传，单图最大约 15 MiB，
每次上传的本地原件合计最多 64 MiB。

两个工具支持自定义尺寸、质量、背景和张数，输出 PNG、JPEG、WebP 或动画 GIF。
使用兼容模型时可设置复杂度、压缩率以及 GIF 帧数与时长。生成工具还支持 SVG，
以文件形式交付。使用 `partial_images=1` 至 `3` 时会接收并保存流式中间图，
在动作完成回执的 `previews` 中提供。模型、组织验证和预览功能的权限要求见
[OpenAI 图像生成指南](https://developers.openai.com/api/docs/guides/image-generation)。

结果中的 `images[i].path` 默认位于主配置目录的 `.purrcept/image-generation` 下，
QQ 可用 `me.phone.qq.send(chat=..., target_id=..., images=[path])` 发送位图，
也可用 `files=[path]` 发送 SVG 文件。
QQ 插件按完整请求帧核算图片空间。帧装不下时，本地图片可通过 `file://` 引用随消息发送，
OneBot 网关须能读取该路径。
若自定义 `output_dir` 到 QQ 默认媒体根目录之外，请将该目录加入 QQ 插件的
`extra_media_roots`。默认回执提供文件路径和媒体 ID，适用于纯文本主脑。
主脑模型声明 `supports_contents = ["text", "image"]` 时，可设置 `include_media=True`
让主脑同时查看位图。
其他字段与默认值可运行 `uv run purrcept-engine plugins gpt_image` 查看。

## 5. 高级参数与资源配额

上下文压缩（`[context]`）、隔离沙箱配额（`[execution]`）、持续活动额度（`[activities]`）与诊断保留期（`[diagnostics]`）均已内置稳妥的默认值，普通用户无需配置；需要精细调优时参见 `src/purrcept_engine/app/config.py` 中的字段定义。
