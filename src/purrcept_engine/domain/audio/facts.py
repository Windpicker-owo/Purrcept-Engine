"""构造并校验纯业务事实；不取得数据库连接或设备资源。"""

from __future__ import annotations

from purrcept_engine.domain.audio.models import AudioFact
from purrcept_engine.values import JsonObject, encode_json


def make_fact(
    plugin_id: str, identity: str, fact: str, occurred_at: float, payload: JsonObject
) -> AudioFact:
    """在进入队列前冻结事实 JSON，插件不能事后修改已接管材料。"""
    return AudioFact(plugin_id, identity, fact, occurred_at, encode_json(payload).decode())
