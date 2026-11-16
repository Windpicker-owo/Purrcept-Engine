"""提供交互式 ``purrcept-engine init`` 配置向导与内置插件配置速查手册。

让新用户无需翻阅源码或记忆复杂的 TOML 结构，即可在终端通过几步问答生成经过严格校验
的可运行 ``purrcept.toml``，并随时通过 ``purrcept-engine plugins`` 查阅全部 11 个内置插件的配置项。
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import TextIO

from purrcept_engine.app.config import EngineConfig
from purrcept_engine.errors import ConfigError


@dataclass(frozen=True, slots=True)
class PluginGuideEntry:
    """单个内置插件的人类可读说明、依赖包与标准配置片段。"""

    entry: str
    title: str
    capability_prefix: str
    extra: str | None
    summary: str
    toml_snippet: str
    key_fields: tuple[tuple[str, str], ...]


PLUGIN_GUIDES: tuple[PluginGuideEntry, ...] = (
    PluginGuideEntry(
        entry="notebook",
        title="随身笔记与长期记忆 (Notebook)",
        capability_prefix="me.notebook.*",
        extra=None,
        summary="提供日记手账、局部编辑、技能发现与附件保存；支持可选后台专题整理与向量混合检索。",
        toml_snippet="""[[plugins]]
instance_id = "notebook"
entry = "notebook"
[plugins.config]
notebook_dir = "notes"
skills_dir = "skills"
seed_examples = true""",
        key_fields=(
            ("notebook_dir", "必填，笔记根目录（相对配置文件目录），如 'notes'"),
            ("skills_dir", "可选，技能子目录（相对 notebook_dir），默认 ''，常用 'skills'"),
            ("seed_examples", "首次启动是否生成示例日记与技能文件，默认 true"),
            ("consolidation.enabled", "是否启用后台专题记忆自动整理，默认 false（启用时需填 consolidation.model）"),
            (
                "embedding.model",
                "可选，向量模型名（如 'openai/text-embedding-3-small'），不配则用关键词检索",
            ),
        ),
    ),
    PluginGuideEntry(
        entry="clock",
        title="世界时钟与闹钟提醒 (Clock)",
        capability_prefix="me.clock.*",
        extra="time（Windows 下使用非 UTC 时区时需要）",
        summary="供主体读取当前本地时间，并设置单次或每日定时唤醒闹钟。",
        toml_snippet="""[[plugins]]
