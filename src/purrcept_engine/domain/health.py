"""记录组件所有者的首个故障，供日志与只读健康投影共用同一事实。

ComponentHealth 由任务或提交所有者创建，在其事件循环中同步锁存故障。它不决定
取消、重试或恢复；原异常继续由原所有者传播。一次实例故障只报告一次，跨层传递
不应重复创建同一组件的报告。查询不读取数据库，也不改变生命周期或派发资格。
"""

from __future__ import annotations

import logging
import time
from typing import cast
from uuid import uuid4

from purrcept_engine.errors import StorageError
from purrcept_engine.values import JsonObject


def _causes(error: BaseException) -> tuple[BaseException, ...]:
    """有界遍历原因和异常组，收尾聚合不能把同一个原始故障再报告一次。"""
    result: list[BaseException] = []
    pending: list[BaseException] = [error]
    while pending and len(result) < 32:
        current = pending.pop()
        if any(current is item for item in result):
            continue
        result.append(current)
        cause = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        if cause is not None:
            pending.append(cause)
        if isinstance(current, BaseExceptionGroup):
            group = cast(BaseExceptionGroup[BaseException], current)
            pending.extend(group.exceptions[:32])
    return tuple(result)


class ComponentHealth:
    """由组件独占的首次故障事实；恢复必须重新建立实际资源和组件实例。"""

    def __init__(self, component: str) -> None:
        """为明确组件创建尚无故障的记录，不安装日志处理器。

        Args:
            component: 日志和健康接口共用的稳定组件名称。
        """
        self.component = component
        self.error: BaseException | None = None
        self.failed_at: float | None = None
        self.error_id: str | None = None
        self.error_code: str | None = None

    def fail(self, error: BaseException) -> None:
        """锁存并报告首个原始故障，重复通知不刷新时间或输出重复 ERROR。

        Args:
            error: 所有者已经取回的真实任务或提交故障。
        """
        if self.error is not None:
            return
        self.error, self.failed_at, self.error_id = error, time.time(), uuid4().hex
        self.error_code = error.code if isinstance(error, StorageError) else "component_failed"
        causes = _causes(error)
        for cause in causes:
            identity = cause.__dict__.get("_purrcept_diagnostic_id")
            occurred = cause.__dict__.get("_purrcept_diagnostic_at")
            if isinstance(identity, str) and isinstance(occurred, float):
                self.error_id, self.failed_at = identity, occurred
                return
        # 错误仍保持原类型和原因链；只附加内部诊断身份供后续故障所有者复用。
        # 状态变更发生在事件循环，不建立全局异常注册表或跨请求去重缓存。
        for cause in causes:
            cause.__dict__["_purrcept_diagnostic_id"] = self.error_id
            cause.__dict__["_purrcept_diagnostic_at"] = self.failed_at
        logging.getLogger(__name__).error(
            "组件已停止处理新工作。",
            extra={
                "component": self.component,
                "error_id": self.error_id,
                "error_code": self.error_code,
                "error_type": type(error).__name__,
            },
            exc_info=(type(error), error, error.__traceback__),
        )

    def snapshot(self) -> JsonObject:
        """返回不含异常正文的稳定故障身份；健康不代表宿主已完成启动。"""
        return {
            "component": self.component,
            "state": "healthy" if self.error is None else "failed",
            "failed_at": self.failed_at,
            "error_id": self.error_id,
            "error_code": self.error_code,
            "error_type": None if self.error is None else type(self.error).__name__,
            "can_process": self.error is None,
        }
