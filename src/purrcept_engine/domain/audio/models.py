"""保存公开值及其语义校验，不打开数据库或调用设备。"""

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
    """一项阶段的持久结果，不以同一字节或文本作为发声经历身份。"""

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
