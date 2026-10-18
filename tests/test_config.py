"""验证真实 TOML 和程序式配置共用的边界、路径及脱敏行为。"""

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
    load_config,
)
from purrcept_engine.errors import ConfigError


def _configuration() -> dict[str, object]:
    """不使用真实模型账号；所有检查仅消费配置值。"""
    return {
        "models": {
            "local": {
                "kind": "generation",
                "model": "fixture-generation",
                "api_base": "http://127.0.0.1:4000/v1",
                "api_key": "fixture-secret",
                "context_window": 32000,
                "max_output_tokens": 4096,
                "capacity_rule": "fixture-text-v1",
            },
            "gate": {
                "kind": "decision",
                "model": "fixture-decision",
                "api_base": "http://127.0.0.1:4001/v1",
                "api_key": "fixture-gate-secret",
                "input_limit": 8192,
                "capacity_rule": "fixture-decision-v1",
            },
        },
        "subject": {
            "foreground": "local",
            "reflection": "local",
            "summary": "local",
            "attention": "gate",
        },
    }


def test_configuration_resolves_and_freezes_startup_snapshot(tmp_path: Path) -> None:
    """配置文件之外的可变映射不能改变已通过校验的启动快照。"""
    raw = _configuration()
    config = EngineConfig.validate(raw, base_dir=tmp_path)
    raw.clear()

    assert config.host.data_dir == tmp_path / ".purrcept-engine"
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
    assert config.models["local"].media_rule == "unsupported"
    assert config.models["local"].supports_contents == ("text",)
    assert isinstance(config.models, MappingProxyType)


def test_models_mapping_and_values_are_immutable(tmp_path: Path) -> None:
    """冻结覆盖模型容器和模型字段，不止阻止顶层属性赋值。"""
    config = EngineConfig.validate(_configuration(), base_dir=tmp_path)
    with pytest.raises(ValidationError, match="frozen"):
        config.models["local"].model = "replacement"
    assert "fixture-secret" not in config.model_dump_json()
    assert "fixture-secret" not in repr(config)


def test_programmatic_construction_requires_explicit_relative_base(tmp_path: Path) -> None:
    """程序式路径不随调用者当前目录漂移，完整对象仍走同一关系校验。"""
    model = GenerationModelSettings(
        kind="generation",
        model="fixture",
        api_base="http://127.0.0.1:4000/v1",
        api_key=SecretStr("fixture-secret"),
        context_window=32000,
        max_output_tokens=4096,
        capacity_rule="fixture-v1",
    )
    config = EngineConfig(
        host=HostSettings(data_dir=Path("state")),
        models={"local": model},
        subject=SubjectSettings(foreground="local", reflection="local", summary="local"),
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
        {"execution": {"max_workers": True}},
        {"execution": {"max_calls": -1}},
        {"context": {"summary_trigger": 0.5, "summary_target": 0.8}},
        {"activities": {"initial_seconds": 3601, "max_remaining_seconds": 3600}},
        {"subject": {"foreground": "missing", "reflection": "local", "summary": "local"}},
        {"subject": {"foreground": "gate", "reflection": "local", "summary": "local"}},
        {"unrecognized": True},
        {"plugins": [{"id": "unknown", "entrypoint": "unknown:plugin", "enabled": True}]},
    ],
)
def test_invalid_shapes_and_relationships_are_rejected(
    section: dict[str, object], tmp_path: Path
) -> None:
    """真实边界拒绝错误类型、未知项和关系冲突，不产生隐式默认或转发。"""
    raw = _configuration()
    raw.update(section)
    with pytest.raises(ConfigError):
        EngineConfig.validate(raw, base_dir=tmp_path)


def test_frame_must_leave_room_for_protocol_envelope() -> None:
    """已允许的有效负载不能因帧配置相等而必然发送失败。"""
    with pytest.raises(ValidationError, match="协议帧"):
        ExecutionSettings(frame_bytes=1024 * 1024)


@pytest.mark.parametrize(
    "changes",
    [
        {"api_key": ""},
        {"api_key": "   "},
        {"api_base": "localhost:4000"},
        {"api_base": "http://account:password@localhost:4000/v1"},
        {"api_base": "http://localhost:bad-port/v1"},
        {"context_window": True},
        {"max_output_tokens": 32000},
        {"capacity_rule": ""},
        {"temperature": 0.7},
    ],
)
def test_model_boundaries_do_not_infer_missing_contracts(changes: dict[str, object]) -> None:
    """不接受空凭据、隐式端点、错误容量或尚未声明的模型参数。"""
    values: dict[str, object] = {
        "kind": "generation",
        "model": "fixture",
        "api_base": "http://127.0.0.1:4000/v1",
        "api_key": "fixture-secret",
        "context_window": 32000,
        "max_output_tokens": 4096,
        "capacity_rule": "fixture-v1",
    }
    values.update(changes)
    with pytest.raises(ValidationError):
        GenerationModelSettings.model_validate(values)


def test_decision_configuration_rejects_generation_budget() -> None:
    """专用判断按协议输入限额校验，不透传生成用途的参数。"""
    with pytest.raises(ValidationError, match="max_output_tokens"):
        DecisionModelSettings.model_validate(
            {
                "kind": "decision",
                "model": "fixture",
                "api_base": "http://127.0.0.1:4001/v1",
                "api_key": "fixture-secret",
                "input_limit": 8192,
                "capacity_rule": "fixture-v1",
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
        "capacity_rule": "fixture-v1",
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
                "capacity_rule": "fixture-v1",
                "input_limit": 8192,
                "supports_contents": value,
            }
        )


def test_removed_audio_flag_is_rejected_instead_of_silently_ignored() -> None:
    with pytest.raises(ValidationError, match="supports_audio"):
        GenerationModelSettings.model_validate(
            {
                "kind": "generation",
                "model": "fixture",
                "api_base": "http://127.0.0.1:4000/v1",
                "api_key": "fixture-secret",
                "capacity_rule": "fixture-v1",
                "context_window": 32000,
                "max_output_tokens": 4096,
                "supports_audio": True,
            }
        )


def test_validation_error_cannot_echo_secret(tmp_path: Path) -> None:
    """Pydantic 输入及原始 TOML 行都不能通过配置错误泄露账号材料。"""
    raw = _configuration()
    raw["host"] = {"close_seconds": "private-account-material"}
    with pytest.raises(ConfigError) as captured:
        EngineConfig.validate(raw, base_dir=tmp_path)
    assert "host.close_seconds" in str(captured.value)
    assert "private-account-material" not in str(captured.value)
    assert "fixture-secret" not in str(captured.value)

    invalid_path = tmp_path / "invalid.toml"
    invalid_path.write_text('api_key = "private-account-material\n', encoding="utf-8")
    with pytest.raises(ConfigError) as syntax_error:
        load_config(invalid_path)
    assert "private-account-material" not in str(syntax_error.value)


def test_example_toml_uses_file_directory_as_path_base(tmp_path: Path) -> None:
    """移动示例或改变工作目录不改变相对路径所属的配置文件目录。"""
    example_path = files("purrcept_engine.app").joinpath("default.toml")
    copied_path = tmp_path / "engine.toml"
    copied_path.write_bytes(example_path.read_bytes())
    config = load_config(copied_path)

    assert config.host.data_dir == tmp_path / ".purrcept-engine"
    assert config.subject.attention == "attention"
    assert config.models["foreground"].media_rule == "unsupported"
    assert config.plugins == ()
