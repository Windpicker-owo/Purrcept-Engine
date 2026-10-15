"""声明并校验宿主的不可变启动配置，不启动模型、插件或设备。

TOML 与程序式调用共同经过 ``EngineConfig.validate``；交叉引用和额度关系
在配置边界核验，再以显式基准目录解析路径。模型名称不决定容量规则或媒体支持，
规则标识由后续模型适配边界核对。插件配置保留为不可变 JSON，纯声明与依赖
核验由 app.assembly 完成，配置本身不构造资源。
"""

from __future__ import annotations

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

from purrcept_engine.domain.knowledge.settings import EmbeddingSettings
from purrcept_engine.errors import ConfigError
from purrcept_engine.execution.settings import ExecutionSettings
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
    "KnowledgeSettings",
    "LoggingSettings",
    "PluginSettings",
    "RetrySettings",
    "SubjectSettings",
    "TurnSettings",
    "WebSettings",
    "load_config",
]

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveInt = Annotated[int, Field(gt=0)]
PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Fraction = Annotated[float, Field(gt=0, lt=1, allow_inf_nan=False)]


def _is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


class _Settings(BaseModel):
    """在配置边界拒绝未知字段与隐式类型转换，构造后不允许修改。"""

    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        hide_input_in_errors=True,
        revalidate_instances="always",
    )


class RetrySettings(_Settings):
    """约束一个逻辑请求；总尝试数含首次，所有等待均消耗总时间。"""

    attempts: PositiveInt = 2
    attempt_seconds: PositiveSeconds = 120.0
    total_seconds: PositiveSeconds = 180.0


class TurnSettings(_Settings):
    """约束一次连续处理批次，传输重试不增加逻辑请求轮数。"""

    max_requests: PositiveInt = 8
    total_seconds: PositiveSeconds = 600.0


class HostSettings(_Settings):
    """拥有权威数据目录及同一次正常关闭的等待期限。"""

    data_dir: Path = Path(".purrcept-engine")
    close_seconds: PositiveSeconds = 30.0

    @field_validator("data_dir", mode="before")
    @classmethod
    def _path_from_toml(cls, value: object) -> object:
        """TOML 路径采用字符串表示，不因此放宽其余字段的严格类型。"""
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("数据目录不能为空")
            return Path(value)
        return value


class DiagnosticSettings(_Settings):
    """控制详细模型流诊断的容量与时间，权威请求和原始参数不受此清理。"""

    retention_days: PositiveInt = 7
    max_bytes: Annotated[int, Field(ge=0)] = 1_048_576


class LoggingSettings(_Settings):
    """独立CLI普通文本日志的有限轮转，嵌入宿主不配置调用者handlers。"""

    retention_days: PositiveInt = 7
    max_bytes: Annotated[int, Field(ge=64)] = 10_485_760
    max_files: PositiveInt = 7


class _ModelSettings(_Settings):
    """显式选择端点、凭据及计量依据；实际传输来源由适配验证。"""

    model: NonEmptyString
    api_base: NonEmptyString
    api_key: SecretStr
    capacity_rule: NonEmptyString
    media_rule: NonEmptyString = "unsupported"

    @field_validator("api_key")
    @classmethod
    def _nonempty_credential(cls, value: SecretStr) -> SecretStr:
        """首批接入范围要求明确凭据；空字符串不是禁用环境继承的方法。"""
        if not value.get_secret_value().strip():
            raise ValueError("首批模型接入必须显式提供非空凭据")
        return value

    @field_validator("api_base")
    @classmethod
    def _explicit_endpoint(cls, value: str) -> str:
        """拒绝不完整端点和 URL 内凭据，避免配置隐式选用另一传输来源。"""
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
            # URL 的端口校验不会访问端点，但必须在传输资源构造之前完成。
            _ = parts.port
        except ValueError:
            raise ValueError("端点必须是完整 HTTP(S) URL，且不能内嵌凭据") from None
        if not valid:
            raise ValueError("端点必须是完整 HTTP(S) URL，且不能内嵌凭据")
        return value


class GenerationModelSettings(_ModelSettings):
    """声明生成模型的总窗口和输出预留；具体计量适用性由适配边界核验。"""

    kind: Literal["generation"]
    context_window: PositiveInt
    max_output_tokens: PositiveInt
    input_limit: PositiveInt | None = None
    supports_audio: bool = False

    @model_validator(mode="after")
    def _output_fits_window(self) -> Self:
        """共享窗口必须仍为完整输入留下正额度。"""
        if self.max_output_tokens >= self.context_window:
            raise ValueError("生成输出额度必须小于模型窗口")
        return self


class DecisionModelSettings(_ModelSettings):
    """声明专用判断的协议输入上限，不接受生成采样或输出预算字段。"""

    kind: Literal["decision"]
    input_limit: PositiveInt


ModelSettings = Annotated[
    GenerationModelSettings | DecisionModelSettings,
    Field(discriminator="kind"),
]


