"""声明、插值并校验宿主的启动配置。

支持从 TOML 文件或 Python 字典加载配置，提供以下人性化能力：
1. 字符串字段支持 ``${ENV_VAR}`` 与 ``${ENV_VAR:-默认值}`` 环境变量插值（``$$`` 转义字面量 ``$``）。
2. 本机回环与局域网模型服务（如 Ollama、vLLM、LM Studio）可省略 ``api_key`` 或留空。
3. 模型默认 ``kind = "generation"`` 及合理的上下文窗口默认值；``subject.reflection`` 未填时默认复用 ``foreground``。
4. TOML 语法报错直接指出具体行列号与常见冲突提示，敏感凭据自动脱敏。
"""

from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal, Self, TypeGuard
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from purrcept_engine.domain.execution.settings import ExecutionSettings
from purrcept_engine.domain.model_requests.contents import ContentKind, validate_supported_contents
from purrcept_engine.errors import ConfigError
from purrcept_engine.infrastructure.logging import redact_secrets
from purrcept_engine.values import JsonObject, decode_object, encode_json

__all__ = [
    "ActivitySettings",
    "CharacterSettings",
    "ContextSettings",
    "DecisionModelSettings",
    "DiagnosticSettings",
    "EngineConfig",
    "ExecutionSettings",
    "GenerationModelSettings",
    "HostSettings",
    "LogLevel",
    "LoggingSettings",
    "PluginSettings",
    "RetrySettings",
    "SubjectSettings",
    "TurnSettings",
    "WebSettings",
    "expand_env_vars",
    "load_config",
]

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveInt = Annotated[int, Field(gt=0)]
PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Fraction = Annotated[float, Field(gt=0, lt=1, allow_inf_nan=False)]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

_ENV_TOKEN = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_TOML_LINE_COL = re.compile(r"\(at line (\d+),\s*column (\d+)\)")
_LOCAL_NO_AUTH_PLACEHOLDER = "local-no-auth"


def _is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_tuple(value: object) -> TypeGuard[tuple[object, ...]]:
    return isinstance(value, tuple)


def _is_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


def _is_local_or_private_endpoint(api_base: str) -> bool:
    """判断模型端点是否为本机回环或局域网私有地址（允许免密访问）。"""
    try:
        hostname = (urlsplit(api_base).hostname or "").strip().lower()
    except ValueError:
        return False
    if not hostname:
        return False
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".local", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def expand_env_vars(
    value: object,
    *,
    environ: Mapping[str, str] | None = None,
    location: str = "config",
) -> object:
    """递归展开配置字符串中的 ``${VAR}`` 与 ``${VAR:-default}`` 环境变量。

    Args:
        value: 解析后的原始 TOML 或字典配置值。
        environ: 环境变量映射，默认读取 ``os.environ``。
        location: 当前字段路径，用于在缺少环境变量时生成精确定位报错。

    Returns:
        完成环境变量替换的新配置结构，不修改原输入容器。

    Raises:
        ConfigError: 引用了未设置且未提供默认值的环境变量。
    """
    env = os.environ if environ is None else environ
    if isinstance(value, str):
        if "$" not in value:
            return value

        def _replace(match: re.Match[str]) -> str:
            if match.group(0) == "$$":
                return "$"
            var_name = match.group(1)
            fallback = match.group(2)
            resolved = env.get(var_name)
            if resolved is not None and resolved != "":
                return resolved
            if fallback is not None:
                return fallback
            if resolved == "":
                raise ConfigError(
                    f"{location}: 环境变量 ${{{var_name}}} 当前为空；"
                    f"请设置有效值或使用 ${{{var_name}:-默认值}}。"
                )
            raise ConfigError(
                f"{location}: 引用了未设置的环境变量 ${{{var_name}}}；"
                f"请先在终端设置该环境变量，或使用 ${{{var_name}:-默认值}}。"
            )

        return _ENV_TOKEN.sub(_replace, value)
    if _is_mapping(value):
        expanded: dict[object, object] = {}
        for key, item in value.items():
            child_loc = f"{location}.{key}" if location != "config" else str(key)
            expanded[key] = expand_env_vars(item, environ=environ, location=child_loc)
        return expanded
    if _is_list(value):
        return [
            expand_env_vars(item, environ=environ, location=f"{location}[{idx}]")
            for idx, item in enumerate(value)
        ]
    if _is_tuple(value):
        return tuple(
            expand_env_vars(item, environ=environ, location=f"{location}[{idx}]")
            for idx, item in enumerate(value)
        )
    return value


