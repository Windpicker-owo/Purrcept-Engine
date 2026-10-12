"""将插件迟到协议回包追加到原调用，不把断线或请求参数当作确认。

宿主为每份 Blueprint 创建有限路径集合；输入、action 与已开始 call 的
对应关系取自 Store。首版结果未提交时等待权威变化通知，避免一个更晚
成功先入账而被执行器的首版 unknown 覆盖。关闭这些等待归插件所有者，
本门面不创建连接或脱离所有者的后台任务。
"""

from purrcept_engine.errors import StorageError
from purrcept_engine.execution.protocol import encode_json
from purrcept_engine.execution.providers import CallContext, CallOutcome, CapabilityInputError
from purrcept_engine.storage.execution import CallRecord
from purrcept_engine.storage.store import Store


class LateCallResults:
    """app 借给一个插件的迟到提交权限，使用同一 Store 的有界控制队列。"""

    def __init__(self, store: Store, paths: frozenset[str]) -> None:
        """固定实际能力绑定范围，运行期重连不扩大路径或替换原身份。"""
        self.store, self.paths = store, paths

    async def record(self, context: CallContext, outcome: CallOutcome) -> CallRecord:
        """核验真实来源并等待初次入账，再追加实际回包，取消不回滚提交。"""
        while True:
            revision = self.store.change_revision
            action = await self.store.get_action(context.action_id)
            call = (
                None
                if action is None
                else next((item for item in action.calls if item.call_id == context.call_id), None)
            )
            if (
                action is None
                or call is None
                or call.path not in self.paths
                or action.input_id != context.input_id
                or call.started_at is None
            ):
                raise CapabilityInputError("迟到回执没有对应本插件的实际已开始调用。")
            if not self.store.authority_available:
                raise StorageError("权威存储不能保存迟到回执。")
            if call.receipt_revision > 0:
                break
            # 不猜执行器已经提交 unknown，也不抢先提交令首版提交发生冲突。
            await self.store.wait_for_change(revision)
        return await self.store.record_call_result(
            context.call_id,
            state=outcome.state,
            result_json=encode_json(outcome.value).decode("utf-8"),
            error_type=outcome.error_type,
            error=outcome.error,
            media=outcome.media,
        )
