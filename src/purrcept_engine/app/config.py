"""声明并校验宿主的不可变启动配置，不启动模型、插件或设备。

TOML 与程序式调用共同经过 ``EngineConfig.validate``；交叉引用和额度关系
在配置边界核验，再以显式基准目录解析路径。模型名称不决定容量规则或媒体支持，
规则标识由后续模型适配边界核对。插件声明登记属于应用装配，当前仅接受零插件配置。
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from purrcept_engine.errors import ConfigError

__all__ = [
    "ActivitySettings",
    "ContextSettings",
    "DecisionModelSettings",
    "EngineConfig",
    "ExecutionSettings",
    "GenerationModelSettings",
    "HostSettings",
    "RetrySettings",
    "SubjectSettings",
    "TurnSettings",
    "load_config",
]

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveInt = Annotated[int, Field(gt=0)]
PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Fraction = Annotated[float, Field(gt=0, lt=1, allow_inf_nan=False)]


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


class ExecutionSettings(_Settings):
    """拥有脚本、IPC、普通派发及输入接管额度，模型不能提高这些上限。"""

    action_seconds: PositiveSeconds = 180.0
    worker_cpu_seconds: PositiveSeconds = 15.0
    worker_memory_bytes: PositiveInt = 512 * 1024 * 1024
    max_calls: PositiveInt = 64
    source_bytes: PositiveInt = 64 * 1024
    output_bytes: PositiveInt = 64 * 1024
    result_bytes: PositiveInt = 1024 * 1024
    call_arguments_bytes: PositiveInt = 1024 * 1024
    call_result_bytes: PositiveInt = 1024 * 1024
    frame_bytes: PositiveInt = 2 * 1024 * 1024
    max_workers: PositiveInt = 4
    max_provider_calls: PositiveInt = 16
    max_pending_inputs: PositiveInt = 1024
    pending_input_bytes: PositiveInt = 256 * 1024 * 1024

    @model_validator(mode="after")
    def _frame_carries_payload(self) -> Self:
        """帧还需容纳协议信封，不能把有效负载上限直接当成帧上限。"""
        largest_payload = max(
            self.result_bytes,
            self.call_arguments_bytes,
            self.call_result_bytes,
        )
        if self.frame_bytes <= largest_payload:
            raise ValueError("协议帧必须大于最大单条 JSON 负载并为信封留出空间")
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
    console: bool = True
    web: bool = False
    plugins: tuple[object, ...] = ()

    @field_validator("plugins", mode="before")
    @classmethod
    def _only_declared_plugins(cls, value: object) -> tuple[object, ...]:
        """零插件可纯校验；尚未登记的启用项不能被静默忽略。"""
        if isinstance(value, (tuple, list)) and not value:
            return ()
        raise ValueError("插件声明尚未登记，当前配置只接受空插件列表")

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
        return type(self)(
            host=HostSettings(data_dir=data_dir.resolve(), close_seconds=self.host.close_seconds),
            models=self.models,
            subject=self.subject,
            context=self.context,
            execution=self.execution,
            activities=self.activities,
            console=self.console,
            web=self.web,
            plugins=self.plugins,
        )


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
