"""模型材料编译、请求证据与重试配置的边界契约，不拥有客户端或提交任务。"""

from typing import Protocol

from purrcept_core.models import DecisionRequest, ModelRequest

from purrcept_engine.domain.model_requests.models import (
    AttemptEvidence,
    AttemptStart,
    RequestEvidence,
    RequestOutcome,
)


class MaterialCompiler(Protocol):
    """按真实 SDK 顺序冻结公开材料与编译器身份；不联网或读取凭据。"""

    def __call__(self, request: ModelRequest | DecisionRequest, *, model: str) -> tuple[str, str]:
        """返回完整材料 JSON 与可核验编译器身份，不裁剪或改变请求。

        Args:
            request: 已按 Core 协议组装的完整生成或判断请求。
            model: 本次调用使用的显式模型名，不在领域层推断替代模型。

        Returns:
            本次读取的完整内容及其版本标记，供后续修改时核验。
        """
        ...


class RetryPolicy(Protocol):
    """已校验配置的最小只读面；app 配置与隔离测试可直接实现。

    attempts 包含首次，attempt_seconds 限制单次，total_seconds 包含全部尝试和
    等待。主体选择用途对应配置；注意判断使用 2/5/8，生成使用 2/120/180。
    包装不接受模型内容修改这些值，也不将一次重试算为新的逻辑请求。
    """

    @property
    def attempts(self) -> int:
        """返回含首次的已校验尝试上限。"""
        ...

    @property
    def attempt_seconds(self) -> float:
        """返回每次尝试的最长等待时间。"""
        ...

    @property
    def total_seconds(self) -> float:
        """返回同一逻辑请求含退避的最长时间。"""
        ...


class RequestEvidenceSink(Protocol):
    """主体借用的权威提交端口，生产实现复用 Store 单线程队列。

    所有方法返回意味着材料已提交，而不是只加入诊断缓冲。失败原样传播并
    阻止包装的后续调用。方法须按 Store 契约保留已接管写入：等待者取消不能
    撤销已接受事务。测试可用内存实现，生产不能以它替代失效的权威库。
    接口值可用 serialization.encode 计算实际 UTF-8 接管字节量。
    """

    async def begin_request(self, request: RequestEvidence) -> None:
        """保存同一逻辑请求的不可变公开编译材料与因果引用。

        Args:
            request: 本次逻辑请求冻结的完整公开材料与因果身份。

        Raises:
            StorageError: 请求证据无法提交或已有权威记录与本次事实冲突。
        """
        ...

    async def begin_attempt(self, start: AttemptStart) -> None:
        """先保存一次尝试身份。

        Args:
            start: 真实尝试开始前须持久化的编号、身份和截止事实。

        Raises:
            StorageError: 请求证据无法提交或已有权威记录与本次事实冲突。

        Note:
            成功前不得调用底层后端。
        """
        ...

    async def finish_attempt(self, result: AttemptEvidence) -> None:
        """保存本次独立结果、原参数与有限诊断覆盖。

        Args:
            result: 本次尝试的实际响应、失败分类与有限诊断。

        Raises:
            StorageError: 请求证据无法提交或已有权威记录与本次事实冲突。
        """
        ...

    async def finish_request(self, request_id: str, state: RequestOutcome) -> None:
        """保存逻辑终态，不覆盖各次尝试。

        Args:
            request_id: 逻辑请求的稳定身份，用于查询或关联同一次受理。
            state: 逻辑请求最终状态，独立于各次传输尝试。

        Raises:
            StorageError: 请求证据无法提交或已有权威记录与本次事实冲突。
        """
        ...
