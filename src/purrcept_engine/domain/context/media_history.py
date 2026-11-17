"""按完整请求大小批量清理已完成历史的媒体，减少缓存前缀重写。

HistoryMedia 由会话、预检或摘要请求各自创建，计量真实 SDK payload 的 UTF-8
JSON 字节数，包含内联媒体的 base64 运输表示。未超过阈值时原样保留历史；
超过时一次替换全部已完成历史中的图片、音频及回执音频编码。当前轮次媒体
始终保留，阈值是历史清理触发线，不是拒收新媒体的硬上限。
清理决定在轮内冻结，后续请求及成功检查点复用相同投影；失败不安装新历史。
原始媒体和证据由存储模块保留，本模块不识别、转写或删除原件。
"""

import logging
from dataclasses import replace

from purrcept_core.models import (
    AudioBlock,
    ContentBlock,
    ConversationCheckpoint,
    ImageBlock,
    Message,
    ModelRequest,
    TextBlock,
    ToolResultBlock,
)

from purrcept_engine.domain.context.audio_receipts import receipt_messages_view
from purrcept_engine.domain.context.templates import EXPIRED_AUDIO_PROMPT, EXPIRED_IMAGE_PROMPT
from purrcept_engine.domain.model_requests.ports import MaterialCompiler
from purrcept_engine.serialization import encode
from purrcept_engine.values import decode_object

DEFAULT_MEDIA_CLEANUP_THRESHOLD_BYTES = 30_000_000


def _expire_block(block: ContentBlock) -> ContentBlock:
    """只替换明确的媒体块，不解释文字标记，也不破坏工具调用和回执协议。"""
    if isinstance(block, AudioBlock):
        return TextBlock(EXPIRED_AUDIO_PROMPT.render())
    if isinstance(block, ImageBlock):
        return TextBlock(EXPIRED_IMAGE_PROMPT.render())
    if isinstance(block, ToolResultBlock):
        content = tuple(_expire_block(item) for item in block.content)
        if any(item is not original for item, original in zip(content, block.content, strict=True)):
            return replace(block, content=content)
    return block


def _expire_message(message: Message) -> Message:
    """递归替换一条旧消息内的媒体，保留其来源、角色和工具调用身份。"""
    content = tuple(_expire_block(block) for block in message.content)
    if any(item is not original for item, original in zip(content, message.content, strict=True)):
        return replace(message, content=content)
    return message


def expire_history_media(checkpoint: ConversationCheckpoint) -> ConversationCheckpoint:
    """显式清理全部已完成轮次的媒体，未完成后缀保持原样。

    Args:
        checkpoint: Core 已校验的完整检查点，轮次边界采用排他消息下标。

    Returns:
        媒体已清理的独立视图；没有媒体时返回原对象，不修改传入值。

    Note:
        轮次包含该次输入、所有工具调用与最终内部响应，不按消息数或附件数
        计数。调用方先判断请求大小，不能再按轮龄定期调用本函数。
        媒体的持久化与归档归原证据所有者。
        普通文字、隐藏推理及工具协议不属于媒体，不按内容块种类删除；
        仅可校验文件头与编码的内联音频运输字节投影为引用。
    """
    history = checkpoint.history
    if not history.turn_boundaries:
        return checkpoint
    end = history.turn_boundaries[-1]
    older = history.messages[:end]
    projected = _expire_messages(older)
    if projected == older:
        return checkpoint
    return replace(
        checkpoint,
        history=replace(history, messages=(*projected, *history.messages[end:])),
    )


def _expire_messages(messages: tuple[Message, ...]) -> tuple[Message, ...]:
    """同批清理原生媒体与回执运输编码，保留消息和工具关联位置。"""
    return tuple(_expire_message(message) for message in receipt_messages_view(messages))


class HistoryMedia:
    """一次 Core 轮次独占的媒体清理决定，预检与摘要可创建独立实例。

    completed_messages 是轮次开始时已完成历史的排他下标，整个轮次保持不变。
    prepare 在纯准备线程中串行调用；project 供后续请求和成功检查点采用同一决定。
    工具结果和中途新输入位于该下标之后，不能因请求超限而被提前清理。
    """

    def __init__(
        self,
        completed_messages: int,
        *,
        compiler: MaterialCompiler,
        threshold_bytes: int = DEFAULT_MEDIA_CLEANUP_THRESHOLD_BYTES,
    ) -> None:
        """固定历史边界、真实 SDK 编译器和清理阈值，不编译或修改历史。"""
        if completed_messages < 0 or threshold_bytes <= 0:
            raise ValueError("历史媒体边界必须非负，清理阈值必须为正。")
        self.completed_messages = completed_messages
        self.compiler = compiler
        self.threshold_bytes = threshold_bytes
        self._original_prefix: tuple[Message, ...] = ()
        self._expired_prefix: tuple[Message, ...] | None = None

    def project(self, messages: tuple[Message, ...]) -> tuple[Message, ...]:
        """应用本轮已决定的批量清理，当前轮次和消息数量保持不变。"""
        if self._expired_prefix is None:
            return messages
        if len(messages) < self.completed_messages:
            raise RuntimeError("媒体清理边界超出实际历史。")
        older = messages[: self.completed_messages]
        if older == self._expired_prefix:
            return messages
        if older != self._original_prefix:
            raise RuntimeError("已清理的历史媒体前缀在轮次中发生变化。")
        return (*self._expired_prefix, *messages[self.completed_messages :])

    def prepare(
        self,
        request: ModelRequest,
        *,
        model: str,
        measured_request: ModelRequest | None = None,
    ) -> ModelRequest:
        """超限时冻结清理决定；measured_request 可提供已投影动态尾部的实际布局。

        阈值使用 payload 的完整 JSON 运输大小，包含工具 schema 与多媒体编码。
        同轮后续请求只重用清理决定，不再次计量或清理本轮新取得的媒体。
        """
        if self._expired_prefix is None and self.completed_messages:
            materials, _ = self.compiler(measured_request or request, model=model)
            payload = decode_object(materials.encode("utf-8"))["payload"]
            if not isinstance(payload, dict):
                raise TypeError("SDK 编译材料必须包含 payload 对象。")
            request_bytes = len(encode(payload).encode("utf-8"))
            if request_bytes > self.threshold_bytes:
                # 在准备线程中只转换一次；后续请求与成功提交直接复用冻结前缀，
                # 不在事件循环中反复扫描旧回执文字或复制媒体。
                self._original_prefix = request.messages[: self.completed_messages]
                self._expired_prefix = _expire_messages(self._original_prefix)
                logging.getLogger(__name__).info(
                    "[历史媒体清理] 请求 %d 字节超过阈值 %d，批量清理已完成历史的媒体。",
                    request_bytes,
                    self.threshold_bytes,
                )
        messages = self.project(request.messages)
        return request if messages is request.messages else replace(request, messages=messages)
