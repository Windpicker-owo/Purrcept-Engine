"""以冻结材料和用户批准的前台正文验证 Core 模板，不从实现生成预期文本。

覆盖固定人格、可选字段、纯空白和模板标记，以及 instruction/reminder/message
的真实协议值；防止渲染重构改变稳定前缀、动态尾部或历史摘要的含义。
"""

from pathlib import Path

import pytest
from purrcept_core.models import (
    ConversationCheckpoint,
    ConversationState,
    Message,
    PromptStability,
    ReminderPlacement,
    ReminderState,
    TextBlock,
)
from pydantic import BaseModel

from purrcept_engine.domain.context.history import summary_request
from purrcept_engine.domain.context.maintenance import SummarySource
from purrcept_engine.domain.context.presentation import render_attention_context, render_material
from purrcept_engine.domain.context.summary_materials import summary_message
from purrcept_engine.domain.execution.catalog import (
    CapabilityDeclaration,
    Catalog,
    ObjectDeclaration,
)
from purrcept_engine.domain.subject.attention import ATTENTION_QUESTION
from purrcept_engine.domain.subject.foreground_tools import (
    foreground_instructions,
    format_character_card,
)
from purrcept_engine.domain.subject.reflection_tools import reflection_instructions
from purrcept_engine.domain.subject.session import DynamicReminder, DynamicState
from purrcept_engine.values import JsonObject, decode_object


class _Parameters(BaseModel):
    """提供包含字典/字符串的真实参数 Schema，示例仍由目录生成器展示。"""

    value: dict[str, str | dict[str, str]] | str


def baseline() -> JsonObject:
    """读取经审阅冻结的正文与身份字段，不随实现自动重新生成。"""
    return decode_object((Path(__file__).parent / "fixtures/prompt_baseline.json").read_bytes())


@pytest.mark.parametrize("case", ["default", "full", "empty", "whitespace", "markers"])
def test_persona_and_foreground_match_frozen_material(case: str) -> None:
    sample = baseline()[case]
    assert isinstance(sample, dict)
    fields = sample["character"]
    assert isinstance(fields, dict)
    character: dict[str, str | None] = {}
    for key, value in fields.items():
        assert value is None or isinstance(value, str)
        character[key] = value
    assert format_character_card(character) == sample["card"]
    catalog = Catalog((ObjectDeclaration("me", "me", "主体"),), ())
    instructions = foreground_instructions(character, catalog)
    assert isinstance(instructions[0].content[0], TextBlock)
    approved = (
        (Path(__file__).parent / "fixtures/foreground_prompt.txt")
        .read_text(encoding="utf-8")
        .removesuffix("\n")
    )
    # 前台采用用户指定正文；角色卡及其它脑区分别核验对应冻结材料。
    assert isinstance(sample["card"], str)
    assert instructions[0].content[0].text == approved.replace("{profile}", sample["card"])
    assert instructions[0].stability is PromptStability.STABLE
    assert instructions[1].content == (TextBlock(catalog.instructions),)


def test_reflection_attention_and_summary_system_text_match_frozen_material() -> None:
    samples = baseline()
    assert reflection_instructions()[0].content == (TextBlock(str(samples["reflection"])),)
    assert ATTENTION_QUESTION.instructions == samples["attention"]
    checkpoint = ConversationCheckpoint(
        ConversationState(
            (Message.user("旧原文 {profile}"), Message.assistant("内部结果")),
            turn_index=1,
            turn_boundaries=(2,),
        ),
        ReminderState(),
    )
    source = SummarySource("foreground", 3, checkpoint, 1, 2, "coverage")
    request = summary_request(source, 1024)
    assert request.instructions[0].content == (TextBlock(str(samples["history"])),)
    message = summary_message(source, " 摘要 {profile}\n尾部\n")
    assert message.text == samples["summary_message"]
    assert message.metadata["source_version"] == 3


def test_material_templates_keep_arbitrary_text_and_reminder_placement() -> None:
    samples = baseline()
    payload: JsonObject = {
        "body": "原文 {profile}\n```\n尾部\n",
        "items": [None, False, 0, "", {}, []],
    }
    assert render_material("完整材料", payload) == samples["material"]
    background: JsonObject = {
        "coverage": "current_work_only",
        "current_turn": None,
        "plans": [{"reason": "原文 {profile}\n````\n"}],
        "activities": [],
    }
    assert render_attention_context(background) == samples["attention_context"]
    reminder = DynamicReminder(DynamicState(" \n{profile}\n ")).reminder()
    assert reminder.content == (TextBlock(" \n{profile}\n "),)
    assert reminder.key == "engine.committed_state"
    assert reminder.placement is ReminderPlacement.TAIL


@pytest.mark.parametrize(
    "example",
    [
        'await {path}(value={"key": {"text": "{profile}"}})  ',
        'await me.emit(value={"key": "{{literal}}"})',
        'await {path}(value="{{path}}")',
    ],
)
def test_catalog_examples_interpolate_only_declared_path_and_keep_python_braces(
    example: str,
) -> None:
    """能力示例允许 Python 字典及任意字面量，只把声明约定的 path 作为模板字段。"""
    catalog = Catalog(
        (ObjectDeclaration("me", "me", "主体"),),
        (
            CapabilityDeclaration(
                "emit", "me", "emit", "原描述\n", _Parameters, 1.0, example, usage="  "
            ),
        ),
    )
    expected = example.replace("{path}", "me.emit")
    schema = catalog.bindings["me.emit"].schema_json
    assert catalog.instructions == (
        "me.emit\n原描述\n\nusage:   \nparameters: " + schema + "\nexample: " + expected
    )


def test_core_templates_keep_bindings_local_and_reject_misspelled_fields() -> None:
    """共享模板不记住上一主体的角色卡，字段误拼由 Core 原生错误直接报告。"""
    from purrcept_core.models import PromptRenderError

    from purrcept_engine.domain.subject.prompts import FOREGROUND_PROMPT

    with pytest.raises(PromptRenderError):
        FOREGROUND_PROMPT.instruction({"profiel": "拼写错误"})
    first = FOREGROUND_PROMPT.instruction({"profile": "第一位 {profile}"})
    second = FOREGROUND_PROMPT.instruction({"profile": "第二位"})
    assert isinstance(first.content[0], TextBlock) and "第一位 {profile}" in first.content[0].text
    assert isinstance(second.content[0], TextBlock) and "第一位" not in second.content[0].text
    assert not FOREGROUND_PROMPT.values


def test_attention_identity_is_explicit_and_keeps_profile_markers_literal() -> None:
    """固定角色卡不串主体，姓名规则不把提名等同抢占或强制发言。"""
    from purrcept_engine.domain.subject.prompts import ATTENTION_IDENTITY_PROMPT

    first = ATTENTION_IDENTITY_PROMPT.render(
        {"profile": "小绒，别称绒绒 {rules} {profile}", "rules": "固定判断规则"}
    )
    second = ATTENTION_IDENTITY_PROMPT.render({"profile": "阿白", "rules": "固定判断规则"})
    assert "小绒，别称绒绒 {rules} {profile}" in first
    assert "小绒" not in second and "阿白" in second
    assert "即使没有 @，也应选择 wake" in first
    assert "同名他人" in first and "仅提名或搭话本身不构成 urgent" in first
    assert "唤起思考不等于必须对外回复" in first
