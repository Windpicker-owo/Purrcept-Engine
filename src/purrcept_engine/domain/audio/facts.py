"""构造带稳定身份、发生时间和严格 JSON 的音频阶段事实。

make_fact 在交接前固定载荷文本，随后由 AudioPort 提交；本模块不合成、
播放音频或推断设备已经完成。
"""

from __future__ import annotations

from purrcept_engine.domain.audio.models import AudioFact
from purrcept_engine.values import JsonObject, encode_json


def make_fact(
    plugin_id: str, identity: str, fact: str, occurred_at: float, payload: JsonObject
) -> AudioFact:
    """在进入队列前冻结事实 JSON，插件不能事后修改已接管材料。

    Args:
        plugin_id: 事实所属插件实例的身份。
        identity: 当前事实的稳定身份，用于去重和关联。
        fact: 此音频阶段事实的稳定名称。
        occurred_at: 原始事实实际发生的 Unix 时间戳（秒）。
        payload: 当前边界采用的业务 JSON 或完整模型请求材料。

    Returns:
        载荷已固定为严格 JSON 的不可变音频阶段事实。
    """
    return AudioFact(plugin_id, identity, fact, occurred_at, encode_json(payload).decode())
