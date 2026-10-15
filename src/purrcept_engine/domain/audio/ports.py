"""插件借用的有限业务端口；实现必须与宿主权威存储共享寿命。"""

from __future__ import annotations

from typing import Protocol

import purrcept_engine.domain.audio.models as audio_models


class AudioPort(Protocol):
    """app 创建并借给输出插件的同库事实端口。

    每个身份只能追加固定登记阶段；未知之后的真实确认另存澄清阶段，
    不覆盖旧事实。内容冲突显式失败。调用方负责身份与真实语义，存储
    负责不可覆盖、阶段/材料有界和真实提交。
    """

    async def append(self, fact: audio_models.AudioFact) -> None:
        """真实提交后返回，既有事实不得被新结果覆盖。"""
        ...

    async def facts(self, plugin_id: str, identity: str) -> tuple[audio_models.AudioFact, ...]:
        """读取保存顺序，不自动合成、播放或补写确认。"""
        ...
