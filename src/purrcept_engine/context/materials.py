"""把已接管原文和同库媒体装为 Core 材料，不制造预览或 OCR 替代。

来源、关系和程序信号作为 user 观察保留；真正图像/音频按同一 BLOB 的
原生内容块交付。未知媒体类型、缺失内容与格式不支持显式失败，容量规则
另在完整请求边界核验。函数不读远端 URL 或调用模型。
"""

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

from purrcept_engine.errors import EngineError, StorageError
from purrcept_engine.storage.model_requests import encode
from purrcept_engine.storage.models import InputRecord, MediaReference


class MediaStore(Protocol):
    """app 注入的只读 BLOB 端口，返回完整字节并核验内容摘要。"""

    async def get_media(self, media_id: str) -> bytes | None:
        """读取同库已接管材料；不存在返回 None，损坏原样失败。"""
        ...


class UnsupportedMaterial(EngineError):
    """完整原文仍保留，但当前 Core 原生媒体路径不能表达该材料。"""


async def media_blocks(
    store: MediaStore, media: tuple[MediaReference, ...]
) -> tuple[ContentBlock, ...]:
    """按引用顺序展开全部原生媒体，不按附件数量猜测模型是否已经查看。"""
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


async def input_message(store: MediaStore, records: tuple[InputRecord, ...]) -> Message:
    """完整提供本批每项输入及原生媒体，输入身份与真实顺序始终独立。"""
    if not records:
        raise ValueError("前台输入批次不能为空。")
    blocks: list[ContentBlock] = []
    for record in records:
        blocks.append(
            TextBlock(
                encode(
                    {
                        "material_kind": "original_input",
                        "input_id": record.input_id,
                        "source": record.source,
                        "occurred_at": record.occurred_at.isoformat(),
                        "received_at": record.received_at.isoformat(),
                        "text": record.text,
                        "relations": record.context.details_json,
                        "missing_materials": record.context.missing_materials,
                        "media": record.media,
                    }
                )
            )
        )
        blocks.extend(await media_blocks(store, record.media))
    return Message(
        MessageRole.USER,
        tuple(blocks),
        metadata={"evidence": tuple(record.input_id for record in records)},
    )
