"""验证 TOML 加载、${ENV} 环境变量插值、本地免密端点、默认字段推导与可读报错。"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from types import MappingProxyType

import pytest
from pydantic import SecretStr, ValidationError

from purrcept_engine.app.config import (
    DecisionModelSettings,
    EngineConfig,
    ExecutionSettings,
    GenerationModelSettings,
    HostSettings,
    SubjectSettings,
    expand_env_vars,
    load_config,
)
from purrcept_engine.errors import ConfigError


def _configuration() -> dict[str, object]:
    return {
        "models": {
            "local": {
                "kind": "generation",
                "model": "fixture-generation",
                "api_base": "http://127.0.0.1:4000/v1",
                "api_key": "fixture-secret",
                "context_window": 32000,
                "max_output_tokens": 4096,
            },
            "gate": {
                "kind": "decision",
                "model": "fixture-decision",
                "api_base": "http://127.0.0.1:4001/v1",
                "api_key": "fixture-gate-secret",
                "input_limit": 8192,
            },
        },
        "subject": {
            "foreground": "local",
            "reflection": "local",
            "attention": "gate",
        },
    }


def test_configuration_resolves_and_freezes_startup_snapshot(tmp_path: Path) -> None:
    raw = _configuration()
    config = EngineConfig.validate(raw, base_dir=tmp_path)
    raw.clear()

    assert config.host.data_dir == tmp_path / ".purrcept-engine"
    assert config.logging.level == "INFO"
    assert config.subject.foreground_retry.attempts == 2
    assert config.subject.foreground_retry.attempt_seconds == 120
    assert config.subject.foreground_retry.total_seconds == 180
    assert config.subject.attention_retry.attempt_seconds == 5
    assert config.subject.attention_retry.total_seconds == 8
    assert config.subject.foreground_turn.max_requests == 8
    assert config.subject.foreground_turn.total_seconds == 600
    assert config.subject.proactive_thinking is False
    assert config.subject.proactive_idle_seconds == 300
    assert config.execution.worker_memory_bytes == 512 * 1024 * 1024
    assert config.execution.pending_input_bytes == 256 * 1024 * 1024
    assert config.activities.initial_seconds == 3600
    assert config.context.page_chars == 8000
    assert config.models["local"].supports_contents == ("text",)
    assert isinstance(config.models, MappingProxyType)


def test_minimal_single_model_config_infers_defaults_and_local_no_auth(tmp_path: Path) -> None:
    """省略 kind、api_key（本机/局域网）与 reflection 时自动采用合理默认值。"""
    minimal = {
        "models": {
            "main": {
                "model": "openai/qwen2.5:14b",
                "api_base": "http://127.0.0.1:11434/v1",
            }
        },
        "subject": {
            "foreground": "main",
        },
    }
    config = EngineConfig.validate(minimal, base_dir=tmp_path)
    main_model = config.models["main"]
    assert isinstance(main_model, GenerationModelSettings)
    assert main_model.context_window == 64000
    assert main_model.max_output_tokens == 4096
    assert main_model.api_key.get_secret_value() == "local-no-auth"
    assert config.subject.reflection == "main"


@pytest.mark.parametrize("kind", ["generation", "decision"])
def test_model_endpoint_can_be_omitted_without_weakening_explicit_url_validation(
    tmp_path: Path, kind: str
) -> None:
    values: dict[str, object] = {
        "kind": kind,
        "model": "gemini/gemini-2.5-flash" if kind == "generation" else "typesafe/jev-1.13.0",
        "api_key": "synthetic-key",
    }
    if kind == "decision":
        values["input_limit"] = 8192
    config = EngineConfig.validate(
        {
            "models": {
                "main": {"model": "openai/test", "api_key": "synthetic-key"},
                "target": values,
            },
            "subject": {"foreground": "main", "attention": "target"},
        },
        base_dir=tmp_path,
    )
    assert config.models["target"].api_base is None
    with pytest.raises(ValidationError):
        GenerationModelSettings.model_validate(
            {"model": "openai/test", "api_key": "synthetic-key", "api_base": ""}
        )
    with pytest.raises(ValidationError, match="api_key"):
        GenerationModelSettings.model_validate({"model": "openai/test"})


def test_environment_variable_interpolation_and_missing_var_error(tmp_path: Path) -> None:
    toml_file = tmp_path / "purrcept.toml"
    toml_file.write_text(
        """
[logging]
level = "${LOG_LEVEL:-debug}"

[models.main]
model = "openai/deepseek-chat"
api_base = "${CUSTOM_BASE:-https://api.deepseek.com/v1}"
api_key = "${DEEPSEEK_API_KEY}"