class _Settings(BaseModel):
    """配置基类：冻结实例、禁止未知拼写错误字段并隐藏报错中的原始凭据值。"""

    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        hide_input_in_errors=True,
        revalidate_instances="always",
    )


class RetrySettings(_Settings):
    """单次逻辑模型请求的重试次数与超时上限。"""

    attempts: PositiveInt = 2
    attempt_seconds: PositiveSeconds = 120.0
    total_seconds: PositiveSeconds = 180.0


class TurnSettings(_Settings):
    """单轮连续思考的最大模型请求轮数与总时长上限。"""

    max_requests: PositiveInt = 8
    total_seconds: PositiveSeconds = 600.0


class HostSettings(_Settings):
    """宿主数据目录与优雅关闭等待时限。"""

    data_dir: Path = Path(".purrcept-engine")
    close_seconds: PositiveSeconds = 30.0

    @field_validator("data_dir", mode="before")
    @classmethod
    def _path_from_toml(cls, value: object) -> object:
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("数据目录路径不能为空")
            return Path(value)
        return value


class DiagnosticSettings(_Settings):
    """SQLite 内模型请求/响应诊断正文的保留天数与容量上限。"""

    retention_days: PositiveInt = 7
    retained_bytes: PositiveInt = 1_073_741_824
    max_bytes: Annotated[int, Field(ge=0)] = 1_048_576


class LoggingSettings(_Settings):
    """终端与滚动文件日志的级别与保留策略。"""

    level: LogLevel = "INFO"
    retention_days: PositiveInt = 7
    max_bytes: Annotated[int, Field(ge=64)] = 10_485_760
    max_files: PositiveInt = 7

    @field_validator("level", mode="before")
    @classmethod
    def _normalize_level(cls, value: object) -> object:
        if isinstance(value, str):
            upper = value.strip().upper()
            return "WARNING" if upper == "WARN" else upper
        return value


class _ModelSettings(_Settings):
    """模型连接公共字段；本机与局域网服务可留空 api_key。"""

    model: NonEmptyString
    api_base: NonEmptyString
    api_key: SecretStr = SecretStr("")
    supports_contents: tuple[ContentKind, ...] = ("text",)

    @field_validator("supports_contents", mode="before")
    @classmethod
    def _contents_from_toml(cls, value: object) -> object:
        return tuple(value) if _is_list(value) else value

    @field_validator("supports_contents")
    @classmethod
    def _supported_contents(cls, value: tuple[ContentKind, ...]) -> tuple[ContentKind, ...]:
        validate_supported_contents(value)
        return value

    @field_validator("api_base")
    @classmethod
    def _explicit_endpoint(cls, value: str) -> str:
        try:
            parts = urlsplit(value)
            valid = (
                parts.scheme in {"http", "https"}
                and parts.hostname is not None
                and parts.username is None
                and parts.password is None
                and not parts.query
                and not parts.fragment
            )
            _ = parts.port
        except ValueError:
            raise ValueError(
                "api_base 必须是完整的 http(s):// 地址，且不能内嵌账号密码"
            ) from None
        if not valid:
            raise ValueError("api_base 必须是完整的 http(s):// 地址，且不能内嵌账号密码")
        return value

    @model_validator(mode="after")
    def _check_or_fill_credential(self) -> Self:
        raw_key = self.api_key.get_secret_value().strip()
        if not raw_key:
            if _is_local_or_private_endpoint(self.api_base):
                object.__setattr__(self, "api_key", SecretStr(_LOCAL_NO_AUTH_PLACEHOLDER))
            else:
                raise ValueError(
                    "远程模型端点必须提供非空 api_key"
                    "（支持 ${ENV_VAR} 引用环境变量；仅本机或局域网免密服务可留空）"
                )
        return self


class GenerationModelSettings(_ModelSettings):
    """对话生成模型配置（默认上下文窗口 64000、最大输出 4096）。"""

    kind: Literal["generation"] = "generation"
    context_window: PositiveInt = 64000
    max_output_tokens: PositiveInt = 4096
    input_limit: PositiveInt | None = None

    @model_validator(mode="after")
    def _output_fits_window(self) -> Self:
        if self.max_output_tokens >= self.context_window:
            raise ValueError(
                f"max_output_tokens ({self.max_output_tokens}) "
                f"必须小于 context_window ({self.context_window})"
            )
        return self