instance_id = "clock"
entry = "clock"
[plugins.config]
timezone = "Asia/Shanghai"
label = "世界时钟\"""",
        key_fields=(
            ("timezone", "IANA 时区名，默认 'Asia/Shanghai'"),
            ("clock_id", "挂载在 me 下的属性名，默认 'clock'"),
            ("label", "设备展示名称，默认 '世界时钟'"),
        ),
    ),
    PluginGuideEntry(
        entry="bed",
        title="自主睡眠与起床 (Bed)",
        capability_prefix="me.bed.*",
        extra=None,
        summary="让主体自主躺下休息或起床；睡眠期间普通刺激仅记入历史，不唤醒主脑。",
        toml_snippet="""[[plugins]]
instance_id = "bed"
entry = "bed\"""",
        key_fields=(
            ("bed_id", "挂载在 me 下的属性名，默认 'bed'"),
            ("label", "展示名称，默认 '床'"),
        ),
    ),
    PluginGuideEntry(
        entry="qq",
        title="QQ 消息与空间接入 (OneBot v11)",
        capability_prefix="me.phone.qq.*",
        extra="qq",
        summary="通过 OneBot v11 正向/反向 WebSocket 收发私聊、群聊、图片、表情包与 QQ 空间动态。",
        toml_snippet="""[[plugins]]
instance_id = "qq"
entry = "qq"
[plugins.config]
mode = "reverse"
host = "127.0.0.1"
port = 8095
access_token = "${ONEBOT_ACCESS_TOKEN:-}"
private_list_type = "whitelist"
private_list = ["123456789"]
group_list_type = "whitelist"
group_list = []""",
        key_fields=(
            ("mode", "连接方式：'reverse'（Engine 监听等待 OneBot 连入）或 'direct'（主动连 OneBot）"),
            ("host / port", "WebSocket 地址与端口，默认 '127.0.0.1' 与 8095"),
            ("access_token", "OneBot 鉴权 Token，支持 '${ONEBOT_ACCESS_TOKEN}'，无密码可留空"),
            ("private_list_type / private_list", "私聊过滤模式（'whitelist' 或 'blacklist'）与 QQ 号数组"),
            ("group_list_type / group_list", "群聊过滤模式（'whitelist' 或 'blacklist'）与群号数组"),
            ("sticker_dir", "本地表情包目录，默认 '.purrcept/qq-stickers'"),
        ),
    ),
    PluginGuideEntry(
        entry="phone",
        title="安卓手机协同客户端 (Purrcept Go)",
        capability_prefix="me.phone.*",
        extra="phone",
        summary="通过双向确认的局域网/公网 HTTPS/WSS 连接安卓手机，支持聊天与显式屏幕辅助操作。",
        toml_snippet="""[[plugins]]
instance_id = "phone"
entry = "phone"
[plugins.config]
listen_host = "0.0.0.0"
port = 8766
public_url = "https://192.168.1.100:8766"
certificate = ".purrcept/phone/phone-cert.pem"
private_key = ".purrcept/phone/phone-key.pem\"""",
        key_fields=(
            ("public_url", "必填，手机可访问到的电脑 HTTPS 根地址（推荐使用 `purrcept-phone` 向导自动生成证书与配置）"),
            ("port", "HTTPS 监听端口，默认 8766（不可与 WebUI 端口冲突）"),
            ("certificate / private_key", "TLS 证书与私钥 PEM 文件路径"),
            ("pairing_enabled", "启动时是否自动开启 2 分钟扫码配对窗口，默认 false"),
        ),
    ),
    PluginGuideEntry(
        entry="computer",
        title="宿主电脑感知与命令行 (Computer)",
        capability_prefix="me.computer.*",
        extra=None,
        summary="显式连接宿主 Windows 桌面，读取前台窗口信息、截取屏幕画面或执行受控 PowerShell 命令。",
        toml_snippet="""[[plugins]]
instance_id = "computer"
entry = "computer\"""",
        key_fields=(
            ("computer_id", "设备节点标识，默认 'computer'（供 browser / meme 等子插件挂载）"),
            ("screenshot_dir", "截图保存目录，默认 '.purrcept/computer-screenshots'"),
            ("preferences", "命令与观察偏好文件路径，默认 '.purrcept/computer.json'"),
        ),
    ),
    PluginGuideEntry(
        entry="browser",
        title="网页搜索与自动化浏览 (Browser)",
        capability_prefix="me.computer.browser.*",
        extra="browser",
        summary="基于 Patchright 驱动本地 Chrome/Edge 搜索网页、提取正文与交互浏览。",
        toml_snippet="""[[plugins]]
instance_id = "browser"
entry = "browser"
[plugins.config]
computer_id = "computer"
channel = "chrome"
headless = true
search_engine = "bing\"""",
        key_fields=(
            ("computer_id", "所属电脑节点 ID，默认 'computer'"),
            ("channel", "浏览器渠道（如 'chrome'、'msedge'），默认 'chrome'"),
            ("headless", "是否无头后台运行，默认 true"),
            ("user_data_dir", "持久化浏览器 Cookie/会话目录，默认 '.purrcept/browser-profile'"),
            ("search_engine", "内置搜索解析引擎：'bing' 或 'duckduckgo'"),
        ),
    ),
    PluginGuideEntry(
        entry="meme",
        title="中文梗百科检索与日报 (Meme)",
        capability_prefix="me.computer.meme.*",
        extra="time（Windows 下使用非 UTC 时区时需要）",
        summary="检索中文热梗词条详情、管理本地收藏，并支持每日定时向主体推送新梗摘要。",
        toml_snippet="""[[plugins]]
instance_id = "meme"
entry = "meme"
[plugins.config]
computer_id = "computer"
daily_enabled = true
daily_at = "09:00"
timezone = "Asia/Shanghai\"""",
        key_fields=(
            ("daily_enabled", "是否开启每日新梗提醒，默认 true"),
            ("daily_at", "每日推送时刻（HH:MM），默认 '09:00'"),
            ("timezone", "IANA 时区名，默认 'Asia/Shanghai'"),
        ),
    ),
    PluginGuideEntry(
        entry="microphone",
        title="语音合成与实时播放 (Microphone / TTS)",
        capability_prefix="me.microphone.*",
        extra="需系统已安装 ffmpeg 与 ffplay",
        summary="调用 TTS 接口将文字合成为语音并播放，支持流式播放、声线预设后处理与联动 Live2D 口型。",
        toml_snippet="""[[plugins]]
instance_id = "microphone"
entry = "microphone"
[plugins.config]
api_base = "https://fishaudio.org/api/open/v3"
api_key = "${FISHAUDIO_API_KEY:-}"
model = "fishaudio-s21pro-flash"
voice_id = "replace-with-voice-id\"""",
        key_fields=(
            ("api_base / api_key", "TTS 服务端地址与凭据"),
            ("model / voice_id", "合成模型名称与目标音色 ID"),
            ("postprocess_profile", "可选，由 `purrcept-voice-studio` 导出的调音预设 JSON 路径"),
            ("proxy", "可选 HTTP(S) 代理地址，如 'http://127.0.0.1:7890'"),
        ),
    ),
    PluginGuideEntry(
        entry="voice_input",
        title="本地麦克风 VAD 收音与语音识别 (Voice Input)",
        capability_prefix="me.voice_input（状态与面板控制）",
        extra="voice-input（另需准备 silero_vad.onnx 模型文件）",
        summary="通过本地麦克风检测人声起止（支持说话自动打断 TTS 播报），经实时 ASR 转写或原声直传主脑。",
        toml_snippet="""[[plugins]]
instance_id = "voice_input"
entry = "voice_input"
[plugins.config]
vad_model_path = ".purrcept/voice-input/silero_vad.onnx"
asr_url = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
asr_api_key = "${DASHSCOPE_API_KEY:-}"
asr_model = "fun-asr-realtime"
vad_interrupt_enabled = true""",
        key_fields=(
            ("vad_model_path", "Silero VAD ONNX 模型路径，默认 '.purrcept/voice-input/silero_vad.onnx'"),
            ("asr_url / asr_api_key / asr_model", "实时语音识别 WebSocket 地址、API Key 与模型名"),
            ("vad_interrupt_enabled", "检测到持续说话时是否自动打断正在播放的语音，默认 true"),
        ),
    ),
    PluginGuideEntry(
        entry="live2d",
        title="VTube Studio 身体与表情驱动 (Live2D)",
        capability_prefix="me.live2d.*",
        extra="live2d",
        summary="连接本机 VTube Studio WebSocket 接口，根据情绪、注视、动作意图与语音韵律实时驱动皮套。",
        toml_snippet="""[[plugins]]
instance_id = "live2d"
entry = "live2d"
[plugins.config]
vts_url = "ws://127.0.0.1:8001"
profile_path = ".purrcept/live2d/profile.json\"""",
        key_fields=(
            ("vts_url", "本机 VTube Studio API 地址，默认 'ws://127.0.0.1:8001'"),
            ("profile_path", "必填，模型参数绑定与校准 JSON 路径"),
            ("preferences_path", "可选，由 `python -m purrcept_engine.app.live2d_studio` 调节的姿态偏好路径"),
        ),
    ),
)


