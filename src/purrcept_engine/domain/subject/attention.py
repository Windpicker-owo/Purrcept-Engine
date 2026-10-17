"""冻结完整注意材料与三种批次判断选项，保持筛选与世界行动的职责边界。

注意评估器按真实 SDK 编译结果预检完整批次；装不下时保留未完成原因，
由协调者保底唤醒，不偷偷删减批次或正文。这里只组装固定角色卡、当前原文、
按模型声明投影的媒体及有限工作背景，不读取完整前台历史、不增设 ASR，
也不决定实际暂停、回复或停止设备。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Literal

from purrcept_core.models import (
    AudioBlock,
    ChoiceOption,
    ChoiceQuestion,
    ContentBlock,
    DecisionBackend,
    DecisionRequest,
    ImageBlock,
    Message,
    MessageRole,
    ModelBackend,
    ModelRequest,
    ModelSettings,
    TextBlock,
)

from purrcept_engine.domain.context.materials import UnsupportedMaterial, input_blocks
from purrcept_engine.domain.context.presentation import render_attention_context
from purrcept_engine.domain.inputs.models import InputRecord, MediaReference
from purrcept_engine.domain.model_requests.contents import ContentKind, validate_supported_contents
from purrcept_engine.domain.model_requests.ports import MaterialCompiler, RetryPolicy
from purrcept_engine.domain.model_requests.service import prepare_off_loop
from purrcept_engine.domain.subject.prompts import (
    ATTENTION_GENERATION_PROMPT,
    ATTENTION_IDENTITY_PROMPT,
    ATTENTION_RULES_PROMPT,
)
from purrcept_engine.domain.subject.session import CapacityCheck
from purrcept_engine.serialization import encode
from purrcept_engine.values import JsonObject, decode_object

ATTENTION_QUESTION = ChoiceQuestion(
    "attention",
    ATTENTION_RULES_PROMPT.render(),
    (
        ChoiceOption("observe", "提交到主脑待处理输入，等待后续唤醒，本批不请求立即思考。"),
        ChoiceOption("wake", "整批进入前台下一轮，可与其他刺激合并，不打断当前生成。"),
        ChoiceOption("urgent", "有明确紧急依据，请求抢占生成；不取消已经开始的行动。"),
    ),
)

type NativeMedia = Callable[[tuple[MediaReference, ...]], Awaitable[tuple[ContentBlock, ...]]]


@dataclass(frozen=True, slots=True)
class AttentionModel:
    """app 按配置 discriminator 装配的单一资源，不猜路径或寻找替代模型。

    Attributes:
        backend: 显式注入的模型后端，资源取得和关闭归装配层。
        model: 本次调用使用的显式模型名，不在领域层推断替代模型。
        capacity: 对完整实际请求执行容量核验的函数。
        retry: 唯一传输重试策略，不额外叠加 SDK 隐式重试。
        compiler: 按实际 SDK 布局冻结完整请求材料的纯编译器。
        profile: 与主脑同源渲染的固定角色卡，用于理解姓名、兴趣和关系；不是外界输入。
        kind: 此业务值所属的稳定种类。
        output_tokens: 为本次生成预留的最大输出 token 数。
        supports_contents: 刺激允许交付的内容类型，默认仅 text；decision 还受协议限制。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。

        kind 与 backend 的精确对应由 app 的已校验装配保证。生成注意仍是无工具、
        无连续历史的一次判断，输出上限显式提供；专用判断不接受生成输出参数。
    """

    backend: DecisionBackend | ModelBackend
    model: str
    capacity: CapacityCheck
    retry: RetryPolicy
    compiler: MaterialCompiler = field(kw_only=True)
    profile: str = field(kw_only=True)
    kind: Literal["decision", "generation"] = field(kw_only=True)
    output_tokens: int | None = field(default=None, kw_only=True)
    supports_contents: tuple[ContentKind, ...] = field(default=("text",), kw_only=True)

    def __post_init__(self) -> None:
        """拒绝无法表达配置选择的组合，不用运行时探测退回另一模型路径。"""
        validate_supported_contents(self.supports_contents)
        if not isinstance(self.profile, str) or not self.profile.strip():
            raise ValueError("注意模型必须提供非空固定角色卡。")
        if self.kind == "decision":
            if self.output_tokens is not None:
                raise ValueError("专用注意判断不能声明生成输出额度。")
        elif self.kind == "generation":
            if (
                isinstance(self.output_tokens, bool)
                or not isinstance(self.output_tokens, int)
                or self.output_tokens <= 0
            ):
                raise ValueError("生成注意判断必须显式声明正的输出额度。")
        else:
            raise ValueError("注意模型必须显式选择 decision 或 generation。")


async def attention_request(
    records: tuple[InputRecord, ...],
    *,
    background: JsonObject,
    media: NativeMedia,
    model: AttentionModel,
) -> DecisionRequest | ModelRequest:
    """完整组装并预检一份实际请求。

    Args:
        records: 按接管顺序取得的完整输入记录。
        background: 本次观察的有限工作状态，不含完整前台历史或角色配置。
        media: 已接管媒体的引用集合，展开时必须取得原始 BLOB。
        model: 本次调用使用的显式模型名，不在领域层推断替代模型。

    Returns:
        已组装完整材料的专用判断或生成请求。

    Raises:
        ValueError: 注意批次不能为空。
        UnsupportedMaterial: 权威材料存在缺口，或声明支持但原生传输无法表达。

    Note:
        不支持的媒体原位替换为明确文字引用，不伪装为已查看或转写；原件留在库中。

        background 是宿主已经冻结的有限工作材料，其范围独立于当前输入覆盖。
        固定角色卡来自 model.profile，进入判断规则且计入完整请求容量；不从
        当前消息提取身份，不按名字字符串硬编码最低唤起或绕过注意判断。
        媒体端口只可展开已经接管的原生内容，缺失权威记录仍是存储故障。
    """
    if not records:
        raise ValueError("注意批次不能为空。")
    # 固定身份进入判断规则层，不与不可信的刺激正文混排。两种后端共用同一绑定，
    # 普通注意和最低唤起升级也不另设一份角色卡或名称匹配捷径。
    question = replace(
        ATTENTION_QUESTION,
        instructions=ATTENTION_IDENTITY_PROMPT.render(
            {"profile": model.profile, "rules": ATTENTION_QUESTION.instructions}
        ),
    )
    blocks: list[ContentBlock] = [TextBlock(render_attention_context(background))]
    for record in records:
        if record.context.missing_materials:
            raise UnsupportedMaterial(
                "attention.missing_materials:" + encode(record.context.missing_materials)
            )
    # Core 的专用判断协议只接受文字与图片；端点声明不能扩大协议的表达范围。
    supports_contents: tuple[ContentKind, ...] = tuple(
        kind
        for kind in model.supports_contents
        if model.kind != "decision" or kind in ("text", "image")
    )
    for block in await input_blocks(records, media=media, supports_contents=supports_contents):
        if not isinstance(block, (TextBlock, ImageBlock, AudioBlock)):
            raise UnsupportedMaterial("attention.unsupported_native_content")
        blocks.append(block)
    if model.kind == "decision":
        decision_blocks: list[TextBlock | ImageBlock] = []
        for block in blocks:
            if not isinstance(block, (TextBlock, ImageBlock)):
                raise UnsupportedMaterial("attention.unsupported_native_content")
            decision_blocks.append(block)
        request = DecisionRequest(tuple(decision_blocks), (question,))
    else:
        # 生成式注意判断必须返回固定 JSON 选择，才能进入同一版本核验流程；
        # 完整正文与原生媒体只是判断依据，此请求不提供世界目录或执行工具。
        request = ModelRequest(
            (Message(MessageRole.USER, tuple(blocks)),),
            instructions=(
                ATTENTION_GENERATION_PROMPT.instruction({"rules": question.instructions}),
            ),
            settings=ModelSettings(max_output_tokens=model.output_tokens),
        )

    def measure() -> ModelRequest | DecisionRequest:
        """冻结后仅做本地计算，不占用实时输入的事件循环。"""
        materials, _ = model.compiler(request, model=model.model)
        payload = decode_object(materials.encode("utf-8"))["payload"]
        if not isinstance(payload, dict):
            raise TypeError("专用判断编译材料缺少 payload 对象。")
        assessment = model.capacity(payload)
        return replace(request, metadata={"engine.capacity": encode(assessment)})

    return await prepare_off_loop(measure)
