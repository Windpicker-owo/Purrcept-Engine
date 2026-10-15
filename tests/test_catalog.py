"""目录与执行使用同一父链和绑定，稳定前缀不随声明遍历顺序变化。"""

from dataclasses import FrozenInstanceError, replace

import pytest
from pydantic import BaseModel, ConfigDict

from purrcept_engine.domain.execution.catalog import (
    CapabilityDeclaration,
    Catalog,
    ObjectDeclaration,
)
from purrcept_engine.errors import ConfigError


class _Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    text: str


def _objects() -> tuple[ObjectDeclaration, ...]:
    return (
        ObjectDeclaration("subject", "me", "主体"),
        ObjectDeclaration("phone", "phone", "设备", "subject"),
        ObjectDeclaration("qq", "qq", "应用", "phone"),
    )


def _capability() -> CapabilityDeclaration[_Parameters]:
    return CapabilityDeclaration(
        "qq.send",
        "qq",
        "send",
        "发送一次文字并保存真实回执。",
        _Parameters,
        20.0,
        'await me.phone.qq.send(text="hi")',
    )


def test_catalog_parent_paths_and_stable_prefix_have_one_definition() -> None:
    forward = Catalog(_objects(), (_capability(),))
    reverse = Catalog(tuple(reversed(_objects())), (_capability(),))
    assert forward.version == reverse.version
    assert forward.instructions == reverse.instructions
    binding = forward.resolve("me.phone.qq.send")
    assert binding.declaration is forward.bindings[binding.path].declaration
    assert forward.object_paths["qq"] == "me.phone.qq"
    assert '"additionalProperties":false' in binding.schema_json
    assert binding.schema_json in forward.instructions
    with pytest.raises(FrozenInstanceError):
        property_name = "instructions"
        setattr(forward, property_name, "modified")


@pytest.mark.parametrize(
    "objects",
    [
        (*_objects(), _objects()[0]),
        (ObjectDeclaration("subject", "me", "主体", "qq"), *_objects()[1:]),
        (ObjectDeclaration("orphan", "device", "设备", "missing"),),
        (ObjectDeclaration("bad", "_private", "私有"),),
        (*_objects(), ObjectDeclaration("other", "qq", "冲突", "phone")),
    ],
)
def test_catalog_rejects_inconsistent_objects(objects: tuple[ObjectDeclaration, ...]) -> None:
    with pytest.raises(ConfigError):
        Catalog(objects, ())


def test_capability_identity_path_and_target_are_checked_before_start() -> None:
    capability = _capability()
    for candidates in (
        (capability, capability),
        (replace(capability, object_id="missing"),),
        (replace(capability, name="_private"),),
        (replace(capability, name="class"),),
        (replace(capability, timeout_seconds=float("inf")),),
        (replace(capability, name="qq", object_id="phone"),),
    ):
        with pytest.raises(ConfigError):
            Catalog(_objects(), candidates)
