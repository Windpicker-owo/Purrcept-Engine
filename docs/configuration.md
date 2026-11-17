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

随时可在终端查阅全部 11 个内置插件的依赖与完整配置示例：

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
| `[models.<名称>]` | — | `model`（如 `openai/模型ID`）、`api_base`、`api_key`、`context_window`（默认 `64000`）、`max_output_tokens`（默认 `4096`）、`supports_contents`（默认 `["text"]`，可选加 `"image"`、`"audio"`）。 |
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

全部 11 个内置插件（`notebook`、`clock`、`bed`、`qq`、`phone`、`computer`、`browser`、`meme`、`microphone`、`voice_input`、`live2d`）的带注释配置示例均已内置在 [default.toml](../src/purrcept_engine/app/default.toml) 中，也可随时执行 `uv run purrcept-engine plugins <插件名>` 在终端直接查看。

## 5. 高级参数与资源配额

上下文压缩（`[context]`）、隔离沙箱配额（`[execution]`）、持续活动额度（`[activities]`）与诊断保留期（`[diagnostics]`）均已内置稳妥的默认值，普通用户无需配置；需要精细调优时参见 `src/purrcept_engine/app/config.py` 中的字段定义。
