"""浏览器十项稳定模型能力的纯声明与一页真实资源装配。

父节点必须是已声明的电脑实例，但浏览器不隐式连接电脑或启动屏幕观察。
模型路径、参数schema与实际调用均来自同一声明；实时页面放动态尾部。
make_factory只用于明确驱动注入，生产factory使用Patchright且不自动安装程序。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from purrcept_engine.errors import ConfigError
from purrcept_engine.plugin_system import (
    CallContext,
    CallOutcome,
    CapabilityDeclaration,
    CapabilityInputError,
    FunctionCapability,
    KnowledgeInputError,
    ObjectDeclaration,
    PluginBlueprint,
    PluginFactory,
    PluginPorts,
    PluginRuntime,
    typed_factory,
)
from purrcept_engine.plugins.browser.driver import (
    BrowserActionError,
    BrowserDriver,
    BrowserInputError,
)
from purrcept_engine.plugins.browser.patchright import PatchrightDriver
from purrcept_engine.plugins.browser.session import BrowserSession
from purrcept_engine.plugins.browser.settings import BrowserSettings
from purrcept_engine.values import JsonObject


class _Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Empty(_Parameters):
    pass


class _Open(_Parameters):
    url: str = Field(min_length=1)


class _Read(_Parameters):
    mode: Literal["markdown", "text"] | None = None
    max_chars: int | None = Field(default=None, gt=0)
    cursor: str | None = Field(default=None, min_length=1)


class _Search(_Parameters):
    query: str = Field(min_length=1)


class _Snapshot(_Parameters):
    interactive: bool = False


class _Ref(_Parameters):
    ref: str = Field(min_length=1)


class _Type(_Ref):
    text: str = Field(min_length=1)
    submit: bool = False


class _Scroll(_Parameters):
    direction: Literal["down", "up", "top", "bottom"] = "down"
    ref: str | None = Field(default=None, min_length=1)
    amount: int = Field(default=600, gt=0)


class _Screenshot(_Parameters):
    full_page: bool = False


async def _result(operation: Awaitable[JsonObject | CallOutcome]) -> CallOutcome:
    """只转换明确业务输入拒绝，交互后失败带真实局部事实，不吞掉SDK/权威错误。"""
    try:
        result = await operation
    except (BrowserInputError, KnowledgeInputError) as error:
        raise CapabilityInputError(str(error)) from error
    except BrowserActionError as error:
        return CallOutcome("unknown", error.facts, type(error).__name__, str(error))
    return result if isinstance(result, CallOutcome) else CallOutcome("confirmed", result)


def make_factory(driver_factory: Callable[[BrowserSettings], BrowserDriver]) -> PluginFactory:
    """在同一能力与生命周期契约下注入驱动，离线验证无需真实浏览器。"""

    def declare(instance_id: str, settings: BrowserSettings, base_dir: Path) -> PluginBlueprint:
        settings = settings.resolve_paths(base_dir)
        object_id = instance_id + ":browser"

        def capability[Parameters: BaseModel](
            name: str,
            parameters: type[Parameters],
            description: str,
            example: str,
            usage: str = "",
            *,
            navigation: bool = False,
        ) -> CapabilityDeclaration[Parameters]:
            return CapabilityDeclaration(
                instance_id + ":browser." + name,
                object_id,
                name,
                description,
                parameters,
                settings.navigation_timeout_seconds
                if navigation
                else settings.action_timeout_seconds,
                example,
                usage,
                concurrency="target_serial",
            )

        status = capability("status", _Empty, "读取当前浏览器页面身份。", "await {path}()")
        open_page = capability(
            "open",
            _Open,
            "在当前页导航到URL。",
            'await {path}(url="https://example.com")',
            "仅允许http/https；加载失败不自动重新导航。",
            navigation=True,
        )
        read = capability(
            "read",
            _Read,
            "取得网页正文的不可变快照并分段读取。",
            "await {path}()",
            "首次省略mode为markdown；cursor续读旧快照，不能同时指定mode。返回content和原文/范围引用。",
        )
        search = capability(
            "search",
            _Search,
            "导航到配置的搜索引擎并取得有限结果。",
            'await {path}(query="天气")',
            "保存完整取得的结果页；有限结果列表不代表远端全部结果。",
            navigation=True,
        )
        snapshot = capability(
            "snapshot",
            _Snapshot,
            "取得当前页面的受支持角色树与交互ref。",
            "await {path}()",
            "每角色最多40个节点；页面变化使旧ref失效。读取快照的cursor不能当交互ref。",
        )
        click = capability("click", _Ref, "点击最近snapshot的ref。", 'await {path}(ref="e1")')
        type_text = capability(
            "type",
            _Type,
            "向最近snapshot的ref清空并输入文本。",
            'await {path}(ref="e1", text="你好", submit=False)',
            "submit=True明确按Enter；不因后续页面读取失败重做输入。",
        )
        scroll = capability(
            "scroll",
            _Scroll,
            "滚动当前页或最近snapshot中的元素。",
            'await {path}(direction="down")',
        )
        save = capability(
            "save",
            _Ref,
            "取得ref指向的图像或元素截图并保存。",
            'await {path}(ref="e1")',
            "优先实际原图；不能取得时明确说明原因并截该元素。原生媒体保存同库，path是磁盘镜像。",
        )
        screenshot = capability(
            "screenshot",
            _Screenshot,
            "取得当前浏览器截图并保存原生媒体。",
            "await {path}()",
            "full_page=True取得整页；不另制造前台输入批次。",
        )

        async def build(ports: PluginPorts) -> PluginRuntime:
            evidence = ports.evidence
            if evidence is None:
                raise ConfigError("browser插件需要宿主原文/媒体端口。")
            session = BrowserSession(instance_id, driver_factory(settings), settings, evidence)
            try:
                await session.start()
            except BaseException as error:
                # build尚未交回Runtime，资源只归session，宿主不能猜SDK取得了什么。
                try:
                    await session.close()
                except BaseException as cleanup:
                    raise BaseExceptionGroup(
                        "浏览器启动与资源收尾均失败。", [error, cleanup]
                    ) from None
                raise

            async def run_status(parameters: _Empty, context: CallContext) -> CallOutcome:
                del parameters, context
                return await _result(session.status())

            async def run_open(parameters: _Open, context: CallContext) -> CallOutcome:
                del context
                return await _result(session.open(parameters.url))

            async def run_read(parameters: _Read, context: CallContext) -> CallOutcome:
                del context
                return await _result(
                    session.read(parameters.mode, parameters.max_chars, parameters.cursor)
                )

            async def run_search(parameters: _Search, context: CallContext) -> CallOutcome:
                del context
                return await _result(session.search(parameters.query))

            async def run_snapshot(parameters: _Snapshot, context: CallContext) -> CallOutcome:
                del context
                return await _result(session.snapshot(interactive=parameters.interactive))

            async def run_click(parameters: _Ref, context: CallContext) -> CallOutcome:
                del context
                return await _result(session.click(parameters.ref))

            async def run_type(parameters: _Type, context: CallContext) -> CallOutcome:
                del context
                return await _result(
                    session.type_text(parameters.ref, parameters.text, submit=parameters.submit)
                )

            async def run_scroll(parameters: _Scroll, context: CallContext) -> CallOutcome:
                del context
                return await _result(
                    session.scroll(parameters.direction, parameters.ref, parameters.amount)
                )

            async def run_save(parameters: _Ref, context: CallContext) -> CallOutcome:
                del context
                return await _result(session.image(ref=parameters.ref))

            async def run_screenshot(parameters: _Screenshot, context: CallContext) -> CallOutcome:
                del context
                return await _result(session.image(ref=None, full_page=parameters.full_page))

            async def prepare_request() -> JsonObject:
                return session.state()

            return PluginRuntime(
                (
                    FunctionCapability(status, run_status),
                    FunctionCapability(open_page, run_open),
                    FunctionCapability(read, run_read),
                    FunctionCapability(search, run_search),
                    FunctionCapability(snapshot, run_snapshot),
                    FunctionCapability(click, run_click),
                    FunctionCapability(type_text, run_type),
                    FunctionCapability(scroll, run_scroll),
                    FunctionCapability(save, run_save),
                    FunctionCapability(screenshot, run_screenshot),
                ),
                close=session.close,
                snapshot=session.state,
                prepare_request=prepare_request,
            )

        return PluginBlueprint(
            (
                ObjectDeclaration(
                    object_id,
                    settings.browser_id,
                    settings.label.strip(),
                    settings.computer_id + ":computer",
                ),
            ),
            (status, open_page, read, search, snapshot, click, type_text, scroll, save, screenshot),
            build,
            required_modules=("patchright", "trafilatura", "PIL"),
        )

    return typed_factory(BrowserSettings, declare)


factory = make_factory(PatchrightDriver)