def render_plugin_guide(target: str | None = None) -> str:
    """渲染全部内置插件目录，或指定插件的详细配置说明与可直接复制的 TOML 片段。"""
    if target is None or not target.strip():
        lines = [
            "Purrcept Engine 内置插件配置速查目录",
            "用法：在 purrcept.toml 中添加对应的 [[plugins]] 段落即可启用。",
            "运行 `purrcept-engine plugins <插件名>`（例如 `purrcept-engine plugins qq`）查看完整配置示例。\n",
        ]
        for item in PLUGIN_GUIDES:
            dep = f"额外依赖: {item.extra}" if item.extra else "无额外依赖"
            lines.append(f"  - {item.entry:<12} {item.title} [{item.capability_prefix}]（{dep}）")
            lines.append(f"                 {item.summary}")
        return "\n".join(lines)

    key = target.strip().lower()
    matched = next((item for item in PLUGIN_GUIDES if item.entry == key), None)
    if matched is None:
        available = ", ".join(item.entry for item in PLUGIN_GUIDES)
        raise ConfigError(f"未知的内置插件名称 '{target}'；可选内置插件：{available}")

    dep_line = matched.extra if matched.extra else "无（开箱即用）"
    field_lines = "\n".join(f"  - {name}: {desc}" for name, desc in matched.key_fields)
    return (
        f"插件：{matched.title} (entry = \"{matched.entry}\")\n"
        f"能力路径：{matched.capability_prefix}\n"
        f"依赖要求：{dep_line}\n"
        f"功能简介：{matched.summary}\n\n"
        f"核心配置字段：\n{field_lines}\n\n"
        f"可直接复制到 purrcept.toml 末尾的示例：\n\n{matched.toml_snippet}"
    )