class DecisionModelSettings(_ModelSettings):
    """专用快速判断模型配置（如 TypeSafe / OpenRouter Decisions 协议）。"""

    kind: Literal["decision"]
    input_limit: PositiveInt


ModelSettings = Annotated[
    GenerationModelSettings | DecisionModelSettings,
    Field(discriminator="kind"),
]


class CharacterSettings(_Settings):
    """角色身份设定六要素（空字符串自动视为未设置）。"""

    name: str | None = None
    one_line: str | None = None
    persona: str | None = None
    relationship: str | None = None
    speech_style: str | None = None
    notes: str | None = None

    @field_validator(
        "name", "one_line", "persona", "relationship", "speech_style", "notes", mode="before"
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return None if value == "" else value

    def record(self) -> dict[str, str | None]:
        """导出固定顺序的角色卡字典。"""
        return {
            "name": self.name,
            "one_line": self.one_line,
            "persona": self.persona,
            "relationship": self.relationship,
            "speech_style": self.speech_style,
            "notes": self.notes,
        }


class SubjectSettings(_Settings):
    """主脑、反思与注意的模型绑定及调度预算；省略 reflection 时默认复用 foreground。"""

    foreground: NonEmptyString
    reflection: NonEmptyString
    attention: NonEmptyString | None = None
    foreground_retry: RetrySettings = Field(default_factory=RetrySettings)
    reflection_retry: RetrySettings = Field(default_factory=RetrySettings)
    summary_retry: RetrySettings = Field(default_factory=RetrySettings)
    attention_retry: RetrySettings = Field(
        default_factory=lambda: RetrySettings(attempt_seconds=5.0, total_seconds=8.0)
    )
    foreground_turn: TurnSettings = Field(default_factory=TurnSettings)
    reflection_turn: TurnSettings = Field(default_factory=TurnSettings)
    character: CharacterSettings = Field(default_factory=CharacterSettings)
    attention_batch_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.1
    attention_batch_size: PositiveInt = 8
    proactive_thinking: bool = False
    proactive_idle_seconds: PositiveSeconds = 300.0
    reflection_batch_seconds: PositiveSeconds = 60.0
    reflection_batch_turns: PositiveInt = 8
    behavior_review_enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def _default_reflection_to_foreground(cls, value: object) -> object:
        if _is_mapping(value) and "foreground" in value and "reflection" not in value:
            copied: dict[object, object] = dict(value)
            copied["reflection"] = copied["foreground"]
            return copied
        return value


class ContextSettings(_Settings):
    """上下文窗口自动摘要维护比例与近期轮次保护阈值。"""

    summary_trigger: Fraction = 0.8
    summary_target: Fraction = 0.5
    input_reserve_fraction: Annotated[float, Field(ge=0, lt=1, allow_inf_nan=False)] = 0.1
    protected_turns: PositiveInt = 2
    page_chars: PositiveInt = 8000

    @model_validator(mode="after")
    def _summary_target_precedes_trigger(self) -> Self:
        if self.summary_target >= self.summary_trigger:
            raise ValueError("summary_target（目标比例）必须小于 summary_trigger（触发比例）")
        return self


class ActivitySettings(_Settings):
    """后台持续目标（Activity）并发上限与运行时间额度。"""

    max_active: PositiveInt = 8
    initial_seconds: PositiveSeconds = 3600.0
    max_remaining_seconds: PositiveSeconds = 3600.0
    checkpoint_seconds: PositiveSeconds = 5.0

    @model_validator(mode="after")
    def _initial_grant_fits_balance(self) -> Self:
        if self.initial_seconds > self.max_remaining_seconds:
            raise ValueError("initial_seconds 不能超过 max_remaining_seconds")
        return self


class PluginSettings(_Settings):
    """单个插件实例的启用声明及子配置快照。"""

    model_config = ConfigDict(revalidate_instances="never")

    instance_id: NonEmptyString
    entry: NonEmptyString
    enabled: bool = True
    config_json: str = Field(default="{}", alias="config", repr=False, exclude=True)

    @field_validator("config_json", mode="before")
    @classmethod
    def _freeze_configuration(cls, value: object) -> str:
        encoded = encode_json(value)
        decode_object(encoded)
        return encoded.decode("utf-8")

    def configuration(self) -> JsonObject:
        """返回插件配置对象的独立字典副本，由对应插件的 Settings 模型二次校验。"""
        return decode_object(self.config_json.encode("utf-8"))


class WebSettings(_Settings):
    """本机 Web 后台面板监听配置。"""

    enabled: bool = False
    port: Annotated[int, Field(ge=0, le=65535)] = 8765


class EngineConfig(_Settings):
    """完整的宿主不可变启动配置快照。"""

    host: HostSettings = Field(default_factory=HostSettings)
    models: Mapping[NonEmptyString, ModelSettings]
    subject: SubjectSettings
    context: ContextSettings = Field(default_factory=ContextSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    activities: ActivitySettings = Field(default_factory=ActivitySettings)
    diagnostics: DiagnosticSettings = Field(default_factory=DiagnosticSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    console: bool = True
    web: WebSettings = Field(default_factory=WebSettings)
    plugins: tuple[PluginSettings, ...] = ()
    _plugin_base_dir: Path | None = PrivateAttr(default=None)

    @field_validator("models", mode="before")
    @classmethod
    def _default_model_kind(cls, value: object) -> object:
        """未显式声明 kind 的模型表默认按 generation（对话生成模型）解析。"""
        if not _is_mapping(value):
            return value
        normalized: dict[object, object] = {}
        for name, item in value.items():
            if _is_mapping(item) and "kind" not in item:
                entry: dict[object, object] = dict(item)
                entry["kind"] = "generation"
                normalized[name] = entry
            else:
                normalized[name] = item
        return normalized

    @field_validator("plugins", mode="before")
    @classmethod
    def _plugin_list(cls, value: object) -> object:
        return tuple(value) if _is_list(value) else value

    @model_validator(mode="after")
    def _check_model_references(self) -> Self:
        declared = ", ".join(sorted(self.models.keys())) or "无"
        for purpose, name in (
            ("foreground", self.subject.foreground),
            ("reflection", self.subject.reflection),
        ):
            model = self.models.get(name)
            if not isinstance(model, GenerationModelSettings):
                raise ValueError(
                    f"subject.{purpose} 引用了 '{name}'，"
                    f"但它不是已声明的生成模型（当前已声明：{declared}）"
                )
        if self.subject.attention is not None and self.subject.attention not in self.models:
            raise ValueError(
                f"subject.attention 引用了未声明的模型 '{self.subject.attention}'"
                f"（当前已声明：{declared}）"
            )
        instance_ids = [item.instance_id for item in self.plugins]
        if len(set(instance_ids)) != len(instance_ids):
            raise ValueError("plugins 中存在重复的 instance_id，请确保每个插件实例名称唯一")

        object.__setattr__(self, "models", MappingProxyType(dict(self.models)))
        return self

    @field_serializer("models")
    def _serialize_models(self, value: Mapping[str, ModelSettings]) -> dict[str, ModelSettings]:
        return dict(value)

    @classmethod
    def validate(
        cls,
        value: object,
        *,
        base_dir: Path | None = None,
        interpolate_env: bool = False,
        environ: Mapping[str, str] | None = None,
    ) -> Self:
        """校验配置字典并按基准目录解析相对路径。

        Args:
            value: 待校验的原始配置字典。
            base_dir: 解析 ``host.data_dir`` 与插件相对路径的绝对基准目录。
            interpolate_env: 是否在校验前展开 ``${ENV_VAR}`` 环境变量占位符。
            environ: 可选的自定义环境变量映射（默认读取 ``os.environ``）。

        Returns:
            已完成校验与路径解析的不可变 ``EngineConfig`` 实例。

        Raises:
            ConfigError: 字段缺失、类型不匹配、引用关系错误或环境变量未定义。
        """
        prepared = expand_env_vars(value, environ=environ) if interpolate_env else value
        try:
            config = cls.model_validate(prepared)
        except ValidationError as error:
            issues = error.errors(include_input=False, include_context=False, include_url=False)
            descriptions: list[str] = []
            for issue in issues:
                location = ".".join(str(part) for part in issue["loc"]) or "config"
                if location == "knowledge":
                    descriptions.append(
                        "knowledge 已移除；向量配置请写在 notebook 插件的 [plugins.config.embedding] 中。"
                    )
                elif location.startswith("subject.memory_"):
                    descriptions.append(
                        f"{location} 已移除；自动记忆整理请写在 "
                        "notebook 插件的 [plugins.config.consolidation] 中。"
                    )
                else:
                    descriptions.append(f"{location}: {issue['msg']}")
            raise ConfigError("配置校验未通过：\n  - " + "\n  - ".join(descriptions)) from None
        return config.resolve(base_dir=base_dir)

    def resolve(self, *, base_dir: Path | None = None) -> Self:
        """按给定基准目录将相对路径解析为绝对路径。"""
        data_dir = self.host.data_dir
        if not data_dir.is_absolute():
            if base_dir is None or not base_dir.is_absolute():
                raise ConfigError("host.data_dir 为相对路径时必须提供绝对 base_dir")
            data_dir = base_dir / data_dir

        resolved = type(self)(
            host=HostSettings(data_dir=data_dir.resolve(), close_seconds=self.host.close_seconds),
            models=self.models,
            subject=self.subject,
            context=self.context,
            execution=self.execution,
            activities=self.activities,
            diagnostics=self.diagnostics,
            logging=self.logging,
            console=self.console,
            web=self.web,
            plugins=self.plugins,
        )
        plugin_base_dir = base_dir if base_dir is not None else self._plugin_base_dir
        if plugin_base_dir is not None and not plugin_base_dir.is_absolute():
            raise ConfigError("plugins: base_dir 必须是绝对路径")
        if self.plugins and plugin_base_dir is None:
            raise ConfigError("plugins: 程序式插件配置必须提供绝对 base_dir")
        resolved._plugin_base_dir = plugin_base_dir
        return resolved

    @property
    def plugin_base_dir(self) -> Path:
        """插件解析相对资源路径所用的基准目录。"""
        if self._plugin_base_dir is not None:
            return self._plugin_base_dir
        return self.host.data_dir


def _detect_toml_conflict_hint(raw_bytes: bytes) -> str:
    """检测常见的 TOML 新手陷阱（如同时保留了 plugins = [] 与 [[plugins]]）。"""
    try:
        text = raw_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return ""
    has_empty_array = bool(re.search(r"(?m)^\s*plugins\s*=\s*\[", text))
    has_array_table = bool(re.search(r"(?m)^\s*\[\[plugins\]\]", text))
    if has_empty_array and has_array_table:
        return (
            "（提示：文件中同时出现了顶层 `plugins = []` 和 `[[plugins]]`，"
            "请删除顶部的 `plugins = []`）"
        )
    return ""


def load_config(path: Path, *, environ: Mapping[str, str] | None = None) -> EngineConfig:
    """读取并校验 TOML 配置文件，自动展开 ``${ENV_VAR}`` 并以文件所在目录解析路径。

    Args:
        path: TOML 配置文件路径。
        environ: 可选的环境变量映射，默认使用 ``os.environ``。

    Returns:
        已解析路径并通过完整校验的 ``EngineConfig``。

    Raises:
        ConfigError: 文件无法读取、TOML 语法错误（附行列号）或配置字段校验失败。
    """
    config_path = path.absolute()
    try:
        raw_bytes = config_path.read_bytes()
    except FileNotFoundError:
        raise ConfigError(
            f"找不到配置文件：{config_path}（可先运行 `purrcept-engine init` 向导生成配置）"
        ) from None
    except OSError as error:
        detail = error.strerror or type(error).__name__
        raise ConfigError(f"无法读取配置文件 {config_path}：{detail}") from None

    try:
        raw_config = tomllib.loads(raw_bytes.decode("utf-8-sig"))
    except UnicodeDecodeError as error:
        raise ConfigError(
            f"配置文件 {config_path.name} 不是有效的 UTF-8 编码（字节偏移 {error.start}）"
        ) from None
    except tomllib.TOMLDecodeError as error:
        raw_text = redact_secrets(str(error)).strip()
        lineno = getattr(error, "lineno", None)
        colno = getattr(error, "colno", None)
        loc_match = _TOML_LINE_COL.search(raw_text)
        if (lineno is None or colno is None) and loc_match is not None:
            lineno = int(loc_match.group(1))
            colno = int(loc_match.group(2))
            msg = raw_text[: loc_match.start()].strip() or "语法格式不正确"
        else:
            raw_msg = getattr(error, "msg", "")
            msg = redact_secrets(raw_msg) if isinstance(raw_msg, str) and raw_msg else raw_text
        pos = (
            f"第 {lineno} 行第 {colno} 列"
            if isinstance(lineno, int) and isinstance(colno, int)
            else "未知位置"
        )
        hint = _detect_toml_conflict_hint(raw_bytes)
        raise ConfigError(
            f"配置文件 {config_path.name} {pos}存在 TOML 语法错误：{msg}{hint}"
        ) from None

    return EngineConfig.validate(
        raw_config,
        base_dir=config_path.parent,
        interpolate_env=True,
        environ=environ,
    )