[subject]
foreground = "main"

[subject.character]
notes = "价格符号转义 $$100"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="DEEPSEEK_API_KEY"):
        load_config(toml_file, environ={})

    loaded = load_config(toml_file, environ={"DEEPSEEK_API_KEY": "sk-live-test-key"})
    assert loaded.logging.level == "DEBUG"
    assert loaded.models["main"].api_base == "https://api.deepseek.com/v1"
    assert loaded.models["main"].api_key.get_secret_value() == "sk-live-test-key"
    assert loaded.subject.character.notes == "价格符号转义 $100"


@pytest.mark.parametrize("settings", [GenerationModelSettings, DecisionModelSettings])
def test_model_proxy_accepts_http_and_rejects_unsupported_or_ambiguous_urls(
    settings: type[GenerationModelSettings] | type[DecisionModelSettings],
) -> None:
    """显式代理在启动时校验，避免 SOCKS 依赖或错填服务地址导致运行期失败。"""
    values: dict[str, object] = {
        "kind": "decision" if settings is DecisionModelSettings else "generation",
        "model": "synthetic-model",
        "api_key": "synthetic-key",
        "input_limit": 8192,
    }
    assert settings.model_validate(values).proxy is None
    for proxy in ("http://127.0.0.1:10808", "https://proxy.invalid:8443"):
        assert settings.model_validate({**values, "proxy": proxy}).proxy == proxy
    for proxy in (
        "",
        "127.0.0.1:10808",
        "socks5://127.0.0.1:10808",
        "http://user:password@proxy.invalid",
        "http://proxy.invalid:invalid",
        "http://proxy.invalid?token=secret",
        "http://proxy.invalid#fragment",
        "http://proxy.invalid/path",
    ):
        with pytest.raises(ValidationError):
            settings.model_validate({**values, "proxy": proxy})


def test_models_mapping_and_values_are_immutable(tmp_path: Path) -> None:
    config = EngineConfig.validate(_configuration(), base_dir=tmp_path)
    with pytest.raises(ValidationError, match="frozen"):
        config.models["local"].model = "replacement"
    assert "fixture-secret" not in config.model_dump_json()
    assert "fixture-secret" not in repr(config)


def test_programmatic_construction_requires_explicit_relative_base(tmp_path: Path) -> None:
    model = GenerationModelSettings(
        kind="generation",
        model="fixture",
        api_base="http://127.0.0.1:4000/v1",
        api_key=SecretStr("fixture-secret"),
        context_window=32000,
        max_output_tokens=4096,
    )
    config = EngineConfig(
        host=HostSettings(data_dir=Path("state")),
        models={"local": model},
        subject=SubjectSettings(foreground="local", reflection="local"),
    )
    with pytest.raises(ConfigError, match="base_dir"):
        config.resolve()
    assert config.resolve(base_dir=tmp_path).host.data_dir == tmp_path / "state"


@pytest.mark.parametrize(
    "section",
    [
        {"host": {"close_seconds": 0}},
        {"host": {"close_seconds": float("inf")}},
        {"host": {"close_seconds": "30"}},
        {"console": "true"},
        {"logging": {"level": "VERBOSE"}},
        {"execution": {"max_workers": True}},
        {"execution": {"max_calls": -1}},
        {"context": {"summary_trigger": 0.5, "summary_target": 0.8}},
        {"context": {"media_cleanup_threshold_mb": 0}},
        {"context": {"media_cleanup_threshold_mb": True}},
        {"activities": {"initial_seconds": 3601, "max_remaining_seconds": 3600}},
        {"subject": {"foreground": "missing", "reflection": "local"}},
        {"subject": {"foreground": "gate", "reflection": "local"}},
        {"subject": {"foreground": "local", "reflection": "local", "summary": "local"}},
        {"unrecognized": True},
        {"plugins": [{"id": "unknown", "entrypoint": "unknown:plugin", "enabled": True}]},
    ],
)
def test_invalid_shapes_and_relationships_are_rejected(
    section: dict[str, object], tmp_path: Path
) -> None:
    raw = _configuration()
    raw.update(section)
    with pytest.raises(ConfigError):
        EngineConfig.validate(raw, base_dir=tmp_path)


def test_frame_must_leave_room_for_protocol_envelope() -> None:
    with pytest.raises(ValidationError, match="协议帧"):
        ExecutionSettings(frame_bytes=1024 * 1024)


