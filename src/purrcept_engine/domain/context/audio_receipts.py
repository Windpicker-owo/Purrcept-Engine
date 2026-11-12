"""在模型视图中把回执内的音频编码替换为可核验引用，原始证据保持完整。

新合成能力返回文件路径；本模块同时处理既有历史和原文分页里的内联 WAV/MP3。
只识别可完整解码且有音频文件头的长 base64，不按字段名删除任意正文，也不
把替换标记当作听过音频。这里不播放、转写、写库或取得新的媒体呈现资格。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import replace

from purrcept_core.models import ContentBlock, Message, MessageRole, TextBlock, ToolResultBlock

from purrcept_engine.domain.knowledge.models import EvidencePage

_ENCODED = re.compile(r"(?:base64://)?(?P<data>[A-Za-z0-9+/]{128,}={0,2})")


def _audio_reference(encoded: str) -> str | None:
    """先检查短文件头再完整校验编码，不探测网络、路径或加载音频解码器。"""
    try:
        head = base64.b64decode(encoded[:16], validate=True)
    except (ValueError, binascii.Error):
        return None
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        media_type = "audio/wav"
    elif head.startswith(b"ID3") or (
        len(head) >= 3
        and head[0] == 255
        and head[1] & 0xE0 == 0xE0
        and head[1] & 6 != 0
        and head[2] & 0xF0 not in (0, 0xF0)
    ):
        media_type = "audio/mpeg"
    else:
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None
    digest = hashlib.sha256(data).hexdigest()
    # 标记不含引号或反斜杠，嵌在 JSON 字符串或二次编码 JSON 中仍保持语法有效。
    # 摘要只表示原件摘要；不声称该身份已经成为 Store 的 MediaReference。
    return f"<音频编码未交付 {media_type} bytes={len(data)} sha256={digest} 原件保留于当前回执>"


def audio_text_view(text: str, *, start: int = 0, end: int | None = None) -> str:
    """返回正文范围的模型视图，音频只显示引用，分页切入编码中部也不泄漏片段。

    Args:
        text: 仍完整保留的权威原文或工具正文。
        start: 原文字符范围起点；分页时使用原始 slice 坐标。
        end: 原文字符范围排他终点；None 表示全文。

    Returns:
        原范围中的普通正文和音频引用，不改变原始字符坐标、游标或权威记录。

    Raises:
        ValueError: 范围不属于完整原文。
    """
    stop = len(text) if end is None else end
    if not 0 <= start <= stop <= len(text):
        raise ValueError("音频回执视图范围不属于原文。")
    if start == stop:
        return ""
    pieces: list[str] = []
    position = start
    for match in _ENCODED.finditer(text):
        if match.end() <= start:
            continue
        if match.start() >= stop:
            break
        reference = _audio_reference(match.group("data"))
        if reference is None:
            continue
        pieces.append(text[position : max(position, match.start())])
        pieces.append(reference)
        position = min(stop, match.end())
    pieces.append(text[position:stop])
    return "".join(pieces)


def evidence_text_view(page: EvidencePage) -> str:
    """用整份回执定位音频后投影原页，不能把省略的二进制记为实际感知。

    Args:
        page: 已核验的原版本及分页范围，范围和阅读账本仍沿用原件。

    Returns:
        行动或调用回执的模型正文视图；其它来源保持原文。
    """
    snapshot = page.snapshot
    if snapshot is None or snapshot.kind not in ("action_result", "call_receipt"):
        return page.text
    if page.slice is not None and page.slice.unit == "characters":
        return audio_text_view(snapshot.text, start=page.slice.start, end=page.slice.end)
    return audio_text_view(page.text)


def receipt_messages_view(messages: tuple[Message, ...]) -> tuple[Message, ...]:
    """投影历史工具正文，保留工具身份、消息数量、用户输入和真实轮次边界。

    Args:
        messages: Core 已提交历史或本轮取得的工具材料，不含可变共享对象。

    Returns:
        独立的模型消息视图；原始历史和同库回执可继续用于核验和恢复。
    """
    projected: list[Message] = []
    for message in messages:
        if message.role is not MessageRole.TOOL:
            projected.append(message)
            continue
        blocks: list[ContentBlock] = []
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                content = tuple(
                    replace(part, text=audio_text_view(part.text))
                    if isinstance(part, TextBlock)
                    else part
                    for part in block.content
                )
                block = replace(block, content=content)
            blocks.append(block)
        projected.append(replace(message, content=tuple(blocks)))
    return tuple(projected)
