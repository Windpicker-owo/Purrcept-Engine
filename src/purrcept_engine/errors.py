"""跨宿主边界的错误类型；内部异常保留原类型，不伪装成业务拒收。"""


class EngineError(RuntimeError):
    """调用者可识别的 Engine 业务或资源边界失败。"""


class ConfigError(ValueError):
    """已脱敏的配置错误；信息包含字段位置，不包含输入值或凭据。"""


class StorageError(EngineError):
    """权威存储拒绝或故障，code 区分容量、冲突、格式及资源状态。"""

    def __init__(self, message: str, *, code: str = "storage_failure") -> None:
        """保留可供接入方判断的原因。

        Args:
            message: 对调用方公开的原因或错误说明。
            code: 供调用方分类处理的稳定错误码。 默认 'storage_failure'。

        Note:
            不以失败返回空成功记录。
        """
        super().__init__(message)
        self.code = code


class HostStateError(EngineError):
    """当前宿主生命周期不允许该操作；已接管工作不因此被撤销。"""


class InstanceLocked(EngineError):
    """同一数据目录已有资源所有者，不能另起宿主处理相同输入。"""
