"""把已确认的服务端输入用量作为后续同布局请求的容量锚点。

应用按用途、端点及模型隔离 UsageMeter，并在启动时从同库恢复最后锚点。
只有完整静态输入及消息前缀逐值一致时，旧输入才沿用实际 input_tokens，
新增消息仍采用唯一内置估算器。摘要、工具目录变化或媒体过期改变前缀时，
整份新布局重新估算。这里不推断模型 tokenizer，也不取得模型客户端。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from purrcept_core.models import DecisionRequest, DecisionResponse, ModelRequest, ModelResponse

from purrcept_engine.domain.context.templates import ENGINE_STATE_REMINDER_KEY
from purrcept_engine.domain.context.token_estimation import estimate_tokens
from purrcept_engine.domain.model_requests.models import RequestEvidence
from purrcept_engine.domain.model_requests.service import prepare_off_loop
from purrcept_engine.serialization import encode
from purrcept_engine.values import JsonObject, decode_object


@dataclass(frozen=True, slots=True)
class UsageAnchor:
    """一份成功请求的实际输入用量与完整前缀指纹，不复制原文或媒体。

    Attributes:
        request_id: 已提交成功响应的真实请求身份。
        compiler: 当时使用的 SDK 编译器版本及源码指纹。
        static_digest: 除消息和输出/流传输选项外的完整输入布局摘要。
        field: SDK 的 messages 或 input 槽位。
        prefix: 按顺序冻结的完整消息摘要，不包含请求局部动态尾部。
        input_tokens: 服务端报告的完整输入用量，包含缓存命中的输入。

    Note:
        动态尾部虽不参与前缀匹配，其当时用量仍包含在 input_tokens 中。
        不估算或扣除这份旧尾部；新尾部按新材料计入，保守余量不会逐轮累积，
        因为每次有效响应都会重新提供完整实际输入用量。输出和推理用量不当作
        已知输入，后续实际重放的响应消息属于新增 payload。
    """

    request_id: str
    compiler: str
    static_digest: str
    field: str
    prefix: tuple[str, ...]
    input_tokens: int

    def __post_init__(self) -> None:
        """拒绝无效持久字段，损坏锚点不能成为零用量或跨协议容量凭据。"""
        if not self.request_id or not self.compiler or self.field not in ("messages", "input"):
            raise ValueError("用量锚点身份或协议无效。")
        if isinstance(self.input_tokens, bool) or self.input_tokens <= 0:
            raise ValueError("用量锚点必须来自正的实际输入用量。")
        if any(
            len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)
            for digest in (self.static_digest, *self.prefix)
        ):
            raise ValueError("用量锚点摘要损坏。")


class UsageStore(Protocol):
    """应用借出的同库用量锚点端口，不持有模型或会话资源。

    应用在装配时读取，唯一请求包装在成功响应及原文证据提交后保存。
    binding 由应用根据用途、端点和模型生成；同身份最后一次成功写入胜出。
    读取缺失返回 None，存储和解码故障必须抛出，不能用空锚点掩盖损坏。
    """

    async def get_usage_anchor(self, binding: str) -> UsageAnchor | None:
        """恢复该绑定的最后完整锚点。

        Args:
            binding: 应用提供的用途及模型端点绑定摘要。

        Returns:
            已保存的不可变锚点；首次运行返回 None。
        """
        ...

    async def put_usage_anchor(self, binding: str, anchor: UsageAnchor) -> None:
        """在真实成功请求已入账后保存完整锚点，不改变原请求或响应。

        Args:
            binding: 应用提供的用途及模型端点绑定摘要。
            anchor: 原请求和响应已确认后冻结的指纹与实际输入用量。
        """
        ...


@dataclass(frozen=True, slots=True)
class UsageEstimate:
    """容量预检的实际基线与新材料估算，零基线表示没有可复用用量。"""

    input_tokens: int
    observed_tokens: int = 0
    new_tokens: int = 0
    request_id: str | None = None


def _digest(value: object) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def _layout(payload: JsonObject) -> tuple[str, str, tuple[str, ...]]:
    """只忽略 SDK 的缓存注解及输出/流传输选项，不深入工具参数删除同名字段。"""
    field = "messages" if "messages" in payload else "input"
    raw = payload.get(field)
    messages = raw if isinstance(raw, list) else [raw]
    prefix: list[str] = []
    for message in messages:
        if isinstance(message, dict):
            cleaned = {key: value for key, value in message.items() if key != "cache_control"}
            content = cleaned.get("content")
            if isinstance(content, list):
                cleaned["content"] = [
                    {key: value for key, value in part.items() if key != "cache_control"}
                    if isinstance(part, dict)
                    else part
                    for part in content
                ]
            prefix.append(_digest(cleaned))
        else:
            prefix.append(_digest(message))
    static = {
        key: value
        for key, value in payload.items()
        if key not in (field, "max_tokens", "max_completion_tokens", "stream", "stream_options")
    }
    tools = static.get("tools")
    if isinstance(tools, list):
        static["tools"] = [
            {key: value for key, value in tool.items() if key != "cache_control"}
            if isinstance(tool, dict)
            else tool
            for tool in tools
        ]
    return field, _digest(static), tuple(prefix)


class UsageMeter:
    """应用按独立用途创建的实际用量所有者，预检只读取不可变锚点。

    observe 由唯一请求包装在成功证据提交后串行调用；失败、取消、零用量和
    缺失 usage 都不覆盖旧锚点。预检可在线程中读取，调用开始时冻结锚点引用，
    不读取数据库或修改共享状态。跨端点和编译器的隔离由应用绑定及 compiler
    指纹共同保证；前缀不匹配时不按比例分摊旧用量或猜测被删除材料的 token。
    """

    def __init__(
        self, store: UsageStore, binding: str, compiler: str, anchor: UsageAnchor | None
    ) -> None:
        """借用已恢复的不可变锚点，构造不读库或调用模型。

        Args:
            store: 应用借出的有限锚点提交端口。
            binding: 当前用途及模型端点的稳定摘要，不包含凭据。
            compiler: 当前安装 SDK 编译器的确切身份，旧编译器的锚点不复用。
            anchor: 当前绑定已恢复的锚点；None 表示尚无实际输入用量。
        """
        self.store, self.binding, self.compiler = store, binding, compiler
        self.anchor = anchor if anchor is None or anchor.compiler == compiler else None

    def estimate(self, payload: JsonObject) -> UsageEstimate:
        """复用匹配前缀的实际完整输入，只估算其后的新消息及动态尾部。

        Args:
            payload: 本次实际 SDK 输入，调用期间不得修改。

        Returns:
            完整规划量以及可审计的已知输入和增量组成。

        Raises:
            ValueError: 新材料不符合内置估算器的媒体或消息契约。
        """
        anchor = self.anchor
        field, static, prefix = _layout(payload)
        if (
            anchor is not None
            and anchor.prefix
            and field == anchor.field
            and static == anchor.static_digest
            and prefix[: len(anchor.prefix)] == anchor.prefix
        ):
            raw = payload[field]
            if isinstance(raw, list):
                added = raw[len(anchor.prefix) :]
                delta: JsonObject = {field: added}
                new_tokens = estimate_tokens(delta) if added else 0
                return UsageEstimate(
                    anchor.input_tokens + new_tokens,
                    anchor.input_tokens,
                    new_tokens,
                    anchor.request_id,
                )
            return UsageEstimate(anchor.input_tokens, anchor.input_tokens, 0, anchor.request_id)
        estimated = estimate_tokens(payload)
        return UsageEstimate(estimated, new_tokens=estimated)

    async def observe(
        self,
        evidence: RequestEvidence,
        request: ModelRequest | DecisionRequest,
        response: ModelResponse | DecisionResponse,
    ) -> None:
        """保存成功响应的 input_tokens，缓存用量不再次相加，缺失或零值保留旧锚点。

        Args:
            evidence: 已提交的真实逻辑请求材料与编译器身份。
            request: 该次请求的 Core 对象，用于核验宿主动态尾部身份。
            response: 同一请求已确认的成功响应，不能传入迟到或失败尝试。

        Note:
            先可靠保存再更新内存；保存失败原样抛出，不使后续请求借用未提交用量。
        """
        if response.usage is None or response.usage.input_tokens == 0:
            return

        def freeze() -> UsageAnchor:
            """请求证据是唯一材料来源，原生媒体与消息布局都参与完整指纹。"""
            payload = decode_object(evidence.materials_json.encode("utf-8"))["payload"]
            if not isinstance(payload, dict):
                raise TypeError("用量锚点缺少完整 SDK payload。")
            field, static, prefix = _layout(payload)
            # Engine 尾部由可信 Reminder 身份定位，不能按外部正文的相似标题猜测。
            has_tail = isinstance(request, ModelRequest) and any(
                reminder.key == ENGINE_STATE_REMINDER_KEY for reminder in request.reminders
            )
            stable = prefix[:-1] if has_tail else prefix
            assert response.usage is not None
            return UsageAnchor(
                evidence.request_id,
                evidence.compiler,
                static,
                field,
                stable,
                response.usage.input_tokens,
            )

        anchor = await prepare_off_loop(freeze)
        await self.store.put_usage_anchor(self.binding, anchor)
        self.anchor = anchor
