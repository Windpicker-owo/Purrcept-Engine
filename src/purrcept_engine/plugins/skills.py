"""解析笔记技能的有限 frontmatter，并生成不含正文的目录值。

文件读取和扫描属于 Notebook；本模块只保留既有 name/description 单行语义，
不实现通用 YAML、不执行技能。ParsedNote 同时携带正文起点，使分页读取能保留
body 字段而不重复交付整份正文。目录值交给实际请求前的异步快照阶段消费。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from purrcept_engine.values import JsonObject


@dataclass(frozen=True, slots=True)
class ParsedNote:
    """一次已稳定取得的文件内容；版本为实际 UTF-8 字节的内容摘要。

    Attributes:
        path: 此操作定位的明确路径。
        name: 从技能元数据取得的展示名称，区别于文件路径。
        description: 向调用者或模型说明可观察行为的文字。
        content: 本次明确写入的完整正文。
        body_start: 技能文件中正文的起始字符位置。
        version: 目标记录的确切版本，区别于可变的当前版本。
        byte_count: 当前内容实际占用的字节数。
        occurred_at: 原始事实实际发生的 Unix 时间戳（秒）。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    path: str
    name: str
    description: str | None
    content: str
    body_start: int
    version: str
    byte_count: int
    occurred_at: float

    @property
    def skill(self) -> bool:
        """description 表示适用说明，有说明才进入技能目录。"""
        return self.description is not None

    def catalog_entry(self, read_path: str) -> JsonObject:
        """投影名称、适用说明、版本和读取入口，正文不能进入目录。

        Args:
            read_path: 用于后续展开该技能正文的已装配能力路径。

        Returns:
            业务对象，按实际结果提供 path、name、description、version、read 字段。
        """
        return {
            "path": self.path,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "read": read_path,
        }


def parse_note(
    path: str, content: str, *, version: str, byte_count: int, occurred_at: float
) -> ParsedNote:
    """保留既有单行 name/description 解析，未知元数据不提升为宿主指令。

    Args:
        path: 此操作定位的明确路径。
        content: 本次明确写入的完整正文。
        version: 目标记录的确切版本，区别于可变的当前版本。
        byte_count: 当前内容实际占用的字节数。
        occurred_at: 原始事实实际发生的 Unix 时间戳（秒）。

    Returns:
        笔记路径、版本和完整正文的解析结果。

    Note:
        只采用首个 frontmatter 结束分隔符，正文里的后续分隔符保持正文。引号仅
        去除一对匹配外层引号；不宣称支持 YAML 转义、嵌套或多行表达式。
    """
    lines = content.splitlines(keepends=True)
    metadata: dict[str, str] = {}
    body_start = 0
    if lines and lines[0].lstrip("\ufeff").strip() == "---":
        offset = len(lines[0])
        for index, line in enumerate(lines[1:], 1):
            offset += len(line)
            if line.strip() == "---":
                body_start = offset
                for item in lines[1:index]:
                    key, separator, value = item.strip().partition(":")
                    if separator and key.lower() in ("name", "description"):
                        value = value.strip()
                        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                            value = value[1:-1]
                        if value.strip():
                            metadata[key.lower()] = value.strip()
                break
    note_path = PurePosixPath(path)
    default_name = (
        note_path.parent.name
        if note_path.name.lower() == "skill.md" and note_path.parent.name
        else note_path.stem
    )
    return ParsedNote(
        path,
        metadata.get("name", default_name),
        metadata.get("description"),
        content,
        body_start,
        version,
        byte_count,
        occurred_at,
    )
