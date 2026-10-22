"""定义模型输入内容声明，不猜测端点能力或替代完整请求容量检查。

应用配置与领域模型共享同一词表。刺激组装按此声明选择原生内容或文字占位，
原始证据不受影响；声明 video 不会为尚无视频块的 Core 添加传输实现。
"""

from typing import Literal

type ContentKind = Literal["text", "image", "audio", "video"]


def validate_supported_contents(contents: tuple[ContentKind, ...]) -> None:
    """校验不可变模型内容声明，文字是框架指令与媒体占位的必要载体。

    Args:
        contents: 包含 text 的非重复内容类型元组，顺序保留配置原貌。

    Raises:
        ValueError: 类型、容器、重复项或必要的 text 声明不符合约定。
    """
    if (
        not isinstance(contents, tuple)
        or any(item not in ("text", "image", "audio", "video") for item in contents)
        or "text" not in contents
        or len(set(contents)) != len(contents)
    ):
        raise ValueError("supports_contents 必须包含 text，且仅含不重复的 text/image/audio/video。")
