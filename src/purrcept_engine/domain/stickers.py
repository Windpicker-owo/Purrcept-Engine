"""Bot 的通用贴纸身份与宿主借给聊天插件的共享库契约。

库记录只引用完整媒体，不保存 QQ 消息、手机凭据或发送目标。宿主按数据库
创建一个实现，通用工具、QQ 和手机发言借用同一端口；发送效果仍由各通道确认。
APP 用户自己的收藏由手机拥有，不属于这个库。
"""

from dataclasses import dataclass
from typing import Protocol

from purrcept_engine.domain.inputs.models import MediaAsset
from purrcept_engine.values import JsonObject


class StickerError(ValueError):
    """贴纸不存在、标签冲突或图片无效；可纠正拒绝不改变原库记录。"""


@dataclass(frozen=True, slots=True)
class StickerRecord:
    """跨通道稳定身份及图片摘要；digest 仅供适配器匹配平台图片的 MD5。"""

    sticker_id: str
    tag: str
    media_id: str
    media_type: str
    digest: str
    created_at: str

    def value(self) -> JsonObject:
        """公开可用于检索、发送和预览的元数据，不交付图片字节。"""
        return {
            "sticker_id": self.sticker_id,
            "tag": self.tag,
            "media_id": self.media_id,
            "media_type": self.media_type,
            "created_at": self.created_at,
        }


class StickerLibrary(Protocol):
    """宿主创建并注入的共享库，由通用能力和聊天适配器异步调用。

    内置实现为 SqliteStickerLibrary，与母 Store 同寿命并串行提交事务。
    同字节重存保留 sticker_id 并更新标签；不同图片不能抢占已有标签。
    读取不借出连接或可变记录，保存返回时媒体与索引已原子提交。
    StickerError 表示业务拒绝，存储故障原样传播；调用等待取消不撤销已受理事务。
    删除只撤销收藏，已发送消息仍保留其媒体引用，不回收仍被其他记录使用的字节。
    """

    async def save(self, media_id: str, *, tag: str) -> StickerRecord:
        """收藏宿主已保存的实际图片；media_id 来自看图、输入或媒体上传结果。"""
        ...

    async def save_image(self, asset: MediaAsset, *, tag: str) -> StickerRecord:
        """适配器取得完整图片后原子导入媒体与收藏，不要求先建立通道私有索引。"""
        ...

    async def list(self) -> tuple[StickerRecord, ...]:
        """按创建顺序取得只含元数据的完整快照；用户和网络分页由调用方负责。"""
        ...

    async def read(
        self, *, sticker_id: str = "", tag: str = "", media_id: str = ""
    ) -> tuple[StickerRecord, MediaAsset]:
        """按贴纸 ID、媒体 ID、标签的优先级读取同次快照的记录及完整图片。"""
        ...

    async def delete(self, sticker_id: str) -> None:
        """撤销确切贴纸 ID 的收藏；不存在时明确拒绝，不猜测删除成功。"""
        ...
