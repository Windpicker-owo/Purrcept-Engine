"""按来源与目标模型声明组装刺激，不制造预览、转写或 OCR 替代。

提供方负责内容语义，框架按来源分组、插入 TextBlock 标题并投影模型支持类型。文字、音频与图片
沿提供方布局交错交付，模型不支持的媒体在原位置变为明确文字引用，原件不变。
支持的媒体从同库恢复完整字节，损坏、缺失和传输不支持仍显式失败；声明支持并不
免除完整请求的容量核验。失败经历接续可显式只带引用。函数不读远端 URL 或调用模型。
"""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Protocol

from purrcept_core.models import (
    AudioBlock,
    AudioBytes,
    ContentBlock,
    ImageBlock,
    ImageBytes,
    Message,
    MessageRole,
    TextBlock,
)

from purrcept_engine.domain.context.templates import (
    INPUT_BATCH_PROMPT,
    INPUT_EVIDENCE_PROMPT,
    INPUT_SOURCE_PROMPT,
    MEDIA_REFERENCE_PROMPT,
    MISSING_MATERIALS_PROMPT,
)
from purrcept_engine.domain.inputs.content import InputMediaIndex, InputPresentation
from purrcept_engine.domain.inputs.models import InputRecord, MediaReference
from purrcept_engine.domain.model_requests.contents import ContentKind, validate_supported_contents
from purrcept_engine.errors import EngineError, StorageError
from purrcept_engine.serialization import render_yaml
from purrcept_engine.values import JsonObject

type InputMediaResolver = Callable[
    [tuple[MediaReference, ...]], Awaitable[tuple[ContentBlock, ...]]
]


class MediaStore(Protocol):
    """app 注入的只读 BLOB 端口，返回完整字节并核验内容摘要。"""

    async def get_media(self, media_id: str) -> bytes | None:
        """读取同库已接管材料。

        Args:
            media_id: 权威库内由内容摘要标识的完整媒体身份。

        Note:
            不存在返回 None，损坏原样失败。
        """
        ...


class UnsupportedMaterial(EngineError):
    """完整原文仍保留，但当前 Core 原生媒体路径不能表达该材料。"""


async def media_blocks(
    store: MediaStore, media: tuple[MediaReference, ...]
) -> tuple[ContentBlock, ...]:
    """按引用顺序展开全部原生媒体，不按附件数量猜测模型是否已经查看。

    Args:
        store: 应用借出的领域存储端口，实际连接和关闭归资源所有者。
        media: 已接管媒体的引用集合，展开时必须取得原始 BLOB。

    Returns:
        按原始顺序展开的完整 Core 内容块，包括真实原生媒体。

    Raises:
        StorageError: 原生媒体引用不存在的 BLOB；原生媒体长度与引用不符。
        UnsupportedMaterial: 参数、材料或当前状态不满足此入口的约束。
    """
    blocks: list[ContentBlock] = []
    for reference in media:
        content = await store.get_media(reference.media_id)
        if content is None:
            raise StorageError("原生媒体引用不存在的 BLOB。", code="invalid_record")
        if len(content) != reference.byte_length:
            raise StorageError("原生媒体长度与引用不符。", code="invalid_record")
        if reference.media_type in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            blocks.append(ImageBlock(ImageBytes(content, reference.media_type)))
        elif reference.media_type in ("audio/wav", "audio/mpeg", "audio/mp3"):
            media_type = (
                "audio/mpeg" if reference.media_type == "audio/mp3" else reference.media_type
            )
            blocks.append(AudioBlock(AudioBytes(content, media_type)))
        else:
            raise UnsupportedMaterial(f"当前原生材料路径不支持媒体类型 {reference.media_type}。")
    return tuple(blocks)


def _media_kind(reference: MediaReference) -> ContentKind | None:
    """只按已接管 MIME 分类，不从附件名称、说明或模型名称猜测内容。"""
    category = reference.media_type.partition("/")[0]
    if category in ("image", "audio", "video"):
        return category
    return None


def _media_reference(reference: MediaReference, alt_text: str | None, *, reason: str) -> TextBlock:
    """在媒体原位置说明未交付原因及可追踪身份，说明文字不冒充已感知内容。"""
    fields: JsonObject = {
        "media_id": reference.media_id,
        "media_type": reference.media_type,
        "byte_length": reference.byte_length,
    }
    if reference.name is not None:
        fields["name"] = reference.name
    if alt_text is not None:
        fields["alt_text"] = alt_text
    return TextBlock(
        MEDIA_REFERENCE_PROMPT.render({"reason": reason, "reference": render_yaml(fields)})
    )