def _toml_quote(value: str) -> str:
    """将普通字符串编码为安全的 TOML 双引号字面量。"""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _ask(
    prompt: str,
    default: str,
    *,
    input_fn: Callable[[str], str],
    output: TextIO,
) -> str:
    output.write(f"{prompt} [{default}]: ")
    output.flush()
    try:
        answer = input_fn("")
    except EOFError:
        output.write("\n")
        return default
    cleaned = answer.strip()
    return cleaned if cleaned else default


def _ask_yes_no(
    prompt: str,
    *,
    default: bool,
    input_fn: Callable[[str], str],
    output: TextIO,
) -> bool:
    hint = "Y/n" if default else "y/N"
    output.write(f"{prompt} [{hint}]: ")
    output.flush()
    try:
        answer = input_fn("").strip().lower()
    except EOFError:
        output.write("\n")
        return default
    if not answer:
        return default
    return answer in {"y", "yes", "1", "true", "是"}


def _web_extra_installed() -> bool:
    return all(importlib.util.find_spec(mod) is not None for mod in ("starlette", "uvicorn"))


def _tzdata_available() -> bool:
    if sys.platform != "win32":
        return True
    return importlib.util.find_spec("tzdata") is not None


def run_interactive_init(
    config_path: Path,
    *,
    input_fn: Callable[[str], str] = input,
    output: TextIO = sys.stdout,
) -> bytes:
    """运行交互式命令行初始化向导，返回校验通过的 UTF-8 TOML 配置字节。"""
    output.write("\n=== Purrcept Engine 初始化配置向导 ===\n")
    output.write("按回车可直接采用方括号内的默认值；随时可按 Ctrl+C 取消。\n\n")
    output.write("请选择模型接入方式：\n")
    output.write("  1) 云端 OpenAI 兼容 API（DeepSeek / 通义千问百炼 / OpenAI / OpenRouter 等）\n")
    output.write("  2) 本地免密模型服务（Ollama / vLLM / LM Studio，无需 API Key）\n")
    output.write("  3) 跳过问答，直接生成带完整插件注释的标准模板文件\n")

    mode = _ask("输入选项编号 (1/2/3)", "1", input_fn=input_fn, output=output)
    if mode == "3":
        return files("purrcept_engine.app").joinpath("default.toml").read_bytes()

    is_local = mode == "2"
    default_base = "http://127.0.0.1:11434/v1" if is_local else "https://api.deepseek.com/v1"
    default_model = "openai/qwen2.5:14b" if is_local else "openai/deepseek-chat"

    api_base = _ask("模型接口地址 (api_base)", default_base, input_fn=input_fn, output=output)
    raw_model = _ask("模型名称 (model)", default_model, input_fn=input_fn, output=output)
    model_name = raw_model if "/" in raw_model else f"openai/{raw_model}"
    if model_name != raw_model:
        output.write(f"  -> 已自动补全协议前缀：{model_name}\n")

    if is_local:
        api_key = _ask(
            "接口密钥（本地服务可直接回车留空免密）", "", input_fn=input_fn, output=output
        )
    else:
        api_key = _ask(
            "接口密钥（推荐填环境变量如 ${OPENAI_API_KEY}，或直接填密钥）",
            "${OPENAI_API_KEY}",
            input_fn=input_fn,
            output=output,
        )

    char_name = _ask("给你的 AI 伙伴起个名字", "小咕", input_fn=input_fn, output=output)
    char_line = _ask(
        "用一句话描述她的身份",
        "住在你电脑里的连续在线 AI 伙伴。",
        input_fn=input_fn,
        output=output,
    )

    enable_starter_plugins = _ask_yes_no(
        "是否默认开启基础随身插件（Notebook 笔记记忆 + Bed 自主休息"
        + (" + Clock 时钟" if _tzdata_available() else "")
        + "）？",
        default=True,
        input_fn=input_fn,
        output=output,
    )

    web_ready = _web_extra_installed()
    enable_web = False
    if web_ready:
        enable_web = _ask_yes_no(
            "检测到已安装 Web 面板依赖，是否默认开启本机 WebUI (端口 8765)？",
            default=True,
            input_fn=input_fn,
            output=output,
        )

    sections: list[str] = [
        "# 由 `purrcept-engine init` 向导生成",
        "# 查看全部内置插件配置说明：`uv run purrcept-engine plugins`",
        "",
        "console = true",
        "",
        "[host]",
        'data_dir = ".purrcept-engine"',
        "",
        "[web]",
        f"enabled = {'true' if enable_web else 'false'}",
        "port = 8765",
        "",
        "[logging]",
        'level = "INFO"',
        "",
        "[models.main]",
        f"model = {_toml_quote(model_name)}",
        f"api_base = {_toml_quote(api_base)}",
        f"api_key = {_toml_quote(api_key)}",
        "context_window = 64000",
        "max_output_tokens = 4096",
        'supports_contents = ["text"]',
        "",
        "[subject]",
        'foreground = "main"',
        'reflection = "main"',
        'attention = "main"',
        "proactive_thinking = false",
        "proactive_idle_seconds = 300.0",
        "",
        "[subject.character]",
        f"name = {_toml_quote(char_name)}",
        f"one_line = {_toml_quote(char_line)}",
        'speech_style = "用自然、简短的中文交流，像熟人聊天一样直接。"',
    ]

    if enable_starter_plugins:
        sections.extend(
            [
                "",
                "[[plugins]]",
                'instance_id = "notebook"',
                'entry = "notebook"',
                "[plugins.config]",
                'notebook_dir = "notes"',
                'skills_dir = "skills"',
                "seed_examples = true",
                "",
                "[[plugins]]",
                'instance_id = "bed"',
                'entry = "bed"',
            ]
        )
        if _tzdata_available():
            sections.extend(
                [
                    "",
                    "[[plugins]]",
                    'instance_id = "clock"',
                    'entry = "clock"',
                    "[plugins.config]",
                    'timezone = "Asia/Shanghai"',
                ]
            )

    sections.append("")
    rendered = "\n".join(sections)

    # 校验结构合法性（若用户使用了尚未设置的 ${ENV_VAR}，用临时占位字典验证其余全部字段无误）
    raw_parsed = tomllib.loads(rendered)
    validation_env = {"OPENAI_API_KEY": "placeholder-for-init-validation"}
    if api_key.startswith("${") and api_key.endswith("}"):
        env_key = api_key[2:-1].split(":-", 1)[0].strip()
        if env_key:
            validation_env[env_key] = "placeholder-for-init-validation"
    EngineConfig.validate(
        raw_parsed,
        base_dir=config_path.resolve().parent,
        interpolate_env=True,
        environ=validation_env,
    )
    return rendered.encode("utf-8")