@pytest.mark.parametrize(
    "changes",
    [
        {"api_base": "https://api.openai.com/v1", "api_key": ""},
        {"api_base": "https://api.openai.com/v1", "api_key": "   "},
        {"api_base": "localhost:4000"},
        {"api_base": "http://account:password@localhost:4000/v1"},
        {"api_base": "http://localhost:bad-port/v1"},
        {"context_window": True},
        {"max_output_tokens": 32000},
        {"capacity_rule": "removed-rule"},
        {"media_rule": "removed-rule"},
        {"temperature": 0.7},
    ],
)
def test_model_boundaries_reject_remote_empty_key_and_bad_urls(
    changes: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "kind": "generation",
        "model": "fixture",
        "api_base": "http://127.0.0.1:4000/v1",
        "api_key": "fixture-secret",
        "context_window": 32000,
        "max_output_tokens": 4096,
    }
    values.update(changes)
    with pytest.raises(ValidationError):
        GenerationModelSettings.model_validate(values)


def test_decision_configuration_rejects_generation_budget() -> None:
    with pytest.raises(ValidationError, match="max_output_tokens"):
        DecisionModelSettings.model_validate(
            {
                "kind": "decision",
                "model": "fixture",
                "api_base": "http://127.0.0.1:4001/v1",
                "api_key": "fixture-secret",
                "input_limit": 8192,
                "max_output_tokens": 4096,
            }
        )


@pytest.mark.parametrize("kind", ["generation", "decision"])
def test_models_share_immutable_supported_content_declaration(kind: str) -> None:
    values: dict[str, object] = {
        "kind": kind,
        "model": "fixture",
        "api_base": "http://127.0.0.1:4000/v1",
        "api_key": "fixture-secret",
    }
    if kind == "generation":
        values.update(context_window=32000, max_output_tokens=4096)
        settings = GenerationModelSettings
    else:
        values["input_limit"] = 8192
        settings = DecisionModelSettings
    assert settings.model_validate(values).supports_contents == ("text",)
    declared = ["text", "image", "audio", "video"]
    values["supports_contents"] = declared
    config = settings.model_validate(values)
    declared.clear()
    assert config.supports_contents == ("text", "image", "audio", "video")
    assert config.model_dump(mode="json")["supports_contents"] == [
        "text",
        "image",
        "audio",
        "video",
    ]


@pytest.mark.parametrize(
    "value", [[], ["audio"], ["text", "text"], ["text", "pdf"], ["text", 1], "text", True]
)
def test_supported_content_declaration_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValidationError):
        DecisionModelSettings.model_validate(
            {
                "kind": "decision",
                "model": "fixture",
                "api_base": "http://127.0.0.1:4000/v1",
                "api_key": "fixture-secret",
                "input_limit": 8192,
                "supports_contents": value,
            }
        )


def test_toml_syntax_error_reports_line_number_and_conflict_hint_without_echoing_secret(
    tmp_path: Path,
) -> None:
    raw = _configuration()
    raw["host"] = {"close_seconds": "private-account-material"}
    with pytest.raises(ConfigError) as captured:
        EngineConfig.validate(raw, base_dir=tmp_path)
    assert "host.close_seconds" in str(captured.value)
    assert "private-account-material" not in str(captured.value)

    invalid_path = tmp_path / "invalid.toml"
    invalid_path.write_text('api_key = "private-account-material\n', encoding="utf-8")
    with pytest.raises(ConfigError) as syntax_error:
        load_config(invalid_path)
    assert "第 1 行" in str(syntax_error.value)
    assert "private-account-material" not in str(syntax_error.value)

    conflict_path = tmp_path / "conflict.toml"
    conflict_body = 'plugins = []\n\n[[plugins]]\ninstance_id = "bed"\nentry = "bed"\n'
    conflict_path.write_text(conflict_body, encoding="utf-8")
    with pytest.raises(ConfigError, match="删除顶部的 `plugins = \\[\\]`"):
        load_config(conflict_path)


def test_default_toml_is_valid_single_model_starter_and_uncommentable(tmp_path: Path) -> None:
    example_path = files("purrcept_engine.app").joinpath("default.toml")
    copied_path = tmp_path / "engine.toml"
    copied_path.write_bytes(example_path.read_bytes())
    config = load_config(copied_path)

    assert config.console is True
    assert config.web.enabled is False
    assert config.host.data_dir == tmp_path / ".purrcept-engine"
    assert config.subject.foreground == "main"
    assert config.subject.reflection == "main"
    assert config.subject.attention == "main"
    assert config.subject.character.name == "小咕"
    assert config.plugins == ()
    assert expand_env_vars("$$literal") == "$literal"
