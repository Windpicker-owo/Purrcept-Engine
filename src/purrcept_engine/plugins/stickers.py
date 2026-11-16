"""声明默认 me.stickers 通用能力，借用宿主共享库而不创建通道私有资源。

save 收藏已经读取或上传的媒体，list 将目录冻结为可续读快照，view 显式交付
实际像素，delete 只撤销收藏。QQ 和手机的发送行为仍归各自聊天适配器。
"""

import json
import time
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from purrcept_engine.domain.stickers import StickerError, StickerLibrary
from purrcept_engine.errors import ConfigError
from purrcept_engine.plugin_system import (
    CallContext,
    CallOutcome,
    CapabilityDeclaration,
    CapabilityInputError,
    EvidenceInputError,
    EvidencePort,
    EvidenceSnapshot,
    FunctionCapability,
    MediaReference,
    ObjectDeclaration,
    PluginBlueprint,
    PluginPorts,
    PluginRuntime,
)
from purrcept_engine.values import JsonValue, decode_object, encode_json


class _Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SaveSticker(_Parameters):
    """媒体身份来自实际输入、上传或 me.view_image，不把消息 ID 猜成媒体。"""

    media_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    tag: str = Field(min_length=1, max_length=64, pattern=r"\S")


class ListStickers(_Parameters):
    """续读绑定已冻结目录，不能混入新关键词。"""

    query: str | None = Field(default=None, max_length=64)
    cursor: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def source_or_cursor(self) -> "ListStickers":
        """防止续读改变搜索条件后跳过或重复条目。"""
        if self.cursor is not None and self.query is not None:
            raise ValueError("cursor 续读不能同时指定 query。")
        return self


class _Lookup(_Parameters):
    sticker_id: str = Field(min_length=1, max_length=128)


class StickerCapabilities:
    """默认工具绑定；宿主创建，调用只借用有限库与证据端口，不拥有其生命周期。"""

    def __init__(self, library: StickerLibrary, evidence: EvidencePort) -> None:
        """保存同库借用，不打开连接或提前读取图片。"""
        self.library, self.evidence = library, evidence

    async def save(self, parameters: SaveSticker, context: CallContext) -> CallOutcome:
        """收藏完整媒体，同内容保留跨通道稳定身份。"""
        del context
        try:
            record = await self.library.save(parameters.media_id, tag=parameters.tag)
        except StickerError as error:
            raise CapabilityInputError(str(error)) from error
        return CallOutcome("confirmed", record.value())

    async def list(self, parameters: ListStickers, context: CallContext) -> CallOutcome:
        """按标签搜索完整库，页长受限且后续修改不改变旧游标内容。"""
        del context
        page_chars = min(self.evidence.page_chars, 4000)
        if parameters.cursor is not None:
            page = await self.evidence.read_evidence_cursor(parameters.cursor, max_chars=page_chars)
        else:
            query = (parameters.query or "").strip().casefold()
            records = await self.library.list()
            selected: list[JsonValue] = [
                record.value() for record in records if query in record.tag.casefold()
            ]
            catalog = encode_json(selected).decode()
            snapshot = EvidenceSnapshot(
                evidence_id="stickers:" + uuid4().hex,
                version=1,
                kind="stickers.catalog",
                text=catalog,
                items_json=catalog,
                source_json=encode_json(
                    {"query": parameters.query or "", "total": len(selected)}
                ).decode(),
                occurred_at=time.time(),
            )
            saved = await self.evidence.put_evidence(snapshot)
            page = await self.evidence.read_evidence(saved.evidence_id, max_chars=page_chars)
        if page.snapshot.kind != "stickers.catalog":
            raise EvidenceInputError("游标不是共享贴纸目录快照。")
        metadata = decode_object(page.snapshot.source_json.encode())
        return CallOutcome(
            "confirmed",
            {
                "stickers": json.loads(page.text),
                "query": metadata.get("query"),
                "total": metadata.get("total"),
                "evidence_ref": page.snapshot.evidence_id,
                "version": page.snapshot.version,
                "next_cursor": page.next_cursor,
            },
        )

    async def view(self, parameters: _Lookup, context: CallContext) -> CallOutcome:
        """显式读出贴纸像素，目录查询不会隐式把整个库注入模型。"""
        try:
            record, asset = await self.library.read(sticker_id=parameters.sticker_id)
        except StickerError as error:
            raise CapabilityInputError(str(error)) from error
        reference = MediaReference(record.media_id, record.media_type, len(asset.content))
        saved = await self.evidence.capture(
            EvidenceSnapshot(
                evidence_id="sticker-view:" + context.call_id,
                version=1,
                kind="stickers.view",
                text="贴纸：" + record.tag,
                source_json=encode_json(record.value()).decode(),
                occurred_at=time.time(),
                media=(reference,),
            ),
            (asset,),
        )
        return CallOutcome("confirmed", record.value(), media=saved.media)

    async def delete(self, parameters: _Lookup, context: CallContext) -> CallOutcome:
        """仅在库确认删除后报告完成，历史聊天的图片引用继续有效。"""
        del context
        try:
            await self.library.delete(parameters.sticker_id)
        except StickerError as error:
            raise CapabilityInputError(str(error)) from error
        return CallOutcome("confirmed", {"sticker_id": parameters.sticker_id, "deleted": True})


def stickers_blueprint() -> PluginBlueprint:
    """默认装配共享能力，不依赖 QQ、手机或可选网络插件是否启用。"""
    declarations = (
        CapabilityDeclaration(
            "engine:stickers.save",
            "stickers",
            "save",
            "将已取得的图片媒体保存到共享贴纸库，供所有聊天通道复用。",
            SaveSticker,
            15.0,
            'await me.stickers.save(media_id="实际图片摘要", tag="猫猫")',
            "URL 或本地图片先调用 me.view_image；media_id 取返回的实际媒体引用。",
        ),
        CapabilityDeclaration(
            "engine:stickers.list",
            "stickers",
            "list",
            "分页浏览或搜索共享贴纸库的标签和稳定身份，不读取图片像素。",
            ListStickers,
            10.0,
            'await me.stickers.list(query="猫")',
        ),
        CapabilityDeclaration(
            "engine:stickers.view",
            "stickers",
            "view",
            "读取一张共享贴纸的真实图片。",
            _Lookup,
            10.0,
            'await me.stickers.view(sticker_id="库中实际身份")',
        ),
        CapabilityDeclaration(
            "engine:stickers.delete",
            "stickers",
            "delete",
            "从共享库删除一张收藏贴纸，保留历史聊天图片。",
            _Lookup,
            10.0,
            'await me.stickers.delete(sticker_id="库中实际身份")',
        ),
    )

    async def build(ports: PluginPorts) -> PluginRuntime:
        """借出宿主共享库，不创建额外文件索引或客户端。"""
        if ports.stickers is None or ports.evidence is None:
            raise ConfigError("共享贴纸能力需要宿主贴纸库与证据端口。")
        implementation = StickerCapabilities(ports.stickers, ports.evidence)
        return PluginRuntime(
            (
                FunctionCapability(declarations[0], implementation.save),
                FunctionCapability(declarations[1], implementation.list),
                FunctionCapability(declarations[2], implementation.view),
                FunctionCapability(declarations[3], implementation.delete),
            )
        )

    return PluginBlueprint(
        (ObjectDeclaration("stickers", "stickers", "共享贴纸库", "subject"),), declarations, build
    )
