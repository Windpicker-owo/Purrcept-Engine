"""定义合成、播放与中断共用的不可变音频阶段记录。

载荷采用已编码 JSON，与实际插件及播放身份关联；这里只恢复数据形状，
不持有播放器、客户端或数据库连接。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from purrcept_engine.values import JsonObject, decode_object

FACTS = frozenset(
    {
        "accepted",
        "synthesis_started",
        "synthesized",
        "playback_started",
        "finished",
        "control_accepted",
        "control_result",
        "control_confirmed",
        "late_confirmed",
    }
)


MAX_FACT_BYTES = 65536


@dataclass(frozen=True, slots=True)
class AudioFact:
    """一项阶段的持久结果，不以同一字节或文本作为发声经历身份。

    Attributes:
        plugin_id: 事实所属插件实例的身份。
        identity: 当前事实的稳定身份，用于去重和关联。
        fact: 此音频阶段事实的稳定名称。
        occurred_at: 原始事实实际发生的 Unix 时间戳（秒）。
        payload_json: 已编码的业务事实，不包含任意 Python 对象。

    Note:
        实例字段在构造后不可重新赋值；嵌套值的冻结范围由字段所属契约决定。
    """

    plugin_id: str
    identity: str
    fact: str
    occurred_at: float
    payload_json: str

    def __post_init__(self) -> None:
        """公开接管边界拒绝任意阶段、无穷时间和巨大材料，不污染权威库。

        每身份阶段数量固定；未知结束若后来取得真实确认，追加
        late_confirmed/control_confirmed，而不覆盖先前未知事实。
        """
        if (
            not self.plugin_id.strip()
            or not self.identity.strip()
            or len(self.plugin_id) > 256
            or len(self.identity) > 256
        ):
            raise ValueError("音频事实必须有有限非空插件与发生身份。")
        if self.fact not in FACTS:
            raise ValueError("音频事实阶段未登记。")
        if (
            isinstance(self.occurred_at, bool)
            or not math.isfinite(self.occurred_at)
            or self.occurred_at < 0
        ):
            raise ValueError("音频事实时间必须是非负有限数值。")
        encoded = self.payload_json.encode()
        if len(encoded) > MAX_FACT_BYTES:
            raise ValueError("音频事实超过64 KiB。")
        decode_object(encoded)

    def payload(self) -> JsonObject:
        """按真实协议恢复已保存的小型事实，不访问设备。"""
        return decode_object(self.payload_json.encode())
