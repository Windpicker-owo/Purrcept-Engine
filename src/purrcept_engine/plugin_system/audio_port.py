"""宿主和现场收音借用的停音接缝，不暴露合成器、队列或通用服务查找。

app 在全部 build 后绑定 BodyPort，然后启动输出运行时。VAD/面板使用
同一个 interrupt 接管规则；生成抢占另由宿主记录，停音成功不冒充抢占成功。
"""

from typing import Protocol

from purrcept_engine.execution.providers import CallContext, CallOutcome
from purrcept_engine.plugin_system.body_port import BodyPort
from purrcept_engine.values import JsonObject


class AudioOutputPort(Protocol):
    """音频所有者向宿主交回的有限控制；寿命与 PluginRuntime 相同。

    app 必须在启动前一次 bind_body，未装配身体显式传 None。interrupt
    先撤销当前及当时已有候播资格，再等待期限内的真实收尾。等待超时不
    恢复播放资格，也不永久静音；新接管说话可以继续。
    本实例最多持有16项未确认控制，沿宿主Provider已批准上限，满载明确
    失败；普通说话名额不占用它。
    """

    def bind_body(self, body: BodyPort | None) -> None:
        """完成装配交接，重复接线属于内部装配错误。"""
        ...

    async def interrupt(self, context: CallContext) -> CallOutcome:
        """停止当前和已有候播，受理与实际清理确认分别返回。"""
        ...

    def snapshot(self) -> JsonObject:
        """读取真实队列/播放状态，不合成或打开设备。"""
        ...
