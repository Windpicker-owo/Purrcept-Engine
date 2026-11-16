"""定义应用借给 Phone 插件的同库账本，不授予 SQL 或主脑访问。

插件在同一事件循环调用；实现由 app 按插件身份绑定，资源寿命归 Store。
消息身份在重连之间稳定，重复身份必须核对内容，确认只表示明确记录的阶段。
设备凭据仅保存摘要；原始凭据只在配对响应中交给手机。
"""

from typing import Protocol

from purrcept_engine.domain.inputs.models import MediaAsset
from purrcept_engine.values import JsonObject


class PhoneStore(Protocol):
    """宿主装配的单实例手机账本；内置 SQLite 实现借用唯一提交线程。

    写入返回表示事务完成，取消等待不撤销已入队事务。所有查询按插件及设备
    隔离，Bot 头像为插件级公开资料；身份冲突明确失败，数据库故障沿用
    StorageError，不能回退到内存。
    """

    async def begin_command(self, identity: str, intent: JsonObject) -> bool:
        """持久登记一次派发意图，已存在返回 False，不能据此再次派发。"""
        ...

    async def complete_command(self, identity: str, result: JsonObject) -> JsonObject:
        """保存实际设备回执并返回原派发记录，用于关联迟到结果。"""
        ...

    async def commands(self, *, identity: str | None = None) -> list[JsonObject]:
        """读取单条派发或最多100条未决命令，不授予重新执行资格。"""
        ...

    async def recover_inputs(self, device_id: str) -> int:
        """从已接管原文修复最多100条缺失聊天索引，不重新调度输入。"""
        ...

    async def put_media(self, device_id: str, content: bytes, media_type: str) -> str:
        """保存本设备完整附件并返回摘要，字节进入同库媒体表。"""
        ...

    async def media(self, device_id: str, media_id: str) -> MediaAsset | None:
        """读取本设备已授权附件或当前 Bot 头像，不暴露其他来源媒体。"""
        ...

    async def set_avatar(self, media_id: str | None) -> JsonObject:
        """以宿主已有图片设置 Bot 头像，None 恢复默认；返回已提交的头像版本。

        头像归插件实例持久保存，不要求手机在线。无效或缺失图片以
        StorageError(code="phone_invalid_avatar") 拒绝，原头像保持不变。
        """
        ...

    async def avatar(self) -> JsonObject | None:
        """读取最新头像版本；尚未设置返回 None，明确恢复默认仍带版本号。"""
        ...

    async def pair(self, device_id: str, name: str, token_hash: str) -> None:
        """绑定唯一手机；相同设备可更新凭据，换设备必须先撤销。"""
        ...

    async def authenticate(self, token_hash: str) -> str | None:
        """从凭据摘要取得已配对设备身份；未知或撤销返回 None。"""
        ...

    async def revoke(self) -> None:
        """撤销当前配对，不删除已经接管的原文和对外消息。"""
        ...

    async def append(self, device_id: str, identity: str, event: JsonObject) -> JsonObject:
        """同身份同内容返回首次事件；内容冲突拒绝，返回值带持久 seq。"""
        ...

    async def events(self, device_id: str, after: int, limit: int = 100) -> list[JsonObject]:
        """按持久序号升序读取有界增量；查询不确认投递。"""
        ...

    async def acknowledge(self, device_id: str, through: int) -> None:
        """单调保存手机已经落盘的连续事件前缀，拒绝超前确认。"""
        ...

    async def device(self) -> JsonObject:
        """返回当前设备和已确认游标，不返回凭据摘要。"""
        ...

    async def progress(self, device_id: str) -> JsonObject:
        """读取本设备最近24条消息的实际输入阶段和当前前台轮次的有限活动。

        仅返回因果身份、阶段、版本、时间和能力路径；不返回提示、思维文本、
        参数、其他来源的消息或模型响应。思考依据在途逻辑模型请求，行动依据
        当前轮次输入关联的运行中行动，不能靠消息接管时间猜测主脑已经开始。
        每条消息可附最近一次真实派发的能力路径和当前回执阶段，用于短行动
        已结束后的事实查询。当前前台轮次可以由其他来源发起，消息仍按设备隔离。
        实现借用 Store 的唯一提交线程，以一次只读操作取得一致的已提交视图。
        """
        ...
