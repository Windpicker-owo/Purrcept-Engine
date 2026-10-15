"""控制台的显式输出声明，文字输入循环由 CLI 的常驻生命周期拥有。

能力调用才显示文字，模型最终自然语言不会由此自动输出。输出结果进入统一
执行回执；这里不持有主脑历史或人员身份。嵌入宿主可注入自己的显示函数。
"""

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from purrcept_engine.domain.execution.catalog import CapabilityDeclaration, ObjectDeclaration
from purrcept_engine.domain.execution.providers import CallContext, CallOutcome, FunctionCapability
from purrcept_engine.plugin_system.contracts import PluginBlueprint, PluginPorts, PluginRuntime


class _Output(BaseModel):
    """保留模型可见的 text 参数；控制台输出不承担路由或身份权限。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    text: str


def console_blueprint(output: Callable[[str], None]) -> PluginBlueprint:
    """纯声明实际 me.console.output 路径，只有调用实现时才使用显示函数。"""
    declaration = CapabilityDeclaration(
        "console.output",
        "console",
        "output",
        "向当前控制台的用户显示一段文字。",
        _Output,
        10.0,
        'await me.console.output(text="你好。")',
        usage="已经决定对当前用户说话时调用；内部联想和推理不会自动显示给用户。",
        concurrency="target_serial",
    )

    async def build(ports: PluginPorts) -> PluginRuntime:
        del ports

        async def deliver(parameters: _Output, context: CallContext) -> CallOutcome:
            del context
            output(parameters.text)
            return CallOutcome("confirmed", {"channel": "console", "text": parameters.text})

        return PluginRuntime((FunctionCapability(declaration, deliver),))

    return PluginBlueprint(
        (ObjectDeclaration("console", "console", "本地控制台", "subject"),),
        (declaration,),
        build,
    )