class SubjectSettings(_Settings):
    """引用命名模型并拥有各用途预算，不在请求失败时选择替代模型。"""

    foreground: NonEmptyString
    reflection: NonEmptyString
    summary: NonEmptyString
    attention: NonEmptyString | None = None
    foreground_retry: RetrySettings = Field(default_factory=RetrySettings)
    reflection_retry: RetrySettings = Field(default_factory=RetrySettings)
    summary_retry: RetrySettings = Field(default_factory=RetrySettings)
    attention_retry: RetrySettings = Field(
        default_factory=lambda: RetrySettings(attempt_seconds=5.0, total_seconds=8.0)
    )
    foreground_turn: TurnSettings = Field(default_factory=TurnSettings)
    reflection_turn: TurnSettings = Field(default_factory=TurnSettings)
    character: CharacterSettings = Field(default_factory=lambda: CharacterSettings())
    attention_batch_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.1
    attention_batch_size: PositiveInt = 8
    attention_defer_seconds: PositiveSeconds = 30.0
    reflection_batch_seconds: PositiveSeconds = 60.0
    reflection_batch_turns: PositiveInt = 8


class CharacterSettings(_Settings):
    """保留固定身份六字段及顺序，不从认识恢复，也不建立人员权限。

    空字符串转为缺省 None；其余文字保留原有空白，不能在配置重写中改变
    模型可见的人格。固定前台指令沿用 Python 字典表示及全部 None 字段。
    """

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
        """返回旧固定提示中的同序六字段，缺省值不会被序列化时省略。"""
        return {
            "name": self.name,
            "one_line": self.one_line,
            "persona": self.persona,
            "relationship": self.relationship,
            "speech_style": self.speech_style,
            "notes": self.notes,
        }


class ContextSettings(_Settings):
    """按最终可用输入预算触发维护，保护完整轮次并限制单页正文。"""

    summary_trigger: Fraction = 0.8
    summary_target: Fraction = 0.5
    input_reserve_fraction: Annotated[float, Field(ge=0, lt=1, allow_inf_nan=False)] = 0.1
    protected_turns: PositiveInt = 2
    page_chars: PositiveInt = 8000

    @model_validator(mode="after")
    def _summary_target_precedes_trigger(self) -> Self:
        """维护目标必须低于触发值；未达到目标不等于候选历史无效。"""
        if self.summary_target >= self.summary_trigger:
            raise ValueError("摘要目标比例必须低于触发比例")
        return self


class ActivitySettings(_Settings):
    """约束未结束活动数及未来运行额度；暂停和离线时间不扣减。"""

    max_active: PositiveInt = 8
    initial_seconds: PositiveSeconds = 3600.0
    max_remaining_seconds: PositiveSeconds = 3600.0
    checkpoint_seconds: PositiveSeconds = 5.0

    @model_validator(mode="after")
    def _initial_grant_fits_balance(self) -> Self:
        """初次授予不能超过任意时刻允许持有的未来运行余额。"""
        if self.initial_seconds > self.max_remaining_seconds:
            raise ValueError("活动初始额度不能超过剩余额度上限")
        return self


class PluginSettings(_Settings):
    """唯一实例与显式工厂入口；插件原配置以不可变规范 JSON 保存。

    外部 TOML/Python 的 config 字段必须是对象。configuration 返回新的副本
    供纯工厂解析，调用者不能通过修改嵌套容器改变启动快照。插件凭据由具体
    配置模型拥有；根配置展示排除这份尚未解析的原始配置，避免回显秘密。
    """

    # 所有字段都是已校验不可变值；复用实例不能再把内部 JSON 文本误作
    # 外部 config 对象解析。原始容器只在首次构造边界取得并规范化。
    model_config = ConfigDict(revalidate_instances="never")

    instance_id: NonEmptyString
    entry: NonEmptyString
    enabled: bool = True
    config_json: str = Field(default="{}", alias="config", repr=False, exclude=True)

    @field_validator("config_json", mode="before")
    @classmethod
    def _freeze_configuration(cls, value: object) -> str:
        """规范化对象并拒绝非 JSON 材料，不借用外部可变配置容器。"""
        encoded = encode_json(value)
        decode_object(encoded)
        return encoded.decode("utf-8")

    def configuration(self) -> JsonObject:
        """返回插件配置副本，纯声明阶段必须经其 Pydantic 配置模型校验。"""
        return decode_object(self.config_json.encode("utf-8"))


class KnowledgeSettings(_Settings):
    """可选向量资源；缺省只使用本地检索，不隐含选择 embedding 模型。"""

    embedding: EmbeddingSettings | None = None


class WebSettings(_Settings):
    """CLI 本机面板接入配置，绑定范围固定为回环且不启动第二个宿主。

    port=0 可供嵌入测试明确选择系统分配端口，实际 URL 在启动后取得。
    不声明公网/代理或人员权限；原始主配置仍只由文件及纯构造边界取得。
    """

    enabled: bool = False
    port: Annotated[int, Field(ge=0, le=65535)] = 8765


