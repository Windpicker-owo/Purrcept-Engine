"""冻结输入提供方的来源与内容顺序，不保存媒体字节或解释平台字段。

InputSubmission 将 Core 内容块转换为此布局：文字原样保存，媒体只记同一提交中
附件的索引及 alt_text。布局随现有 JSON 扩展入账；组装时按索引从同库恢复原生块，
不会把所有音频/图片移到文字末尾。本模块不读取媒体或决定注意级别。
"""

from dataclasses import dataclass

from purrcept_core.models import TextBlock

from purrcept_engine.values import JsonObject, JsonValue

PRESENTATION_KEY = "engine.input_presentation"


@dataclass(frozen=True, slots=True)
class InputMediaIndex:
    """一个原生媒体块在有序内容中的位置，媒体实际所有权仍归同次提交。

    Attributes:
        index: InputSubmission.media / InputRecord.media 中从零起算的附件位置。
        alt_text: 原始 Core 块的说明，不能作为音频转写或已读取像素的证明。
    """

    index: int
    alt_text: str | None = None

    def __post_init__(self) -> None:
        """拒绝布尔索引和负数，防止布局引用其他输入或意外倒序索引。"""
        if type(self.index) is not int or self.index < 0:
            raise ValueError("输入媒体索引必须是非负整数。")
        if self.alt_text is not None and (
            not isinstance(self.alt_text, str) or not self.alt_text.strip()
        ):
            raise ValueError("输入媒体说明必须是非空字符串或 None。")


@dataclass(frozen=True, slots=True)
class InputPresentation:
    """提供方在正式接管前冻结的分组来源与内容顺序。

    Attributes:
        source: 提供方显式声明的非空单行分组标识，例如 me.phone.qq 或 microphone。
        blocks: TextBlock 与附件位置的不可变序列，顺序和重复出现都有实际语义。

    Note:
        通过 InputSubmission.with_content 创建。宿主增加标题和证据关联，不改写
        文字，也不将中间的媒体挪到末尾；模型不支持的媒体在原位置变为文字引用。
        块中不允许工具、推理或待下载 URL。
    """

    source: str
    blocks: tuple[TextBlock | InputMediaIndex, ...]

    def __post_init__(self) -> None:
        """在持久化之前核验来源与有限块种类，不借任意对象探测内容。"""
        if (
            not isinstance(self.source, str)
            or not self.source.strip()
            or any(char in self.source for char in "\r\n")
        ):
            raise ValueError("输入展示来源必须是非空单行标识。")
        if (
            not isinstance(self.blocks, tuple)
            or not self.blocks
            or any(not isinstance(block, (TextBlock, InputMediaIndex)) for block in self.blocks)
        ):
            raise ValueError("输入展示必须是非空的文字块与媒体位置元组。")

    def to_json(self) -> JsonObject:
        """取得独立 JSON 布局快照，不把原生媒体字节重复编码为 base64。

        Returns:
            仅含来源、原文与媒体索引/说明的 JSON 对象。
        """
        blocks: list[JsonValue] = []
        for block in self.blocks:
            if isinstance(block, TextBlock):
                blocks.append({"type": "text", "text": block.text})
            else:
                blocks.append({"type": "media", "index": block.index, "alt_text": block.alt_text})
        return {"source": self.source, "blocks": blocks}

    @classmethod
    def from_json(cls, value: JsonValue) -> "InputPresentation":
        """严格恢复已接管布局，未知字段或块种类按损坏处理。

        Args:
            value: 持久化 JSON 扩展中的展示对象，不接受字段猜测或缺省修复。

        Returns:
            独立且不可变的来源与内容布局。

        Raises:
            ValueError: 对象字段、来源、块种类、文字、索引或媒体说明不合法。
        """
        if not isinstance(value, dict) or set(value) != {"source", "blocks"}:
            raise ValueError("输入展示字段不符合契约。")
        source, raw_blocks = value["source"], value["blocks"]
        if not isinstance(source, str) or not isinstance(raw_blocks, list):
            raise ValueError("输入展示来源和内容列表无效。")
        blocks: list[TextBlock | InputMediaIndex] = []
        for block in raw_blocks:
            if not isinstance(block, dict):
                raise ValueError("输入展示块必须是对象。")
            if block.get("type") == "text" and set(block) == {"type", "text"}:
                text = block["text"]
                if not isinstance(text, str):
                    raise ValueError("输入文字块必须保留字符串原文。")
                blocks.append(TextBlock(text))
            elif block.get("type") == "media" and set(block) == {"type", "index", "alt_text"}:
                index, alt_text = block["index"], block["alt_text"]
                if isinstance(index, bool) or not isinstance(index, int):
                    raise ValueError("输入媒体索引必须是整数。")
                if alt_text is not None and not isinstance(alt_text, str):
                    raise ValueError("输入媒体说明无效。")
                blocks.append(InputMediaIndex(index, alt_text))
            else:
                raise ValueError("输入展示块的种类或字段不符合契约。")
        return cls(source, tuple(blocks))
