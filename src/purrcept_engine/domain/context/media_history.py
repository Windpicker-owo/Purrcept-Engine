"""按完整轮次保留近期媒体，不识别、转写或删除原始证据。

会话恢复、首发容量预览及摘要材料共同使用 expire_history_media，最近两个
已完成轮次和未完成后缀保留图片与音频。更早的原生媒体块原位换成固定文字，
包括嵌套工具结果和 URL 图像；消息、工具关联、回执文字及轮次边界保持原值。
这里只产生不可变检查点视图。新会话成功后才随正常提交安装，失败保留原版本；
已保存的输入、模型请求和工具回执继续提供原始媒体证据。
"""

from dataclasses import replace

from purrcept_core.models import (
    AudioBlock,
    ContentBlock,
    ConversationCheckpoint,
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
)

from purrcept_engine.domain.context.templates import EXPIRED_AUDIO_PROMPT, EXPIRED_IMAGE_PROMPT


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
    """保留最近两个完整轮次及当前后缀的媒体，旧图片和音频原位替换为固定文本。

    Args:
        checkpoint: Core 已校验的完整检查点，轮次边界采用排他消息下标。

    Returns:
        媒体窗口已应用的独立视图；没有过期媒体时返回原对象，不修改传入值。

    Note:
        轮次包含该次输入、所有工具调用与最终内部响应，不按消息数或附件数
        计数。摘要占位没有新轮次边界。投影重复执行不继续缩短窗口；当前轮内
        工具循环和传输重试不推进过期范围。媒体的持久化与归档归原证据所有者。
        文字、隐藏推理及工具协议不属于媒体，不按内容块是否为 TextBlock 来删除。
    """
    history = checkpoint.history
    if len(history.turn_boundaries) <= 2:
        return checkpoint
    end = history.turn_boundaries[-3]
    older = history.messages[:end]
    projected = tuple(_expire_message(message) for message in older)
    if all(item is original for item, original in zip(projected, older, strict=True)):
        return checkpoint
    return replace(
        checkpoint,
        history=replace(history, messages=(*projected, *history.messages[end:])),
    )
