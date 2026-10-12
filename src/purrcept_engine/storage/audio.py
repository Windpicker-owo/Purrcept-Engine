"""音频合成/播放的不可变阶段事实，全部 SQL 在 Store 唯一连接执行。

AudioLedger 只允许以宿主播放与控制身份追加小型 JSON 回执。接受、合成、
开始和最终结果分别保存；主体会话失败不能回滚已播放事实。重启只查询
历史，不从未终止记录自动恢复声音。
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from purrcept_engine.errors import StorageError
from purrcept_engine.execution.protocol import JsonObject, decode_object, encode_json

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

SCHEMA = (
    "CREATE TABLE audio_facts ("
    "sequence INTEGER PRIMARY KEY AUTOINCREMENT, plugin_id TEXT NOT NULL, "
    "identity TEXT NOT NULL, fact TEXT NOT NULL, occurred_at REAL NOT NULL, "
    "payload_json TEXT NOT NULL, UNIQUE(plugin_id,identity,fact))",
)


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


def append(connection: sqlite3.Connection, fact: AudioFact) -> None:
    """同身份同事实只接受相同内容，禁止覆盖此前真实结果。"""
    decode_object(fact.payload_json.encode())
    existing = connection.execute(
        "SELECT payload_json FROM audio_facts WHERE plugin_id=? AND identity=? AND fact=?",
        (fact.plugin_id, fact.identity, fact.fact),
    ).fetchone()
    if existing is not None:
        if existing[0] != fact.payload_json:
            raise StorageError("音频事实身份冲突。", code="audio_fact_conflict")
        return
    with connection:
        connection.execute(
            "INSERT INTO audio_facts(plugin_id,identity,fact,occurred_at,payload_json) "
            "VALUES(?,?,?,?,?)",
            (fact.plugin_id, fact.identity, fact.fact, fact.occurred_at, fact.payload_json),
        )


def facts(connection: sqlite3.Connection, plugin_id: str, identity: str) -> tuple[AudioFact, ...]:
    """按权威提交顺序读取，坏字段明确失败而不是返回空历史。"""
    rows = connection.execute(
        "SELECT plugin_id,identity,fact,occurred_at,payload_json FROM audio_facts "
        "WHERE plugin_id=? AND identity=? ORDER BY sequence",
        (plugin_id, identity),
    ).fetchall()
    result: list[AudioFact] = []
    for row in rows:
        values: tuple[object, ...] = tuple(row)
        plugin, item, stage, occurred, payload = values
        if (
            not isinstance(plugin, str)
            or not isinstance(item, str)
            or not isinstance(stage, str)
            or not isinstance(payload, str)
        ):
            raise StorageError("音频事实字段无效。", code="invalid_record")
        if (
            not isinstance(occurred, (float, int))
            or isinstance(occurred, bool)
            or not math.isfinite(occurred)
        ):
            raise StorageError("音频事实时间无效。", code="invalid_record")
        try:
            result.append(AudioFact(plugin, item, stage, float(occurred), payload))
        except ValueError as error:
            raise StorageError("权威音频事实格式无效。", code="invalid_record") from error
    return tuple(result)


def make_fact(
    plugin_id: str, identity: str, fact: str, occurred_at: float, payload: JsonObject
) -> AudioFact:
    """在进入队列前冻结事实 JSON，插件不能事后修改已接管材料。"""
    return AudioFact(plugin_id, identity, fact, occurred_at, encode_json(payload).decode())