async def _record_content(
    record: InputRecord,
    presentation: InputPresentation | None,
    media: InputMediaResolver,
    *,
    media_as_references: bool,
    supports_contents: tuple[ContentKind, ...],
) -> tuple[ContentBlock, ...]:
    """先选择能交付的附件再读 BLOB，按冻结索引回填，避免过滤后错配原位置。"""
    layout = (
        presentation.blocks
        if presentation is not None
        else (
            TextBlock(record.text),
            *(InputMediaIndex(index) for index in range(len(record.media))),
        )
    )
    indices = {block.index for block in layout if isinstance(block, InputMediaIndex)}
    if indices != set(range(len(record.media))):
        raise StorageError("输入内容布局与已接管附件不一致。", code="invalid_record")
    selected = tuple(
        index
        for index, reference in enumerate(record.media)
        if not media_as_references and _media_kind(reference) in supports_contents
    )
    native: dict[int, ContentBlock] = {}
    if selected:
        resolved = await media(tuple(record.media[index] for index in selected))
        if len(resolved) != len(selected):
            raise StorageError("原生媒体端口没有返回完整附件序列。", code="invalid_record")
        native = dict(zip(selected, resolved, strict=True))
    blocks: list[ContentBlock] = []
    for block in layout:
        if isinstance(block, TextBlock):
            blocks.append(block)
            continue
        reference = record.media[block.index]
        if media_as_references or block.index not in native:
            reason = (
                "仅提供引用，未展开像素或音频："
                if media_as_references
                else "当前模型输入通道不支持此媒体，未交付原件；以下仅为引用与提供方说明："
            )
            blocks.append(_media_reference(reference, block.alt_text, reason=reason))
        else:
            original = native[block.index]
            if not isinstance(original, (ImageBlock, AudioBlock)):
                raise UnsupportedMaterial("input.unsupported_native_content")
            kind = "image" if isinstance(original, ImageBlock) else "audio"
            if kind != _media_kind(reference):
                raise StorageError(
                    "原生媒体端口返回的类型与附件引用不一致。", code="invalid_record"
                )
            blocks.append(replace(original, alt_text=block.alt_text))
    if record.context.missing_materials:
        blocks.append(
            TextBlock(
                MISSING_MATERIALS_PROMPT.render(
                    {"items": "\n".join(record.context.missing_materials)}
                )
            )
        )
    return tuple(blocks)


async def input_blocks(
    records: tuple[InputRecord, ...],
    *,
    media: InputMediaResolver,
    media_as_references: bool = False,
    supports_contents: tuple[ContentKind, ...] = ("text",),
) -> tuple[ContentBlock, ...]:
    """按来源分组并按模型支持类型交付内容，保持文字与媒体位置顺序。

    Args:
        records: 按接管顺序取得的完整输入记录。
        media: 从同库按所选引用顺序恢复原生块的只读端口，不支持的媒体不调用此端口。
        media_as_references: 是否仅在原位置交付引用，用于未完成轮次的经历接续。
        supports_contents: 目标模型允许的内容类型，默认仅 text；不支持的媒体原位转文字。

    Returns:
        分组后的 Core 块序列；组按首次出现排序，同组输入按接管顺序排列。

    Raises:
        ValueError: 输入批次为空或模型内容声明非法。
        StorageError: 冻结布局与媒体引用不一致，或端口缺少原生媒体。
        UnsupportedMaterial: 原生端口返回无法作为输入交付的内容块。

    Note:
        框架不解析 YAML、不解释插件字段、不摘要或改写提供方文字。跨来源分组不
        表示全局时间顺序；框架展示信封发生/接收时间，细分消息关系由提供方表达。
        不同接入账号不混成一组。未声明布局时采用原文/附件的固定默认布局，
        不把 details_json 塞入模型。
        批次可能受容量和数量限制，因此标题不声称包含所有仍在缓冲区的刺激。
        不支持的媒体不读取 BLOB；支持的媒体仍须通过存储与容量校验，故障不降级。
    """
    if not records:
        raise ValueError("前台输入批次不能为空。")
    validate_supported_contents(supports_contents)
    groups: dict[tuple[str, str, str], list[tuple[InputRecord, InputPresentation | None]]] = {}
    for record in records:
        presentation = record.context.presentation
        source = record.source.adapter_id if presentation is None else presentation.source
        source = source.replace("\r", "\\r").replace("\n", "\\n")
        key = (source, record.source.adapter_id, record.source.account_id)
        groups.setdefault(key, []).append((record, presentation))

    blocks: list[ContentBlock] = [TextBlock(INPUT_BATCH_PROMPT.render())]
    for (source, _, _), entries in groups.items():
        blocks.append(TextBlock(INPUT_SOURCE_PROMPT.render({"source": source})))
        for record, presentation in entries:
            # 时间属于接管信封，不依赖具体插件是否在正文中提供完整日期或秒数。
            # 明示日期和记录中的时区偏移，避免跨日或迟到输入被历史中的位置误导。
            blocks.append(
                TextBlock(
                    INPUT_EVIDENCE_PROMPT.render(
                        {
                            "input_id": record.input_id,
                            "occurred_at": record.occurred_at.isoformat(),
                            "received_at": record.received_at.isoformat(),
                        }
                    )
                )
            )
            blocks.extend(
                await _record_content(
                    record,
                    presentation,
                    media,
                    media_as_references=media_as_references,
                    supports_contents=supports_contents,
                )
            )
    return tuple(blocks)


async def input_message(
    store: MediaStore,
    records: tuple[InputRecord, ...],
    *,
    media_as_references: bool = False,
    supports_contents: tuple[ContentKind, ...] = ("text",),
) -> Message:
    """把按来源分组的输入块装成同一 Core user 消息，保留原始证据关联。

    Args:
        store: 只读取已接管 BLOB 的存储端口。
        records: 本批已接管输入，分组和顺序契约见 input_blocks。
        media_as_references: 是否保留尚未展开媒体的经历引用。默认 False。
        supports_contents: 模型允许交付的内容类型，默认仅 text；其余媒体保留文字引用。

    Returns:
        包含提供方原内容块及框架新增文字块的消息，metadata 关联完整 input_id。

    Raises:
        ValueError: 输入批次为空或模型内容声明非法。
        StorageError: 权威媒体缺失或冻结布局损坏。
        UnsupportedMaterial: 原生媒体类型无法交付。
    """

    async def native(references: tuple[MediaReference, ...]) -> tuple[ContentBlock, ...]:
        """只恢复同库原件，不查询外部平台。"""
        return await media_blocks(store, references)

    blocks = await input_blocks(
        records,
        media=native,
        media_as_references=media_as_references,
        supports_contents=supports_contents,
    )
    return Message(
        MessageRole.USER,
        blocks,
        metadata={"evidence": tuple(record.input_id for record in records)},
    )