class EngineConfig(_Settings):
    """供宿主装配使用的配置快照；相对路径必须经显式基准目录解析。

    程序式调用者可直接构造各配置对象，随后调用 ``resolve``；外部原始值
    应使用 ``validate``，以取得脱敏的 ``ConfigError``。所有模型用途引用在
    构造时验证，模型映射在校验后冻结，不向宿主泄露可变配置容器。
    """

    host: HostSettings = Field(default_factory=HostSettings)
    models: Mapping[NonEmptyString, ModelSettings]
    subject: SubjectSettings
    context: ContextSettings = Field(default_factory=ContextSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    activities: ActivitySettings = Field(default_factory=ActivitySettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    diagnostics: DiagnosticSettings = Field(default_factory=DiagnosticSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    console: bool = True
    web: WebSettings = Field(default_factory=WebSettings)
    plugins: tuple[PluginSettings, ...] = ()
    _plugin_base_dir: Path | None = PrivateAttr(default=None)

    @field_validator("plugins", mode="before")
    @classmethod
    def _plugin_list(cls, value: object) -> object:
        """TOML 数组转为不可变实例序列，具体项仍经严格字段校验。"""
        return tuple(value) if _is_list(value) else value

    @model_validator(mode="after")
    def _check_model_references(self) -> Self:
        """先验证用途，再冻结映射；设备离线状态不改变启动时的模型选择。"""
        generation_references = (
            ("foreground", self.subject.foreground),
            ("reflection", self.subject.reflection),
            ("summary", self.subject.summary),
        )
        for purpose, name in generation_references:
            model = self.models.get(name)
            if not isinstance(model, GenerationModelSettings):
                raise ValueError(f"subject.{purpose}: 必须引用已声明的生成模型")
        if self.subject.attention is not None and self.subject.attention not in self.models:
            raise ValueError("subject.attention: 必须引用已声明的模型")
        instance_ids = [item.instance_id for item in self.plugins]
        if len(set(instance_ids)) != len(instance_ids):
            raise ValueError("plugins: 插件实例身份重复")

        object.__setattr__(self, "models", MappingProxyType(dict(self.models)))
        return self

    @field_serializer("models")
    def _serialize_models(self, value: Mapping[str, ModelSettings]) -> dict[str, ModelSettings]:
        """序列化只读映射为普通对象，SecretStr 仍按其脱敏规则输出。"""
        return dict(value)

    @classmethod
    def validate(cls, value: object, *, base_dir: Path | None = None) -> Self:
        """统一校验外部值并解析路径；失败只报告字段位置与不含输入的原因。"""
        try:
            config = cls.model_validate(value)
        except ValidationError as error:
            issues = error.errors(include_input=False, include_context=False, include_url=False)
            descriptions: list[str] = []
            for issue in issues:
                location = ".".join(str(part) for part in issue["loc"]) or "config"
                descriptions.append(f"{location}: {issue['msg']}")
            raise ConfigError("配置校验失败：" + "; ".join(descriptions)) from None
        return config.resolve(base_dir=base_dir)

    def resolve(self, *, base_dir: Path | None = None) -> Self:
        """以显式绝对目录解析启动路径；不打开资源或依赖进程工作目录。"""
        data_dir = self.host.data_dir
        if not data_dir.is_absolute():
            if base_dir is None or not base_dir.is_absolute():
                raise ConfigError("host.data_dir: 相对路径必须提供绝对 base_dir")
            data_dir = base_dir / data_dir

        # 重新构造经过完整校验的快照，不能用 model_copy(update=...) 绕过边界。
        resolved = type(self)(
            host=HostSettings(data_dir=data_dir.resolve(), close_seconds=self.host.close_seconds),
            models=self.models,
            subject=self.subject,
            context=self.context,
            execution=self.execution,
            activities=self.activities,
            knowledge=self.knowledge,
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
        """返回已固定的配置基准目录；空插件配置无需额外路径信息。"""
        if self._plugin_base_dir is not None:
            return self._plugin_base_dir
        return self.host.data_dir


def load_config(path: Path) -> EngineConfig:
    """读取一份 TOML，以文件所在目录解析路径，并以脱敏错误拒绝不合法配置。"""
    config_path = path.absolute()
    try:
        with config_path.open("rb") as source:
            raw_config = tomllib.load(source)
    except tomllib.TOMLDecodeError:
        # TOML 异常可能携带整段原始行，不能将凭据连同语法错误一起输出。
        raise ConfigError("TOML 语法无效，请检查配置文件结构") from None
    except OSError:
        raise ConfigError("无法读取配置文件，请检查路径与读取权限") from None
    return EngineConfig.validate(raw_config, base_dir=config_path.parent)
